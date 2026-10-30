# uninstall.ps1 - 卸载：停服务、删任务、恢复 TRCC 自启、可选移除虚拟显示器
param(
    [string]$TaskName = 'TrofeoLCDMirror',
    [switch]$RemoveVirtualDisplay,
    [switch]$KeepConfig
)

$ErrorActionPreference = 'SilentlyContinue'
$repo = $PSScriptRoot

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "需要管理员权限。" -ForegroundColor Red
    exit 1
}

Write-Host "停止并删除计划任务 $TaskName ..."
Stop-ScheduledTask -TaskName $TaskName
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Host "结束守护进程 ..."
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -like 'pythonw*' -and $_.ExecutablePath -like "$repo*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

$trcc = Get-ScheduledTask -TaskName 'TRCCAppStartup' -ErrorAction SilentlyContinue
if ($trcc) {
    Enable-ScheduledTask -TaskName 'TRCCAppStartup' | Out-Null
    Write-Host "已恢复 TRCC 自启任务" -ForegroundColor Yellow
}

if ($RemoveVirtualDisplay) {
    $di = Join-Path $repo 'vdd\deviceinstaller64.exe'
    if (Test-Path $di) {
        Push-Location (Join-Path $repo 'vdd')
        Write-Host "移除虚拟显示器 ..."
        & $di enableidd 0
        & $di stop usbmmidd
        & $di remove usbmmidd
        Pop-Location
        Write-Host "虚拟显示器已移除" -ForegroundColor Yellow
    }
}

if (-not $KeepConfig) {
    Write-Host "保留 panel_config.json 与 .venv（如不需要可手动删除）"
}

Write-Host "`n卸载完成。需要回到 TRCC 请手动启动 C:\Program Files\TRCCCAP\TRCC.exe" -ForegroundColor Green
