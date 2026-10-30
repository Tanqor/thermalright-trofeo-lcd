"""launcher.py - 以管理员权限启动守护进程（开机时弹一次 UAC）

为什么需要它：
  AMD 平台的 CPU 温度（Tctl/Tdie）必须通过 ring0 内核驱动读取，而加载这个驱动
  需要管理员权限。计划任务即使用 RunLevel=Highest，在 Interactive 登录类型下
  也不会自动提权，所以守护进程拿到的是普通用户令牌，LHM 读到的温度恒为 0。

做法：
  计划任务运行本脚本（普通权限）。它检查自己是否有管理员权限：
    - 有  -> 直接 exec 守护进程（正常情况，理论上不会走到）
    - 没有 -> 用 ShellExecuteW 的 runas 动词弹 UAC，提权后再启动守护进程
  用户点一次「是」，之后整个会话里守护进程都是管理员权限，温度正常。

注意：
  - 不要用 S4U 登录类型：S4U 跑在 session 0，看不到用户桌面，会导致
    找不到虚拟显示器，进而反复 enableidd 1 生成多块虚拟屏。
  - 提权失败（用户点「否」）时，仍然以普通权限启动守护进程——机箱屏照常工作，
    只是 CPU 温度显示为 0。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(HERE, "extended_display.py")
PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.exists(PYW):
    PYW = sys.executable

SW_SHOWNORMAL = 1
FLAG_FILE = os.path.join(HERE, "launcher.log")


def log(msg: str) -> None:
    try:
        with open(FLAG_FILE, "a", encoding="utf-8") as f:
            import time
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def spawn_daemon(elevated: bool) -> bool:
    """启动守护进程。elevated=True 时通过 runas 提权。"""
    if elevated:
        # ShellExecuteW 返回 > 32 表示成功
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", PYW, f'"{DAEMON}"', HERE, SW_SHOWNORMAL)
        log(f"提权启动返回 {r}")
        return r > 32

    try:
        subprocess.Popen([PYW, DAEMON], cwd=HERE,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        log("普通权限启动成功")
        return True
    except Exception as e:
        log(f"普通权限启动失败: {e}")
        return False


def main() -> int:
    if is_admin():
        log("已有管理员权限，直接启动守护进程")
        return 0 if spawn_daemon(False) else 1

    log("无管理员权限，尝试 UAC 提权（CPU 温度需要）")
    if spawn_daemon(True):
        return 0

    # 用户拒绝了 UAC —— 仍然启动，保证机箱屏能用
    log("提权被拒绝，退回普通权限启动（CPU 温度将不可用）")
    return 0 if spawn_daemon(False) else 1


if __name__ == "__main__":
    sys.exit(main())
