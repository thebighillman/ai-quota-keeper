# -*- coding: utf-8 -*-
"""DPAPI 加密 L1 测试：真调 Windows CryptProtectData，不联网。

覆盖：加密往返 / 加密后仍能读写 / 篡改密文时拒绝写入 / 一键解回明文 / 备份文件存在。
用法：python tests/test_dpapi.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = Path(tempfile.mkdtemp(prefix="dpapi_"))
os.environ["QUOTA_DATA_HOME"] = str(DATA)
CFG = DATA / "accounts_config.json"

spec = importlib.util.spec_from_file_location("proxy_dpapi", BASE / "本地代理" / "额度管家_本地代理.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
# 强制指向测试目录: 若测试目录还没有配置文件, _data_file 会兜底到开发台模板 —— 绝不能改那个
m.CONFIG_PATH = CFG
assert m.CONFIG_PATH == CFG, m.CONFIG_PATH

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


SAMPLE = {"accounts": [{"platform": "deepseek", "name": "主号", "api_key": "enc1:abc"}],
          "poll_seconds": 300}


@case("DPAPI 可用性检测")
def _():
    assert m.security_state()["dpapiAvailable"] is True, m.security_state()


@case("明文 → 加密（文件变 DPAPI1: 开头）")
def _():
    CFG.write_text(json.dumps(SAMPLE, ensure_ascii=False, indent=2), encoding="utf-8")
    st = m.set_encryption(True)
    assert st["ok"] and st["encryptAccounts"] is True, st
    raw = CFG.read_bytes()
    assert raw.startswith(b"DPAPI1:"), raw[:20]
    assert b"deepseek" not in raw, "明文内容不应出现在密文里"


@case("加密后 load_config 内容完全一致")
def _():
    cfg = m.load_config()
    assert cfg["accounts"][0]["name"] == "主号", cfg
    assert cfg["poll_seconds"] == 300, cfg


@case("加密状态下保存 → 仍是密文且能读回")
def _():
    cfg = m.load_config()
    cfg["accounts"].append({"platform": "kimi", "name": "二号", "api_key": "enc1:xyz"})
    m.save_config(cfg)
    raw = CFG.read_bytes()
    assert raw.startswith(b"DPAPI1:"), "保存后应保持加密"
    again = m.load_config()
    assert len(again["accounts"]) == 2, again
    assert m.security_state()["decryptFailed"] is False


@case("篡改密文 → 解密失败且拒绝写入（关键保护）")
def _():
    good = CFG.read_bytes()
    CFG.write_bytes(good[:40] + b"XXXX" + good[44:])
    cfg = m.load_config()
    assert cfg.get("_decryptFailed") is True, cfg
    st = m.security_state()
    assert st["decryptFailed"] is True and st["decryptError"], st
    try:
        m.save_config({"accounts": []})
        raise AssertionError("解密失败时不应允许写入")
    except RuntimeError:
        pass
    CFG.write_bytes(good)          # 还原，供后续用例


@case("切回明文：内容一致、有密文备份")
def _():
    st = m.set_encryption(False)
    assert st["ok"] and st["encryptAccounts"] is False, st
    raw = CFG.read_bytes()
    assert raw.startswith(b"{"), raw[:10]
    cfg = json.loads(raw.decode("utf-8"))
    assert len(cfg["accounts"]) == 2, cfg
    backups = list(DATA.glob("accounts_config.json.密文备份-*"))
    assert backups, "切回明文前应留一份密文备份"
    assert m.security_state()["decryptFailed"] is False


@case("明文状态下保存不加密、文件可读")
def _():
    m.save_config({"accounts": [{"platform": "custom", "name": "三号", "api_key": "enc1:q"}]})
    raw = CFG.read_bytes()
    assert raw.startswith(b"{"), raw[:10]
    assert json.loads(raw.decode("utf-8"))["accounts"][0]["name"] == "三号"


print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(0 if fail == 0 else 1)
