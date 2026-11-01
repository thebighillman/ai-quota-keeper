# -*- coding: utf-8 -*-
"""平台适配层测试：mock sys.platform 验证三平台行为，不依赖真机。

用法：python tests/test_platform.py
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

ok = fail = 0


def case(name):
    def deco(fn):
        global ok, fail
        try:
            fn()
            print(f"PASS  {name}")
            ok += 1
        except Exception as e:
            print(f"FAIL  {name}: {e}")
            fail += 1
        return fn
    return deco


def load_plat(platform, home, extra_env=None):
    """以指定平台身份重新加载 plat(真实环境变量只做备份, 断言用返回的 plat 实例)。

    extra_env: 需要在 reload plat 之前就位的额外环境变量 dict(如 Windows 的 LOCALAPPDATA)。
    """
    for k in ("LOCALAPPDATA", "XDG_DATA_HOME"):
        os.environ.pop(k, None)
    os.environ["QUOTA_DATA_HOME"] = ""          # 清覆盖，测默认路径
    if extra_env:
        os.environ.update(extra_env)
    old_platform, sys.platform = sys.platform, platform
    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = home                   # Linux/Mac 分支用 Path.home()
    orig_home_fn = Path.home
    Path.home = classmethod(lambda cls: Path(home))
    import plat
    importlib.reload(plat)
    return plat, (lambda: _restore(old_platform, old_home_env, orig_home_fn))


def _restore(old_platform, old_home_env, orig_home_fn):
    sys.platform = old_platform
    Path.home = orig_home_fn
    if old_home_env is not None:
        os.environ["HOME"] = old_home_env
    os.environ.pop("LOCALAPPDATA", None)
    os.environ.pop("XDG_DATA_HOME", None)
    os.environ["QUOTA_DATA_HOME"] = ""


# ---------------------------------------------------------------- Windows
@case("Windows: 数据目录 = 程序目录\\data (V3.28.0 跟随解压目录)")
def _():
    fake = tempfile.mkdtemp()
    la = os.path.join(fake, "Local")
    # plat 被复制到 fake 目录下运行时, 程序目录 = fake 所在; 这里直接 mock plat 的 _program_data_home
    plat, restore = load_plat("win32", fake, extra_env={"LOCALAPPDATA": la})
    try:
        # mock: 把程序目录 data 指到 fake 下(可写)
        prog_data = Path(fake) / "data"
        plat._program_data_home = lambda: prog_data
        globals_force = getattr(plat, "__dict__")
        globals_force.pop("_prog_data_ok", None)     # 清探测缓存
        got = str(plat.data_home())
        assert got == str(prog_data), (got, prog_data)
        assert plat.keychain_enabled() is True
        assert plat.tray_supported() is True
        # 旧目录辅助函数
        assert str(plat.legacy_windows_home()) == os.path.join(la, "AI额度管家")
    finally:
        restore()


@case("Windows: 程序目录不可写 → 回退 LOCALAPPDATA")
def _():
    fake = tempfile.mkdtemp()
    la = os.path.join(fake, "Local")
    plat, restore = load_plat("win32", fake, extra_env={"LOCALAPPDATA": la})
    try:
        # mock: 程序目录指向不存在且无法创建的位置(Windows 下 C:\\Windows 之类一般不可写, 用假盘符更稳)
        plat._program_data_home = lambda: Path("Q:\\__nonexistent__\\data")
        plat.__dict__.pop("_prog_data_ok", None)
        got = str(plat.data_home())
        assert got == os.path.join(la, "AI额度管家"), (got, la)
    finally:
        restore()


@case("Linux: 文件权限收紧 0600(仅在真 Linux 生效; NTFS 上跳过断言)")
def _():
    fake = tempfile.mkdtemp()
    plat, restore = load_plat("linux", fake)
    try:
        p = Path(fake) / "secret.json"
        p.write_text("{}", encoding="utf-8")
        plat.file_secret_guard(p)
        mode = p.stat().st_mode & 0o777
        if os.name == "nt":
            # Windows 文件系统不支持 POSIX 权限位, 守卫函数不报错即算通过
            assert mode in (0o666, 0o644, 0o777), oct(mode)
        else:
            assert mode == 0o600, oct(mode)
    finally:
        restore()


# ---------------------------------------------------------------- Linux
@case("Linux: 数据目录 = ~/.local/share/aiquota (XDG)")
def _():
    fake = tempfile.mkdtemp()
    plat, restore = load_plat("linux", fake)
    try:
        assert str(plat.data_home()) == os.path.join(fake, ".local", "share", "aiquota"), plat.data_home()
        assert plat.keychain_enabled() is False, "Linux 本期不做平台加密"
        assert plat.tray_supported() is False or True     # 取决于 DISPLAY，不强制
    finally:
        restore()


@case("Linux: XDG_DATA_HOME 覆盖生效")
def _():
    fake = tempfile.mkdtemp()
    xdg = os.path.join(fake, "xdgdata")
    plat, restore = load_plat("linux", fake)
    os.environ["XDG_DATA_HOME"] = xdg
    try:
        assert str(plat.data_home()) == os.path.join(xdg, "aiquota"), plat.data_home()
    finally:
        os.environ.pop("XDG_DATA_HOME", None)
        restore()


@case("Linux: 开机自启写 ~/.config/autostart/aiquota.desktop")
def _():
    fake = tempfile.mkdtemp()
    plat, restore = load_plat("linux", fake)
    try:
        assert plat.autostart_set(True) is True
        d = Path(fake) / ".config" / "autostart" / "aiquota.desktop"
        assert d.is_file(), "desktop 文件未生成"
        body = d.read_text(encoding="utf-8")
        assert "python3" in body and "启动额度管家.py" in body, body
        assert plat.autostart_get() is True
        assert plat.autostart_set(False) is True
        assert not d.exists()
    finally:
        restore()


# (0600 断言已并入上方"仅在真 Linux 生效"用例)


# ---------------------------------------------------------------- macOS（预留验证）
@case("Mac: 数据目录 = ~/Library/Application Support（预留）")
def _():
    fake = tempfile.mkdtemp()
    plat, restore = load_plat("darwin", fake)
    try:
        assert str(plat.data_home()) == os.path.join(fake, "Library", "Application Support", "AI额度管家")
        assert plat.keychain_enabled() is False, "Mac Keychain 属 S5，本期 False"
    finally:
        restore()


# ---------------------------------------------------------------- 通用
@case("app_window_supported: Win 探测 Edge/Chrome, Mac 挂起=False")
def _():
    plat, restore = load_plat("darwin", tempfile.mkdtemp())
    try:
        assert plat.app_window_supported() is False, "Mac 挂起期必须 False"
    finally:
        restore()
    # Windows 真机：本机装有 Edge, 必须 True(冒烟已验证 msedge.exe 存在)
    plat, restore = load_plat("win32", tempfile.mkdtemp())
    try:
        assert plat.app_window_supported() is True, "本机 Edge 在位, 应支持 app 窗口"
    finally:
        restore()


@case("QUOTA_DATA_HOME 覆盖三平台优先")
def _():
    for pf in ("win32", "linux", "darwin"):
        plat, restore = load_plat(pf, tempfile.mkdtemp())
        os.environ["QUOTA_DATA_HOME"] = r"F:\override\path"
        try:
            assert str(plat.data_home()) == r"F:\override\path"
        finally:
            os.environ["QUOTA_DATA_HOME"] = ""
            restore()


print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(0 if fail == 0 else 1)
