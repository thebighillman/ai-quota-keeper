# 创建「AI额度管家」桌面快捷方式(指定正确的图标, 避免默认显示 python 图标)
# 由 创建桌面快捷方式.bat 调用
$ErrorActionPreference = "Stop"

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $dir "启动AI额度管家.bat"
$icon = Join-Path $dir "AI额度管家.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "AI额度管家.lnk"

if (-not (Test-Path $target)) {
    Write-Host "  找不到启动脚本: $target"
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath = $target
$sc.WorkingDirectory = $dir
$sc.Description = "AI额度管家 - 盯着各个 AI 平台的余额和花费"
if (Test-Path $icon) {
    $sc.IconLocation = $icon
} else {
    Write-Host "  提示: 没找到图标文件 AI额度管家.ico, 将使用系统默认图标"
}
$sc.WindowStyle = 7   # 最小化启动, 不闪黑框
$sc.Save()

if (Test-Path $lnk) {
    Write-Host "  快捷方式已创建: $lnk"
    exit 0
} else {
    exit 1
}
