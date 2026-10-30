"""orient.py - 方向校准工具

往机箱屏发一张带明显方向标记的测试图，用来判断面板的物理安装方向。

用法（先停守护进程）：
    Stop-ScheduledTask TrofeoLCDMirror
    .venv\\Scripts\\python.exe src\\orient.py            # 原始方向
    .venv\\Scripts\\python.exe src\\orient.py --rotate 180
    .venv\\Scripts\\python.exe src\\orient.py --flip h

图上四个标记：
    上边  绿色  "▲ 上 UP"
    下边  红色  "▼ 下 DOWN"
    左边  蓝色  "◀ 左 LEFT"
    右边  黄色  "右 RIGHT ▶"
    左上角还有一个白色小方块，用来判断是否镜像。

看完告诉我：哪个颜色/文字出现在哪一边，我就能算出该用哪种变换。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths          # noqa: E402
import trofeo         # noqa: E402

W, H = 1920, 462


def font(size):
    for name in ("msyhbd.ttc", "msyh.ttc", "simhei.ttf"):
        try:
            return ImageFont.truetype(os.path.join(r"C:\Windows\Fonts", name), size)
        except Exception:
            continue
    return ImageFont.load_default()


def build(rotate=0, flip="none"):
    img = Image.new("RGB", (W, H), (12, 14, 20))
    d = ImageDraw.Draw(img)

    # 边框，标出四边
    d.rectangle([0, 0, W - 1, H - 1], outline=(90, 90, 100), width=3)

    f_big = font(64)
    f_mid = font(40)

    def center_text(y, text, fill, fnt):
        tw = d.textlength(text, font=fnt)
        d.text(((W - tw) / 2, y), text, font=fnt, fill=fill)

    # 上 / 下
    center_text(18, "▲ 上 UP ▲", (90, 230, 130), f_big)
    center_text(H - 82, "▼ 下 DOWN ▼", (240, 90, 90), f_big)

    # 左 / 右（靠边竖排位置）
    d.text((40, H // 2 - 60), "◀\n左\nL\nE\nF\nT", font=f_mid, fill=(90, 170, 250), spacing=6)
    rw = d.textlength("R", font=f_mid)
    d.multiline_text((W - 70, H // 2 - 60), "▶\n右\nR\nI\nG\nH\nT",
                     font=f_mid, fill=(250, 210, 90), spacing=6)

    # 中央：正读文本 + 大箭头
    center_text(H // 2 - 96, "方向校准 / ORIENTATION", (235, 235, 240), f_mid)
    center_text(H // 2 - 30, "ABC 123 你好", (200, 210, 230), f_big)
    center_text(H // 2 + 46, "若这行字是正的，方向就对了", (170, 180, 200), font(34))

    # 左上角白色方块（判断镜像）
    d.rectangle([18, 96, 118, 146], fill=(255, 255, 255))
    d.text((126, 100), "左上角方块", font=font(30), fill=(255, 255, 255))

    # 右上角：序号
    d.text((W - 300, 96), "右上角", font=font(30), fill=(255, 255, 255))

    # 应用变换
    if flip == "h":
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    elif flip == "v":
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    if rotate % 360 == 180:
        img = img.rotate(180)
    return img


def main():
    ap = argparse.ArgumentParser(description="方向校准")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 180])
    ap.add_argument("--flip", default="none", choices=["none", "h", "v"])
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--save", metavar="PATH", help="同时把图和日志存成 PNG")
    args = ap.parse_args()

    img = build(args.rotate, args.flip)
    if args.save:
        img.save(args.save)
        print("已保存:", args.save)

    print(f"发送校准图：rotate={args.rotate} flip={args.flip}，持续 {args.seconds:.0f}s")
    t = trofeo.find_device()
    dev = trofeo.Trofeo(t)
    dev.open()
    try:
        info = dev.handshake()
        print("握手 OK:", info)
        import io
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        data = buf.getvalue()
        n = 0
        end = time.time() + args.seconds
        while time.time() < end:
            dev.send_jpeg(data)
            n += 1
            time.sleep(0.12)
        print("已发送", n, "次")
    finally:
        dev.close()


if __name__ == "__main__":
    main()
