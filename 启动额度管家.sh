#!/usr/bin/env bash
# AI额度管家 Linux 启动脚本
# 用法：双击（桌面环境支持时）或终端执行 ./启动额度管家.sh
#   前台运行（默认）：./启动额度管家.sh           —— 关闭终端即停止
#   后台无窗口运行：  ./启动额度管家.sh --daemon  —— 日志写到 数据目录/运行日志.log
set -u
cd "$(dirname "$0")" || exit 1

APP="启动额度管家.py"
if [ ! -f "$APP" ]; then
  echo "错误：未找到 $APP，请在本程序目录内运行本脚本。" >&2
  exit 1
fi

# 探测 python3（优先 python3，回退 python）
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    # 确认是 Python 3
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' 2>/dev/null; then
      PY="$c"
      break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "错误：未找到 Python 3。请先安装（如 sudo apt install python3）。" >&2
  exit 1
fi

# 端口可用环境变量覆盖：QUOTA_PORT=8789 ./启动额度管家.sh
if [ "${1:-}" = "--daemon" ]; then
  LOG="${QUOTA_DATA_HOME:-$HOME/.local/share/aiquota}/运行日志.log"
  mkdir -p "$(dirname "$LOG")"
  # 已启动则不重复起（探活）
  PORT="${QUOTA_PORT:-8788}"
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/version" >/dev/null 2>&1; then
    echo "已在运行（端口 $PORT），无需重复启动。"
    exit 0
  fi
  nohup "$PY" "$APP" >>"$LOG" 2>&1 &
  echo "已后台启动（PID $!），日志：$LOG"
  echo "停止：pkill -f '$APP'"
else
  exec "$PY" "$APP"
fi
