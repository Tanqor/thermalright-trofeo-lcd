"""mirror.py - 把屏幕/窗口内容实时推到 Thermalright Trofeo Vision 9.16 机箱屏

需要：pip install pyusb mss pillow   以及 trofeo.py 在同一目录。

用法示例：
  # 镜像主屏的某个区域（左上角 0,0 起，宽高 1600x385，正好 4.16:1）
  python mirror.py --region 0,0,1600,385

  # 镜像第 2 块屏（装了虚拟显示器之后就是"扩展屏"模式）
  python mirror.py --monitor 2

  # 把 16:9 内容居中填满整条屏（裁掉上下），默认就是 crop
  python mirror.py --monitor 1 --fit crop

  # 竖向压缩塞进去（不变形但压扁）
  python mirror.py --monitor 1 --fit stretch

  # 加黑边保持比例
  python mirror.py --monitor 1 --fit pad

说明：
  * 屏固定 1920x462。源不是这个比例时，crop=裁切填满，pad=加黑边，stretch=拉伸。
  * 实测约 24 fps 上限（USB 带宽限制），--fps 默认 24。
  * 画面无变化时会跳帧以省带宽，但最多 1.5 秒必发一帧（否则屏会自己回 logo）。
  * 设备被占用/拔出会自动重连。
"""
from __future__ import annotations

import argparse
import ctypes
import io
import sys
import threading
import time

import mss
from PIL import Image

import trofeo

MAX_JPEG = 440_000
KEEPALIVE_S = 1.5
IDLE_PERIOD = 0.2   # 画面静止时的抓屏间隔（秒）

# 看门狗：改显示器配置/驱动重置会让 WinUSB 句柄静默失效——写入不报错但数据
# 进不到屏里，表现为画面永久冻结在最后一帧。以下两个阈值用来判定"发送停滞"。
SEND_STUCK_S = 3.0    # 单帧发送超过这个时长就认为卡住（正常单帧约 40ms）
SEND_IDLE_S = 5.0     # 连续这么久没有成功发送，且生产者仍在出帧，就重置连接
WATCHDOG_TICK_S = 1.0


def fit_image(img: Image.Image, tw: int, th: int, mode: str) -> Image.Image:
    if img.size == (tw, th):
        return img
    if mode == "stretch":
        return img.resize((tw, th), Image.BILINEAR)
    sw, sh = img.size
    if mode == "crop":
        # 保持比例，放大到覆盖目标，再居中裁切
        scale = max(tw / sw, th / sh)
        nw, nh = int(round(sw * scale)), int(round(sh * scale))
        r = img.resize((max(nw, tw), max(nh, th)), Image.BILINEAR)
        left = (r.width - tw) // 2
        top = (r.height - th) // 2
        return r.crop((left, top, left + tw, top + th))
    # pad：保持比例缩放到内接，四周补黑
    scale = min(tw / sw, th / sh)
    nw, nh = max(1, int(round(sw * scale))), max(1, int(round(sh * scale)))
    r = img.resize((nw, nh), Image.BILINEAR)
    canvas = Image.new("RGB", (tw, th), (0, 0, 0))
    canvas.paste(r, ((tw - nw) // 2, (th - nh) // 2))
    return canvas


def encode(img: Image.Image) -> bytes:
    for q in (85, 70, 55, 40):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q)
        if buf.tell() < MAX_JPEG:
            return buf.getvalue()
    return buf.getvalue()


def ts() -> str:
    """日志时间戳，便于把告警和系统事件（休眠/分辨率变化）对齐。"""
    return time.strftime("%H:%M:%S")


def transform(img: Image.Image, rotate: int = 0, flip: str = "none") -> Image.Image:
    """在编码前对整幅画面做方向变换。

    物理面板的安装方向可能和图像不一致（倒装 / 反向），而 Windows 的
    「显示方向」设置对这块屏无效——屏是直接接收 JPEG 显示的。
    所以方向必须在发送前处理。

    rotate: 0 或 180（90/270 会改变宽高比，与面板不符，不支持）
    flip:   none / h（左右镜像）/ v（上下镜像）
    """
    if flip == "h":
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    elif flip == "v":
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    if rotate % 360 == 180:
        img = img.rotate(180)
    return img


class Link:
    """带自动重连的发送端。

    除了异常重连，还暴露 ``sending_since`` / ``last_ok`` 给看门狗用：
    WinUSB 句柄失效时写入可能既不报错也不生效，只能靠"多久没成功发出去"判断。
    """

    def __init__(self):
        self.t = None
        self._lock = threading.Lock()
        self.sending_since = 0.0   # >0 表示正在发一帧，值是开始时间
        self.last_ok = time.time()  # 最后一次成功发出的时间
        self._need_reconnect = False

    def suspect(self, reason: str = ""):
        """标记当前句柄可疑：下次 send 前先重连（不打断正在进行的发送）。

        会被抓屏失败路径高频调用，所以只在"从正常转为可疑"时打一次日志。
        """
        if self._need_reconnect:
            return          # 已经是可疑状态，不重复刷日志
        self._need_reconnect = True
        if reason and not self.sending_since:
            print(f"[{ts()}] [warn] 疑似连接失效（{reason}），下次发送前重连")

    def connect(self):
        dev = trofeo.find_device()
        t = trofeo.Trofeo(dev).open()
        t.handshake()
        with self._lock:
            self.t = t
        return t

    def force_reset(self, reason: str):
        """看门狗用：丢掉当前句柄并断开，下一次 send 会重新连接。

        在另一个线程里 close 通常能让阻塞中的 bulk 写立刻失败返回。
        """
        with self._lock:
            t = self.t
            self.t = None
        self.sending_since = 0.0
        self.last_ok = time.time()   # 重置观察窗口，避免看门狗每个 tick 重复触发
        if t is not None:
            try:
                t.close()
            except Exception:
                pass
        print(f"[{ts()}] [warn] 看门狗重置 USB 连接（{reason}）")

    def send(self, jpeg: bytes) -> bool:
        if self._need_reconnect:
            self._need_reconnect = False
            with self._lock:
                t_old = self.t
                self.t = None
            if t_old is not None:
                try:
                    t_old.close()
                except Exception:
                    pass
        with self._lock:
            t = self.t
        if t is None:
            try:
                t = self.connect()
            except SystemExit:
                raise
            except Exception:
                return False
        self.sending_since = time.time()
        try:
            t.send_jpeg(jpeg)
            self.last_ok = time.time()
            return True
        except Exception as e:
            print(f"[{ts()}] [warn] 发送失败，准备重连: {e}")
            try:
                t.close()
            except Exception:
                pass
            with self._lock:
                self.t = None
            time.sleep(0.5)
            try:
                self.connect()
            except Exception:
                pass
            return False
        finally:
            self.sending_since = 0.0


def grab_region(sct, args):
    if args.region:
        l, t_, w, h = (int(v) for v in args.region.split(","))
        return {"left": l, "top": t_, "width": w, "height": h}
    idx = args.monitor
    mons = sct.monitors
    if idx >= len(mons):
        raise SystemExit(f"没有第 {idx} 块屏（共 {len(mons)-1} 块）")
    return mons[idx]


def refresh_region(sct, args, current):
    """按显示器序号取当前坐标；位置没变就返回原对象。

    显示器位置会在用户拖动「显示设置」后变化，而抓屏本身仍然成功——此时
    grab_region 不会因异常被重新调用，守护进程就会一直抓旧坐标（表现为
    画面内容错位/抓到的是一块空白区）。每帧核对一次，坐标变了立刻跟上。
    """
    if args.region:
        return current
    idx = args.monitor
    mons = sct.monitors
    if idx >= len(mons):
        return current
    live = mons[idx]
    if (live["left"], live["top"], live["width"], live["height"]) == (
            current["left"], current["top"], current["width"], current["height"]):
        return current
    print(f"[{ts()}] [warn] 显示器位置变化，更新区域: "
          f"({current['left']},{current['top']}) -> ({live['left']},{live['top']})")
    return live


def main():
    ap = argparse.ArgumentParser(description="把屏幕内容推到机箱屏")
    ap.add_argument("--monitor", type=int, default=1, help="mss 显示器序号，1=主屏")
    ap.add_argument("--region", help="截取区域 左,上,宽,高")
    ap.add_argument("--fit", choices=["crop", "pad", "stretch"], default="crop")
    ap.add_argument("--fps", type=float, default=24.0)
    ap.add_argument("--seconds", type=float, default=0, help="0=一直跑")
    ap.add_argument("--quality", type=int, default=0, help="强制 JPEG 质量，0=自适应")
    ap.add_argument("--no-detect", action="store_true",
                    help="关掉变化检测，每帧都发（测速/放视频时用）")
    ap.add_argument("--panel", action="store_true",
                    help="在左上角叠加 462x462 信息面板（时间/天气/CPU/内存/GPU）")
    ap.add_argument("--dump-frame", metavar="PATH",
                    help="调试：把第 3 帧实际发出的画面存成 PNG")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 180],
                    help="发送前旋转整幅画面：0 或 180（面板倒装时用 180）")
    ap.add_argument("--flip", default="none", choices=["none", "h", "v"],
                    help="发送前镜像：none/h(左右)/v(上下)")
    args = ap.parse_args()

    if args.fps > 30:
        print(f"[warn] 硬件上限约 24fps，实际不到 {args.fps}")

    link = Link()
    t = link.connect()
    tw, th = t.width, t.height
    print(f"目标屏 {tw}x{th}，模式 {args.fit}，目标 {args.fps} fps")

    sct = (mss.MSS if hasattr(mss, "MSS") else mss.mss)()
    region = grab_region(sct, args)
    print("截取区域:", region)

    def new_sct():
        return (mss.MSS if hasattr(mss, "MSS") else mss.mss)()

    cap = {"sct": sct}

    period = 1.0 / max(1.0, args.fps)
    end = None if args.seconds == 0 else time.time() + args.seconds

    # 信息面板（可选）
    provider = None
    panel = None
    if args.panel:
        from stats_provider import StatsProvider
        from panel import Panel
        provider = StatsProvider()
        provider.start()
        panel = Panel(th)   # 卡片是正方形，边长 = 屏高（462）
        print("信息面板已启用")

    # 抓屏/编码（生产者）与 USB 发送（消费者）分离：USB 单帧约 40ms 是绝对
    # 瓶颈，串行做会把抓屏时间叠加进去（实测 17.8fps），流水线后逼近 23fps。
    import threading

    cond = threading.Condition()
    slot = {"jpeg": None, "seq": 0, "stop": False, "error": None, "last_frame_time": 0.0}
    stats = {"sent": 0}

    def producer():
        nonlocal region
        last_sig = None
        last_emit = 0.0
        fails = 0                 # 连续抓屏失败次数
        total_fails = 0           # 累计失败次数（排查用）
        try:
            while not slot["stop"]:
                t0 = time.perf_counter()
                if end is not None and time.time() >= end:
                    break
                try:
                    region = refresh_region(cap["sct"], args, region)
                    raw = cap["sct"].grab(region)
                    if fails:
                        print(f"[{ts()}] [warn] 抓屏恢复正常（连续失败 {fails} 次后）")
                        fails = 0
                except Exception as e:
                    # 常见于显示器配置变化 / 驱动重置 / 会话切换 / 显示休眠。
                    # 立即重建 DXGI 抓屏上下文，不等待——等待会造成可见卡顿。
                    fails += 1
                    total_fails += 1
                    # 连续失败可能上千次，日志限流：第 1 次 + 之后每 50 次
                    if fails == 1 or fails % 50 == 0:
                        print(f"[{ts()}] [warn] 抓屏失败(第 {fails} 次/累计 {total_fails} 次)，重建抓屏上下文:", e)
                    try:
                        cap["sct"].close()
                    except Exception:
                        pass
                    try:
                        cap["sct"] = new_sct()
                        region = grab_region(cap["sct"], args)
                        if fails == 1:
                            print(f"[{ts()}] [warn] 抓屏上下文已重建，区域: {region['width']}x{region['height']}")
                        # 改显示器位置/分辨率往往同时让 WinUSB 句柄失效，主动重连一次
                        # （比等看门狗超时快，用户几乎看不到中断）
                        link.suspect("显示器配置变化")
                        continue          # 立即重试，不 sleep
                    except Exception as e2:
                        print(f"[{ts()}] [warn] 重建失败，1s 后重试:", e2)
                        time.sleep(1.0)
                    continue
                img = Image.frombytes("RGB", raw.size, raw.rgb)
                img = fit_image(img, tw, th, args.fit)
                if panel is not None:
                    try:
                        img = panel.apply(img, provider.snapshot())
                    except Exception as e:
                        print("[warn] 面板渲染失败:", e)
                if args.rotate or args.flip != "none":
                    img = transform(img, args.rotate, args.flip)
                sig = img.resize((48, 12), Image.BILINEAR).tobytes()
                now = time.time()
                changed = args.no_detect or sig != last_sig
                if changed or (now - last_emit) >= KEEPALIVE_S:
                    data = encode(img)
                    if args.dump_frame and slot.get("dumped") != 3:
                        try:
                            slot["dumped"] = slot.get("dumped", 0) + 1
                            if slot["dumped"] == 3:
                                img.save(args.dump_frame)
                                print("已保存调试帧:", args.dump_frame)
                        except Exception:
                            pass
                    with cond:
                        slot["jpeg"] = data
                        slot["seq"] += 1
                        slot["last_frame_time"] = now
                        cond.notify()
                    last_sig = sig
                    last_emit = now
                # 自适应：画面没变就降低抓屏频率（24fps -> ~5fps），
                # 大幅省 CPU；保活帧仍按 KEEPALIVE_S 准时发。
                spent = time.perf_counter() - t0
                target = period if changed else max(period, IDLE_PERIOD)
                if target > spent:
                    time.sleep(target - spent)
        except BaseException as e:            # noqa: BLE001 — 传回主线程
            with cond:
                slot["error"] = e
        finally:
            with cond:
                slot["stop"] = True
                cond.notify()

    def watchdog():
        """检测发送停滞并强制重连。

        场景：改显示器位置/分辨率、驱动重置后，WinUSB 句柄会静默失效——写调用
        不报错但数据进不到屏里，画面永久冻结。生产者还在出帧，消费者也在"发"，
        靠异常检测不到，只能用时间判定。
        """
        resets = 0
        while True:
            time.sleep(WATCHDOG_TICK_S)
            if slot["stop"]:
                return
            now = time.time()
            # 生产者是否还在出新帧
            with cond:
                last_frame = slot.get("last_frame_time", 0.0)
            producing = (now - last_frame) < SEND_IDLE_S
            stuck_for = now - link.sending_since if link.sending_since else 0.0
            if stuck_for > SEND_STUCK_S:
                resets += 1
                link.force_reset(f"单帧发送卡住 {stuck_for:.1f}s，第 {resets} 次")
                continue
            idle_for = now - link.last_ok
            if idle_for > SEND_IDLE_S and producing:
                resets += 1
                link.force_reset(f"已 {idle_for:.1f}s 没有成功发送，第 {resets} 次")
                continue
            if resets and idle_for < 2.0:
                print(f"[{ts()}] [warn] 发送已恢复（看门狗重置 {resets} 次后）")
                resets = 0

    def consumer():
        # USB 写是硬实时路径：给它更高线程优先级，避免抓屏/编码线程抢占
        # 导致每帧从 42ms 掉到 54ms。
        try:
            k = ctypes.windll.kernel32
            k.SetThreadPriority(k.GetCurrentThread(), 2)  # THREAD_PRIORITY_HIGHEST
        except Exception:
            pass
        last_seq = 0
        try:
            while True:
                with cond:
                    while slot["seq"] == last_seq and not slot["stop"]:
                        cond.wait(0.5)
                    if slot["stop"] and slot["seq"] == last_seq:
                        break
                    data = slot["jpeg"]
                    last_seq = slot["seq"]
                if data is not None and link.send(data):
                    stats["sent"] += 1
        except BaseException as e:            # noqa: BLE001
            with cond:
                slot["error"] = e
                slot["stop"] = True
                cond.notify()

    t_start = time.time()
    th_p = threading.Thread(target=producer, daemon=True)
    th_c = threading.Thread(target=consumer, daemon=True)
    th_w = threading.Thread(target=watchdog, daemon=True)
    th_p.start()
    th_c.start()
    th_w.start()
    try:
        while th_p.is_alive() or th_c.is_alive():
            th_p.join(0.2)
            th_c.join(0.2)
    except KeyboardInterrupt:
        with cond:
            slot["stop"] = True
            cond.notify()
    finally:
        dur = time.time() - t_start
        sent = stats["sent"]
        print(f"\n[{ts()}] 已发送 {sent} 帧，用时 {dur:.1f}s，平均 {sent/max(dur,1e-9):.1f} fps")
        if slot["error"]:
            print(f"[{ts()}] 错误:", slot["error"])
        if provider is not None:
            try:
                provider.stop()
            except Exception:
                pass
        try:
            link.t.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
