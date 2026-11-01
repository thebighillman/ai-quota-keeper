# -*- coding: utf-8 -*-
"""桌面壳（V3.27.1）：pywebview 独立窗口 + pystray 托盘，替代浏览器/Edge --app 的默认形态。

启动链路：
    双击「AI额度管家」桌面图标 → launch.bat → 桌面壳.pyw（本文件）
      ① 单实例(命名互斥量)：已在跑则把已有窗口前置，本进程直接退出
      ② 线程内拉起 HTTP 服务（复用 启动额度管家.py 的 Handler，不开浏览器）
      ③ pywebview 加载 http://127.0.0.1:PORT（WebView2 渲染，独立窗口）
      ④ 任务栏图标：WM_SETICON 加载程序目录 AI额度管家.ico（32/16 双尺寸）
      ⑤ pystray 托盘（打开主界面 / 退出），关窗最小化时气泡提示一次
    窗口关闭 = 最小化到托盘（服务继续跑，手机/远程还能访问）
    托盘「退出」= 真正停止服务并退壳

依赖：pywebview + pystray + pillow（内测包已内置；源码运行需 pip install pywebview pystray pillow）。
找不到依赖时回退：打印提示并调用传统启动（Edge app 窗口/浏览器），不会崩。
"""
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CORE_PY = BASE_DIR / "启动额度管家.py"
ICON_ICO = BASE_DIR / "AI额度管家.ico"
WINDOW_TITLE = "AI额度管家"

ERROR_ALREADY_EXISTS = 183
WM_SETICON = 0x80
ICON_BIG, ICON_SMALL = 1, 0
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
SW_RESTORE, SW_SHOW = 9, 5


# ---------------------------------------------------------------- 日志
def _log(msg: str):
    """写壳日志（数据目录/运行日志.log）—— pythonw 下 stderr 被吞, 出事只能靠日志。"""
    try:
        from datetime import datetime
        # 数据目录优先(与主程序同口径), 兜底程序目录
        cand = [BASE_DIR / "data", BASE_DIR]
        for d in cand:
            if d.is_dir():
                p = d / "运行日志.log"
                with open(p, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [壳] {msg}\n")
                return
    except Exception:
        pass


# ---------------------------------------------------------------- 核心服务加载
def _load_core():
    """以模块方式加载主程序（import 不会启动服务，main 只在 __main__ 时跑）。"""
    spec = importlib.util.spec_from_file_location("qg_core", str(CORE_PY))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _server_running(port: int) -> bool:
    """探活：端口已有服务在跑（含 /api/version 应答）。"""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _start_server_thread(core):
    """线程内起 HTTP 服务（复用主程序 Handler；不开浏览器、不打控制台横幅）。"""
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer((core.HOST, core.PORT), core.Handler)
    try:
        core._start_daemons()
    except Exception:
        pass
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ---------------------------------------------------------------- Win32: 单实例与窗口图标
def _acquire_single_instance():
    """命名互斥量保证全局只有一个壳。返回 (是否首个实例, 句柄)。

    重试窗口: os.execv 自重启(更新主程序后)时, 新进程启动瞬间旧进程可能还没退、
    互斥量还在 → 误判"已有实例"直接退出会导致更新后软件静默消失。
    撞互斥量时先重试 ~3.2 秒, 期间旧进程退出就接管为主实例。"""
    if not sys.platform.startswith("win"):
        return True, None                       # 非 Windows 无此机制, 不阻止
    import ctypes
    name = "Local\\AIQuotaGuardian_Shell_SingleInstance"
    kernel32 = ctypes.windll.kernel32
    deadline = time.time() + 3.2
    handle = kernel32.CreateMutexW(None, False, name)
    while handle and kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        if time.time() >= deadline:
            return False, None                  # 确有常驻实例(非重启窗口期)
        time.sleep(0.4)                         # 等旧进程退出(execv 窗口期)
        handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True, None                       # 创建失败不阻止启动
    return True, handle


def _focus_existing_window():
    """把已在跑的壳窗口前置（隐藏状态也拉出来）。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def _apply_window_icon():
    """后台线程轮询找窗口句柄, WM_SETICON 挂上 ico(任务栏+标题栏图标)。
    pywebview(winforms) 不支持直接设 icon, 走 Win32。失败不影响功能。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        ico = str(ICON_ICO)
        if not ICON_ICO.is_file():
            return
        for _ in range(60):                     # 最多等 12 秒等窗口创建
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
            if hwnd:
                hbig = user32.LoadImageW(None, ico, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
                hsmall = user32.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
                if hbig:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hbig)
                if hsmall:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hsmall)
                return
            time.sleep(0.2)
    except Exception:
        pass


# ---------------------------------------------------------------- 托盘图标
def _tray_image():
    """托盘图标：优先用程序目录 AI额度管家.ico，找不到画个纯色块兜底。"""
    from PIL import Image
    if ICON_ICO.is_file():
        try:
            img = Image.open(str(ICON_ICO))
            img = img.convert("RGBA").resize((64, 64), Image.LANCZOS)
            return img
        except Exception:
            pass
    return Image.new("RGBA", (64, 64), (18, 35, 58, 255))     # #12233A 主题色


# ---------------------------------------------------------------- 主流程
def _set_app_id():
    """V3.33.1: 给本进程设置 AppUserModelID。
    不设的话, 任务栏会把 pythonw.exe 归到 Python 名下、显示 Python 图标;
    设了之后任务栏按本软件独立显示(图标由窗口 WM_SETICON 提供)。"""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "AIQuotaGuardian.Desktop.1")
    except Exception as e:
        try:
            _log(f"SetAppUserModelID 失败: {e!r}")
        except Exception:
            pass


def main():
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

    _set_app_id()          # 任务栏不再显示 Python 图标

    # ---- 单实例: 已在跑 → 前置已有窗口, 本进程退出
    first, mutex = _acquire_single_instance()
    if not first:
        _focus_existing_window()
        return                                  # 重复双击只弹已有窗口

    try:
        import webview
        import pystray
    except ImportError as e:
        sys.stderr.write(f"桌面壳依赖缺失({e})，回退浏览器模式\n")
        os.system(f'"{sys.executable}" "{CORE_PY}"')
        return

    core = _load_core()
    url = f"http://{core.HOST}:{core.PORT}/"

    server = None
    if not _server_running(core.PORT):
        server = _start_server_thread(core)
        for _ in range(30):
            if _server_running(core.PORT):
                break
            time.sleep(0.1)

    state = {"win": None, "quitting": False, "ballooned": False}

    def show_window():
        w = state["win"]
        if w is not None:
            try:
                w.restore()
                w.show()
            except Exception:
                pass

    def quit_app(_icon=None, _item=None):
        if state["quitting"]:
            return
        state["quitting"] = True
        try:
            if server is not None:
                server.shutdown()
        except Exception:
            pass
        try:
            if state["win"] is not None:
                state["win"].destroy()
        except Exception:
            pass
        try:
            icon.stop()
        except Exception:
            pass

        def _force():
            time.sleep(1.5)
            os._exit(0)                         # 兜底: 托盘 COM 线程偶发不退
        threading.Thread(target=_force, daemon=True).start()

    # ---- 托盘(V3.32.2 重做: 双击=打开主界面; 右键=打开/退出)
    #  ⚠️ 关键修复: pystray 在 Windows 上**必须在主线程 run()**, 子线程要用 run_detached()。
    #     原来写 threading.Thread(target=icon.run) 会在子线程里被 Win32 后端拒绝,
    #     异常又在 daemon 线程里被静默吞掉 —— 表现就是「托盘没图标、退不出去」。
    #     现在: run_detached() 挂到 pywebview 的主循环上, 并记录失败原因到日志。
    menu = pystray.Menu(
        pystray.MenuItem("打开主界面", lambda: show_window(), default=True),
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon("AIQuotaGuardian", _tray_image(), WINDOW_TITLE, menu)
    tray_ok = False
    try:
        icon.run_detached()
        icon.visible = True
        tray_ok = True
        _log("托盘图标已创建(run_detached)")
    except Exception as e:
        _log(f"托盘创建失败(run_detached): {e!r}")
        try:
            icon.run()          # 兜底: 直接跑(会阻塞, 放最后)
            tray_ok = True
        except Exception as e2:
            _log(f"托盘创建彻底失败: {e2!r} —— 关闭窗口将直接退出, 请用「关于系统 → 退出软件」")
    # 把托盘状态落盘, 供外部(和用户)核对: data/tray_state.json
    try:
        import json as _json
        for _d in (BASE_DIR / "data", BASE_DIR):
            if _d.is_dir():
                (_d / "tray_state.json").write_text(_json.dumps({
                    "tray_ok": tray_ok,
                    "icon_visible": bool(getattr(icon, "visible", False)),
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                }, ensure_ascii=False, indent=1), encoding="utf-8")
                break
    except Exception:
        pass

    # ---- 窗口
    win = webview.create_window(
        WINDOW_TITLE,
        url,
        width=1280, height=840,
        min_size=(960, 640),
        text_select=True,
    )
    state["win"] = win

    # 窗口创建后挂任务栏图标(后台线程轮询句柄)
    threading.Thread(target=_apply_window_icon, daemon=True).start()

    def on_closing():
        if state["quitting"]:
            return True
        # V3.32.2: 托盘不可用时关闭窗口 = 直接退出, 避免「窗口关了、软件还在跑、又没图标可点」
        if not tray_ok:
            _log("关闭窗口: 托盘不可用, 直接退出")
            state["quitting"] = True
            try:
                if server is not None:
                    server.shutdown()
            except Exception:
                pass
            threading.Thread(target=lambda: (time.sleep(0.6), os._exit(0)), daemon=True).start()
            return True
        try:
            win.hide()
        except Exception:
            pass
        # 首次最小化到托盘给气泡提示(避免用户以为软件没了)
        if not state["ballooned"]:
            state["ballooned"] = True
            try:
                icon.notify("软件已最小化到右下角托盘。双击图标重新打开，右键可退出。", WINDOW_TITLE)
            except Exception:
                pass
        return False                            # 阻止默认关闭(隐藏到托盘)

    win.events.closing += on_closing

    webview.start(gui="edgechromium")           # WebView2(Chromium)

    if not state["quitting"]:
        try:
            if server is not None:
                server.shutdown()
        except Exception:
            pass
        os._exit(0)                             # 确保进程不挂


if __name__ == "__main__":
    main()
