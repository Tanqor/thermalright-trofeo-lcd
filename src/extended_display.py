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
import winmon

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
DM_DISPLAYORIENTATION = 0x00000080
DMDO_DEFAULT = 0                       # 横向
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


def _enable_stamp_path() -> str:
    return os.path.join(paths.LOG_DIR, "last_enableidd.txt")


def _enableidd_cooldown_ok(min_seconds: float = 300.0) -> bool:
    """跨进程冷却：距离上次 enableidd 不足 min_seconds 就不要再调用。

    `_ENABLEIDD_TRIED` 只是进程内变量，而守护进程会被计划任务反复重启
    （看门狗自愈、崩溃重启）。如果在虚拟屏尚未枚举出来的窗口里每个新进程
    都调一次 enableidd，几轮下来就会堆出多块虚拟屏——这在实际使用中真的
    发生过。用文件记录上次调用时间，把冷却期跨进程延续下去。
    """
    try:
        with open(_enable_stamp_path(), encoding="utf-8") as f:
            last = float(f.read().strip())
        gap = time.time() - last
        if gap < min_seconds:
            print(f"上次 enableidd 距今仅 {gap:.0f}s（<{min_seconds:.0f}s），跳过以免重复新增")
            return False
    except Exception:
        pass
    return True


def _stamp_enableidd() -> None:
    try:
        os.makedirs(paths.LOG_DIR, exist_ok=True)
        with open(_enable_stamp_path(), "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _find_virtual_monitor():
    """找那块「本来就是我们的虚拟屏、但当前分辨率/方向不对」的显示器。

    返回 (设备名, 宽, 高) 或 None。
    判据：非主屏，且宽或高等于 462（usbmmidd 的 462 高度是这块屏独有的，
    改成纵向后变成 462x1920，宽变成 462 仍然能认出来）。
    """
    for d in winmon.list_displays():
        if d["primary"]:
            continue
        if d["width"] == TARGET_H or d["height"] == TARGET_H:
            return d["name"], d["width"], d["height"]
    return None


def _remove_extra_virtuals():
    """把多余的虚拟显示器（usbmmidd 多启出来的）关掉，只留目标那块。

    `enableidd 0` 每调一次删一块，删除顺序由驱动决定，所以每删一块都重新
    检查，绝不把目标屏删掉。
    """
    exe = DEVICEINSTALLER
    if not os.path.exists(exe):
        return
    for _ in range(3):                    # 最多 4 块，最多删 3 块
        displays = winmon.list_displays()
        target = [d for d in displays
                  if not d["primary"] and d["width"] == TARGET_W
                  and d["height"] == TARGET_H]
        extras = [d for d in displays
                  if not d["primary"] and d not in target]
        if not extras:
            return
        print(f"发现 {len(extras)} 块多余虚拟屏，删除一块…")
        try:
            subprocess.run([exe, "enableidd", "0"], timeout=60, capture_output=True)
        except Exception as e:
            print("enableidd 0 失败:", e)
            return
        time.sleep(4)
        if not [d for d in winmon.list_displays()
                if not d["primary"] and d["width"] == TARGET_W
                and d["height"] == TARGET_H]:
            # 目标屏被删掉了，立刻补回
            print("目标屏被误删，重新启用")
            try:
                subprocess.run([exe, "enableidd", "1"], timeout=60,
                               capture_output=True)
            except Exception:
                pass
            time.sleep(5)
            return


def ensure_virtual_display():
    """确保存在一块 1920x462 的非主显示器。必要时启用 usbmmidd 并设置模式。

    注意：usbmmidd v2 的 `enableidd 1` 不是幂等的——每执行一次就新增一块虚拟
    显示器，最多 4 块。守护进程会无限重启，如果每次都调用它，几轮下来就会把
    4 块全用满（实测发生过：桌面多出 3 块 1920x1080 的虚拟屏）。
    因此这里加三道保险：
      1. 每个进程生命周期内最多调用一次 enableidd；
      2. 跨进程冷却，重启后的新进程也不会马上再调一次；
      3. 若已存在多余的虚拟显示器，先清掉多余的，只留 462 那块。
    """
    global _ENABLEIDD_TRIED
    user32 = ctypes.windll.user32

    def have_target():
        return any(w == TARGET_W and h == TARGET_H and not prim
                   for _, w, h, prim in list_displays())

    if not have_target():
        # 先判断"虚拟屏还在、只是模式不对" vs "虚拟屏根本没启用"。
        # 前者（例如用户把显示方向改成纵向，分辨率变成 462x1920）绝对
        # 不能再调 enableidd——那会新增一块虚拟屏。这种情况交给下面的
        # "把某块非主显示器设成 1920x462" 去纠正即可。
        virtual_present = _find_virtual_monitor()
        if virtual_present:
            print(f"虚拟屏存在但模式是 {virtual_present[1]}x{virtual_present[2]}，"
                  f"尝试改回 {TARGET_W}x{TARGET_H} 横向")
        elif _ENABLEIDD_TRIED:
            print("虚拟屏缺失，但本进程已尝试过 enableidd，跳过以免重复新增")
        elif not _enableidd_cooldown_ok():
            pass
        elif os.path.exists(DEVICEINSTALLER):
            _ENABLEIDD_TRIED = True
            _stamp_enableidd()
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
        # 直接强制设回 1920x462 横向，不再先检查"模式列表里有没有这个模式"。
        # 纵向状态下这块屏当前的模式是 462x1920，EnumDisplaySettings 里未必
        # 列得出 1920x462，之前先检查再设置就永远走不到设置这一步（实测：
        # 改纵向后守护进程一直报"没能把虚拟屏设成 1920x462"）。
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        user32.EnumDisplaySettingsW(name, -1, ctypes.byref(dm))
        dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYORIENTATION
        dm.dmPelsWidth = TARGET_W
        dm.dmPelsHeight = TARGET_H
        dm.dmDisplayOrientation = DMDO_DEFAULT      # 强制回横向
        r = user32.ChangeDisplaySettingsExW(name, ctypes.byref(dm), None,
                                            CDS_UPDATEREGISTRY, None)
        print(f"设置 {name} -> {TARGET_W}x{TARGET_H} 横向 返回 {r}")
        time.sleep(2)
        if have_target():
            return True
    print("警告：没能把虚拟屏设成 1920x462")
    return False

def find_monitor_index():
    """返回目标虚拟屏在 mss monitor 里的序号。

    必须用实时枚举：mss 的 sct.monitors 在 MSS() 构造时缓存，改显示设置
    后不会更新，拿它永远找不到新状态。序号按"第几块非主显示器"推算——
    mss 的 monitors[0] 是并集，后面各屏的顺序与 Windows 枚举顺序一致。
    """
    for i, d in enumerate(winmon.list_displays()):
        if d["primary"]:
            continue
        if d["width"] == TARGET_W and d["height"] == TARGET_H:
            # Windows 枚举里非主屏的顺序 == mss monitors 里的顺序
            return _mss_index_of(d["name"])
    return None


def _mss_index_of(device_name: str):
    """把 \\\\.\\DISPLAY20 这种名字换算成 mss 的显示器序号。

    mss 的 monitors 顺序与 EnumDisplayDevices 的顺序一致（monitors[0] 是
    并集，所以要 +1）。名字不稳定时不猜，直接按位置顺序兜底。
    """
    try:
        disp = winmon.list_displays()
        order = [d["name"] for d in disp]
        return order.index(device_name) + 1
    except Exception:
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
    missing = 0          # 连续找不到目标屏的次数
    while True:
        try:
            ensure_virtual_display()
            # 兜底：万一 usbmmidd 被多启出了几块虚拟屏（enableidd 非幂等），
            # 这里主动清掉多余的，只留 1920x462 那块。
            try:
                _remove_extra_virtuals()
            except Exception as e:
                print("清理多余虚拟屏失败:", e)
            idx = find_monitor_index()
            if idx is None:
                # 目标屏暂时不可用（例如用户刚在显示设置里改方向，驱动正在
                # 重配；usbmmidd 其实不支持纵向，会短暂没有有效模式）。
                # 这里不能立刻抛异常——那会触发 supervisor 每 3s 重启一次，
                # 每次重启都要重开 USB 设备，用户看到的就是"卡死"。
                # 改成原地等，等它自己回来。
                missing += 1
                if missing == 1 or missing % 10 == 0:
                    print(f"[{time.strftime('%H:%M:%S')}] [warn] 暂时找不到 "
                          f"{TARGET_W}x{TARGET_H} 虚拟屏（第 {missing} 次），"
                          f"等待显示设置恢复…")
                time.sleep(3)
                if missing >= 40:        # 约 2 分钟仍没有，才交给 supervisor
                    raise RuntimeError(
                        f"连续 {missing} 次找不到 {TARGET_W}x{TARGET_H} 虚拟屏")
                continue
            if missing:
                print(f"[{time.strftime('%H:%M:%S')}] 虚拟屏已恢复（等待了 {missing} 轮）")
                missing = 0
            print(f"使用 mss monitor {idx}")
            wait_capture_ready()
            rotate, flip = screen_transform()
            if rotate or flip != "none":
                print(f"方向变换: rotate={rotate} flip={flip}")
            sys.argv = ["mirror.py", "--monitor", str(idx),
                        "--target-size", f"{TARGET_W}x{TARGET_H}",
                        "--fit", "stretch",
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
            # 尺寸不匹配（多半是用户把显示方向改成纵向，分辨率变成 462x1920）
            # 要立刻纠正，而且不该走退避——这是可恢复的配置问题，不是故障。
            if type(e).__name__ == "LayoutMismatch":
                print(f"[{time.strftime('%H:%M:%S')}] 显示模式不匹配，立即纠正")
                try:
                    ensure_virtual_display()
                    _remove_extra_virtuals()
                except Exception as e2:
                    print("纠正失败:", e2)
                restarts = 0        # 配置类问题不计入重启退避
                continue

        restarts += 1
        delay = min(30, 3 * restarts)
        print(f"[{time.strftime('%H:%M:%S')}] [supervisor] 镜像结束（{why}），"
              f"{delay}s 后重启（第 {restarts} 次）")
        try:
            log.flush()
        except Exception:
            pass

        # 短窗口内反复重启（例如 USB 持续异常）时，退避时间要够长，
        # 避免快速重启循环：每次重启都会重新打开 USB 设备，太密反而更难恢复。
        if restarts >= 5:
            delay = max(delay, 60)
        time.sleep(delay)

    try:
        log.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
