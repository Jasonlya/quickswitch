@echo off
rem QuickSwitch Windows 端（server）启动脚本
rem 首次运行/排错建议用本脚本：会保留控制台日志，便于看到 DDC 与连接信息。
rem 全局热键需要管理员权限：请右键「以管理员身份运行」。
setlocal
cd /d "%~dp0.."

net session >nul 2>&1
if errorlevel 1 (
    echo [警告] 当前不是管理员权限，全局热键可能无法注册。
    echo        请关闭本窗口，改为右键「以管理员身份运行」。
    echo.
)

echo 工作目录：%CD%
python main.py %*
echo.
echo 进程已退出（退出码 %ERRORLEVEL%）
pause
endlocal
