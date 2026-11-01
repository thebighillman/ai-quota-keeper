@echo off
chcp 65001 >nul
title AI额度管家 · 一键停止
echo 正在停止 AI额度管家 相关服务...

rem 停止 本地代理(8787) 和 网页服务(3000/3001)
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8787,3000,3001 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"

rem 停止 MySQL
taskkill /f /im mysqld.exe >nul 2>&1

echo.
echo 已全部停止(MySQL / 本地代理 / 网页服务)
timeout /t 3 >nul
