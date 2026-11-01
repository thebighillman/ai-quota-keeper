#!/usr/bin/env bash
# 安装 Linux 桌面快捷方式（开始菜单/桌面图标）
# 用法：./安装桌面快捷方式.sh
# 结果：~/.local/share/applications/aiquota.desktop + 桌面副本（桌面环境允许时）
set -u
cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"

if [ ! -f "启动额度管家.sh" ]; then
  echo "错误：未找到 启动额度管家.sh" >&2
  exit 1
fi
chmod +x "启动额度管家.sh" 2>/dev/null || true

ICON=""
for f in "AI额度管家.ico" "favicon.ico" "app/dist/public/favicon.ico"; do
  if [ -f "$f" ]; then ICON="$DIR/$f"; break; fi
done

DESKTOP_FILE="$HOME/.local/share/applications/aiquota.desktop"
mkdir -p "$(dirname "$DESKTOP_FILE")"
{
  echo "[Desktop Entry]"
  echo "Type=Application"
  echo "Name=AI额度管家"
  echo "Comment=AI 额度与订阅成本管家"
  echo "Exec=bash \"$DIR/启动额度管家.sh\""
  echo "Terminal=false"
  echo "Categories=Utility;"
  [ -n "$ICON" ] && echo "Icon=$ICON"
} > "$DESKTOP_FILE"
echo "已写入：$DESKTOP_FILE"

# 桌面副本（部分桌面环境需要信任后才显示）
if [ -d "$HOME/Desktop" ]; then
  cp "$DESKTOP_FILE" "$HOME/Desktop/aiquota.desktop"
  chmod +x "$HOME/Desktop/aiquota.desktop" 2>/dev/null || true
  echo "已复制到桌面：$HOME/Desktop/aiquota.desktop（如显示为普通文件，右键选择「允许启动」即可）"
fi

echo "完成。之后可在开始菜单搜索「AI额度管家」。"
