"""gen_preview.py - 生成 README 用的预览图与演示 GIF

用法（在仓库根目录，用 .venv 的 python）：
    .venv\\Scripts\\python.exe tools\\gen_preview.py

输出：
    docs/preview.png   1920x462 静态预览（实时数据）
    docs/preview.gif   动画演示（数据波动）
"""
from __future__ import annotations

import math
import os
import sys
import time

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import panel as pnl  # noqa: E402

DOCS = os.path.join(ROOT, "docs")
os.makedirs(DOCS, exist_ok=True)

W, H = 1920, 462


def background() -> Image.Image:
    """做一张类似壁纸的背景：渐变 + 光斑 + 两块半透明"窗口"，
    用来体现信息卡片的半透明效果。"""
    img = Image.new("RGB", (W, H), (18, 20, 28))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)],
               fill=(int(62 + 74 * t), int(72 + 92 * t), int(104 + 116 * t)))

    blob = Image.new("RGB", (W, H), (0, 0, 0))
    bd = ImageDraw.Draw(blob)
    bd.ellipse([-260, -180, 760, 700], fill=(124, 96, 208))
    bd.ellipse([860, -140, 1520, 330], fill=(86, 126, 216))
    bd.ellipse([1330, 110, 1980, 540], fill=(214, 116, 158))
    bd.ellipse([420, 240, 1060, 700], fill=(74, 196, 176))
    blob = blob.filter(ImageFilter.GaussianBlur(95))
    img = Image.blend(img, blob, 0.62)

    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([742, 56, 1188, 402], 18,
                        fill=(255, 255, 255, 24), outline=(255, 255, 255, 42), width=2)
    d.rounded_rectangle([1236, 96, 1812, 424], 18,
                        fill=(255, 255, 255, 16), outline=(255, 255, 255, 34), width=2)
    for i in range(6):
        y = 96 + i * 46
        d.rounded_rectangle([784, y, 784 + (320 - i * 26), y + 14], 7,
                            fill=(255, 255, 255, 58))
    for i in range(5):
        y = 136 + i * 54
        d.rounded_rectangle([1268, y, 1268 + (486 - i * 42), y + 16], 8,
                            fill=(255, 255, 255, 46))
    return img


def live_snapshot() -> dict:
    """尽量取实时数据；取不到就用一份合理的示例。"""
    try:
        import stats_provider as sp
        p = sp.StatsProvider()
        p.start()
        for _ in range(18):
            time.sleep(1)
            if p.snapshot().get("w_ok"):
                break
        s = p.snapshot()
        p.stop()
        if s:
            print("使用实时数据:", s.get("city"), s.get("w_temp"), "℃")
            return s
    except Exception as e:  # noqa: BLE001
        print("实时数据不可用:", e)
    return {
        "time": time.strftime("%H:%M"), "date": time.strftime("%m月%d日"),
        "week": "周四", "city": "深圳", "w_temp": 29.0, "w_desc": "多云",
        "w_hum": 62, "w_ok": True, "cpu_util": 34.0, "cpu_temp": 57.0,
        "cpu_power": 62.0, "mem_pct": 68.0, "mem_used": 15.7, "mem_total": 23.1,
        "gpu_util": 38.0, "gpu_temp": 49.0, "gpu_mem_pct": 46.0,
    }


def clamp(v, lo=1.0, hi=99.0):
    return max(lo, min(hi, v))


def main():
    base_s = live_snapshot()
    p = pnl.Panel(H)

    # 1) 静态预览
    png = p.apply(background(), base_s)
    out_png = os.path.join(DOCS, "preview.png")
    png.save(out_png, optimize=True)
    print("已生成", out_png, os.path.getsize(out_png) // 1024, "KB")

    # 2) 动画 GIF
    frames = []
    N, FPS = 30, 10
    bg = background()
    for i in range(N):
        t = i / N * 2 * math.pi
        s = dict(base_s)
        s["cpu_util"] = clamp(38 + 17 * math.sin(t))
        s["cpu_temp"] = clamp(56 + 5 * math.sin(t + 0.6), 30, 95)
        s["mem_pct"] = clamp(68 + 5 * math.sin(t * 0.7))
        s["gpu_util"] = clamp(36 + 22 * math.sin(t * 1.3 + 1.2))
        s["gpu_temp"] = clamp(49 + 4 * math.sin(t * 1.3 + 1.6), 30, 95)
        s["gpu_mem_pct"] = clamp(46 + 6 * math.sin(t * 0.5))
        img = p.apply(bg.copy(), s)
        frames.append(img.resize((960, 231), Image.LANCZOS))

    out_gif = os.path.join(DOCS, "preview.gif")
    frames[0].save(out_gif, save_all=True, append_images=frames[1:],
                   duration=int(1000 / FPS), loop=0, optimize=True, disposal=2)
    print("已生成", out_gif, os.path.getsize(out_gif) // 1024, "KB")


if __name__ == "__main__":
    main()
