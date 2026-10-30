"""cursor.py - 抓取当前鼠标光标的真实图像（GDI），用于叠加进镜像画面

mss / BitBlt 抓屏不包含光标（光标是独立叠加层），所以扩展屏镜像里看不到鼠标。
这里用 GetCursorInfo + GetIconInfo + DrawIconEx 把光标画到一块 32 位 DIB 上，
再读出来交给 Pillow 合成。

只依赖 ctypes + Pillow，不需要管理员权限。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


# ── Win32 结构 ──────────────────────────────────────────────────
class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", wintypes.POINT),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


CURSOR_SHOWING = 0x00000001
DI_NORMAL = 0x0003
DIB_RGB_COLORS = 0
BI_RGB = 0
_MAX_CURSOR = 512          # 正常光标不会这么大，超过就当异常丢弃

# ── 显式声明签名 ────────────────────────────────────────────────
# 不声明的话 ctypes 默认按 32 位 int 处理句柄，64 位下句柄会溢出报
# OverflowError: int too long to convert。
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC

gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                                   wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP

gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ

gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL

gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL

gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.GetObjectW.restype = ctypes.c_int

user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
user32.GetCursorInfo.restype = wintypes.BOOL

user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ICONINFO)]
user32.GetIconInfo.restype = wintypes.BOOL

user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                              wintypes.HANDLE, ctypes.c_int, ctypes.c_int,
                              wintypes.UINT, wintypes.HANDLE, wintypes.UINT]
user32.DrawIconEx.restype = wintypes.BOOL

SRCCOPY = 0x00CC0020
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, wintypes.HDC,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL

# hCursor -> (Image, hotspot_x, hotspot_y)；光标句柄对同一形状是稳定的，
# 缓存起来避免每帧重画一遍 GDI。
_cache: dict[int, tuple[Image.Image, int, int]] = {}
_dpi_done = False


def _ensure_dpi() -> None:
    """让 GetCursorPos 返回物理像素，和 mss 抓屏坐标一致。

    mss 自己会设置一次；这里重复设置失败也无所谓。
    """
    global _dpi_done
    if _dpi_done:
        return
    _dpi_done = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def _mono_cursor_image(hbm_mask: int, w: int, h: int) -> Image.Image | None:
    """把单色光标掩码转成 RGBA。

    单色光标的 hbmMask 是 1bpp 位图，高度是光标的两倍：
      上半部分 = AND 掩码，下半部分 = XOR 掩码。
    合成规则（与 Windows 一致）：
      AND=1 XOR=0 -> 透明
      AND=1 XOR=1 -> 反色（这里近似成黑）
      AND=0 XOR=0 -> 黑
      AND=0 XOR=1 -> 白
    为了在任意背景上都看得见，这里把「透明」做成 alpha=0，其余按黑白着色，
    并给白像素补一圈深色描边（否则白色光标在浅色壁纸上会消失）。
    """
    half = h // 2 if h % 2 == 0 else h
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 1
    bmi.bmiHeader.biCompression = BI_RGB
    bmi.bmiHeader.biClrUsed = 2
    bmi.bmiColors[0] = 0x000000          # 索引 0 = 黑
    bmi.bmiColors[1] = 0xFFFFFF          # 索引 1 = 白

    hdc = gdi32.CreateCompatibleDC(None)
    if not hdc:
        return None
    src_dc = gdi32.CreateCompatibleDC(None)
    hbmp = None
    old = None
    old_src = None
    try:
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            hdc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not hbmp or not bits:
            return None
        old = gdi32.SelectObject(hdc, hbmp)
        old_src = gdi32.SelectObject(src_dc, hbm_mask)
        if not gdi32.BitBlt(hdc, 0, 0, w, h, src_dc, 0, 0, SRCCOPY):
            return None
        gdi32.SelectObject(src_dc, old_src)
        old_src = None
        gdi32.SelectObject(hdc, old)
        old = None

        stride = ((w + 31) // 32) * 4          # 1bpp 每行 4 字节对齐
        raw = ctypes.string_at(bits, stride * h)

        def bit(x: int, y: int) -> int:
            byte = raw[y * stride + (x >> 3)]
            return (byte >> (7 - (x & 7))) & 1

        out = Image.new("RGBA", (w, half), (0, 0, 0, 0))
        px = out.load()
        for y in range(half):
            for x in range(w):
                a = bit(x, y)          # AND 掩码
                z = bit(x, y + half)   # XOR 掩码
                if a and not z:
                    continue                       # 透明
                if a and z:
                    px[x, y] = (0, 0, 0, 255)      # 反色 -> 黑
                elif not a and z:
                    px[x, y] = (255, 255, 255, 255)  # 白
                else:
                    px[x, y] = (0, 0, 0, 255)      # 黑
        return out
    finally:
        if old:
            gdi32.SelectObject(hdc, old)
        if old_src:
            gdi32.SelectObject(src_dc, old_src)
        if hbmp:
            gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(src_dc)
        gdi32.DeleteDC(hdc)


def _add_outline(img: Image.Image) -> Image.Image:
    """给白色光标补一圈深色描边，保证在浅色壁纸上也看得清。"""
    from PIL import ImageFilter
    alpha = img.getchannel("A")
    grown = alpha.filter(ImageFilter.MaxFilter(3))
    outline = Image.new("RGBA", img.size, (0, 0, 0, 0))
    outline.putalpha(grown)
    outline.paste((0, 0, 0, 200), (0, 0), grown)
    # 把深色描边垫在下面
    res = Image.new("RGBA", img.size, (0, 0, 0, 0))
    res.alpha_composite(outline)
    res.alpha_composite(img)
    return res


def _read_cursor_bitmap(hcursor: int) -> tuple[Image.Image, int, int] | None:
    """读取光标图像。彩色光标走 DIB 绘制，单色光标走掩码解析。"""
    ii = ICONINFO()
    if not user32.GetIconInfo(hcursor, ctypes.byref(ii)):
        return None

    hdc = None
    hbmp = None
    try:
        src = ii.hbmColor or ii.hbmMask
        if not src:
            return None

        bm = BITMAP()
        if not gdi32.GetObjectW(src, ctypes.sizeof(BITMAP), ctypes.byref(bm)):
            return None
        w, h = bm.bmWidth, bm.bmHeight
        if w <= 0 or h <= 0 or w > _MAX_CURSOR or h > _MAX_CURSOR:
            return None

        # ── 单色光标：hbmColor 为空，掩码高度翻倍 ──
        if not ii.hbmColor:
            img = _mono_cursor_image(ii.hbmMask, w, h)
            if img is None:
                return None
            return _add_outline(img), int(ii.xHotspot), int(ii.yHotspot)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h        # 负数 = 自上而下，省得再翻转
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        hdc = gdi32.CreateCompatibleDC(None)
        if not hdc:
            return None
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            hdc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not hbmp or not bits:
            return None

        old = gdi32.SelectObject(hdc, hbmp)
        try:
            if not user32.DrawIconEx(hdc, 0, 0, hcursor, w, h, 0, None, DI_NORMAL):
                return None
        finally:
            gdi32.SelectObject(hdc, old)

        raw = ctypes.string_at(bits, w * h * 4)
        img = Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", 0, 1)
        img = img.copy()      # 脱离 DIB 内存

        # 少数彩色光标 alpha 全 0，退化成"非黑即不透明"
        if img.getchannel("A").getextrema()[1] == 0:
            px = img.load()
            for y in range(h):
                for x in range(w):
                    r, g, b, _a = px[x, y]
                    px[x, y] = (r, g, b, 255 if (r or g or b) else 0)

        return img, int(ii.xHotspot), int(ii.yHotspot)
    finally:
        if hbmp:
            gdi32.DeleteObject(hbmp)
        if hdc:
            gdi32.DeleteDC(hdc)
        # GetIconInfo 会复制位图，用完必须删，否则每帧泄漏 GDI 句柄
        if ii.hbmMask:
            gdi32.DeleteObject(ii.hbmMask)
        if ii.hbmColor:
            gdi32.DeleteObject(ii.hbmColor)


def get_cursor() -> tuple[Image.Image, int, int, int, int] | None:
    """返回 (光标图, 热点x, 热点y, 屏幕x, 屏幕y)；光标不可见时返回 None。"""
    _ensure_dpi()
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(ci)):
        return None
    if not (ci.flags & CURSOR_SHOWING) or not ci.hCursor:
        return None

    key = int(ci.hCursor)
    got = _cache.get(key)
    if got is None:
        got = _read_cursor_bitmap(key)
        if got is None:
            return None
        if len(_cache) > 32:       # 防止长期运行句柄表无限增长
            _cache.clear()
        _cache[key] = got

    img, hx, hy = got
    return img, hx, hy, int(ci.ptScreenPos.x), int(ci.ptScreenPos.y)
