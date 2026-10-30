"""trofeo.py - 独立控制 Thermalright Trofeo Vision 9.16 (0416:5408) 机箱屏

不依赖 TRCC。基于 USB Bulk 协议（逆向自 USBLCDNEW.dll 的
ThreadSendDeviceDataLY），参考 Lexonight1/thermalright-trcc-linux。

协议要点：
  握手： 向 Bulk OUT 写 2048 字节 (16 字节头: 02 FF 00 x6, 01, 00...)，
         从 Bulk IN 读 512 字节。回包 resp[0]=3, resp[1]=0xFF, resp[8]=1。
  分辨率：从回包 u32@24 = 宽, u32@28 = 高原始值, 再减去 resp[44] = 高。
  帧：   JPEG 数据切成 512 字节块（16 字节块头 + 496 字节数据），
         再以 4096 字节为单位写 USB，最后读 512 字节 ACK。
  保活： 屏在 ~2-3 秒无帧后会自动回到 logo，所以需要循环发送。

用法：
  python trofeo.py probe                  # 只读设备信息，不写屏
  python trofeo.py handshake              # 握手并打印分辨率/PM
  python trofeo.py image 图片.jpg [--seconds N] [--fps F]
  python trofeo.py demo  [--seconds N]    # 生成测试画面循环显示
"""
from __future__ import annotations

import argparse
import io
import struct
import sys
import time

import usb.core
import usb.util
import usb.backend.libusb1

VID = 0x0416
PID = 0x5408

_DLL_CANDIDATES = [
    r"C:\Program Files (x86)\Steam\libusb-1.0.dll",
    r"C:\Program Files\Tencent\Weixin\4.1.15.56\libusb-1.0.dll",
    r"C:\Program Files\bililive\livehime\8.12.0.11221\plugins\libusb-1.0.dll",
]

# 握手：16 字节头 + 2032 字节 0
_HANDSHAKE_HEADER = bytes([
    0x02, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])
_HANDSHAKE_PAYLOAD = _HANDSHAKE_HEADER + bytes(2032)

_CHUNK_SIZE = 512
_CHUNK_HEADER_SIZE = 16
_CHUNK_DATA_SIZE = 496
_USB_WRITE_SIZE = 4096
_READ_SIZE = 512

HANDSHAKE_TIMEOUT_MS = 1000
WRITE_TIMEOUT_MS = 5000
READ_TIMEOUT_MS = 1000


def get_backend():
    for path in _DLL_CANDIDATES:
        try:
            b = usb.backend.libusb1.get_backend(find_library=lambda x, p=path: p)
            if b is not None:
                return b
        except Exception:
            continue
    b = usb.backend.libusb1.get_backend()
    if b is None:
        raise SystemExit("找不到 libusb-1.0.dll，无法访问 USB 设备")
    return b


def find_device():
    backend = get_backend()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        raise SystemExit("没有找到 0416:5408（屏幕未连接？或已被其他程序独占？）")
    return dev


class Trofeo:
    def __init__(self, dev):
        self.dev = dev
        self.ep_out = None
        self.ep_in = None
        self.width = None
        self.height = None
        self.pm = None
        self.sub = None

    # ── 生命周期 ────────────────────────────────────────────────
    def open(self):
        d = self.dev
        try:
            d.set_configuration()
        except usb.core.USBError:
            pass  # 已配置过
        cfg = d.get_active_configuration()
        intf = cfg[(0, 0)]

        self.ep_out = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_OUT
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK,
        )
        self.ep_in = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_IN
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK,
        )
        if self.ep_out is None or self.ep_in is None:
            raise RuntimeError("没有找到 Bulk 端点")
        try:
            usb.util.claim_interface(d, intf.bInterfaceNumber)
        except usb.core.USBError as e:
            raise SystemExit(
                "无法占用设备（可能 TRCC 正在运行并独占它）。\n"
                f"请先退出 TRCC / USBLCDNEW.exe 再试。原始错误: {e}"
            )
        return self

    def close(self):
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass

    # ── 协议 ────────────────────────────────────────────────────
    def handshake(self):
        self.ep_out.write(_HANDSHAKE_PAYLOAD, timeout=HANDSHAKE_TIMEOUT_MS)
        resp = bytes(self.ep_in.read(_READ_SIZE, timeout=HANDSHAKE_TIMEOUT_MS))
        if len(resp) < 37 or resp[0] != 3 or resp[1] != 0xFF or resp[8] != 1:
            raise RuntimeError(
                "握手校验失败: len=%d [0]=%s [1]=%s [8]=%s"
                % (len(resp), resp[0] if resp else None,
                   resp[1] if len(resp) > 1 else None,
                   resp[8] if len(resp) > 8 else None)
            )
        self.raw_reply = resp
        raw = resp[20]
        if raw <= 3:
            raw = 1
        self.pm = 64 + raw
        self.sub = (resp[22] if len(resp) > 22 else 0) + 1

        # 分辨率：优先用回包自带的时序字段
        w = int.from_bytes(resp[24:28], "little") if len(resp) >= 28 else 0
        h = int.from_bytes(resp[28:32], "little") if len(resp) >= 32 else 0
        b44 = resp[44] if len(resp) > 44 else 0
        if 16 <= w <= 8192 and 16 <= (h - b44) <= 8192:
            self.width, self.height = w, h - b44
        else:
            self.width, self.height = self._resolution_from_pm()
        return self

    def _resolution_from_pm(self):
        # FBL 192 家族：1920x462 / 1920x440 / 1280x480；其余按 PM 兜底
        table = {
            65: (1920, 462), 66: (1920, 462), 67: (1920, 462),
            68: (1920, 440), 69: (1280, 480),
        }
        return table.get(self.pm, (1920, 462))

    def _chunk(self, payload: bytes, chunk_cmd: int) -> bytes:
        total = len(payload)
        num_chunks = total // _CHUNK_DATA_SIZE + 1
        last_data = total % _CHUNK_DATA_SIZE
        chunks = bytearray(num_chunks * _CHUNK_SIZE)
        for i in range(num_chunks):
            off = i * _CHUNK_SIZE
            is_last = i == num_chunks - 1
            data_len = last_data if is_last else _CHUNK_DATA_SIZE
            chunks[off] = 0x01
            chunks[off + 1] = 0xFF
            struct.pack_into("<I", chunks, off + 2, total)
            struct.pack_into("<H", chunks, off + 6, data_len)
            chunks[off + 8] = chunk_cmd
            struct.pack_into("<H", chunks, off + 9, num_chunks)
            struct.pack_into("<H", chunks, off + 11, i)
            src = i * _CHUNK_DATA_SIZE
            chunks[off + _CHUNK_HEADER_SIZE:off + _CHUNK_HEADER_SIZE + data_len] = \
                payload[src:src + data_len]
        # 块数补到 4 的倍数 (LY)
        pad = 4
        rem = num_chunks % pad
        if rem:
            num_chunks += pad - rem
        total_bytes = num_chunks * _CHUNK_SIZE
        return bytes(chunks) + bytes(total_bytes - len(chunks))

    def send_jpeg(self, jpeg: bytes):
        frame = self._chunk(jpeg, chunk_cmd=1)  # 0x5408 -> byte[8]=1
        pos, total = 0, len(frame)
        while pos < total:
            remaining = total - pos
            if remaining >= _USB_WRITE_SIZE:
                n = _USB_WRITE_SIZE
            else:
                n = min(2048, remaining)
            self.ep_out.write(frame[pos:pos + n], timeout=WRITE_TIMEOUT_MS)
            pos += _USB_WRITE_SIZE
        try:
            self.ep_in.read(_READ_SIZE, timeout=READ_TIMEOUT_MS)  # ACK
        except usb.core.USBTimeoutError:
            pass  # ACK 可缺省

    # ── 图像 ────────────────────────────────────────────────────
    def send_image_bytes(self, jpeg: bytes):
        self.send_jpeg(jpeg)

    def send_pil(self, img):
        from PIL import Image
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=90)
        data = buf.getvalue()
        if len(data) >= 450000:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=60)
            data = buf.getvalue()
        self.send_jpeg(data)


# ── 手写一个简单的测试画面（不依赖 PIL 之外的字体）────────────────
def make_demo_frame(w, h, tick: int):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (10, 12, 24))
    d = ImageDraw.Draw(img)
    # 渐变条
    for x in range(w):
        c = int(255 * x / max(1, w - 1))
        d.line([(x, 0), (x, h // 6)], fill=(c, 40, 255 - c))
    # 大字
    msg = "NO TRCC"
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("arial.ttf", max(24, h // 4))
    except Exception:
        font = None
    bbox = d.textbbox((0, 0), msg, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((w - tw) // 2, (h - th) // 2 - 20), msg, fill=(0, 255, 180), font=font)
    t = time.strftime("%H:%M:%S")
    try:
        from PIL import ImageFont
        f2 = ImageFont.truetype("arial.ttf", max(14, h // 8))
    except Exception:
        f2 = None
    b2 = d.textbbox((0, 0), t, font=f2)
    d.text(((w - (b2[2] - b2[0])) // 2, int(h * 0.72)), t,
           fill=(255, 255, 255), font=f2)
    # 移动方块，证明是动态的
    x = (tick * 12) % max(1, w - 40)
    d.rectangle([x, int(h * 0.9), x + 40, int(h * 0.9) + 10], fill=(255, 120, 0))
    return img


def main():
    ap = argparse.ArgumentParser(description="独立控制 Thermalright Trofeo Vision 9.16 屏幕")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="只读设备信息，不写屏")
    sub.add_parser("handshake", help="握手并打印分辨率")

    p_img = sub.add_parser("image", help="显示一张图片")
    p_img.add_argument("path")
    p_img.add_argument("--seconds", type=float, default=0, help="显示时长，0=一直显示(保活)")
    p_img.add_argument("--fps", type=float, default=1.0, help="保活重发频率")

    p_demo = sub.add_parser("demo", help="显示内置测试画面")
    p_demo.add_argument("--seconds", type=float, default=10)
    p_demo.add_argument("--fps", type=float, default=4.0)

    args = ap.parse_args()

    dev = find_device()
    print(f"找到设备: {dev.idVendor:04x}:{dev.idProduct:04x} bus={dev.bus} addr={dev.address}")

    if args.cmd == "probe":
        for cfg in dev:
            for intf in cfg:
                print(f"  接口 {intf.bInterfaceNumber} 类={intf.bInterfaceClass}")
                for ep in intf:
                    print(f"    端点 0x{ep.bEndpointAddress:02x} "
                          f"attr=0x{ep.bmAttributes:02x} maxpkt={ep.wMaxPacketSize}")
        return

    t = Trofeo(dev).open()
    try:
        t.handshake()
        print(f"握手成功: PM={t.pm} SUB={t.sub} 分辨率={t.width}x{t.height}")

        if args.cmd == "handshake":
            return

        if args.cmd == "image":
            from PIL import Image
            img = Image.open(args.path)
            t.send_pil(img)
            print("已发送一帧")
            end = None if args.seconds == 0 else time.time() + args.seconds
            while end is None or time.time() < end:
                time.sleep(1.0 / max(0.1, args.fps))
                t.send_pil(img)

        elif args.cmd == "demo":
            end = time.time() + args.seconds
            tick = 0
            while time.time() < end:
                t.send_pil(make_demo_frame(t.width, t.height, tick))
                tick += 1
                time.sleep(1.0 / max(0.5, args.fps))
    except KeyboardInterrupt:
        pass
    finally:
        t.close()


if __name__ == "__main__":
    main()
