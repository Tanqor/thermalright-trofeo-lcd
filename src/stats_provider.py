"""stats_provider.py - 采集 CPU / 内存 / GPU / 天气，供面板渲染使用

数据源：
  CPU 占用/温度 : LibreHardwareMonitorLib (需要管理员；AMD 必须在 ring0 读温度)
  内存          : psutil
  GPU 占用/温度/显存 : nvidia-smi（NVIDIA 卡最可靠）
  天气          : open-meteo.com（免 API key）

所有采集放在后台线程，主线程随时取最新快照，绝不阻塞 USB 发送。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

import psutil

import paths

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = paths.config_path()
LHM_DLL = paths.lhm_dll()

DEFAULT_CFG = {
    "city": "",
    "lat": None,
    "lon": None,
    "use_system_location": True,
    "weather_minutes": 10,
    "gpu_seconds": 2,
}

# WMO 天气代码 -> 中文
WMO = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "小毛雨", 53: "毛雨", 55: "浓毛雨",
    56: "冻毛雨", 57: "冻毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷暴冰雹", 99: "强雷暴冰雹",
}

WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception:
            pass
    return cfg


# ── 系统定位（Windows 定位服务 / Wi-Fi 定位）──────────────────────
def get_system_location(timeout=12.0):
    """返回 (lat, lon, accuracy米) 或 None。需要系统定位开启且授权桌面应用。"""
    try:
        import clr
        import System
        clr.AddReference("System.Device")
        from System.Device.Location import GeoCoordinateWatcher, GeoPositionAccuracy
    except Exception as e:
        print("[stats] 加载 System.Device 失败:", e)
        return None
    try:
        w = GeoCoordinateWatcher(GeoPositionAccuracy.Default)
        if not w.TryStart(False, System.TimeSpan.FromSeconds(timeout)):
            print("[stats] 定位服务启动失败（Permission=%s）" % w.Permission)
            return None
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                loc = w.Position.Location
                if not loc.IsUnknown:
                    return (float(loc.Latitude), float(loc.Longitude),
                            float(loc.HorizontalAccuracy))
                time.sleep(0.5)
            print("[stats] 定位超时（Status=%s，请检查 设置>隐私和安全性>位置 是否开启）"
                  % w.Status)
        finally:
            w.Stop()
    except Exception as e:
        print("[stats] 定位异常:", e)
    return None


def reverse_city(lat, lon):
    """坐标 -> 中文城市名。多个源 + 重试，全部失败返回空串。"""
    apis = [
        ("https://api.bigdatacloud.net/data/reverse-geocode-client"
         f"?latitude={lat}&longitude={lon}&localityLanguage=zh", "bdc"),
        ("https://nominatim.openstreetmap.org/reverse"
         f"?format=jsonv2&lat={lat}&lon={lon}&accept-language=zh-CN", "osm"),
    ]
    for url, kind in apis:
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "TrofeoLCD/1.0"})
                with urllib.request.urlopen(req, timeout=12) as r:
                    js = json.load(r)
                if kind == "bdc":
                    city = js.get("city") or js.get("locality") or ""
                else:
                    a = js.get("address") or {}
                    city = (a.get("city") or a.get("town") or a.get("county")
                            or a.get("state") or "")
                city = (city or "").strip()
                if city.endswith("市"):
                    city = city[:-1]
                if city:
                    return city
                break
            except Exception as e:
                print(f"[stats] 反查城市失败({kind} 第{attempt+1}次):", e)
                time.sleep(1.0)
    return ""


# ── 天气 ────────────────────────────────────────────────────────────
def geocode(city: str):
    url = ("https://geocoding-api.open-meteo.com/v1/search"
           f"?name={urllib.parse.quote(city)}&count=1&language=zh&format=json")
    with urllib.request.urlopen(url, timeout=15) as r:
        js = json.load(r)
    res = (js.get("results") or [None])[0]
    if not res:
        raise RuntimeError("找不到城市: " + city)
    return res["latitude"], res["longitude"], res.get("name") or city


def fetch_weather(lat, lon):
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code"
           "&timezone=auto")
    with urllib.request.urlopen(url, timeout=15) as r:
        js = json.load(r)
    cur = js.get("current") or {}
    code = int(cur.get("weather_code", -1))
    return {
        "temp": cur.get("temperature_2m"),
        "feels": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "desc": WMO.get(code, "未知"),
    }


# ── GPU（nvidia-smi）───────────────────────────────────────────────
def nvidia_smi():
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "nvidia-smi.exe")
    if not os.path.exists(exe):
        exe = "nvidia-smi"
    q = "utilization.gpu,temperature.gpu,memory.used,memory.total"
    out = subprocess.run([exe, f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=8,
                         creationflags=0x08000000)  # CREATE_NO_WINDOW
    line = out.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    return {
        "util": float(parts[0]),
        "temp": float(parts[1]),
        "mem_used": float(parts[2]),
        "mem_total": float(parts[3]),
    }


# ── 采集器 ──────────────────────────────────────────────────────────
class StatsProvider(threading.Thread):
    def __init__(self, cfg=None):
        super().__init__(daemon=True)
        self.cfg = cfg or load_cfg()
        self._lock = threading.Lock()
        self._data = {
            "time": "--:--", "date": "", "week": "",
            "cpu_util": None, "cpu_temp": None, "cpu_power": None,
            "mem_pct": None, "mem_used": None, "mem_total": None,
            "gpu_util": None, "gpu_temp": None, "gpu_mem_pct": None,
            "city": self.cfg.get("city", ""), "w_temp": None,
            "w_desc": "", "w_hum": None, "w_ok": False,
        }
        self._stop = threading.Event()
        self._comp = None
        self._lat = self.cfg.get("lat")
        self._lon = self.cfg.get("lon")
        self._loc_done = False
        self._next_weather = 0.0
        self._next_gpu = 0.0

    def _resolve_location(self):
        """优先用系统定位，失败才用配置里的城市。"""
        if self._loc_done:
            return
        if bool(self.cfg.get("use_system_location", True)):
            loc = get_system_location()
            if loc:
                self._lat, self._lon = loc[0], loc[1]
                city = reverse_city(loc[0], loc[1])
                # 反查失败时不能沿用配置里的旧城市（那是占位值，会显示错地名）
                self.cfg["city"] = city or "本地"
                with self._lock:
                    self._data["city"] = self.cfg.get("city", "")
                print("[stats] 系统定位: %.5f,%.5f 精度%.0fm -> %s"
                      % (loc[0], loc[1], loc[2], self.cfg.get("city", "")))
                self._loc_done = True
                return
            print("[stats] 系统定位不可用，回退到配置城市:", self.cfg.get("city"))
        self._loc_done = True

    # ---- LHM ----
    def _init_lhm(self):
        try:
            import clr
            clr.AddReference(LHM_DLL)
            from LibreHardwareMonitor.Hardware import Computer
            c = Computer()
            c.IsCpuEnabled = True
            c.IsGpuEnabled = True
            c.IsMemoryEnabled = True
            c.Open()
            self._comp = c
            return True
        except Exception as e:
            print("[stats] LHM 初始化失败（CPU 温度将不可用）:", e)
            self._comp = None
            return False

    def _read_lhm(self):
        if self._comp is None:
            return
        cpu_util = cpu_temp = cpu_power = None
        for hw in self._comp.Hardware:
            try:
                hw.Update()
            except Exception:
                continue
            name = (hw.Name or "")
            is_cpu = "Ryzen" in name or "Core" in name or "CPU" in name.upper()
            for s in hw.Sensors:
                st = str(s.SensorType)
                if st == "Load" and s.Name == "CPU Total":
                    cpu_util = s.Value
                elif st == "Temperature" and s.Name in (
                        "Core (Tctl/Tdie)", "CPU Package", "Core Average"):
                    cpu_temp = s.Value
                elif st == "Power" and s.Name in ("Package", "CPU Package"):
                    cpu_power = s.Value
            if not is_cpu:
                continue
        with self._lock:
            if cpu_util is not None:
                self._data["cpu_util"] = cpu_util
            if cpu_temp is not None:
                self._data["cpu_temp"] = cpu_temp
            if cpu_power is not None:
                self._data["cpu_power"] = cpu_power

    def _read_mem(self):
        vm = psutil.virtual_memory()
        with self._lock:
            self._data["mem_pct"] = vm.percent
            self._data["mem_used"] = vm.used / 2 ** 30
            self._data["mem_total"] = vm.total / 2 ** 30

    def _read_gpu(self):
        try:
            g = nvidia_smi()
            with self._lock:
                self._data["gpu_util"] = g["util"]
                self._data["gpu_temp"] = g["temp"]
                self._data["gpu_mem_pct"] = (100.0 * g["mem_used"] / g["mem_total"]
                                             if g["mem_total"] else None)
        except Exception:
            pass

    def _read_weather(self):
        try:
            if not self._lat or not self._lon:
                self._lat, self._lon, nm = geocode(self.cfg.get("city", "上海"))
                self.cfg["city"] = nm
            w = fetch_weather(self._lat, self._lon)
            with self._lock:
                self._data.update({
                    "city": self.cfg.get("city", ""),
                    "w_temp": w["temp"], "w_desc": w["desc"],
                    "w_hum": w["humidity"], "w_ok": True,
                })
            return True
        except Exception as e:
            print("[stats] 天气获取失败:", e)
            return False

    def run(self):
        self._init_lhm()
        self._resolve_location()
        while not self._stop.is_set():
            now = datetime.now()
            with self._lock:
                self._data["time"] = now.strftime("%H:%M")
                self._data["date"] = f"{now.month}月{now.day}日"
                self._data["week"] = WEEK[now.weekday()]
            self._read_lhm()
            self._read_mem()
            t = time.time()
            if t >= self._next_gpu:
                self._read_gpu()
                self._next_gpu = t + float(self.cfg.get("gpu_seconds", 2))
            if t >= self._next_weather:
                ok = self._read_weather()
                # 失败别等 10 分钟，60 秒后重试
                self._next_weather = t + (60 * float(self.cfg.get("weather_minutes", 10))
                                          if ok else 60)
            self._stop.wait(1.0)

    def stop(self):
        self._stop.set()
        if self._comp is not None:
            try:
                self._comp.Close()
            except Exception:
                pass

    def snapshot(self):
        with self._lock:
            return dict(self._data)


if __name__ == "__main__":
    p = StatsProvider()
    p.start()
    for _ in range(3):
        time.sleep(1.2)
        print(json.dumps(p.snapshot(), ensure_ascii=False, default=lambda o: round(o, 1)))
    p.stop()
