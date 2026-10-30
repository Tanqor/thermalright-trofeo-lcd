# Thermalright Trofeo Vision 9.16 → Windows 扩展屏

把 Thermalright 机箱/AIO 上的 **Trofeo Vision 9.16 LCD**（USB `0416:5408`）当成 Windows 的
**第二块显示器**用，并且左上角常驻一块半透明信息卡片（时间 / 天气 / CPU / 内存 / 显卡）。

**完全不依赖官方 TRCC 软件**（那个 16 MB 的 WPF 程序）。协议是逆向 `USBLCDNEW.dll`
的 `ThreadSendDeviceDataLY` 得到的。

---

## 效果

- Windows 显示设置里多出一块 **1920×462** 的扩展屏，窗口可以直接拖过去
- 左上角 **462×462 半透明卡片**，内容：

```
┌──────────────────────────────┐
│ 15:04              10月30日   │
│                    周四       │
│ ─────────────────────────────│
│ 深圳 31°  阴      湿度 40%    │
│                              │
│ CPU  ██████░░░░░  34% · 57°C │
│ 内存 ███████░░░░  70% · 16.3G│
│ 显卡 █████░░░░░░  38% · 49°C │
└──────────────────────────────┘
```

---

## 快速开始

1. 装 [Python 3.9+](https://www.python.org/downloads/windows/)（安装时勾选 **Add python.exe to PATH**）
2. 克隆本仓库，**右键 `setup.ps1` → 使用 PowerShell 运行**（需要管理员）

```powershell
git clone <本仓库地址>
cd thermalright-trofeo-lcd
# 右键 setup.ps1 -> 使用 PowerShell 运行
```

或者命令行：

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

`setup.ps1` 会自动完成：建 `.venv` → 装依赖 → 装 usbmmidd 虚拟显示器并设成 1920×462 →
注册登录自启任务 → 如果存在 `TRCCAppStartup` 则禁用它。

装完把窗口拖到主屏**右侧外面**即可看到效果。

---

## 硬件要求

| 项目 | 要求 |
|---|---|
| 屏幕 | Thermalright **Trofeo Vision 9.16**，USB ID `0416:5408` |
| 系统 | Windows 10 / 11 x64 |
| 驱动 | 屏幕需已绑定 **WinUSB** 驱动（装了官方 TRCC 就会自动装好） |

同方案的表亲屏（`0416:5302` / `0416:5406` 等）协议不同，**不能**直接用。
其他 PID 只需改 `src/trofeo.py` 里的 `VID/PID` 和 `src/extended_display.py` 的分辨率，
但握手/分块细节可能不同，需自行验证。

> 如果设备没绑定 WinUSB，可先用 [Zadig](https://zadig.akeo.ie/) 给它装 WinUSB 驱动。

---

## 配置

`panel_config.json`（首次安装自动从 `config.example.json` 生成）：

```json
{
  "city": "",
  "lat": null,
  "lon": null,
  "use_system_location": true,
  "weather_minutes": 10,
  "gpu_seconds": 2
}
```

| 字段 | 说明 |
|---|---|
| `use_system_location` | `true` = 用 Windows 定位服务自动定位（Wi‑Fi 定位，需系统位置权限开启） |
| `city` | 自动定位失败时的兜底城市，如 `"深圳"`；`use_system_location=false` 时直接用它 |
| `lat` / `lon` | 直接指定经纬度（优先于 `city`） |
| `weather_minutes` | 天气刷新间隔（分钟） |
| `gpu_seconds` | 显卡数据刷新间隔（秒） |

改完重启任务生效：

```powershell
Stop-ScheduledTask TrofeoLCDMirror; Start-ScheduledTask TrofeoLCDMirror
```

---

## 工作原理

```
Windows 虚拟显示器 (usbmmidd, 1920×462)
        │  DXGI/GDI 抓屏
        ▼
   mirror.py  ── 抓屏 → 缩放 → 叠加面板 → JPEG 编码
        │           （双线程流水线：抓屏/编码 ∥ USB 发送）
        ▼
   trofeo.py  ── USB Bulk 协议
        │        握手: 写 2048B(头 02 FF 00... 01) → 读 512B
        │              校验 resp[0]=3, resp[1]=0xFF, resp[8]=1
        │              分辨率 = u32@24 × (u32@28 − resp[44])
        │        帧:   JPEG → 512B 分块(16B 头 + 496B 数据)
        │              → 4096B USB 写 → 512B ACK
        ▼
   屏幕 (0416:5408) 解 JPEG 显示
```

关键细节：

- **端点**：Bulk OUT `0x09`、Bulk IN `0x81`（**不是**常见示例里的 `0x01`，必须按描述符探测）
- **保活**：屏约 2–3 秒收不到帧就自动回 logo，所以必须持续重发
- **帧上限**：单帧 JPEG 建议 < 450 KB

---

## 性能（本机实测）

| 指标 | 数值 |
|---|---|
| 帧率上限 | **约 24 fps**（协议每帧约 42 ms 固定开销，与画质无关） |
| CPU | 约 1.8% 全系统（≈1/3 个核） |
| 内存 | 约 130 MB |
| 延迟 | 约 50–70 ms |

对比官方 TRCC：内存 526 MB、CPU 1.7% —— 内存约为其 1/4。

**限制**：24 fps 适合视频/监控/信息面板，**不适合游戏和鼠标精细操作**。

---

## 手动运行 / 调试

```powershell
# 用什么都不依赖的方式看设备是否正常
.\.venv\Scripts\python.exe src\trofeo.py handshake

# 带面板，跑 20 秒
.\.venv\Scripts\python.exe src\mirror.py --monitor 2 --fit stretch --fps 24 --seconds 20 --panel

# 抓一帧实际发出的画面（排查"看不到卡片"）
.\.venv\Scripts\python.exe src\mirror.py --monitor 2 --fit stretch --seconds 8 --panel --dump-frame out.png

# 单独看采集到的数据
.\.venv\Scripts\python.exe src\stats_provider.py
```

> 运行前先停守护进程，否则设备被占用：`Stop-ScheduledTask TrofeoLCDMirror`

常用参数：

| 参数 | 作用 |
|---|---|
| `--monitor N` | 镜像第 N 块屏（1=主屏，2=虚拟扩展屏） |
| `--region l,t,w,h` | 只截取一块区域（不镜像整屏） |
| `--fit crop\|pad\|stretch` | 比例不符时：裁切填满 / 加黑边 / 拉伸 |
| `--fps` | 目标帧率，硬件上限约 24 |
| `--panel` | 叠加 462×462 信息面板 |
| `--no-detect` | 关掉变化检测，每帧必发 |

---

## 排查

| 现象 | 处理 |
|---|---|
| `Access denied (insufficient permissions)` | 设备被别的程序占用，关掉 TRCC/USBLCDNEW.exe |
| 找不到 `0416:5408` | 屏幕没插好，或驱动不是 WinUSB（用 Zadig 装） |
| 没有 1920×462 显示器 | 重跑 `setup.ps1`，或 `vdd\deviceinstaller64.exe enableidd 1` |
| 卡片看不到 | 用 `--dump-frame` 抓帧确认；或屏幕比例不是 1920×462 |
| CPU 温度显示 `--` | 需要管理员权限（LibreHardwareMonitor 要读 MSR）；确认任务"以最高权限运行" |
| 天气一直"获取中" | 检查网络；系统定位需开启"设置 → 隐私和安全性 → 位置" |
| 界面卡顿/低帧 | 属正常，24 fps 是硬件上限 |

日志：`%LOCALAPPDATA%\TrofeoLCD\mirror.log`

---

## 卸载

```powershell
powershell -ExecutionPolicy Bypass -File uninstall.ps1
# 连虚拟显示器一起移除：
powershell -ExecutionPolicy Bypass -File uninstall.ps1 -RemoveVirtualDisplay
```

会删除计划任务、停掉进程、恢复 `TRCCAppStartup` 自启。

---

## 目录结构

```
├── src/
│   ├── trofeo.py            USB 协议层（握手/分块/发送/ACK/保活）
│   ├── mirror.py            抓屏 → 编码 → 推流（双线程流水线）
│   ├── panel.py             462×462 半透明卡片渲染
│   ├── stats_provider.py    CPU/内存/显卡/定位/天气采集
│   ├── extended_display.py  守护入口：确保虚拟屏 + 启动镜像
│   └── paths.py             路径解析（仓库布局 / 安装布局）
├── vdd/                     Amyuni usbmmidd 虚拟显示器驱动（见授权）
├── vendor/lhm/              LibreHardwareMonitorLib（读 CPU 温度）
├── setup.ps1 / uninstall.ps1
├── requirements.txt
├── config.example.json
└── README.md
```

---

## 致谢与授权

- USB 协议逆向并交叉验证自开源项目 [Lexonight1/thermalright-trcc-linux](https://github.com/Lexonight1/thermalright-trcc-linux)
- 虚拟显示器：[Amyuni usbmmidd](https://www.amyuni.com/forum/viewtopic.php?t=3069)（保留其 `vdd/License.txt`）
- 传感器库：[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)（MPL-2.0）
- 天气：[open-meteo.com](https://open-meteo.com/)；逆地理：[BigDataCloud](https://www.bigdatacloud.com/)、[OpenStreetMap Nominatim](https://nominatim.openstreetmap.org/)

本仓库自有代码以 MIT 授权，详见 `LICENSE`；第三方组件的授权见 `THIRD_PARTY_NOTICES.md`。
