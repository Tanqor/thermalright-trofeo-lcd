"""paths.py - 路径解析

让同一份代码在两种布局下都能运行：
  1) 直接跑克隆下来的仓库：<repo>/src/*.py, <repo>/vendor/lhm, <repo>/vdd
  2) 用 setup.ps1 安装后的副本：文件摊平在同一目录，带 lhm/ 和 vdd/
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
# src/ 的上一级是仓库根；如果脚本被摊平复制，则 HERE 本身就是根
ROOT = os.path.dirname(HERE) if os.path.basename(HERE).lower() == "src" else HERE

_APPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
LOG_DIR = os.path.join(_APPDATA, "TrofeoLCD")
LOG_PATH = os.path.join(LOG_DIR, "mirror.log")


def _first_file(candidates):
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return candidates[0]


def _first_dir_containing(filename, candidates):
    for p in candidates:
        if p and os.path.isfile(os.path.join(p, filename)):
            return p
    return candidates[0]


def lhm_dll() -> str:
    return _first_file([
        os.environ.get("TROFEO_LHM_DLL"),
        os.path.join(ROOT, "vendor", "lhm", "LibreHardwareMonitorLib.dll"),
        os.path.join(HERE, "vendor", "lhm", "LibreHardwareMonitorLib.dll"),
        os.path.join(HERE, "lhm", "LibreHardwareMonitorLib.dll"),
        os.path.join(LOG_DIR, "lhm", "LibreHardwareMonitorLib.dll"),
    ])


def vdd_dir() -> str:
    return _first_dir_containing("deviceinstaller64.exe", [
        os.environ.get("TROFEO_VDD_DIR"),
        os.path.join(ROOT, "vdd"),
        os.path.join(HERE, "vdd"),
        os.path.join(LOG_DIR, "vdd"),
    ])


def config_path() -> str:
    """优先用仓库根的 panel_config.json；不存在则用同目录的。"""
    for p in (os.path.join(ROOT, "panel_config.json"),
              os.path.join(HERE, "panel_config.json")):
        if os.path.isfile(p):
            return p
    return os.path.join(ROOT, "panel_config.json")
