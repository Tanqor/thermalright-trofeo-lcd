# setup.ps1 - 一键安装：把 Thermalright Trofeo Vision 9.16 机箱屏变成 Windows 扩展屏
#
# 用法（必须管理员）：
#   右键此文件 -> "使用 PowerShell 运行"
#   或： powershell -ExecutionPolicy Bypass -File setup.ps1
#
# 做的事：
#   1. 创建 .venv 并安装 Python 依赖
#   2. 安装 usbmmidd 虚拟显示器驱动，设成 1920x462 并启用
#   3. 生成 panel_config.json（若不存在）
#   4. 注册计划任务：登录时以最高权限启动守护进程
#   5. 若存在 TRCC 自启任务则禁用它（避免抢占 USB 设备）

param(
    [string]$TaskName = 'TrofeoLCDMirror',
    [switch]$NoTask,
    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
Set-Location $repo

function Fail($msg) {
    Write-Host "`n[错误] $msg" -ForegroundColor Red
    exit 1
}
function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# ---------- 0. 管理员 ----------
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "需要管理员权限。请右键 setup.ps1 -> 使用 PowerShell 运行，或在管理员终端里执行。"
}

Write-Host "Thermalright Trofeo Vision 9.16 -> Windows 扩展屏" -ForegroundColor Green
Write-Host "仓库目录: $repo"

# ---------- 1. Python ----------
Step "检查 Python"
$py = $null
$cands = @()
if ($PythonExe) { $cands += $PythonExe }
$cands += @('py', 'python', 'python3')
foreach ($c in $cands) {
    try {
        if ($c -eq 'py') {
            $v = & py -3 -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) { $py = 'py -3'; break }
        } else {
            $v = & $c -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) { $py = $c; break }
        }
    } catch { }
}
if (-not $py) {
    Fail "没找到可用的 Python。请先安装 Python 3.9+ (https://www.python.org/downloads/windows/)，安装时勾选 'Add python.exe to PATH'。"
}

$verStr = if ($py -eq 'py -3') { & py -3 -c "import sys;print('%d.%d'%sys.version_info[:2])" } else { & $py -c "import sys;print('%d.%d'%sys.version_info[:2])" }
$parts = $verStr.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 9)) {
    Fail "Python 版本过低 ($verStr)，需要 3.9 及以上。"
}
Write-Host "使用 Python $verStr ($py)"

# ---------- 2. 虚拟环境 ----------
$venv = Join-Path $repo '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
$venvPyW = Join-Path $venv 'Scripts\pythonw.exe'

if (-not (Test-Path $venvPy)) {
    Step "创建虚拟环境 .venv"
    if ($py -eq 'py -3') { & py -3 -m venv $venv } else { & $py -m venv $venv }
    if (-not (Test-Path $venvPy)) { Fail "虚拟环境创建失败" }
} else {
    Write-Host "虚拟环境已存在，跳过创建"
}

Step "安装 Python 依赖"
& $venvPy -m pip install --upgrade pip --quiet --timeout 60
$reqFile = Join-Path $repo 'requirements.txt'
& $venvPy -m pip install --timeout 60 -r $reqFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "默认 pip 源失败，改用清华镜像重试 ..." -ForegroundColor Yellow
    & $venvPy -m pip install --timeout 60 -i https://pypi.tuna.tsinghua.edu.cn/simple -r $reqFile
}
if ($LASTEXITCODE -ne 0) {
    Fail "依赖安装失败。可手动执行：`n  .venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt"
}

Step "自检依赖"
& $venvPy -c "import usb.core, mss, psutil, clr, PIL; print('依赖 OK')"
if ($LASTEXITCODE -ne 0) { Fail "依赖自检失败" }

# ---------- 3. 配置 ----------
$cfg = Join-Path $repo 'panel_config.json'
if (-not (Test-Path $cfg)) {
    Step "生成 panel_config.json"
    Copy-Item (Join-Path $repo 'config.example.json') $cfg
    Write-Host "已生成。城市留空 = 自动用系统定位。" -ForegroundColor Yellow
} else {
    Write-Host "panel_config.json 已存在，保留"
}

# ---------- 4. 虚拟显示器 ----------
Step "安装虚拟显示器驱动 (usbmmidd)"
$vdd = Join-Path $repo 'vdd'
$di  = Join-Path $vdd 'deviceinstaller64.exe'
if (-not (Test-Path $di)) { Fail "找不到 $di" }

Push-Location $vdd
try {
    & $di install usbmmIdd.inf usbmmidd 2>&1 | Out-String | Write-Host
} catch {
    Write-Host "驱动可能已安装过，继续。" -ForegroundColor Yellow
}
Pop-Location

Step "设置虚拟屏分辨率为 1920x462"
$key = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\usbmmIdd\Parameters\Monitors'
if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
foreach ($i in 0..9) { Remove-ItemProperty -Path $key -Name "$i" -ErrorAction SilentlyContinue }
Set-Item -Path $key -Value '1920,462'
Set-ItemProperty -Path $key -Name '0' -Value '1920,462'
Set-ItemProperty -Path $key -Name '1' -Value '1920,480'
Set-ItemProperty -Path $key -Name '2' -Value '1920,1080'

Add-Type -AssemblyName System.Windows.Forms
$hasTarget = $false
foreach ($s in [System.Windows.Forms.Screen]::AllScreens) {
    if ($s.Bounds.Width -eq 1920 -and $s.Bounds.Height -eq 462) { $hasTarget = $true }
}
if ($hasTarget) {
    Write-Host "已存在 1920x462 显示器，跳过 enableidd"
} else {
    Push-Location $vdd
    & $di enableidd 1 2>&1 | Out-String | Write-Host
    Pop-Location
    Start-Sleep -Seconds 5
    Add-Type -AssemblyName System.Windows.Forms
    $ok = $false
    foreach ($s in [System.Windows.Forms.Screen]::AllScreens) {
        if ($s.Bounds.Width -eq 1920 -and $s.Bounds.Height -eq 462) { $ok = $true }
    }
    if ($ok) { Write-Host "虚拟屏已启用 1920x462" -ForegroundColor Green }
    else { Write-Host "警告：虚拟屏没出现在 1920x462，稍后由守护进程再尝试设置一次。" -ForegroundColor Yellow }
}

# ---------- 5. 计划任务 ----------
if ($NoTask) {
    Write-Host "`n跳过计划任务（-NoTask）。手动运行：" -ForegroundColor Yellow
    Write-Host "  $venvPy `"$repo\src\extended_display.py`""
} else {
    Step "注册计划任务 $TaskName"
    $action  = New-ScheduledTaskAction -Execute $venvPyW -Argument "`"$(Join-Path $repo 'src\extended_display.py')`"" -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $princ   = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
    $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
    # 兜底：进程若被杀/崩溃，计划任务会在 1 分钟后自动拉起

    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $princ -Settings $set -Force `
        -Description "把虚拟扩展屏镜像到 Thermalright 机箱屏" | Out-Null

    $trcc = Get-ScheduledTask -TaskName 'TRCCAppStartup' -ErrorAction SilentlyContinue
    if ($trcc) {
        Disable-ScheduledTask -TaskName 'TRCCAppStartup' | Out-Null
        Write-Host "已禁用 TRCC 自启任务（避免抢占 USB 设备）" -ForegroundColor Yellow
    }

    Start-ScheduledTask -TaskName $TaskName
    Write-Host "计划任务已启动" -ForegroundColor Green
}

Write-Host "`n================ 完成 ================" -ForegroundColor Green
Write-Host "日志: $env:LOCALAPPDATA\TrofeoLCD\mirror.log"
Write-Host "把窗口拖到主屏右侧外面，就会出现在机箱屏上。"
Write-Host "改城市: 编辑 panel_config.json 后重启任务："
Write-Host "  Stop-ScheduledTask $TaskName; Start-ScheduledTask $TaskName"
Write-Host "卸载: powershell -ExecutionPolicy Bypass -File uninstall.ps1"
