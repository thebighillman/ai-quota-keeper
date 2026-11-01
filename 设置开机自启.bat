@echo off
chcp 65001 >nul
title 设置开机自启
rem 优先用程序目录内置的便携 Python, 找不到再退回系统 PATH 里的 pythonw
set "PYW=%~dp0python\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"

echo 正在设置开机自启...
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut([Environment]::GetFolderPath('Startup')+'\AI额度管家挂机.lnk'); $s.TargetPath='%PYW%'; $s.Arguments='\"%~dp0挂机守护.py\"'; $s.WorkingDirectory='%~dp0'; $s.Description='AI额度管家 挂机守护'; $s.Save()"

echo.
echo 已设置开机自启! 以后每次开机, 会自动在后台静默拉起:
echo   MySQL + 本地代理 + 网页服务 (不弹窗口, 崩溃自动重启)
echo.
echo 取消自启: 运行「取消开机自启.bat」
echo.
pause
