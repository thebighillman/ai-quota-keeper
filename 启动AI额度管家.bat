@echo off
chcp 936 >nul
cd /d "%~dp0"

rem ============================================================
rem  AI额度管家 启动器 (V3.33.4)
rem  编码: GBK + CRLF (配合 chcp 936, 老系统也不会乱码)
rem  优先 pythonw 拉起桌面壳: 独立窗口 + 右下角托盘 + 无黑框
rem ============================================================

set "PYW=%~dp0python\pythonw.exe"
set "PY=%~dp0python\python.exe"

rem ---- 1) 内置便携 Python: pythonw 无控制台启动桌面壳
if not exist "%PYW%" goto trysys
for %%F in ("%~dp0*.pyw") do (
    start "" "%PYW%" "%%~fF"
    goto done
)

:trysys
rem ---- 2) 系统 Python: 用 pythonw 启动(仍不产生黑框)
where pythonw >nul 2>&1
if errorlevel 1 goto fallback
for %%F in ("%~dp0*.pyw") do (
    start "" pythonw "%%~fF"
    goto done
)

:fallback
rem ---- 3) 兜底: 传统浏览器模式(仅当没有桌面壳组件时才走到这)
if not exist "%PY%" goto nosys
echo 未找到桌面壳组件, 使用浏览器模式启动...
"%PY%" "%~dp0启动额度管家.py"
echo.
echo 已停止。
pause
exit /b 0

:nosys
echo.
echo  [错误] 没找到可用的运行环境。
echo  请确认本目录下有 python\pythonw.exe, 或已安装 Python 3.8+。
echo.
pause
exit /b 1

:done
exit /b 0
