# -*- coding: utf-8 -*-
"""
AI额度管家 · 挂机守护 (V3.6)
===============================
长期挂机运行: 每 15 秒巡检一次, MySQL / 本地代理 / 网页服务 哪个挂了自动拉起哪个。

用法:
  直接运行:  python 挂机守护.py         (前台, 窗口里能看到日志)
  配合开机自启: 运行「设置开机自启.bat」 (开机后后台静默运行, 不弹窗)

零依赖, 仅 Python 标准库。
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PROXY_DIR = BASE / "本地代理"
APP_DIR = BASE / "app"
LOG_FILE = BASE / "挂机日志.txt"

# 外部依赖的可执行文件路径: 按需改成你机器上的实际路径, 留空则跳过该项巡检
MYSQL_EXE = r""      # 如 r"D:\mysql\bin\mysqld.exe"
NODE_EXE = r""       # 如 r"C:\Program Files\nodejs\node.exe"
NPM_CLI = r""        # 如 r"C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js"

# Windows 下让子进程脱离本进程独立存活(守护退出/被重启后服务仍在)
DETACH = 0x00000008 if sys.platform == "win32" else 0
NUL = subprocess.DEVNULL


def _log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _spawn(cmd, cwd):
    kw = dict(cwd=str(cwd), stdout=NUL, stderr=NUL, stdin=NUL,
              close_fds=True)
    if DETACH:
        kw["creationflags"] = DETACH
    return subprocess.Popen(cmd, **kw)


def start_mysql():
    if not MYSQL_EXE:          # 未配置 MySQL 路径则跳过巡检
        return False
    if _port_open("127.0.0.1", 3306):
        return False
    _log("MySQL 未运行, 正在拉起...")
    _spawn([MYSQL_EXE], Path(MYSQL_EXE).parent)
    return True


def start_proxy():
    if _port_open("127.0.0.1", 8787):
        return False
    _log("本地代理未运行, 正在拉起...")
    _spawn([sys.executable, "额度管家_本地代理.py"], PROXY_DIR)
    return True


def start_app():
    # 网页服务端口不固定(3000 被占则 vite 自动换 3001+), 探测 3000-3003
    for p in (3000, 3001, 3002, 3003):
        if _port_open("127.0.0.1", p):
            return False
    _log("网页服务未运行, 正在拉起...")
    if Path(NODE_EXE).exists() and Path(NPM_CLI).exists():
        cmd = [NODE_EXE, NPM_CLI, "run", "dev"]
    else:
        cmd = ["npm", "run", "dev"]
    _spawn(cmd, APP_DIR)
    return True


def main():
    _log("挂机守护启动, 每 15 秒巡检 MySQL(3306) / 代理(8787) / 网页(3000)")
    while True:
        try:
            start_mysql()
            time.sleep(2)  # MySQL 起来后稍等再起依赖它的网页
            start_proxy()
            start_app()
        except Exception as e:
            _log(f"巡检异常: {e}")
        time.sleep(15)


if __name__ == "__main__":
    main()
