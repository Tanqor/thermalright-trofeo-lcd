# 第三方组件声明

本仓库包含以下第三方组件。它们各自遵循其原始授权，**不是** MIT。

---

## 1. Amyuni usbmmidd（虚拟显示器驱动）— `vdd/`

Copyright 2014-2021 Amyuni Technologies Inc.
https://www.amyuni.com

授权原文见 `vdd/License.txt`，要点：

> Permission is granted to anyone to use this software for any purpose subject to
> the following restrictions:
> 1. The origin of this software must not be misrepresented; you must not claim
>    that you wrote the original software. If you use this software in a product,
>    an acknowledgment in the product documentation would be required.
> 2. Altered versions must be plainly marked as such, and must not be
>    misrepresented as being the original software.
> 3. This notice may not be removed or altered from any distribution.

**本仓库未修改 usbmmidd 的任何文件**，仅原样再分发，并在此致谢 Amyuni Technologies。
用户使用该软件需接受其附带广告页的条款；如需无广告请购买其商业版本。

---

## 2. LibreHardwareMonitor（传感器库）— `vendor/lhm/`

`LibreHardwareMonitorLib.dll`、`HidSharp.dll`
来源：https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
授权：**Mozilla Public License 2.0 (MPL-2.0)**

本仓库未修改这些二进制文件，仅原样再分发。MPL-2.0 全文见
https://www.mozilla.org/en-US/MPL/2.0/

对应的源代码可从上述 GitHub 仓库获取。

---

## 3. Python 依赖

通过 pip 安装，遵循各自授权：`pyusb` (BSD)、`mss` (MIT)、`psutil` (BSD)、
`pythonnet` (MIT)、`Pillow` (HPND)。

---

## 4. 参考实现

USB 协议逆向参考并交叉验证自：
https://github.com/Lexonight1/thermalright-trcc-linux （其作者对协议的整理公开）

本仓库的 Python 实现为独立编写。
