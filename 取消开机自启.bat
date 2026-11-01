@echo off
chcp 65001 >nul
title 取消开机自启
echo 正在取消开机自启...
powershell -NoProfile -Command "Remove-Item ([Environment]::GetFolderPath('Startup')+'\AI额度管家挂机.lnk') -ErrorAction SilentlyContinue"
echo.
echo 已取消开机自启(下次开机不再自动运行)
echo.
echo 提示: 当前已在后台运行的服务不受影响, 可双击「停止AI额度管家.bat」手动停掉。
echo.
pause
