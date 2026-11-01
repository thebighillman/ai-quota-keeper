@echo off
chcp 936 >nul
cd /d "%~dp0"
echo.
echo  正在创建桌面快捷方式...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools_shortcut.ps1"
if exist "%USERPROFILE%\Desktop\AI额度管家.lnk" (
  echo  完成！桌面上已经有「AI额度管家」图标了，双击就能用。
) else (
  echo  创建失败。请手动右键「启动AI额度管家.bat」→ 发送到 → 桌面快捷方式，
  echo  然后右键桌面图标 → 属性 → 更改图标 → 选择本目录的 AI额度管家.ico
)
echo.
pause
