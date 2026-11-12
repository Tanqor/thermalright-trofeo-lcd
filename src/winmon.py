"""winmon.py - 实时枚举 Windows 显示器的坐标/分辨率

为什么需要它：mss 的 `monitors` 列表是在 `MSS()` 构造时算好并缓存的，
整个对象生命周期内都不会更新。用户一改「显示设置」，拿到的一直是旧坐标
（实测：同一个 sct 对象移动副屏后仍报旧位置，只有新建 MSS 才反映新值）。
守护进程是长期运行的，所以必须每次直接问 Windows。

只依赖 ctypes。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

# DEVMODE.dmFields 位
DM_POSITION = 0x00000020
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000

ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
CDS_UPDATEREGISTRY = 0x00000001

DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004


class DEVMODE(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", ctypes.c_long),
        ("dmPositionY", ctypes.c_long),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", wintypes.SHORT),
        ("dmDuplex", wintypes.SHORT),
        ("dmYResolution", wintypes.SHORT),
        ("dmTTOption", wintypes.SHORT),
        ("dmCollate", wintypes.SHORT),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


class DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


def _dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def list_displays() -> list[dict]:
    """返回所有已连接到桌面的显示器。

    每项：{'name': '\\\\.\\DISPLAY1', 'left', 'top', 'width', 'height',
           'primary': bool}
    顺序与 Windows 的枚举顺序一致（改显示设置后可能变）。
    """
    _dpi_aware()
    out: list[dict] = []
    i = 0
    while True:
        dd = DISPLAY_DEVICE()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICE)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            break
        i += 1
        if not (dd.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP):
            continue
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        if not user32.EnumDisplaySettingsW(dd.DeviceName, ENUM_CURRENT_SETTINGS,
                                          ctypes.byref(dm)):
            continue
        out.append({
            "name": dd.DeviceName,
            "left": int(dm.dmPositionX),
            "top": int(dm.dmPositionY),
            "width": int(dm.dmPelsWidth),
            "height": int(dm.dmPelsHeight),
            "primary": bool(dd.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE),
        })
    return out


def monitor_list() -> list[dict]:
    """和 mss 的 `sct.monitors` 同构：[并集, 各屏...]，但每次都是实时值。"""
    mons = list_displays()
    if not mons:
        return []
    x0 = min(m["left"] for m in mons)
    y0 = min(m["top"] for m in mons)
    x1 = max(m["left"] + m["width"] for m in mons)
    y1 = max(m["top"] + m["height"] for m in mons)
    union = {"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0}
    return [union] + [{"left": m["left"], "top": m["top"],
                       "width": m["width"], "height": m["height"]} for m in mons]


def set_position(name: str, x: int, y: int) -> int:
    """把某块屏移到 (x, y)。返回 0 表示成功（DISP_CHANGE_SUCCESSFUL）。"""
    dm = DEVMODE()
    dm.dmSize = ctypes.sizeof(DEVMODE)
    if not user32.EnumDisplaySettingsW(name, ENUM_CURRENT_SETTINGS,
                                       ctypes.byref(dm)):
        return -1
    dm.dmFields = DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT
    dm.dmPositionX = x
    dm.dmPositionY = y
    return user32.ChangeDisplaySettingsExW(name, ctypes.byref(dm), None,
                                           CDS_UPDATEREGISTRY, None)
