# -*- coding: utf-8 -*-
"""V3.28.0 反向迁移测试：LOCALAPPDATA 旧数据 → 程序目录\\data。

模拟场景：
  1) 旧目录有数据、data\\ 没有 → 自动搬 + 旧目录改名留证
  2) 旧目录有数据、data\\ 也有 → 以 data\\ 为准, 旧目录改名留证(不覆盖)
  3) 旧目录为空 → 不动
用法：python tests/test_migrate_v328.py
"""
import json
import os
import shutil
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


def make_env_with_legacy(has_legacy_data, has_new_data):
    """搭一个假的程序目录 + 假 LOCALAPPDATA, 返回 (prog_dir, legacy_dir, data_dir)。"""
    root = Path(tempfile.mkdtemp())
    prog = root / "prog"
    prog.mkdir()
    # plat 复制到 prog 下, 这样 _program_data_home() 就是 prog\\data
    plat_src = BASE / "plat" / "__init__.py"
    (prog / "plat").mkdir()
    shutil.copy2(plat_src, prog / "plat" / "__init__.py")

    legacy = root / "local" / "AI额度管家"
    legacy.mkdir(parents=True)
    os.environ["LOCALAPPDATA"] = str(root / "local")
    os.environ["QUOTA_DATA_HOME"] = ""

    if has_legacy_data:
        (legacy / "data.json").write_text('{"v": "legacy"}', encoding="utf-8")
        (legacy / "accounts_config.json").write_text('{"a": []}', encoding="utf-8")
    if has_new_data:
        d = prog / "data"
        d.mkdir()
        (d / "data.json").write_text('{"v": "new"}', encoding="utf-8")
    return prog, legacy, prog / "data"


def run_migrate(prog):
    """把主程序当模块加载(触发 _migrate_user_data + _migrate_legacy_home)。

    注意: 必须清掉 sys.modules 里的 plat 缓存, 否则命中上一用例的 plat(路径错乱)。"""
    import importlib.util
    sys.modules.pop("plat", None)
    sys.path.insert(0, str(prog))
    try:
        core = prog / "启动额度管家.py"
        shutil.copy2(BASE / "启动额度管家.py", core)
        spec = importlib.util.spec_from_file_location("qg_mig", str(core))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.pop(0)
        sys.modules.pop("plat", None)


@case("迁移: 旧目录有数据、data\\ 无 → 搬过去 + 旧目录改名")
def _():
    prog, legacy, data = make_env_with_legacy(True, False)
    m = run_migrate(prog)
    assert (data / "data.json").is_file(), "data\\data.json 应存在"
    assert json.loads((data / "data.json").read_text(encoding="utf-8"))["v"] == "legacy", "内容应是旧数据"
    assert (data / "accounts_config.json").is_file(), "accounts_config 应一起搬"
    assert not legacy.exists(), "旧目录应已改名"
    assert legacy.with_name(legacy.name + ".有旧数据-已迁移到程序目录data").exists(), "应留改名备份"
    assert str(m.DATA_HOME) == str(data), f"DATA_HOME 应=程序目录data: {m.DATA_HOME}"
    shutil.rmtree(prog.parent)


@case("共存: data\\ 已有数据 → 以新为准, 旧目录改名保留")
def _():
    prog, legacy, data = make_env_with_legacy(True, True)
    run_migrate(prog)
    assert json.loads((data / "data.json").read_text(encoding="utf-8"))["v"] == "new", "data\\ 内容应保持新版"
    assert not legacy.exists(), "旧目录应改名"
    assert legacy.with_name(legacy.name + ".有旧数据-已迁移到程序目录data").is_dir(), "应留改名备份目录"
    shutil.rmtree(prog.parent)


@case("空旧目录: 不动")
def _():
    prog, legacy, data = make_env_with_legacy(False, False)
    run_migrate(prog)
    assert legacy.exists(), "空旧目录不应被动"
    shutil.rmtree(prog.parent)


print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(0 if fail == 0 else 1)
