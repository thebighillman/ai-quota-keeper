# -*- coding: utf-8 -*-
"""AI额度管家 · 守护进程(掉线自动拉起)
================================================================
作用：
  - 每 30 秒检查一次 http://127.0.0.1:8788/api/health 是否活着
  - 不通 → 自动拉起「启动额度管家.py」，并把日志写入「守护日志.log」
  - 连着 24 小时稳定运行，视为正常，不再频繁重启

为什么要它：
  电脑休眠、杀毒清理、窗口被误关都会让服务静默退出，
  手机端就访问不到了。这个守护让它自己爬起来。

用法：
  - 手动跑一次：python 额度管家_守护.py
  - 开机自启：把下面这行加到「启动」文件夹的启动项里
      pythonw "F:/sites/AI额度管家/1-基础启动文件包/AI额度管家_一键版_V3.5/额度管家_守护.py"
  - 停止：任务管理器结束 pythonw.exe / python.exe 进程

不装系统服务、不需管理员权限、随时可删。
"""
import os
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# 关键: 本机可能配了系统代理(HTTP_PROXY), 走代理访问 8788 会拿到 502 假象。
# 守护必须直连本机, 否则会把"服务正常"误判成"掉线"(反复重启) 或反之。
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

BASE = Path(__file__).resolve().parent
APP = BASE / "启动额度管家.py"
LOG = BASE / "守护日志.log"
CHECK_URL = "http://127.0.0.1:8788/api/health"
INTERVAL = 30
# 拉起后给它的观察期(秒), 期间不算异常
STARTUP_GRACE = 40
# 连续失败多少次才拉起(避免刚启动时的短暂抖动)
FAIL_THRESHOLD = 2
LOG_MAX = 256 * 1024


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        if LOG.exists() and LOG.stat().st_size > LOG_MAX:
            # 截断前半部分, 防日志无限膨胀
            txt = LOG.read_text(encoding="utf-8", errors="ignore")
            LOG.write_text(txt[len(txt) // 2:], encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def alive() -> bool:
    try:
        # ProxyHandler({}) = 显式不用任何代理
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(CHECK_URL, timeout=6) as r:
            return r.status == 200
    except Exception:
        return False


def find_python() -> str:
    for p in (os.environ.get("PYEXE"), "python", "py -3"):
        if not p:
            continue
        try:
            exe = p.split()[0]
            if p == "py -3":
                subprocess.run(["py", "-3", "-c", "print(1)"], capture_output=True, timeout=8)
            else:
                subprocess.run([exe, "-c", "print(1)"], capture_output=True, timeout=8)
            return p
        except Exception:
            continue
    # 兜底: 同目录便携 Python
    bundled = str(Path(__file__).resolve().parent / "python" / "python.exe")
    if Path(bundled).exists():
        return bundled
    return "python"


def main():
    py = find_python()
    log(f"=== 守护启动(每 {INTERVAL} 秒巡检) ===")
    fails = 0
    proc = None
    while True:
        try:
            if alive():
                if fails:
                    log(f"已恢复 (连续失败 {fails} 次)")
                fails = 0
                proc = None
            else:
                fails += 1
                if fails >= FAIL_THRESHOLD:
                    log(f"服务无响应(连续 {fails} 次) → 正在拉起")
                    args = py.split() + [str(APP)]
                    try:
                        proc = subprocess.Popen(
                            args, cwd=str(BASE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        log(f"拉起成功 PID={proc.pid}")
                    except Exception as e:
                        log(f"拉起失败: {e}")
                    fails = 0
                    time.sleep(STARTUP_GRACE)
        except KeyboardInterrupt:
            log("=== 守护停止 ===")
            return
        except Exception as e:
            log(f"巡检异常: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
