# -*- coding: utf-8 -*-
"""平台适配层(V3.26.0)：所有平台差异收敛到这里，核心层禁止散落 if sys.platform。

统一接口(核心层只调这些)：
    data_home()        用户数据目录
    open_path(p)       用系统文件管理器打开目录/文件
    autostart_get() / autostart_set(on)    开机自启(HKCU Run / autostart .desktop)
    keychain_enabled() 平台密钥加密是否可用(Win=DPAPI / Linux=False)
    tray_supported()   托盘是否支持

Windows 行为与 V3.22.0 起完全一致(零变化)；Linux 新增 XDG 路径；Mac 预留未测。
"""
import os
import subprocess
import sys
from pathlib import Path

APP_DIR_NAME = "AI额度管家"


def is_windows() -> bool:
    # sys.platform 优先(可被测试 mock)；回退 os.name 兜底
    return sys.platform.startswith("win") if sys.platform != "" else os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


def platform_name() -> str:
    return "windows" if is_windows() else ("macos" if is_macos() else "linux")


# ---------------------------------------------------------------- 数据目录
def _program_data_home():
    """程序目录\\data —— V3.28.0 起的默认数据位置(跟随解压目录, 用户可见可备份)。

    注意: 本文件在 <程序目录>\\plat\\__init__.py, 程序根 = parent.parent。
    """
    return Path(__file__).resolve().parent.parent / "data"


def _dir_writable(p) -> bool:
    """目录是否可写(探测: 建目录 + 写删临时文件)。"""
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def data_home() -> Path:
    """用户数据目录：QUOTA_DATA_HOME 覆盖 > 平台默认。

    Win   程序目录\\data                    (V3.28.0: 跟随解压目录, 用户可见可整夹备份;
                                          Program Files 等不可写位置自动回退 %LOCALAPPDATA%\\AI额度管家)
    Mac   ~/Library/Application Support/AI额度管家
    Linux ~/.local/share/aiquota            (XDG 标准, 小写避免大小写敏感文件系统问题)
    """
    env = str(os.environ.get("QUOTA_DATA_HOME") or "").strip()
    if env:
        return Path(env)
    if is_windows():
        local = _program_data_home()
        # 缓存探测结果(模块级), 避免每次调用都写探针文件
        key = "_prog_data_ok"
        ok = globals().get(key)
        if ok is None:
            ok = _dir_writable(local)
            globals()[key] = ok
        if ok:
            return local
        base = str(os.environ.get("LOCALAPPDATA") or "").strip()
        root = Path(base) if base else (Path.home() / "AppData" / "Local")
        return root / APP_DIR_NAME
    if is_macos():
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    xdg = str(os.environ.get("XDG_DATA_HOME") or "").strip()
    root = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return root / "aiquota"


def legacy_windows_home() -> Path:
    """V3.22.0~V3.27.x 的旧数据目录(%LOCALAPPDATA%\\AI额度管家), 反向迁移时用。"""
    base = str(os.environ.get("LOCALAPPDATA") or "").strip()
    root = Path(base) if base else (Path.home() / "AppData" / "Local")
    return root / APP_DIR_NAME


def open_path(p) -> bool:
    """系统文件管理器打开；成功返回 True。"""
    p = str(p)
    try:
        if is_windows():
            os.startfile(p)                       # noqa: S606 Windows 专用
            return True
        if is_macos():
            subprocess.run(["open", p], check=False, timeout=10)
            return True
        subprocess.run(["xdg-open", p], check=False, timeout=10)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 独立窗口(类原生应用观感)
_EDGE_CANDIDATES = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
)
_CHROME_CANDIDATES = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
)


def _find_browser_exe(candidates) -> str:
    for c in candidates:
        p = os.path.expandvars(c)
        if os.path.isfile(p):
            return p
    return ""


def app_window_supported() -> bool:
    """当前平台能否用独立 app 窗口(无浏览器边框)打开页面。"""
    if is_windows():
        return bool(_find_browser_exe(_EDGE_CANDIDATES) or _find_browser_exe(_CHROME_CANDIDATES))
    if is_macos():
        return False                            # Mac 挂起, 不启用
    # Linux: 有 chrome/chromium 即可 --app
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if os.path.isfile(os.path.join(d, name)):
                return True
    return False


def open_app_window(url: str) -> bool:
    """以独立 app 窗口打开 url(Edge/Chrome --app 模式, 无地址栏和标签页, 类似原生软件)。

    找不到可用浏览器时返回 False, 调用方回退 webbrowser.open。
    """
    try:
        if is_windows():
            exe = _find_browser_exe(_EDGE_CANDIDATES)
            if not exe:
                exe = _find_browser_exe(_CHROME_CANDIDATES)
            if not exe:
                return False
            subprocess.Popen([exe, f"--app={url}"],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                                          | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            return True
        if is_macos():
            return False                        # Mac 挂起
        for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            for d in os.environ.get("PATH", "").split(os.pathsep):
                exe = os.path.join(d, name)
                if os.path.isfile(exe):
                    subprocess.Popen([exe, f"--app={url}"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------- 开机自启
_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = "AIQuotaGuardian"
_LINUX_DESKTOP = ("[Desktop Entry]\n"
                  "Type=Application\n"
                  "Name=AI额度管家\n"
                  "Exec={cmd}\n"
                  "Terminal=false\n"
                  "X-GNOME-Autostart-enabled=true\n")


def _win_run_value() -> str:
    """自启命令：优先程序目录内置 pythonw，回退系统 pythonw。"""
    here = Path(__file__).resolve().parent
    pyw = here / "python" / "pythonw.exe"
    entry = here / "桌面壳.pyw" if (here / "桌面壳.pyw").is_file() else here / "启动额度管家.py"
    if pyw.is_file():
        return f'"{pyw}" "{entry}"'
    return f'pythonw "{entry}"'


def autostart_get() -> bool:
    try:
        if is_windows():
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as k:
                winreg.QueryValueEx(k, _AUTOSTART_NAME)
                return True
        if is_macos():
            return (Path.home() / "Library" / "LaunchAgents" / "com.aiquota.guardian.plist").is_file()
        return (Path.home() / ".config" / "autostart" / "aiquota.desktop").is_file()
    except OSError:
        return False
    except FileNotFoundError:
        return False


def autostart_set(on: bool) -> bool:
    """开/关开机自启(只动当前用户, 不需要管理员)。返回是否成功。"""
    try:
        if is_windows():
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY,
                                0, winreg.KEY_SET_VALUE) as k:
                if on:
                    winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ, _win_run_value())
                else:
                    try:
                        winreg.DeleteValue(k, _AUTOSTART_NAME)
                    except FileNotFoundError:
                        pass
            return True
        if is_macos():
            # 预留：LaunchAgents plist(未测, Mac 挂起期不启用)
            return False
        d = Path.home() / ".config" / "autostart"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "aiquota.desktop"
        if on:
            f.write_text(_LINUX_DESKTOP.format(cmd=_win_run_value().replace("pythonw", "python3")),
                         encoding="utf-8")
        elif f.exists():
            f.unlink()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 能力门控
def keychain_enabled() -> bool:
    """平台级密钥加密：Windows=DPAPI(ctypes 可用)；Linux/Mac 本期不做(Linux 用文件权限 0600 兜底)。"""
    return is_windows()


def tray_supported() -> bool:
    if is_windows():
        return True
    if is_macos():
        return True
    # Linux：主流桌面(GNOME/KDE/Cinnamon)有托盘；无 X 的服务器没有
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def file_secret_guard(path) -> None:
    """敏感文件权限收紧：Linux/macOS 0600；Windows 目录已在用户 Profile 下(A CL 自带)。"""
    try:
        if not is_windows():
            os.chmod(str(path), 0o600)
    except OSError:
        pass
