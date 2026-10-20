# 注册 Windows 任务计划：登录时自动启动 QuickSwitch，并以最高权限运行
# （keyboard 库注册全局热键需要管理员权限，因此 RunLevel Highest 是必须的）
#
# 用法（管理员 PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -ProjectDir C:\Tools\quickswitch
# 卸载：
#   Unregister-ScheduledTask -TaskName QuickSwitch -Confirm:$false

param(
    [string]$ProjectDir = "C:\Tools\quickswitch",
    [string]$TaskName = "QuickSwitch",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

if (-not $Python) {
    $candidate = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $candidate) {
        $candidate = Get-Command python.exe -ErrorAction SilentlyContinue
    }
    if (-not $candidate) {
        throw "未找到 python.exe / pythonw.exe，请安装 Python 3.10+ 或手动指定 -Python 路径"
    }
    $Python = $candidate.Source
}

$main = Join-Path $ProjectDir "main.py"
if (-not (Test-Path $main)) {
    throw "找不到 $main，请用 -ProjectDir 指定 QuickSwitch 所在目录"
}

$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$main`"" -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "已注册任务 $TaskName"
Write-Host "  程序：$Python"
Write-Host "  参数：$main"
Write-Host "  目录：$ProjectDir"
Write-Host ""
Write-Host "立即启动：Start-ScheduledTask -TaskName $TaskName"
Write-Host "查看状态：Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
