"""panel.py - 渲染左侧 462x462 半透明信息卡片（时间/天气/CPU/内存/GPU）

只依赖 Pillow。渲染结果是一张 RGBA 图，用 alpha 复合到镜像画面左上角，
所以底下拖动窗口的内容会隐隐透出来（半透明效果）。
"""
from __future__ import annotations

import time

from PIL import Image, ImageDraw, ImageFont

FONTS = r"C:\Windows\Fonts"
F_TIME = (FONTS + r"\bahnschrift.ttf", 92)   # 大数字时间
F_BIG = (FONTS + r"\msyhbd.ttc", 30)         # 城市/温度
F_MID = (FONTS + r"\msyhbd.ttc", 23)         # 数值
F_LBL = (FONTS + r"\msyh.ttc", 21)           # 标签
F_SML = (FONTS + r"\msyh.ttc", 18)           # 日期/湿度

# 卡片配色
CARD = (14, 16, 22, 150)      # 半透明深色底
BORDER = (255, 255, 255, 38)
TXT = (255, 255, 255, 255)
SUB = (190, 198, 212, 255)
TRACK = (255, 255, 255, 42)

ACCENT = {
    "cpu": (78, 200, 255),
    "mem": (172, 142, 255),
    "gpu": (118, 228, 152),
}


def _font(spec):
    path, size = spec
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


class Panel:
    """渲染并缓存面板；只有数据变化时才重画。"""

    def __init__(self, size=462):
        self.size = size
        self._cache_img = None
        self._cache_key = None
        self._f_time = _font(F_TIME)
        self._f_big = _font(F_BIG)
        self._f_mid = _font(F_MID)
        self._f_lbl = _font(F_LBL)
        self._f_sml = _font(F_SML)

    # ── 小工具 ──────────────────────────────────────────────────
    @staticmethod
    def _pct(v):
        return "--" if v is None else f"{v:.0f}%"

    @staticmethod
    def _temp(v):
        return "--" if v is None else f"{v:.0f}°"

    def _bar(self, d, box, frac, color):
        x0, y0, x1, y1 = box
        r = (y1 - y0) // 2
        d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=TRACK)
        if frac and frac > 0:
            frac = min(1.0, frac)
            w = max(r * 2, int((x1 - x0) * frac))
            d.rounded_rectangle([x0, y0, x0 + w, y1], radius=r,
                                fill=color + (225,))

    def render(self, s: dict) -> Image.Image:
        """按快照渲染，返回 RGBA 图像（带缓存）。"""
        key = (s.get("time"), s.get("date"), s.get("week"),
               None if s.get("cpu_util") is None else round(s["cpu_util"]),
               None if s.get("cpu_temp") is None else round(s["cpu_temp"]),
               None if s.get("mem_pct") is None else round(s["mem_pct"]),
               None if s.get("gpu_util") is None else round(s["gpu_util"]),
               None if s.get("gpu_temp") is None else round(s["gpu_temp"]),
               s.get("city"), s.get("w_desc"),
               None if s.get("w_temp") is None else round(s["w_temp"]))
        if key == self._cache_key and self._cache_img is not None:
            return self._cache_img

        S = self.size
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        pad = 8
        d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=26,
                            fill=CARD, outline=BORDER, width=1)

        x0 = 30
        x1 = S - 30

        # ── 时间 ──
        t = s.get("time") or "--:--"
        d.text((x0 - 3, 16), t, font=self._f_time, fill=TXT)

        # 右上：日期 / 星期
        date = s.get("date") or ""
        week = s.get("week") or ""
        for txt, y in ((date, 34), (week, 64)):
            if not txt:
                continue
            tw = d.textlength(txt, font=self._f_sml)
            d.text((x1 - tw, y), txt, font=self._f_sml, fill=SUB)

        # 分隔线
        d.line([x0, 132, x1, 132], fill=(255, 255, 255, 30), width=1)

        # ── 天气 ──
        city = s.get("city") or ""
        wt_ = s.get("w_temp")
        wd = s.get("w_desc") or ""
        wh = s.get("w_hum")
        if s.get("w_ok") and wt_ is not None:
            head = f"{city} {wt_:.0f}°"
            d.text((x0 - 2, 146), head, font=self._f_big, fill=TXT)
            hw = d.textlength(head, font=self._f_big)
            d.text((x0 + hw + 14, 156), wd, font=self._f_mid, fill=ACCENT["cpu"])
            if wh is not None:
                hum = f"湿度 {wh:.0f}%"
                tw = d.textlength(hum, font=self._f_sml)
                d.text((x1 - tw, 158), hum, font=self._f_sml, fill=SUB)
        else:
            d.text((x0 - 2, 150), f"{city} 天气获取中…" if city else "天气获取中…",
                   font=self._f_mid, fill=SUB)

        # ── 三行指标 ──
        rows = [
            ("CPU", "cpu", s.get("cpu_util"), s.get("cpu_temp"), "%"),
            ("内存", "mem", s.get("mem_pct"), None, "%"),
            ("显卡", "gpu", s.get("gpu_util"), s.get("gpu_temp"), "%"),
        ]
        y = 214
        rowh = 72
        for label, k, util, temp, unit in rows:
            color = ACCENT[k]
            # 标签
            d.text((x0, y + 8), label, font=self._f_lbl, fill=SUB)
            # 数值（右侧对齐）
            if temp is not None:
                val = f"{self._pct(util)} · {temp:.0f}°C"
            else:
                if k == "mem":
                    used = s.get("mem_used")
                    total = s.get("mem_total")
                    val = (f"{util:.0f}% · {used:.1f}G" if util is not None and used
                           else self._pct(util))
                else:
                    val = self._pct(util)
            tw = d.textlength(val, font=self._f_mid)
            d.text((x1 - tw, y + 4), val, font=self._f_mid, fill=TXT)
            # 进度条
            bx0 = x0 + 66
            bx1 = x1 - max(tw + 18, 96)
            if bx1 > bx0 + 30:
                frac = None if util is None else util / 100.0
                self._bar(d, [bx0, y + 16, bx1, y + 28], frac, color)
            y += rowh

        self._cache_img = img
        self._cache_key = key
        return img

    def apply(self, frame: Image.Image, s: dict) -> Image.Image:
        """把面板半透明叠加到画面左上角。"""
        p = self.render(s)
        if frame.mode != "RGB":
            frame = frame.convert("RGB")
        base = frame.convert("RGBA")
        base.alpha_composite(p, (0, 0))
        return base.convert("RGB")
