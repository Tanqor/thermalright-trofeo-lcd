"""extended_display.py - 一键把机箱屏变成 Windows 扩展屏（开机自启入口）

流程：
  1. 确保 usbmmidd 虚拟显示器已启用，且分辨率是 1920x462
  2. 找到那块虚拟屏在 mss 里的序号
  3. 直接跑 mirror 循环，把虚拟屏内容推到机箱屏

由计划任务在登录时以最高权限拉起（启用虚拟屏需要管理员）。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

import paths

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = paths.LOG_DIR
LOG = paths.LOG_PATH
DEVICEINSTALLER = os.path.join(paths.vdd_dir(), "deviceinstaller64.exe")

TARGET_W, TARGET_H = 1920, 462
FPS = "24"

# usbmmidd 的 enableidd 1 每调用一次就新增一块虚拟屏（最多 4 块），
# 守护进程会反复重启，所以整个进程生命周期内只允许尝试一次。
_ENABLEIDD_TRIED = False


def show_cursor() -> bool:
    """从 panel_config.json 读 cursor 开关（默认开）。

    mss 抓屏不含光标，所以要自己画进去，否则扩展屏上看不到鼠标。
    """
    try:
        import json
        with open(paths.config_path(), encoding="utf-8-sig") as f:
            cfg = json.load(f)
        return bool(cfg.get("cursor", True))
    except Exception:
        return True


def screen_transform():
    """从 panel_config.json 读方向设置：rotate / flip。

    面板物理安装方向可能和图像不一致（倒装、反向），Windows 的显示方向
    设置对这块屏无效，必须在发送前变换。
    """
    try:
        import json
        with open(paths.config_path(), encoding="utf-8-sig") as f:
            cfg = json.load(f)
        rotate = int(cfg.get("rotate", 0) or 0)
        flip = str(cfg.get("flip", "none") or "none").lower()
    except FileNotFoundError:
        print("[warn] 没找到 panel_config.json，方向按 rotate=0 flip=none 处理")
        rotate, flip = 0, "none"
    except Exception as e:
        # 千万别静默回落：配置文件读不出来（比如被写成带 BOM 的 UTF-8）会
        # 让画面方向突然反过来，这里必须留下线索。
        print(f"[warn] 读 panel_config.json 失败（{type(e).__name__}: {e}），"
              f"方向按 rotate=0 flip=none 处理")
    if rotate not in (0, 180):
        rotate = 0
    if flip not in ("none", "h", "v"):
        flip = "none"
    return rotate, flip


# ── 日志 ────────────────────────────────────────────────────────────
def setup_log():
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 2 * 1024 * 1024:
            os.replace(LOG, LOG + ".1")
    except Exception:
        pass
    f = open(LOG, "a", encoding="utf-8", buffering=1)
    f.write("\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    sys.stdout = f
    sys.stderr = f
    return f


# ── 虚拟显示器 ──────────────────────────────────────────────────────
class DEVMODE(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),   # DEVMODEW：WCHAR[32] = 64 字节
        ("dmSpecVersion", ctypes.c_ushort), ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort), ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
        ("dmDisplayOrientation", ctypes.c_ulong), ("dmDisplayFixedOutput", ctypes.c_ulong),
        ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short), ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort), ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong), ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong), ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong), ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong), ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong), ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong), ("dmPanningHeight", ctypes.c_ulong),
    ]


DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
CDS_UPDATEREGISTRY = 0x00000001


class DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", ctypes.c_ulong),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


def list_displays():
    """返回 [(device_name, w, h, primary)]"""
    user32 = ctypes.windll.user32
    out = []
    i = 0
    while True:
        dd = DISPLAY_DEVICE()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICE)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            break
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        if user32.EnumDisplaySettingsW(dd.DeviceName, -1, ctypes.byref(dm)):
            out.append((dd.DeviceName, dm.dmPelsWidth, dm.dmPelsHeight,
                        bool(dd.StateFlags & 0x4)))  # DISPLAY_DEVICE_PRIMARY_DEVICE = 0x4
        i += 1
    return out


def ensure_virtual_display():
    """确保存在一块 1920x462 的非主显示器。必要时启用 usbmmidd 并设置模式。

    注意：usbmmidd v2 的 `enableidd 1` 不是幂等的——每执行一次就新增一块虚拟
    显示器，最多 4 块。守护进程会无限重启，如果每次都调用它，几轮下来就会把
    4 块全用满（实测发生过：桌面多出 3 块 1920x1080 的虚拟屏）。
    因此这里加两道保险：
      1. 每个进程生命周期内最多调用一次 enableidd；
      2. 若已存在多余的虚拟显示器，先清掉多余的，只留 462 那块。
    """
    global _ENABLEIDD_TRIED
    user32 = ctypes.windll.user32

    def have_target():
        return any(w == TARGET_W and h == TARGET_H and not prim
                   for _, w, h, prim in list_displays())

    if not have_target():
        if _ENABLEIDD_TRIED:
            print("虚拟屏缺失，但本进程已尝试过 enableidd，跳过以免重复新增")
        elif os.path.exists(DEVICEINSTALLER):
            _ENABLEIDD_TRIED = True
            print("启用虚拟显示器 enableidd 1 ...")
            try:
                subprocess.run([DEVICEINSTALLER, "enableidd", "1"], timeout=60,
                               capture_output=True)
            except Exception as e:
                print("enableidd 失败:", e)
            time.sleep(5)
        else:
            print("找不到", DEVICEINSTALLER)

    if have_target():
        print("虚拟屏已就绪 1920x462")
        return True

    # 启用了但模式不对：挑一块非主显示器设成 1920x462
    for name, w, h, prim in list_displays():
        if prim:
            continue
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        ok = False
        idx = 0
        while user32.EnumDisplaySettingsW(name, idx, ctypes.byref(dm)):
            if dm.dmPelsWidth == TARGET_W and dm.dmPelsHeight == TARGET_H:
                ok = True
                break
            idx += 1
        if not ok:
            continue
        dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT
        r = user32.ChangeDisplaySettingsExW(name, ctypes.byref(dm), None,
                                            CDS_UPDATEREGISTRY, None)
        print(f"设置 {name} -> {TARGET_W}x{TARGET_H} 返回 {r}")
        time.sleep(2)
        if have_target():
            return True
    print("警告：没能把虚拟屏设成 1920x462")
    return False


def find_monitor_index():
    import mss
    sct = (mss.MSS if hasattr(mss, "MSS") else mss.mss)()
    for i, m in enumerate(sct.monitors):
        if i == 0:
            continue
        if m["width"] == TARGET_W and m["height"] == TARGET_H:
            return i
    return None


def wait_capture_ready(timeout=90.0):
    """等桌面抓屏真正可用，再开始推流。

    开机时虚拟显示器的驱动/合成器可能比本服务晚就绪：EnumDisplayDevices 已经
    报告显示器存在，但 BitBlt 仍会失败——实测开机瞬间连续失败 2028 次才恢复。
    这里用一个真实抓屏试探，就绪后再启动镜像，避免开机阶段空转。

    正常情况下第一次试探就成功，不会引入任何延迟。
    """
    import mss
    cls = mss.MSS if hasattr(mss, "MSS") else mss.mss
    t0 = time.time()
    wait = 0.5
    attempt = 0
    while time.time() - t0 < timeout:
        attempt += 1
        sct = None
        try:
            sct = cls()
            target = None
            for i, m in enumerate(sct.monitors):
                if i == 0:
                    continue
                if m["width"] == TARGET_W and m["height"] == TARGET_H:
                    target = m
                    break
            if target is None:
                raise RuntimeError("没找到 1920x462 显示器")
            sct.grab({"left": target["left"], "top": target["top"],
                      "width": target["width"], "height": target["height"]})
            if attempt > 1:
                print(f"抓屏就绪（等待 {time.time() - t0:.0f}s，试探 {attempt} 次）")
            return True
        except Exception:
            time.sleep(wait)
            wait = min(2.0, wait * 1.5)
        finally:
            if sct is not None:
                try:
                    sct.close()
                except Exception:
                    pass
    print(f"警告：抓屏 {timeout:.0f}s 内未就绪，仍然继续启动")
    return False


def main():
    log = setup_log()
    print("启动扩展屏服务")
    sys.path.insert(0, HERE)
    import mirror

    restarts = 0
    while True:
        try:
            ensure_virtual_display()
            idx = find_monitor_index()
            if idx is None:
                raise RuntimeError("没有找到 1920x462 的虚拟显示器")
            print(f"使用 mss monitor {idx}")
            wait_capture_ready()
            rotate, flip = screen_transform()
            if rotate or flip != "none":
                print(f"方向变换: rotate={rotate} flip={flip}")
            sys.argv = ["mirror.py", "--monitor", str(idx), "--fit", "stretch",
                        "--fps", FPS, "--panel",
                        "--rotate", str(rotate), "--flip", flip]
            if not show_cursor():
                sys.argv.append("--no-cursor")
            mirror.main()
            why = "正常退出"
        except KeyboardInterrupt:
            print("收到中断，退出")
            break
        except BaseException as e:          # noqa: BLE001 — 守护进程必须活下去
            why = f"异常 {type(e).__name__}: {e}"

        restarts += 1
        delay = min(30, 3 * restarts)
        print(f"[{time.strftime('%H:%M:%S')}] [supervisor] 镜像结束（{why}），"
              f"{delay}s 后重启（第 {restarts} 次）")
        try:
            log.flush()
        except Exception:
            pass
        time.sleep(delay)

    try:
        log.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
