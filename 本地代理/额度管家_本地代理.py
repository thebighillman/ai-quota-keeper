#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI额度管家 · 本地代理 (V3.2 · 纯 API 版)
=========================================
浏览器页面 -> 本代理 -> 各平台余额/配额接口

只支持两类 API 账号(积分平台/网页关联已于 V3.2 移除):
  1. 按量付费 API(payg)  DeepSeek / SiliconFlow / Kimi 按量 / OpenAI
                         填 API Key 即自动采集真实余额(元)
  2. 订阅制 API(sub)     智谱 GLM Coding Plan / MiniMax Coding Plan / Kimi For Coding
                         配置里加 "query": "quota" + API Key, 采集剩余配额 %
其余平台手动录入兜底。

Key 隐私策略(与 V3.2 前端一致):
  - 界面不显示 Key, 接口不返回 Key 内容
  - 配置文件以混淆形式(enc1: 前缀)保存 Key, 不落明文; 代理查询时自动解密
  - 启动时自动把旧配置里的明文 Key 一次性转为混淆存储
  - Key 请自行保管(可在平台官网 API Keys 页面随时查看/重置)

用法:
  1. 编辑 accounts_config.json 填入平台与 Key (或在 V3.1 页面添加后同步)
  2. python3 额度管家_本地代理.py      (零依赖, 监听 127.0.0.1:8787)
  3. 打开 V3.1 页面, 「多账号管理」→「⬇ 从本地代理同步」

零依赖, 仅用 Python 标准库。Key 只存在你自己电脑上, 代理只监听 127.0.0.1。
"""

import json
import base64
import hashlib
import hmac
import os
import re
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST, PORT = "127.0.0.1", 8788
BASE_DIR = Path(__file__).resolve().parent

# 平台适配层(V3.26.0): 与主程序同一份 plat; 找不到时回退内置 Windows 逻辑
try:
    sys.path.insert(0, str(BASE_DIR.parent))
    import plat as _plat
except Exception:                                 # pragma: no cover
    _plat = None


def _data_home():
    """用户数据目录(V3.26.0): 走 plat(三平台); 回退 Windows 老逻辑。"""
    if _plat is not None:
        return _plat.data_home()
    env = str(os.environ.get("QUOTA_DATA_HOME") or "").strip()
    if env:
        return Path(env)
    base = str(os.environ.get("LOCALAPPDATA") or "").strip()
    return Path(base or (Path.home() / "AppData" / "Local")) / "AI额度管家"


DATA_HOME = _data_home()


def _data_file(name):
    """数据目录优先; 老位置有文件时先用老位置(主程序首次启动会迁移过去, 独立跑代理也不坏)。"""
    p = DATA_HOME / name
    if p.exists():
        return p
    old = BASE_DIR / name
    return old if old.exists() else p


CONFIG_PATH = _data_file("accounts_config.json")
HISTORY_PATH = _data_file("balance_history.jsonl")
ALERT_STATE_PATH = _data_file("alert_state.json")
CACHE_TTL = 60          # 余额查询缓存(秒)
TIMEOUT = 12            # 平台接口超时(秒)

# 美元平台按汇率折算人民币(仅影响趋势/汇总口径)
PLATFORM_RATE = {
    "openai": 7.2, "claude": 7.2, "gemini": 7.2, "minimax": 7.2,
    "openrouter": 7.2, "grok": 7.2,
}


def rate_of(acc):
    """单账号折算率: 账号配置里的 rate 优先, 否则用平台默认值"""
    try:
        r = float(acc.get("rate") or 0)
        if r > 0:
            return r
    except (TypeError, ValueError):
        pass
    return PLATFORM_RATE.get(acc.get("platform", ""), 1)


# 账号原币(V3.19.0): 存储永远原币, 换算只在主程序展示层。
# 这里只负责标注「这个平台的钱是什么币」, 不做任何换算 —— 避免与主程序双重折算。
PLATFORM_CURRENCY = {
    "openai": "USD", "claude": "USD", "gemini": "USD",
    "openrouter": "USD", "grok": "USD", "mistral": "USD",
}


def currency_of(acc):
    """账号币种: 配置里写了就用配置(用户可改), 否则按平台默认, 再缺省 CNY。"""
    c = str(acc.get("currency") or "").strip().upper()
    if c:
        return c
    return PLATFORM_CURRENCY.get(acc.get("platform", ""), "CNY")


def effective_rate(acc):
    """汇总口径折算率: 订阅配额(query=quota)账号不折算金额, 恒为0"""
    if acc.get("query") == "quota":
        return 0.0
    return rate_of(acc)


_cache = {}
_cfg_lock = threading.Lock()


# ---------------------------------------------------------------- 配置读写
# 可选 DPAPI 加密(V3.24.0): 落盘时用 Windows 数据保护 API 包裹整个配置文件。
# 目的: 防"别的程序/别人拷走文件" —— 不防本机当前登录用户本人(威胁模型对本地工具足够)。
# 铁律: 解密失败时【绝不写盘】, 否则一次误判就把用户的 Key 库清空。
_SEC_MAGIC = "DPAPI1:"
_CFG_STATE = {"decryptFailed": False, "error": ""}


def _dpapi_available():
    return sys.platform.startswith("win")


def _data_blob(data: bytes):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi_protect(data: bytes) -> bytes:
    """CryptProtectData: 绑定当前用户+当前机器。"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    din, _keep = _data_blob(data)
    dout = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(din), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(dout))
    if not ok:
        raise RuntimeError(f"DPAPI 加密失败(错误码 {ctypes.windll.kernel32.GetLastError()})")
    try:
        return ctypes.string_at(dout.pbData, dout.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dout.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    """CryptUnprotectData: 只有同一用户在同一台机器上才能解开。"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    din, _keep = _data_blob(blob)
    dout = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(din), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(dout))
    if not ok:
        raise RuntimeError("DPAPI 解密失败(换了机器/换了 Windows 用户, 或文件被改动)")
    try:
        return ctypes.string_at(dout.pbData, dout.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dout.pbData)


def _config_is_encrypted() -> bool:
    try:
        with open(CONFIG_PATH, "rb") as f:
            return f.read(len(_SEC_MAGIC)) == _SEC_MAGIC.encode("ascii")
    except OSError:
        return False


def load_config():
    try:
        raw = CONFIG_PATH.read_bytes()
    except FileNotFoundError:
        return {"accounts": []}
    except OSError as e:
        _CFG_STATE.update(decryptFailed=True, error=f"读取配置失败: {e}")
        return {"accounts": [], "_decryptFailed": True}
    if raw.startswith(_SEC_MAGIC.encode("ascii")):
        if not _dpapi_available():
            _CFG_STATE.update(decryptFailed=True,
                              error="配置文件已加密, 但当前系统不支持 DPAPI(仅 Windows)")
            return {"accounts": [], "_decryptFailed": True}
        try:
            plain = _dpapi_unprotect(base64.b64decode(raw[len(_SEC_MAGIC):]))
            _CFG_STATE.update(decryptFailed=False, error="")
            return json.loads(plain.decode("utf-8"))
        except Exception as e:
            # 关键: 解密失败只报告, 不覆盖文件(否则用户 Key 直接丢光)
            _CFG_STATE.update(decryptFailed=True, error=str(e))
            print(f"[配置] 解密失败: {e} —— 已阻止写入, 请恢复 accounts_config.json", flush=True)
            return {"accounts": [], "_decryptFailed": True}
    try:
        _CFG_STATE.update(decryptFailed=False, error="")
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        _CFG_STATE.update(decryptFailed=True, error=f"配置文件不是合法 JSON: {e}")
        return {"accounts": [], "_decryptFailed": True}


def _write_config_bytes(payload: bytes):
    """原子写: 先写临时文件再替换, 避免写一半断电留下坏文件。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)


def save_config(cfg):
    with _cfg_lock:
        if _CFG_STATE.get("decryptFailed"):
            raise RuntimeError("配置文件解密失败, 已阻止写入以免覆盖原有 Key; "
                               "请先恢复 accounts_config.json（或关闭加密）")
        body = json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
        if _config_is_encrypted():
            if not _dpapi_available():
                raise RuntimeError("当前系统不支持 DPAPI, 无法写入加密配置")
            payload = _SEC_MAGIC.encode("ascii") + base64.b64encode(_dpapi_protect(body))
        else:
            payload = body
        _write_config_bytes(payload)


def security_state():
    """加密状态(供界面展示): 是否已加密、DPAPI 是否可用、最近一次解密错误。"""
    enc = _config_is_encrypted()
    try:
        size = CONFIG_PATH.stat().st_size
    except OSError:
        size = 0
    return {"encryptAccounts": enc, "dpapiAvailable": _dpapi_available(),
            "path": str(CONFIG_PATH), "size": size,
            "decryptError": _CFG_STATE.get("error") or "",
            "decryptFailed": bool(_CFG_STATE.get("decryptFailed"))}


def set_encryption(on: bool):
    """开/关加密。切到加密会先备份明文; 切回明文会先备份密文。失败不动原文件。"""
    import shutil as _sh
    on = bool(on)
    if on and not _dpapi_available():
        return {"ok": False, "error": "当前系统不支持 DPAPI(仅 Windows 可用)"}
    with _cfg_lock:
        cfg = load_config()
        if cfg.get("_decryptFailed"):
            return {"ok": False, "error": "配置文件当前无法解密, 不能切换加密状态; "
                                          "请先恢复 accounts_config.json"}
        cfg.pop("_decryptFailed", None)
        body = json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
        # 切换前留一份原文(加密格式不同, 万一出问题还能人工救回)
        try:
            if CONFIG_PATH.exists():
                bidx = "明文备份" if on else "密文备份"
                _sh.copy2(CONFIG_PATH, CONFIG_PATH.with_name(
                    f"accounts_config.json.{bidx}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"))
        except OSError:
            pass
        try:
            if on:
                _write_config_bytes(_SEC_MAGIC.encode("ascii") + base64.b64encode(_dpapi_protect(body)))
            else:
                _write_config_bytes(body)
        except Exception as e:
            return {"ok": False, "error": f"切换失败, 原文件未改动: {e}"}
        _CFG_STATE.update(decryptFailed=False, error="")
        return {"ok": True, **security_state()}


def poll_seconds(cfg):
    return int(cfg.get("poll_seconds", 300))


# ---------------------------------------------------------------- Key 混淆存储
# 配置文件不以明文保存 API Key, 存 enc1:<base64(xor)> 形式, 查询时自动解密。
_OBF_SECRET = b"quota-guardian-local-obfuscation-v1"


def obfuscate_key(key):
    """明文 -> 混淆存储格式。已是 enc1: 开头则原样返回(幂等)。"""
    key = (key or "").strip()
    if not key:
        return ""
    if key.startswith("enc1:"):
        return key
    raw = key.encode("utf-8")
    xored = bytes(b ^ _OBF_SECRET[i % len(_OBF_SECRET)]
                  for i, b in enumerate(raw))
    return "enc1:" + base64.b64encode(xored).decode("ascii")


def deobfuscate_key(stored):
    """混淆存储格式 -> 明文。非 enc1: 开头(旧配置/手填)原样返回。"""
    stored = (stored or "").strip()
    if not stored:
        return ""
    if not stored.startswith("enc1:"):
        return stored
    try:
        xored = base64.b64decode(stored[5:])
        return bytes(b ^ _OBF_SECRET[i % len(_OBF_SECRET)]
                     for i, b in enumerate(xored)).decode("utf-8")
    except Exception:
        return stored


def key_of(acc):
    """取账号真实 Key(自动解密)。适配器/判定一律走这里。"""
    return deobfuscate_key(acc.get("api_key") or "")


# ---------------------------------------------------------------- 平台适配器
def _get_json(url, key, bearer=True, headers=None):
    hdrs = {
        "User-Agent": "quota-guardian-proxy/3.2",
        "Accept": "application/json",
    }
    # 智谱套餐配额接口要求裸 Key 认证(不带 Bearer 前缀), 见官方文档
    hdrs["Authorization"] = f"Bearer {key}" if bearer else key
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---- 额度窗口(V3.17.11): 5小时 / 7天窗口进度 + 重置时间, 只展示不计费 ----
def _ts_ms(v):
    """resetTime(ISO 字符串 / 秒 / 毫秒时间戳) -> 毫秒时间戳; 取不到返回 None。
    火山方舟的 5 小时窗口空闲时会返回 -1(无重置计划), 一律视为取不到。"""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            n = float(v)
            if n <= 0:
                return None
            return int(n if n >= 1e11 else n * 1000)
        s = str(v).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)   # 平台给的无时区时间按 UTC 处理
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _win(key, label, used_pct, reset_ms=None, used=None, total=None, unit=""):
    """统一窗口结构: usedPct/remainingPct 为 0-100 的百分比, resetAt 为毫秒时间戳"""
    try:
        up = float(used_pct)
    except (TypeError, ValueError):
        up = 0.0
    up = max(0.0, min(100.0, up))
    d = {"key": key, "label": label,
         "usedPct": round(up, 1), "remainingPct": round(100.0 - up, 1)}
    ms = _ts_ms(reset_ms)
    if ms:
        d["resetAt"] = ms
    if used is not None:
        d["used"] = used
    if total is not None:
        d["total"] = total
    if unit:
        d["unit"] = unit
    return d


def _win_order(ws):
    """5小时 -> 7天 -> 其它, 保证并列展示顺序稳定"""
    rank = {"5h": 0, "7d": 1}
    return sorted(ws, key=lambda w: (rank.get(w.get("key"), 9), w.get("label", "")))


# ---- 按量付费 API(余额制, 返回元) ----
def adapter_deepseek(acc):
    data = _get_json("https://api.deepseek.com/user/balance", key_of(acc))
    infos = data.get("balance_infos") or []
    cny = [b for b in infos if b.get("currency") == "CNY"] or infos
    if not cny:
        raise RuntimeError("接口返回中没有余额信息")
    return {"remaining": float(cny[0]["total_balance"]), "live": True,
            "note": "DeepSeek 实时余额(含赠送+充值)"}


def adapter_siliconflow(acc):
    data = _get_json("https://api.siliconflow.cn/v1/user/info", key_of(acc))
    d = data.get("data") or {}
    return {"remaining": float(d.get("balance", 0)), "live": True,
            "note": "SiliconFlow 实时余额"}


def adapter_moonshot(acc):
    data = _get_json("https://api.moonshot.cn/v1/users/me/balance", key_of(acc))
    d = data.get("data") or {}
    return {"remaining": float(d.get("available_balance", 0)), "live": True,
            "note": "Kimi 实时可用余额"}


def adapter_openrouter(acc):
    """OpenRouter: 优先 GET /api/v1/credits(账户总额度−已用, 普通 Key 即可, 美元)。

    旧版只读 /api/v1/key 的 limit_remaining —— 没给 Key 设 credit limit 的账号会直接失败,
    覆盖率太低。新版以 credits 为主、key 为兜底(补充该 Key 的限额视角)。
    """
    base = str(acc.get("base_url") or "").strip().rstrip("/")
    if not base:
        api = "https://openrouter.ai/api/v1"
    elif base.endswith("/api/v1"):
        api = base                      # 用户已填到 /api/v1
    elif base.endswith("/api"):
        api = base + "/v1"
    else:
        api = base + "/api/v1"
    # ① 账户级: total_credits - total_usage
    try:
        data = _get_json(api + "/credits", key_of(acc))
        d = data.get("data") or {}
        credits, usage = d.get("total_credits"), d.get("total_usage")
        if credits is not None and usage is not None:
            rem = float(credits) - float(usage)
            return {"remaining": rem, "live": True, "currency": "USD",
                    "note": f"OpenRouter 账户余额(充 ${float(credits):.2f} − 已用 ${float(usage):.2f}, 美元)"}
    except Exception:
        pass  # 回退 Key 视角, 不让单一端点故障导致整个账号查询失败
    # ② Key 级: limit_remaining(需给 Key 设 credit limit)
    data = _get_json(api + "/key", key_of(acc))
    d = data.get("data") or {}
    if d.get("limit_remaining") is None:
        raise RuntimeError("OpenRouter 未返回账户余额(credits 接口不可用), 请手动维护余额")
    return {"remaining": float(d["limit_remaining"]), "live": True, "currency": "USD",
            "note": "OpenRouter Key 剩余额度(credits, 美元)"}


# ---------------------------------------------------------------- 海外平台(V3.20.0)
def _api_base(acc, default):
    """自定义接入点: 所有海外平台都可用 base_url 指定中转/网关。
    国内直连 api.openai.com / api.mistral.ai 常不通, 用户各自填自己的地址(方案 §2.4)。"""
    b = str(acc.get("base_url") or "").strip().rstrip("/")
    return b or default


def _err_text(e):
    """把 HTTP/网络异常翻成一句人话(不同平台共用)。"""
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403):
            return (f"Key 认证失败(HTTP {e.code}): Key 无效、已过期或没有该接口权限")
        if e.code == 429:
            return "请求过于频繁(HTTP 429), 稍后自动重试"
        return f"接口返回 HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return (f"无法连接({e.reason})。国内网络请配置代理, "
                "或在账号「API地址」填可用的中转地址")
    return str(e)


def _probe_then_manual(acc, label, probe, hint, currency=None):
    """探活成功、但官方没有余额接口 → 返回手填余额(快照差值算消耗)。

    与 ChatAnywhere 同一套成熟机制: live=False 但 Key 已验证, 界面标「官方未开放/需手动维护」。
    """
    try:
        probe()
    except Exception as e:
        raise RuntimeError(f"{label} " + _err_text(e))
    bal = float(acc.get("remaining") or 0)
    note = f"{label} Key 有效。{hint}"
    if bal > 0:
        note += f"。上次记录 {bal:,.2f}"
    out = {"remaining": bal, "live": False, "note": note, "kind": "money"}
    if currency:
        out["currency"] = currency
    return out


def adapter_mistral(acc):
    """Mistral: GET /v1/credit_balance(方案指定); 中转站没开这个接口时回退探活+手填。"""
    base = _api_base(acc, "https://api.mistral.ai")
    key = key_of(acc).strip()
    try:
        data = _get_json(f"{base}/v1/credit_balance", key)
        bal = None
        for k in ("credit_balance", "balance", "credits", "remaining"):
            if data.get(k) is not None:
                bal = data[k]
                break
        if bal is None and isinstance(data.get("data"), dict):
            for k in ("credit_balance", "balance", "credits"):
                if data["data"].get(k) is not None:
                    bal = data["data"][k]
                    break
        if bal is not None:
            return {"remaining": float(bal), "live": True, "currency": "USD",
                    "note": "Mistral 账户余额(credit_balance)"}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError("Mistral " + _err_text(e))
        # 404/405 = 该接入点没有余额接口 → 继续走探活
    except urllib.error.URLError as e:
        raise RuntimeError("Mistral " + _err_text(e))
    return _probe_then_manual(
        acc, "Mistral", lambda: _get_json(f"{base}/v1/models", key),
        "官方 credit_balance 接口不可用(或该中转站未开放), 请在控制台看余额后点「采集」手填, "
        "系统按两次录入差额算消耗", currency="USD")


def _anthropic_headers(key):
    # Anthropic 用 x-api-key 认证, 且必须带 anthropic-version
    return {"x-api-key": key, "anthropic-version": "2023-06-01"}


def adapter_claude(acc):
    """Anthropic Claude: Admin API 查成本报表(仅 sk-ant-admin 开头的 Admin Key);
    普通 Key 只能探活, 余额手填。"""
    base = _api_base(acc, "https://api.anthropic.com")
    key = key_of(acc).strip()
    hdrs = _anthropic_headers(key)
    month_used = None
    if key.startswith("sk-ant-admin"):
        try:
            start = datetime.now().strftime("%Y-%m-01T00:00:00Z")
            data = _get_json(f"{base}/v1/organization/cost_report"
                             f"?starting_at={start}&bucket_width=1d",
                             key, bearer=False, headers=hdrs)
            total = 0.0
            for bucket in data.get("data") or []:
                for r in bucket.get("results") or []:
                    try:
                        total += float(r.get("amount") or 0)
                    except (TypeError, ValueError):
                        pass
            month_used = round(total, 4)
        except Exception:
            month_used = None   # 报表失败不影响探活结论
    out = _probe_then_manual(
        acc, "Anthropic Claude",
        lambda: _get_json(f"{base}/v1/models", key, bearer=False, headers=hdrs),
        "官方无预付余额查询接口(Admin API 只能查用量), 请在控制台看余额后点「采集」手填",
        currency="USD")
    if month_used is not None:
        out["monthlyUsed"] = month_used
        out["note"] += f"; 本月消耗 ${month_used:,.2f}(Admin API 成本报表)"
    return out


def adapter_openai(acc):
    """OpenAI: 探活 /v1/models; org Admin Key 额外查 Costs API 得本月消耗。
    官方已关闭公开余额接口(credit_grants), 余额必须手填。"""
    base = _api_base(acc, "https://api.openai.com")
    key = key_of(acc).strip()
    month_used = None
    if key.startswith("sk-admin-"):
        try:
            now = datetime.now()
            start = int(datetime(now.year, now.month, 1).timestamp())
            data = _get_json(f"{base}/v1/organization/costs"
                             f"?start_time={start}&bucket_width=1d", key)
            total = 0.0
            for bucket in data.get("data") or []:
                for r in bucket.get("results") or []:
                    amt = r.get("amount") or {}
                    try:
                        total += float(amt.get("value") or 0)
                    except (TypeError, ValueError, AttributeError):
                        pass
            month_used = round(total, 4)
        except Exception:
            month_used = None
    out = _probe_then_manual(
        acc, "OpenAI", lambda: _get_json(f"{base}/v1/models", key),
        "官方已关闭公开余额接口(credit_grants), 请在 platform.openai.com 看余额后点「采集」手填",
        currency="USD")
    if month_used is not None:
        out["monthlyUsed"] = month_used
        out["note"] += f"; 本月消耗 ${month_used:,.2f}(Costs API)"
    return out


def adapter_gemini(acc):
    """Google Gemini(AI Studio): 探活 /v1beta/models?key=..., 余额手填。
    若填了 OpenAI 兼容中转地址, 改用 Bearer + /v1/models。"""
    base = _api_base(acc, "https://generativelanguage.googleapis.com")
    key = key_of(acc).strip()

    def probe():
        if "generativelanguage.googleapis.com" in base:
            _get_json(f"{base}/v1beta/models?key={urllib.parse.quote(key)}",
                      "", bearer=False)
        else:
            _get_json(f"{base}/v1/models", key)

    return _probe_then_manual(
        acc, "Gemini", probe,
        "AI Studio 免费额度官方无余额接口, 请手填余额后按快照差值算消耗",
        currency="USD")


def adapter_groq(acc):
    """Groq: 无任何用量/余额接口(2026-10 实测 31 条候选路径全灭) → 探活 + 手填。"""
    base = _api_base(acc, "https://api.groq.com")
    return _probe_then_manual(
        acc, "Groq", lambda: _get_json(f"{base}/openai/v1/models", key_of(acc).strip()),
        "Groq 官方无用量/余额接口, 请手填余额后按快照差值算消耗",
        currency="USD")


def adapter_stepfun(acc):
    """阶跃星辰 StepFun: GET /v1/accounts (返回人民币余额)"""
    data = _get_json("https://api.stepfun.com/v1/accounts", key_of(acc))
    bal = data.get("balance")
    if bal is None:
        raise RuntimeError("阶跃星辰接口未返回余额信息")
    cash = float(data.get("total_cash_balance", 0))
    voucher = float(data.get("total_voucher_balance", 0))
    return {"remaining": float(bal), "live": True,
            "note": f"阶跃星辰余额(充值{cash:.2f}+赠送{voucher:.2f})"}


# ---- 可灵 Kling(多模态视频/图像) ----
def _b64url_encode(raw_bytes):
    return base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")


def _kling_jwt(access_key, secret_key):
    """旧版可灵 API 用 AK/SK 生成 JWT(HS256, 标准库实现, 有效期30分钟)"""
    now = int(time.time())
    header = _b64url_encode(json.dumps(
        {"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    payload = _b64url_encode(json.dumps(
        {"iss": access_key, "exp": now + 1800, "nbf": now - 5},
        separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _b64url_encode(
        hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def _kling_auth_key(acc):
    """可灵鉴权: 配置填 secret_key(AK) 时走 JWT; 否则用 api_key 作为新版 API Key 直连"""
    ak = (acc.get("access_key") or "").strip()
    sk = (acc.get("secret_key") or "").strip()
    if ak and sk:
        return _kling_jwt(ak, sk)
    return key_of(acc)


def adapter_kling(acc):
    """可灵开放平台: POST /account/billing/package 查询资源包抵扣明细(剩余积分)
    国内域名 api-beijing.klingai.com; 新版 API Key Bearer 直连, 旧版 AK/SK 生成 JWT。
    返回各资源包剩余额度(积分), 汇总为剩余总量(积分, 非金额)。
    注意: 可灵 API 计费与网页端积分是两套独立体系, 这里查的是 API 资源包。"""
    token = _kling_auth_key(acc)
    # 查询近 180 天的资源包(资源包有效期180天)
    now_ms = int(time.time() * 1000)
    body = json.dumps({
        "start_time": now_ms - 180 * 86400 * 1000,
        "end_time": now_ms,
        "cursor": "",
        "limit": 50,
        "filters": [
            {"key": "product_type", "values": []},
            {"key": "api_key_name", "values": []},
            {"key": "package_name", "values": []},
            {"key": "package_id", "values": []},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api-beijing.klingai.com/account/billing/package",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "User-Agent": "quota-guardian-proxy/3.2",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    # 返回结构: data.result.detail[] 或 data.result, 各资源包 remaining/balance 字段
    result = (data.get("data") or {}).get("result") or {}
    details = result.get("detail") or result.get("packages") or []
    if isinstance(details, dict):
        details = [details]
    total_remaining = 0.0
    n = 0
    for p in (details if isinstance(details, list) else []):
        for key in ("remaining_quantity", "remaining", "balance", "remaining_amount",
                    "remaining_quota", "left_quantity"):
            if key in p and p[key] is not None:
                try:
                    total_remaining += float(p[key])
                    n += 1
                except (TypeError, ValueError):
                    pass
                break
    if n == 0:
        raise RuntimeError(
            "可灵未返回资源包剩余额度: 该账号可能没有购买 API 资源包"
            "(可灵 API 计费与网页端积分独立, 免费试用/网页积分不在此列), "
            "或鉴权方式不对——新版在控制台生成 API Key 填 api_key, 旧版填 access_key+secret_key")
    return {"remaining": total_remaining, "live": True,
            "note": f"可灵 API 资源包剩余 {total_remaining:,.0f} 积分", "kind": "points"}


def adapter_qwen_quota(acc):
    """阿里百炼(通义)——按 Key 形态自动路由:
    ① 'AK空格SK'(阿里云 AccessKey): 查阿里云账户余额(BSS 接口, 官方开放, 稳定可用)
    ② sk- 开头(百炼 API Key): 百炼免费额度/用量官方**不提供 API Key 直连接口**
       (官方 CLI bailian-cli 里所有额度/用量查询命令均为 auth:console, 即走控制台登录态),
       故这里不再猜测失效端点, 直接给出可行动的提示, 引导改用阿里云 AccessKey 查余额"""
    raw = key_of(acc).strip()
    parts = raw.split()
    if len(parts) == 2 and parts[0] and parts[1] and " " in raw:
        return adapter_qwen_payg(acc)
    # sk- Key: 官方无开放接口, 明确提示而不是静默回落
    raise RuntimeError(
        "百炼免费额度/用量阿里官方未开放 API Key 直连查询接口"
        "(官方 CLI 中该功能走控制台登录态), 系统无法自动采集。"
        "建议改填阿里云 AccessKey(填法 'AK空格SK')自动查账户余额——"
        "按量付费扣的就是这笔余额; 免费额度请在百炼控制台「用量/额度」页查看后手动填写。"
        "获取 AccessKey: 阿里云控制台 → 右上角头像 → AccessKey 管理(建议用 RAM 子账号并只授 AliyunBSSReadOnlyAccess)")


def adapter_qwen_token_plan(acc):
    """阿里百炼 Token Plan(订阅, sk-sp- 专用 Key)用量。
    官方 CLI(bailian-cli) 中 Token Plan 用量查询为 auth:`console`(控制台登录态),
    且实测 DashScope 网关的 /api/v1/services/tokenplan/balance 为**内部接口**:
    GET 返回 "Request method 'GET' is not supported"(400), POST 缺少未公开的 model/task
    内部参数(400/404), 外部无法稳定构造 —— 故本适配器不再盲调该接口。
    这里改为明确告知用户, 避免把错误伪装成实时值。"""
    raise RuntimeError(
        "百炼 Token Plan 用量阿里官方未开放 API Key 直连查询接口"
        "(官方 CLI 中该功能走控制台登录态, 其网关内部接口需未公开参数, 外部不可用)。"
        "请在百炼控制台 → 套餐用量 页查看后手动填写剩余 Credits; "
        "或改用「阿里百炼(通义)」入口填阿里云 AccessKey(AK空格SK)自动查账户余额")


# ---- 阿里云账户余额(百炼按量扣费的真实余额, 需阿里云 AccessKey) ----
def _aliyun_rpc_sign(ak, sk, action, version):
    """阿里云 RPC 风格签名(HMAC-SHA1), 返回完整请求 query 字符串"""
    params = {
        "Action": action,
        "Format": "JSON",
        "Version": version,
        "AccessKeyId": ak,
        "SignatureMethod": "HMAC-SHA1",
        "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureVersion": "1.0",
        "SignatureNonce": hashlib.md5(
            f"{time.time_ns()}-{os.getpid()}".encode()).hexdigest()[:16],
    }
    # 1. 排序 + percent-encode(RPC 规则: + -> %20, * -> %2A, %7E 还原为 ~)
    def _penc(s):
        return (urllib.parse.quote(str(s), safe="-_.~")
                .replace("+", "%20").replace("*", "%2A"))
    sorted_query = "&".join(f"{_penc(k)}={_penc(v)}" for k, v in sorted(params.items()))
    # 2. 待签名串: HTTP方法 & percent-encode("/") & percent-encode(sorted_query)
    string_to_sign = "GET&%2F&" + _penc(sorted_query)
    # 3. HMAC-SHA1, key = SK + "&"
    digest = hmac.new((sk + "&").encode("utf-8"),
                      string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    sig = base64.b64encode(digest).decode()
    return sorted_query + "&Signature=" + _penc(sig)


def adapter_qwen_payg(acc):
    """百炼按量付费余额 = 阿里云账户余额(百炼按量扣费扣的就是它)。
    走阿里云 BSS QueryAccountBalance(business.aliyuncs.com, RPC 签名)。
    需阿里云 AccessKey: 控制台 → AccessKey 管理, API Key 框填 'AK空格SK'。
    建议 RAM 子账号并只授 AliyunBSSReadOnlyAccess 权限。"""
    raw = key_of(acc)
    parts = raw.split()
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise RuntimeError(
            "查询百炼按量余额需要阿里云 AccessKey(AK/SK), 不是百炼的 sk- API Key。"
            "获取: 阿里云控制台 → 右上角头像 → AccessKey 管理(建议建 RAM 子账号并只授 "
            "AliyunBSSReadOnlyAccess 权限)。填法: API Key 框里填 'AK空格SK'。"
            "若只想跟踪免费额度, 请用「阿里百炼(通义·免费额度)」入口填 sk- Key。")
    ak, sk = parts
    query = _aliyun_rpc_sign(ak, sk, "QueryAccountBalance", "2017-12-14")
    req = urllib.request.Request(
        "https://business.aliyuncs.com/?" + query,
        headers={"User-Agent": "quota-guardian-proxy/3.12",
                 "Accept": "application/json"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        if e.code in (400, 401, 403, 404) and ("InvalidAccessKeyId" in detail
                                          or "SignatureDoesNotMatch" in detail
                                          or "Forbidden" in detail
                                          or "NotAuthorized" in detail
                                          or "access key is not found" in detail):
            raise RuntimeError(
                f"阿里云 AK/SK 认证未通过: 请核对 AccessKey 是否复制完整。{detail[:120]}")
        raise RuntimeError(f"阿里云 BSS 接口返回 HTTP {e.code}: {detail[:120]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接阿里云服务器: {e.reason}")
    if not data.get("Success"):
        raise RuntimeError(f"阿里云 BSS 接口报错: {data.get('Message') or data.get('Code')}")
    d = data.get("Data") or {}
    avail = d.get("AvailableAmount")
    if avail is None:
        raise RuntimeError("阿里云接口未返回账户余额")
    avail = float(avail)
    cash = d.get("AvailableCashAmount")
    note = f"阿里云账户余额 ¥{avail:,.2f}(百炼按量扣此余额)"
    if cash is not None:
        try:
            note += f", 其中现金 ¥{float(cash):,.2f}"
        except (TypeError, ValueError):
            pass
    if avail < 0:
        note += " —— 已欠费, 尽快充值"
    return {"remaining": avail, "live": True, "note": note}


# ---- 火山方舟 Agent Plan(订阅制, 需火山引擎 AK/SK V4 签名) ----
def _hmac_sha256(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _volc_sign(ak, sk, region, service, method, path, query, body, x_date,
               host="open.volcengineapi.com", simple=False):
    """火山引擎 OpenAPI V4 签名(标准库实现, 返回 Authorization 头)。
    host 必须与真实请求 Host 一致(否则 401); billing 用 billing.volcengineapi.com。
    simple=True 用于无 body 的 GET: 只签 host;x-date(实测 billing 需要这样)。"""
    short_date = x_date[:8]
    payload_hash = hashlib.sha256(body).hexdigest()
    if simple:
        signed_headers = "host;x-date"
        canonical = "\n".join([
            method, path, query,
            f"host:{host}",
            f"x-date:{x_date}",
            "", signed_headers, payload_hash,
        ])
    else:
        signed_headers = "content-type;host;x-content-sha256;x-date"
        canonical = "\n".join([
            method, path, query,
            "content-type:application/json",
            f"host:{host}",
            f"x-content-sha256:{payload_hash}",
            f"x-date:{x_date}",
            "", signed_headers, payload_hash,
        ])
    hashed_req = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    scope = f"{short_date}/{region}/{service}/request"
    string_to_sign = "\n".join(["HMAC-SHA256", x_date, scope, hashed_req])
    k_date = _hmac_sha256(sk.encode("utf-8"), short_date)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return (f"HMAC-SHA256 Credential={ak}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")


def _volc_ak_sk(acc):
    """火山方舟 AK/SK 来源: 账号配置 access_key/secret_key, 或 api_key 填 'AK SK'(空格分隔)"""
    ak = (acc.get("access_key") or "").strip()
    sk = (acc.get("secret_key") or "").strip()
    if ak and sk:
        return ak, sk
    raw = key_of(acc)
    parts = raw.split()
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return "", ""


def adapter_volc_agent_plan(acc):
    """火山方舟 Agent Plan(个人版): POST open.volcengineapi.com GetAFPUsage
    返回 5h/日/周/月 四窗口 AFP 用量; 需火山引擎 AccessKey(AK/SK)签名, 推理 Key 不可用。
    剩余% 口径取「最紧窗口」(各窗口剩余%的最小值), 避免月额度还多但 5h 窗口已见底被误判充裕。"""
    ak, sk = _volc_ak_sk(acc)
    if not ak or not sk:
        raise RuntimeError(
            "火山方舟 Agent Plan 需要火山引擎 AccessKey(AK/SK)签名, 不是推理用的 API Key。"
            "获取: 火山引擎控制台 → 右上角头像 → API 访问密钥 → 新建密钥(得到 AK/SK)。"
            "填法: API Key 框里填 'AK空格SK'(如 AKTPLxxxx 你的SK), 保存后自动混淆存储。")
    region, service = "cn-beijing", "ark"
    action_query = "Action=GetAFPUsage&Version=2024-01-01"
    body = b"{}"
    x_date = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    auth = _volc_sign(ak, sk, region, service, "POST", "/", action_query,
                      body, x_date)
    req = urllib.request.Request(
        f"https://open.volcengineapi.com/?{action_query}",
        data=body, method="POST",
        headers={"Authorization": auth,
                 "Content-Type": "application/json",
                 "Host": "open.volcengineapi.com",
                 "X-Content-Sha256": hashlib.sha256(body).hexdigest(),
                 "X-Date": x_date,
                 "User-Agent": "quota-guardian-proxy/3.10",
                 "Accept": "application/json"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(
                f"火山引擎 AK/SK 签名未通过(HTTP {e.code}): 请核对 AK/SK 是否复制完整、"
                "该密钥是否仍有效(控制台 → API 访问密钥)。")
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        raise RuntimeError(f"火山方舟接口返回 HTTP {e.code}: {e.reason} {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接火山引擎服务器: {e.reason}")
    resp = data.get("ResponseMetadata") or {}
    if resp.get("Error"):
        err = resp["Error"]
        raise RuntimeError(f"火山方舟接口报错: {err.get('Code', '?')} {err.get('Message', '')}")
    res = data.get("Result") or data
    plan = (res.get("PlanType") or "").strip()
    if not plan:
        raise RuntimeError(
            "火山方舟接口没有返回套餐信息: 该账号可能没有订阅 Agent Plan(个人版)。"
            "企业版席位请用 GetSeatAFPUsage(暂不支持), 或到控制台手动查看。")
    windows = []
    for key, label in (("AFPFiveHour", "5h"), ("AFPDaily", "日"),
                       ("AFPWeekly", "周"), ("AFPMonthly", "月")):
        w = res.get(key) or {}
        quota, used = float(w.get("Quota") or 0), float(w.get("Used") or 0)
        if quota > 0:
            rem_pct = max(0.0, (quota - used) / quota * 100)
            # ResetTime=-1 表示窗口空闲/无重置计划(方舟凌晨重置后的常态), 归一成 None
            rt = _ts_ms(w.get("ResetTime"))
            windows.append((label, rem_pct, quota, used, rt))
    if not windows:
        raise RuntimeError("火山方舟 Agent Plan 接口没有返回有效用量窗口")
    worst_pct = min(w[1] for w in windows)
    note = f"方舟 Agent Plan({plan}档)剩余 {worst_pct:.0f}%(最紧窗口)"
    label_cn = {"5h": "5 小时", "日": "1 天", "周": "7 天", "月": "30 天"}
    key_of_label = {"5h": "5h", "日": "1d", "周": "7d", "月": "30d"}
    out_windows = []
    for label, rem_pct, quota, used, reset_ms in windows:
        note += f", {label}剩{rem_pct:.0f}%"
        if label == "5h" and reset_ms:
            note += "(重置 " + datetime.fromtimestamp(
                reset_ms / 1000).strftime("%H:%M") + ")"
        out_windows.append(_win(key_of_label.get(label, label), label_cn.get(label, label),
                                100.0 - rem_pct, reset_ms, used, quota, "AFP"))
    # 口径(V3.17.8): 配额%取「月窗口」(计费周期口径, 与钱包折算/摊销一致);
    # 最紧窗口(5h/周)是限流预警不是钱包价值, 只留在 note 里提示, 不再当配额落盘
    monthly_pct = next((w[1] for w in windows if w[0] == "月"), None)
    val = monthly_pct if monthly_pct is not None else worst_pct
    return {"remaining": round(val, 1), "live": True,
            "note": note, "kind": "percent",
            "plan": f"{plan} 档", "windows": _win_order(out_windows)}

# ---- 火山方舟·按量(Doubao-Seed): Billing QueryBalanceAcct ----


def adapter_volc_payg(acc):
    """火山方舟·按量(Doubao-Seed 等): Billing QueryBalanceAcct 查账户余额。
    推理用 ark- 开头的 API Key(OpenAI 兼容), 余额查询需火山引擎 AK/SK(V4 签名),
    与 Agent Plan 同一套 AK/SK —— 填法: API Key 框填 'AK空格SK'。
    返回可用余额(元), live=True。"""
    ak, sk = _volc_ak_sk(acc)
    if not ak or not sk:
        raise RuntimeError(
            "火山方舟·按量查余额需要火山引擎 AccessKey(AK/SK), 不是推理用的 ark- API Key。"
            "获取: 火山引擎控制台 → 右上角头像 → API 访问密钥 → 新建密钥(得到 AK/SK); "
            "推理 Key(ark-开头)在方舟控制台「API Key管理」另建。"
            "填法: API Key 框里填 'AK空格SK'(如 AKTPLxxxx 你的SK)。")
    region, service = "cn-beijing", "billing"
    action_query = "Action=QueryBalanceAcct&Version=2022-01-01"
    body = b""
    x_date = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    auth = _volc_sign(ak, sk, region, service, "GET", "/", action_query,
                      body, x_date, host="billing.volcengineapi.com", simple=True)
    req = urllib.request.Request(
        f"https://billing.volcengineapi.com/?{action_query}",
        method="GET",
        headers={"Authorization": auth,
                 "Host": "billing.volcengineapi.com",
                 "X-Date": x_date,
                 "User-Agent": "quota-guardian-proxy/3.10",
                 "Accept": "application/json"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "ignore")
        except Exception:
            pass
        if e.code == 403 and "AccessDenied" in raw:
            raise RuntimeError(
                "AK/SK 本身有效, 但缺少「费用中心只读」权限, 查不到余额。"
                "解决: 火山引擎控制台 → 访问控制(IAM) → 用户 → 给这个 AK 所属用户授予 "
                "「BillingCenterReadOnlyAccess(费用中心只读)」策略, 再点重试。")
        if e.code in (401, 403):
            raise RuntimeError(
                f"火山引擎 AK/SK 签名未通过(HTTP {e.code}): 核对 AK/SK 是否复制完整、是否仍有效"
                "(控制台 → 右上角头像 → API 访问密钥)。")
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        raise RuntimeError(f"火山引擎费用接口返回 HTTP {e.code}: {e.reason} {detail}")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        raise RuntimeError(f"无法连接火山引擎计费服务器: {reason}")
    resp_meta = data.get("ResponseMetadata") or {}
    if resp_meta.get("Error"):
        err = resp_meta["Error"]
        raise RuntimeError(f"火山引擎费用接口报错: {err.get('Code', '?')} {err.get('Message', '')}")
    res = data.get("Result") or {}
    avail = res.get("AvailableBalance")
    try:
        balance = float(avail)
    except (TypeError, ValueError):
        raise RuntimeError("火山引擎费用接口没有返回可用余额")
    freeze = 0.0
    try:
        freeze = float(res.get("FreezeAmount") or 0)
    except (TypeError, ValueError):
        pass
    note = f"方舟·按量可用余额 ¥{balance:.2f}"
    if freeze > 0:
        note += f"(另有冻结 ¥{freeze:.2f})"
    return {"remaining": round(balance, 2), "live": True, "note": note,
            "kind": "money", "unit": "¥"}


def adapter_manual(acc):
    return {"remaining": float(acc.get("remaining", 0)), "live": False,
            "note": "该平台无公开余额接口, 显示配置中的手动值"}


# ---- 订阅制 API(配额制, 返回剩余 %) ----
def adapter_glm_quota(acc):
    """智谱 GLM Coding Plan: GET /api/monitor/usage/quota/limit (裸 Key 认证)。
    无套餐时明确报错(智谱按量余额官方无公开 API, 已实测确认)。"""
    key = key_of(acc)
    try:
        data = _get_json("https://open.bigmodel.cn/api/monitor/usage/quota/limit",
                         key, bearer=False)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(
                f"智谱 API Key 认证失败(HTTP {e.code}): Key 无效或已过期。"
                "请到 open.bigmodel.cn → API Keys 页面重新复制完整的 Key。")
        raise RuntimeError(f"智谱接口返回 HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接智谱服务器: {e.reason}")
    limits = (data.get("data") or {}).get("limits") if data.get("success") else None
    if not limits:
        raise RuntimeError(
            "该 Key 所属的智谱账号没有 Coding Plan 套餐(官方接口明确返回: 不存在 coding plan)。"
            "注意智谱按账号隔离——如果你订阅过 Coding Plan, 请确认这个 Key 是不是订阅账号下创建的"
            "(open.bigmodel.cn → API Keys 页面可核对所属账号)。"
            "按量付费余额智谱官方未开放查询 API, 可到网页端「财务总览」查看后手动录入;"
            "订阅 Coding Plan 后此接口自动恢复配额监控。")
    # 智谱 2026-09 起改版: type=CREDIT_LIMIT + 套餐档位(level), 旧版为 TOKENS_LIMIT
    level = ((data.get("data") or {}).get("level") or "").strip()
    pct_month = pct_week = pct_5h = None
    reset_ms = None
    remain_5h = None  # 新版 5h 窗口剩余积分数
    windows = []
    for it in limits:
        it_type = it.get("type")
        if it_type not in ("TOKENS_LIMIT", "CREDIT_LIMIT"):
            continue
        unit = it.get("unit")
        pct = it.get("percentage")
        if unit == 3:                # 5 小时窗口(number=5, unit=分钟)
            pct_5h = pct
            remain_5h = it.get("remaining")
            key_w, label_w = "5h", "5 小时"
        elif unit == 6:              # 周窗口(number=1)
            pct_week = pct
            key_w, label_w = "7d", "7 天"
        else:                        # 月窗口
            pct_month = pct
            reset_ms = it.get("nextResetTime") or reset_ms
            key_w, label_w = "30d", "30 天"
        if pct is not None:
            windows.append(_win(key_w, label_w, float(pct),
                                it.get("nextResetTime"),
                                it.get("currentValue"), it.get("usage"), "积分"))
    # 优先级: 月 > 周 > 5小时; 新版 CREDIT_LIMIT 的 percentage 已是"已用百分比"
    used = pct_month if pct_month is not None else pct_week
    if used is None:
        used = pct_5h
    if used is None:
        raise RuntimeError("智谱套餐接口没有返回限额信息")
    remaining_pct = max(0.0, 100.0 - float(used))
    plan = f"{level.upper()} 档" if level else "套餐"
    note = f"智谱 Coding Plan({plan})剩余 {remaining_pct:.0f}%"
    if pct_5h is not None:
        note += f", 5小时已用 {float(pct_5h):.0f}%"
    if remain_5h is not None:
        note += f"(剩 {remain_5h} 积分)"
    if pct_week is not None:
        note += f", 本周已用 {float(pct_week):.0f}%"
    if reset_ms:
        note += ", 重置 " + datetime.fromtimestamp(reset_ms / 1000).strftime("%m-%d %H:%M")
    return {"remaining": round(remaining_pct, 1), "live": True,
            "note": note, "kind": "percent",
            "plan": plan, "windows": _win_order(windows)}


def adapter_minimax_quota(acc):
    """MiniMax Coding Plan: GET /v1/api/openplatform/coding_plan/remains (Bearer)
    返回两个窗口: 区间窗口(5小时) 与 周窗口。
    ⚠️ 取值必须取「最紧窗口」的最小值 —— 之前按 月→周→区间 顺序取,
    周窗口恒为 100% 会把真实消耗(区间 91%)完全盖住, 表现为「一直在用却看不出变化」。"""
    data = _get_json("https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains",
                     key_of(acc))
    lst = data.get("model_remains") or []
    if not lst:
        raise RuntimeError("MiniMax 套餐接口返回异常(可能不是 Coding Plan 套餐)")
    main = lst[0]
    iv_pct = main.get("current_interval_remaining_percent")
    wk_pct = main.get("current_weekly_remaining_percent")
    mo_pct = main.get("current_monthly_remaining_percent")
    pcts = [float(v) for v in (iv_pct, wk_pct, mo_pct) if v is not None]
    if not pcts:
        raise RuntimeError("MiniMax 套餐接口没有返回剩余百分比")
    # ★ 成本口径: 剩余额度用「周」窗口 —— 5小时是限流窗口(用完即重置),
    #   拿它做成本核算会严重失真(用户口径: 30天总费用固定, 折算到周/月份度再推算)。
    #   MiniMax 官方周窗口 status=3 表示「不限量」, 此时用区间窗口(5h)只做展示,
    #   成本口径退化为「按月计费、不限量的按量摊销」—— 见 _sub_monthly_cost_from_window。
    if wk_pct is not None and main.get("current_weekly_status") != 3:
        pct = float(wk_pct)
        cost_window = "weekly"
    else:
        # 周窗口不计量(不限量): 剩余额度显示 100%, 成本按月费均摊到天
        pct = 100.0
        cost_window = "monthly"
    note = f"MiniMax 套餐剩余 {pct:.0f}%"
    if iv_pct is not None:
        note += f"(5小时窗口剩 {float(iv_pct):.0f}%"
    if wk_pct is not None:
        note += f", 周窗口剩 {float(wk_pct):.0f}%"
    end_ms = main.get("weekly_end_time") or main.get("monthly_end_time")
    if end_ms:
        note += ", 重置 " + datetime.fromtimestamp(end_ms / 1000).strftime("%m-%d %H:%M")
    # 额度窗口面板: 只展示「真实在计量」的窗口。
    # status=3 表示该窗口不计量(不限量), 硬显示一个恒 100% 的「7 天」只会误导 —— 直接不返回。
    windows = []
    if iv_pct is not None and main.get("current_interval_status") != 3:
        windows.append(_win("5h", "5 小时", 100.0 - float(iv_pct),
                            main.get("end_time"), None, None, "MiniMax 区间"))
    if wk_pct is not None and main.get("current_weekly_status") != 3:
        windows.append(_win("7d", "7 天", 100.0 - float(wk_pct),
                            main.get("weekly_end_time"), None, None, "MiniMax 周"))
    return {"remaining": round(pct, 1), "live": True,
            "note": note, "kind": "percent", "plan": "MiniMax 套餐",
            "costWindow": cost_window,
            "windows": _win_order(windows)}


# ---- MiniMax 按量付费(账户余额, 官方 /v1/account/balance) ----------------
# 实测(2026-10): /v1/query/balance 已不存在(404);
# 真实端点是 GET https://api.minimaxi.com/v1/account/balance, 但只接受
# 「admin key」(账号管理密钥), 普通推理 Key / Coding Plan Key 会返回 403
# token_type_mismatch("api key is not an admin key")。
def adapter_minimax_payg(acc):
    """MiniMax 账户余额: GET https://api.minimaxi.com/v1/account/balance (Bearer admin key)
    返回账户余额(含赠金, 单位元)。需在 MiniMax 控制台「接口密钥」里创建管理员密钥。"""
    key = key_of(acc).strip()
    url = "https://api.minimaxi.com/v1/account/balance"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "User-Agent": "quota-guardian-proxy/3.13",
    })
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        # 国内站普通账号没有 admin key: 官方余额接口调不通。
        # 这时不报错, 退回「你填的余额 + 快照差值自动算消耗」模式, 保证监控继续。
        if e.code in (401, 403) and ("admin key" in detail or "token_type_mismatch" in detail):
            base = float(acc.get("remaining") or 0)
            return {"remaining": base, "live": False,
                    "note": ("MiniMax 官方余额接口只对「管理员密钥(admin key)」开放, "
                             "普通账号拿不到自动余额, 因此无法自动采集。"
                             "请打开 MiniMax 控制台「余额」页, 在本账号上点「采集」把当前余额填一次; "
                             "系统会按两次录入的差额自动算消耗(建议每天或每次充值后更新一次)"
                             + ("。上次记录 ¥%.2f" % base if base > 0 else ""))}
        if e.code in (401, 403):
            raise RuntimeError(f"MiniMax 密钥无效或已失效(HTTP {e.code})")
        raise RuntimeError(f"MiniMax 余额接口返回 HTTP {e.code}: {detail[:120]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 MiniMax: {e.reason}")
    br = data.get("base_resp") or {}
    if br.get("status_code") not in (None, 0, "0"):
        raise RuntimeError(f"MiniMax 余额接口报错: {br.get('status_msg') or br.get('status_code')}")
    bal = None
    for holder in (data, data.get("data") or {}):
        for k in ("account_balance", "balance", "total_balance", "available_balance"):
            v = holder.get(k)
            if v not in (None, ""):
                bal = v
                break
        if bal is not None:
            break
    if bal is None:
        raise RuntimeError(f"MiniMax 余额接口未返回余额字段: {str(data)[:120]}")
    bal = float(bal)
    note = f"MiniMax 账户余额 ¥{bal:,.2f}(含赠金, 按量扣此余额)"
    if bal < 20:
        note += " —— 余额偏低, 建议充值"
    return {"remaining": bal, "live": True, "note": note}


# ---- ChatAnywhere(国内 OpenAI 兼容转发站, V3.18.0) --------------------------
# 2026-10-30 实测探测(api.chatanywhere.tech):
#   /v1/dashboard/billing/subscription|usage → 302 跳官网首页(one-api 计费接口不存在)
#   /v1/models → 标准 OpenAI 风格 401 JSON(探活路径可用, 带 Key 即验证)
# 结论: 官方无 Key 直连余额接口(余额查询页是网页, 遵守 ToS 不爬),
#       走「探活 + 手填余额 + 快照差值」模式 —— 与 MiniMax 按量同一套机制。
# Host: 国内 api.chatanywhere.tech(默认) / 海外 api.chatanywhere.org;
#       账号可在「API地址」填自定义 Base URL(如其他 one-api 系转发站同构复用)。
def _chatanywhere_base(acc):
    """自定义 Base URL 优先; 默认国内 Host。末尾斜杠与 /v1 后缀归一。"""
    raw = str(acc.get("base_url") or acc.get("api_base") or "").strip()
    if raw:
        return raw.rstrip("/").removesuffix("/v1")
    return "https://api.chatanywhere.tech"


def adapter_chatanywhere(acc):
    base = _chatanywhere_base(acc)
    key = key_of(acc).strip()
    # ① 探活: /v1/models 验证 Key 有效(Key 错会得到 OpenAI 风格 401 JSON)
    try:
        data = _get_json(f"{base}/v1/models", key)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(
                f"ChatAnywhere API Key 认证失败(HTTP {e.code}): Key 无效或已过期。"
                "请到官网控制台重新复制; 默认接入点为 api.chatanywhere.tech, "
                "海外网络可在账号「API地址」填 https://api.chatanywhere.org")
        raise RuntimeError(f"ChatAnywhere 接口返回 HTTP {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"无法连接 ChatAnywhere({e.reason})。国内默认 Host 是 api.chatanywhere.tech, "
            "若填了海外地址 api.chatanywhere.org 需自备网络; 也可在账号「API地址」改回国内 Host")
    models = data.get("data") or []
    # ② 余额: 官方无 Key 直连接口 —— 返回手填值, 快照差值算消耗
    base_bal = float(acc.get("remaining") or 0)
    hint = ("请到 ChatAnywhere 官网(api.chatanywhere.org 余额查询页)查看当前余额, "
            "在本账号点「采集」录入; 系统按两次录入的差额自动算消耗"
            "(建议每天或每次充值后更新一次)")
    note = f"ChatAnywhere Key 有效(可用模型 {len(models)} 个)。余额官方未开放 API 查询, {hint}"
    if base_bal > 0:
        note += f"。上次记录 ¥{base_bal:,.2f}"
    return {"remaining": base_bal, "live": False, "note": note, "kind": "money"}


# ---- 智谱 GLM 按量付费(官方无余额接口 -> 用量接口折算) ---------------------
# 实测: /api/paas/v4/users/me/balance、/api/biz/user/rights 等均 404,
# 智谱目前只开放了「模型用量」接口, 拿不到账户余额。
# 折中方案: GET /api/monitor/usage/model-usage 取本月 token 用量,
# 按官方单价折算本月消耗; 余额 = 手填累计充值 − 估算消耗。
_GLM_PRICING = {  # 元/百万 token(输入, 输出) —— 与「价格政策」内置表保持一致
    "default": (7.2, 23.0),      # GLM-5 系列
    "glm-5": (7.2, 23.0),
    "glm-5.3": (7.2, 23.0),
    "glm-5.3-flash": (2.3, 8.0),
    "glm-4.7": (4.3, 15.8),
    "glm-4.6": (4.3, 15.8),
    "glm-4.5": (4.0, 16.0),
}


def _glm_price(model):
    m = (model or "").strip().lower()
    if m in _GLM_PRICING:
        return _GLM_PRICING[m]
    for k, v in _GLM_PRICING.items():
        if k != "default" and m.startswith(k):
            return v
    return _GLM_PRICING["default"]


def _glm_month_cost(acc):
    """查智谱本月模型用量并折算金额。返回 (金额, token 总量, 模型明细)"""
    key = key_of(acc)
    now = datetime.now()
    start = datetime(now.year, now.month, 1)
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    q = urllib.parse.urlencode({"startTime": fmt(start), "endTime": fmt(now)})
    data = _get_json(
        "https://open.bigmodel.cn/api/monitor/usage/model-usage?" + q, key)
    body = data.get("data") or {}
    total = body.get("totalUsage") or {}
    total_tokens = int(total.get("totalTokensUsage") or 0)
    items = total.get("modelSummaryList") or body.get("modelSummaryList") or []
    # 接口只给总 token, 不给输入/输出拆分 -> 按 输入:输出 = 7:3 估算混合单价
    cost = 0.0
    for it in items:
        name = it.get("modelName") or ""
        tok = float(it.get("totalTokens") or 0)
        if tok <= 0:
            continue
        pin, pout = _glm_price(name)
        cost += tok / 1e6 * (pin * 0.7 + pout * 0.3)
    if not items and total_tokens:
        pin, pout = _GLM_PRICING["default"]
        cost = total_tokens / 1e6 * (pin * 0.7 + pout * 0.3)
    return round(cost, 2), total_tokens, items


def adapter_glm_payg(acc):
    """智谱 GLM 按量付费: 官方未开放账户余额接口(实测多个端点均 404)。
    官方「模型用量」接口是按**账号**统计的(同一账号下不同 Key 查出完全相同的数字),
    因此账号里同时有 Coding Plan 时, 套餐内调用会被算进“按量费用” —— 实测虚高十几倍。
    所以: 账号下有 Coding Plan -> 只展示用量, 不折算费用、不参与账目;
          纯按量账号(无套餐) -> 才按 token×单价折算。"""
    cost, tokens, items = _glm_month_cost(acc)
    paid = float(acc.get("totalRecharge") or 0)
    remaining = max(0.0, paid - cost) if paid > 0 else 0.0
    models = "、".join(
        f"{it.get('modelName') or '?'} {int(float(it.get('totalTokens') or 0))/1e6:.1f}M"
        for it in items[:3]) or "暂无"
    # 该账号是否同时有 Coding Plan(有则用量混了套餐调用, 不能折算成按量费用)
    try:
        cfg = load_config()
        has_plan = any(a.get("platform") == "glm" and a.get("query") == "quota"
                       for a in cfg.get("accounts", []))
    except Exception:
        has_plan = False
    if has_plan:
        # 有 Coding Plan 混用: 账号级用量含套餐调用, 不能折算成按量费用。
        # 但「按量账户余额」仍然可以自己录: 用户在智谱控制台看余额 -> 点「采集」录入,
        # 系统按两次录入的差额算消耗(与 MiniMax 同一套机制)。
        note = (f"智谱本月用量 {tokens/1e6:.2f}M tokens, 模型: {models}。"
                "注意: 官方用量接口按「账号」统计, 该账号下还有 Coding Plan, "
                "这个数字包含套餐内调用, 折算成金额会虚高十几倍, 所以不按它算钱。"
                "要算按量花销, 请打开智谱控制台「账户余额」页, 在本账号点「采集」录入当前余额, "
                "系统会按两次录入的差额自动算消耗(建议每天或每次充值后更新一次)")
        return {"remaining": round(remaining, 2), "live": False, "note": note,
                "kind": "money", "monthlyUsed": 0.0}
    note = (f"智谱本月用量 {tokens/1e6:.2f}M tokens(约 ¥{cost:.2f}, 按输入:输出=7:3 估算), "
            f"模型: {models}")
    if paid <= 0:
        note = "余额未知(智谱官方无余额接口)：" + note
    return {"remaining": round(remaining, 2), "live": True, "note": note,
            "kind": "money", "monthlyUsed": cost}


def adapter_kimi_coding(acc):
    """Kimi Coding Plan 订阅: GET api.kimi.com/coding/v1/usages (Bearer)
    Key 在 www.kimi.com/code/console 创建(sk-kimi-xxx), 与开放平台按量 Key 不通用。
    返回 usages.limit_5h / limit_7d 两个窗口(used_ratio + reset_time), 并列展示;
    账户配额取 7 天窗口(5 小时只是限流, 不参与配额与金额)。"""
    data = _get_json("https://api.kimi.com/coding/v1/usages", key_of(acc))
    usage = data.get("usage") or {}
    usages = data.get("usages") or {}
    try:
        limit = float(usage.get("limit") or 0)
        remain = float(usage.get("remaining") or 0)
    except (TypeError, ValueError):
        limit, remain = 0, 0
    if limit <= 0:
        raise RuntimeError(
            "Kimi Coding Plan 接口未返回限额。请确认: ① 已订阅 Kimi Code 套餐; "
            "② Key 是在 www.kimi.com/code/console 创建的 Coding Plan Key"
            "(sk-kimi-xxx)——开放平台(platform.kimi.com)的按量 Key 不能查此接口, "
            "按量余额请用「Kimi(月之暗面)」平台入口添加。")
    pct = max(0.0, remain / limit * 100)
    # ---- 结构化窗口(V3.17.11): 优先 usages.limit_5h / limit_7d ----
    windows = []
    for k, label in (("limit_5h", "5 小时"), ("limit_7d", "7 天")):
        w = usages.get(k) or {}
        try:
            ratio = float(w.get("used_ratio"))
        except (TypeError, ValueError):
            continue
        windows.append(_win(k.replace("limit_", ""), label, ratio * 100.0,
                            w.get("reset_time")))
    # 兼容旧版返回: limits[] 里 300 分钟窗口 = 5 小时; usage = 7 天
    if not any(w["key"] == "5h" for w in windows):
        for it in (data.get("limits") or []):
            det = it.get("detail") or it
            try:
                l5 = float(det.get("limit") or 0)
                u5 = float(det.get("used") or 0)
            except (TypeError, ValueError):
                continue
            if l5 > 0:
                windows.append(_win("5h", "5 小时", u5 / l5 * 100.0,
                                    det.get("resetTime")))
                break
    if not any(w["key"] == "7d" for w in windows):
        windows.append(_win("7d", "7 天", (1 - pct / 100.0) * 100.0,
                            usage.get("resetTime")))
    pct_5h = next((w["remainingPct"] for w in windows if w["key"] == "5h"), None)
    # V3.17.12: 账户配额一律取「7 天窗口」——5 小时是限流窗口, 每天归零 4-5 次,
    # 拿它当配额会把整周期算成用尽(金额虚高)。5 小时只在额度窗口面板展示。
    note = f"Kimi Coding 7天窗口剩余 {pct:.0f}%"
    if pct_5h is not None:
        note += f", 5小时限流剩{pct_5h:.0f}%(仅预警不计费)"
    reset7 = next((w.get("resetAt") for w in windows if w["key"] == "7d"), None)
    if reset7:
        note += ", 重置 " + datetime.fromtimestamp(reset7 / 1000).strftime("%m-%d %H:%M")
    return {"remaining": round(pct, 1), "live": True,
            "note": note, "kind": "percent",
            "plan": "Kimi Code 套餐", "windows": _win_order(windows)}


ADAPTERS = {
    "deepseek": adapter_deepseek,
    # 按量付费: 与订阅同平台共存, 靠 query=quota 区分——不带 quota 的走这里
    "minimax": adapter_minimax_payg,
    "glm": adapter_glm_payg,
    "siliconflow": adapter_siliconflow,
    "kimi": adapter_moonshot,
    "openai": adapter_openai,
    "openrouter": adapter_openrouter,
    "stepfun": adapter_stepfun,
    "kling": adapter_kling,
    "qwen": adapter_qwen_quota,   # 按 Key 形态自动分流: sk-查免费额度 / AK SK查账户余额
    "chatanywhere": adapter_chatanywhere,   # V3.18.0: 国内转发站, 探活+手填余额
    # V3.20.0 海外平台: 余额接口优先(openrouter/mistral), 其余探活+手填(官方无余额接口)
    "mistral": adapter_mistral,
    "claude": adapter_claude,
    "gemini": adapter_gemini,
    "grok": adapter_groq,
}
QUOTA_ADAPTERS = {"glm": adapter_glm_quota, "minimax": adapter_minimax_quota,
                  "kimi": adapter_kimi_coding, "kimicode": adapter_kimi_coding,
                  "qwen": adapter_qwen_quota,
                  "qwenplan": adapter_qwen_token_plan,
                  "doubao": adapter_volc_agent_plan,
                  "arkpayg": adapter_volc_payg}


def pick_adapter(acc):
    """采集方式优先级: 订阅配额(query=quota) > 平台官方余额接口 > 手动"""
    if acc.get("query") == "quota" and acc.get("platform") in QUOTA_ADAPTERS:
        return QUOTA_ADAPTERS[acc["platform"]]
    return ADAPTERS.get(acc.get("platform"), adapter_manual)


def auto_collectable(acc):
    """是否能自动采集(供后台快照线程判断)"""
    if acc.get("query") == "quota" and acc.get("platform") in QUOTA_ADAPTERS:
        return bool(key_of(acc))
    return acc.get("platform") in ADAPTERS and bool(key_of(acc))


# ---------------------------------------------------------------- 余额快照
def append_history(account_name, platform, remaining):
    rec = {"ts": time.time(), "account": account_name,
           "platform": platform, "remaining": remaining}
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _friendly_err(e):
    """把技术性报错翻译成用户能行动的中文提示"""
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403):
            return f"Key 无效或没有权限(HTTP {e.code}), 请检查是否复制完整、是否过期"
        if e.code == 429:
            return "平台限流(HTTP 429), 请稍后再采集"
        return f"平台接口返回 HTTP {e.code}"
    msg = str(e)
    low = msg.lower()
    if "codec can't encode" in low or "latin-1" in low:
        return "Key 含有非法字符(可能是占位符), 请填入真实 API Key"
    if "timed out" in low or "timeout" in low:
        return "连接平台超时, 请检查本机网络(公司/校园网可能需要代理)"
    if "name or service not known" in low or "getaddrinfo failed" in low:
        return "域名解析失败, 请检查网络连接"
    if "certificate" in low:
        return "证书校验失败, 请检查系统时间是否正确"
    return msg


def query_account(acc, force=False):
    now = time.time()
    hit = _cache.get(acc["id"])
    if not force and hit and now - hit["ts"] < CACHE_TTL:
        return dict(hit["result"])
    adapter = pick_adapter(acc)
    try:
        result = adapter(acc)
    except Exception as e:
        msg = _friendly_err(e)
        # 官方本就不提供接口(而非"查询失败")时, 不加误导性的"已回退手动值"前缀
        prefix = "" if "未开放" in msg else "查询失败, 已回退手动值: "
        result = {"remaining": float(acc.get("remaining", 0)), "live": False,
                  "note": prefix + msg}
        if acc.get("query") == "quota":  # 订阅账号失败时仍按百分比口径展示
            result["kind"] = "percent"
    if result["live"]:
        prev = hit["result"]["remaining"] if hit else None
        if prev is None or abs(prev - result["remaining"]) > 1e-9:
            append_history(acc.get("name", f"账号{acc['id']}"),
                           acc.get("platform", "custom"), result["remaining"])
    _cache[acc["id"]] = {"ts": now, "result": result}
    return dict(result)


# ---------------------------------------------------------------- 消耗计算
def load_history(days=40):
    if not HISTORY_PATH.exists():
        return []
    cutoff = time.time() - days * 86400
    out = []
    with open(HISTORY_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ts", 0) >= cutoff:
                out.append(rec)
    return out


def account_month_used(account_name, platform, rate, convert=True):
    """本月余额消耗(按量账号): 本月首日至今 首末快照差值; 余额上升=充值, 不计负消耗"""
    now = datetime.now()
    month_start = datetime(now.year, now.month, 1).timestamp()
    recs = [r for r in load_history(40)
            if r.get("account") == account_name and r.get("ts", 0) >= month_start]
    if len(recs) < 2:
        return 0.0
    drop = max(0.0, recs[0]["remaining"] - recs[-1]["remaining"])
    return drop * rate if convert else drop


# ---------------------------------------------------------------- 预警推送(微信)
# 场景: 多个自建系统共用/分用 API Key, 脚本跑飞或循环调用会在几十分钟内烧掉大额余额。
# 熔断逻辑: 某 Key 近 1 小时余额下降超过阈值(默认 ¥20) -> 立即推送; 另有余额/配额过低预警。
def _post_json(url, payload, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def push_alert(cfg, title, text):
    """推送一条消息到微信(企业微信群机器人 或 Server酱); 返回 (成功与否, 详情)"""
    n = cfg.get("notify") or {}
    if not n.get("enabled"):
        print(f"[预警-未推送] {title}: {text[:80]}")
        return False, "notify.enabled 未开启(仅打印到代理窗口)"
    channel = n.get("channel", "wecom")
    try:
        if channel == "serverchan":
            key = (n.get("sendkey") or "").strip()
            if not key or "在这里" in key:
                return False, "未配置 Server酱 SendKey"
            _post_json(f"https://sctapi.ftqq.com/{key}.send",
                       {"title": title, "desp": text})
            return True, "Server酱 已推送"
        hook = (n.get("webhook") or "").strip()
        if not hook or "在这里" in hook:
            return False, "未配置企业微信群机器人 webhook"
        _post_json(hook, {"msgtype": "markdown",
                          "markdown": {"content": f"**{title}**\n{text}"}})
        return True, "企业微信 已推送"
    except Exception as e:
        return False, f"推送失败: {e}"


def _load_alert_state():
    try:
        return json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"cooldowns": {}}


def _save_alert_state(st):
    ALERT_STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                                encoding="utf-8")


# ---------------------------------------------------------------- 预警规则引擎(V3.31.1)
# 用户可自定义多条规则。规则 = {id, type, scope[], threshold, enabled, note}
#   type:  balance_low      余额低于 threshold 元        (scope: payg)
#          quota_low        配额低于 threshold %         (scope: sub)
#          budget_pct       本月消耗达预算 threshold %    (scope: payg)
#          period_end_days  距周期重置/到期 ≤ threshold 天 (scope: sub/online)
#          burn             1 小时消耗超 threshold 元     (scope: payg)
# scope: payg / sub / online 的任意组合
RULE_TYPES = {
    "balance_low":     {"label": "余额低于指定金额", "unit": "元", "default": 50, "scopes": ["payg"]},
    "quota_low":       {"label": "配额剩余低于指定比例", "unit": "%", "default": 20, "scopes": ["sub"]},
    "budget_pct":      {"label": "本月消耗达预算的指定比例", "unit": "%", "default": 90, "scopes": ["payg"]},
    "period_end_days": {"label": "周期结束前指定天数提醒", "unit": "天", "default": 5, "scopes": ["sub", "online"]},
    "burn":            {"label": "1 小时消耗超过指定金额", "unit": "元", "default": 20, "scopes": ["payg"]},
}

_ONLINE_SUB_PLATFORMS = {"chatgpt", "claude_sub", "gemini_sub"}


def account_kind(acc):
    """账号计费类别: payg(按量) / sub(套餐订阅) / online(在线订阅)"""
    p = acc.get("platform", "")
    if p in _ONLINE_SUB_PLATFORMS:
        return "online"
    if acc.get("query") == "quota" or p in ("kimicode", "qwenplan", "doubao"):
        return "sub"
    return "payg"


def default_rules(n):
    """老配置(没有 rules)时的默认规则: 等价于原来的三条硬编码阈值。"""
    return [
        {"id": "default-burn", "type": "burn",
         "scope": ["payg"], "threshold": float(n.get("burn_threshold_per_hour", 20)), "enabled": True},
        {"id": "default-balance", "type": "balance_low",
         "scope": ["payg"], "threshold": float(n.get("low_balance_threshold", 50)), "enabled": True},
        {"id": "default-quota", "type": "quota_low",
         "scope": ["sub"], "threshold": float(n.get("low_quota_percent", 20)), "enabled": True},
    ]


def get_rules(n):
    """取生效规则: 有 rules 用 rules, 否则用默认三条。"""
    rules = n.get("rules")
    if isinstance(rules, list) and rules:
        return [r for r in rules if isinstance(r, dict) and r.get("enabled", True)]
    return default_rules(n)


def _load_ledger_accounts():
    """读账本里的账号(取 budget / 订阅周期信息), 失败返回空表。
    代理与主程序共用数据目录, 这里只读不写。"""
    for cand in (DATA_HOME / "data.json", BASE_DIR / "data.json"):
        try:
            if cand.is_file():
                d = json.loads(cand.read_text(encoding="utf-8"))
                return {str(a.get("name") or ""): a for a in (d.get("accounts") or [])}
        except Exception:
            continue
    return {}


def _period_end_days(acc, ledger_acc):
    """距周期结束(或订阅到期)还有几天; 算不出来返回 None。"""
    today = datetime.now().date()
    # ① 明确写了有效期
    vu = str(acc.get("validUntil") or (ledger_acc or {}).get("validUntil") or "").strip()
    if vu:
        try:
            return (datetime.strptime(vu[:10], "%Y-%m-%d").date() - today).days
        except Exception:
            pass
    # ② 按重置周期推算下一次重置日
    la = ledger_acc or {}
    cycle = str(la.get("resetCycle") or "").strip().lower()
    day = la.get("resetDay")
    if cycle in ("weekly", "monthly"):
        from datetime import timedelta
        try:
            if cycle == "weekly":
                wd = int(day if day is not None else 0)          # 周一=0
                delta = (wd - today.weekday()) % 7
                return 0 if delta == 0 else delta                 # 0=今天重置
            dom = int(day if day is not None else 1)              # 1-31
            if dom >= today.day:
                nxt = today.replace(day=min(dom, 28))
                return (nxt - today).days
            # 下个月同一天
            year = today.year + (1 if today.month == 12 else 0)
            month = 1 if today.month == 12 else today.month + 1
            nxt = today.replace(year=year, month=month, day=min(dom, 28))
            return (nxt - today).days
        except Exception:
            return None
    # ③ 包月订阅按"订阅开始日"推本月同日
    since = str(la.get("subSince") or "").strip()
    if since:
        try:
            s = datetime.strptime(since[:10], "%Y-%m-%d").date()
            y, m = today.year, today.month
            cand = today.replace(day=min(s.day, 28))
            if cand < today:
                y = y + (1 if m == 12 else 0)
                m = 1 if m == 12 else m + 1
                cand = today.replace(year=y, month=m, day=min(s.day, 28))
            return (cand - today).days
        except Exception:
            return None
    return None


def evaluate_alerts(cfg):
    """每轮采集后评估预警规则(V3.31.1: 规则由用户自定义, 见 RULE_TYPES)。
    积分类账号(可灵资源包)不参与金额类判定。"""
    n = cfg.get("notify") or {}
    cooldown = int(n.get("cooldown", 7200))
    now = time.time()
    state = _load_alert_state()
    cds = state.setdefault("cooldowns", {})
    history = load_history(2)          # 熔断窗口只需近2天
    ledger = _load_ledger_accounts()   # 预算/周期信息在账本里
    rules = get_rules(n)
    triggered = []

    for i, acc in enumerate(cfg.get("accounts", []), start=1):
        if not auto_collectable(acc):
            continue
        name = acc.get("name", f"账号{i}")
        kind = account_kind(acc)
        is_points = acc.get("platform") == "kling"
        rate = effective_rate(acc)
        recs = [r for r in history if r.get("account") == name]
        latest = recs[-1]["remaining"] if recs else None
        la = ledger.get(name) or {}

        for rule in rules:
            rtype = rule.get("type")
            scope = rule.get("scope") or (RULE_TYPES.get(rtype, {}).get("scopes") or [])
            if kind not in scope:
                continue
            # V3.32.4: 规则可指定到具体账号(空=该类别全部账号)
            pinned = rule.get("accounts") or []
            if pinned and name not in pinned:
                continue
            th = float(rule.get("threshold") or 0)
            key = f"{name}|{rtype}|{th:g}"
            if now - cds.get(key, 0) < cooldown:
                continue

            if rtype == "burn":
                if kind != "payg" or is_points or latest is None:
                    continue
                win = [r for r in recs if r["ts"] >= now - 3600]
                if len(win) >= 2:
                    drop = max(0.0, win[0]["remaining"] - win[-1]["remaining"]) * rate
                    if drop >= th:
                        triggered.append(("burn", key,
                            f"账号「{name}」近 1 小时消耗约 ¥{drop:.1f}（触发规则：超过 ¥{th:.0f}），"
                            "疑似脚本跑飞或循环调用，请立即检查对应系统。"))

            elif rtype == "balance_low":
                if kind != "payg" or is_points or latest is None:
                    continue
                money = latest * rate
                if money <= th:
                    triggered.append(("low", key,
                        f"账号「{name}」余额约 ¥{money:.1f}，低于你设的 ¥{th:.0f}，请及时充值。"))

            elif rtype == "quota_low":
                if kind != "sub" or latest is None:
                    continue
                if latest <= th:
                    triggered.append(("low", key,
                        f"账号「{name}」剩余配额 {latest:.0f}%，低于你设的 {th:.0f}%，请及时续费或切换账号。"))

            elif rtype == "budget_pct":
                if kind != "payg" or is_points:
                    continue
                budget = float(la.get("budget") or 0)
                if budget <= 0:
                    continue
                # 本月消耗 = 自然月内快照下降累加(与账本同口径, 这里用近2天history近似不足, 取月度估算)
                month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
                month_recs = [r for r in recs if r["ts"] >= month_start]
                used = 0.0
                for a1, b1 in zip(month_recs, month_recs[1:]):
                    used += max(0.0, a1["remaining"] - b1["remaining"])
                used *= rate
                pct = used / budget * 100
                if pct >= th:
                    triggered.append(("budget", key,
                        f"账号「{name}」本月已花 约 ¥{used:.1f}，达到预算 ¥{budget:.0f} 的 {pct:.0f}%"
                        f"（触发规则：≥{th:.0f}%），注意控制用量。"))

            elif rtype == "period_end_days":
                if kind == "payg":
                    continue
                left = _period_end_days(acc, la)
                if left is None:
                    continue
                if 0 <= left <= th:
                    triggered.append(("period", key,
                        f"账号「{name}」的计费周期还有 {left} 天结束（触发规则：≤{th:.0f} 天），"
                        "需要续费的话现在可以安排。"))

    for kind, key, msg in triggered:
        title = {"burn": "AI额度管家·异常消耗熔断",
                 "budget": "AI额度管家·预算预警",
                 "period": "AI额度管家·周期到期提醒"}.get(kind, "AI额度管家·余额预警")
        ok, detail = push_alert(cfg, title, msg)
        print(f"[预警] {msg} -> {detail}")
        cds[key] = now  # 无论推送成败都进冷却, 避免窗口刷屏

    # 每日消耗日报(可选): 到点推送一次各账号当日消耗
    if n.get("daily_report"):
        hour = int(n.get("daily_report_hour", 9))
        today = time.strftime("%Y-%m-%d")
        if datetime.now().hour == hour and state.get("daily_report") != today:
            lines, total = [], 0.0
            for i, acc in enumerate(cfg.get("accounts", []), start=1):
                if not auto_collectable(acc):
                    continue
                if acc.get("query") == "quota" or acc.get("platform") == "kling":
                    continue
                name = acc.get("name", f"账号{i}")
                day_recs = [r for r in history
                            if r.get("account") == name
                            and time.strftime("%Y-%m-%d", time.localtime(r["ts"])) == today]
                if len(day_recs) >= 2:
                    used = max(0.0, day_recs[0]["remaining"] - day_recs[-1]["remaining"]) * effective_rate(acc)
                    if used > 0:
                        lines.append(f"· {name}: ¥{used:.2f}")
                        total += used
            text = (f"今日(截至 {time.strftime('%H:%M')})各账号消耗:\n"
                    + ("\n".join(lines) if lines else "今日暂无消耗记录")
                    + f"\n合计: ¥{total:.2f}")
            ok, detail = push_alert(cfg, "AI额度管家·每日消耗日报", text)
            print(f"[日报] -> {detail}")
            state["daily_report"] = today  # 无论推送成败都记日期, 避免当小时内每轮重复发

    if triggered or state.get("daily_report") or cds:
        _save_alert_state(state)
    return [t[2] for t in triggered]


# ---------------------------------------------------------------- 版本与更新
# 版本号存 VERSION 文件。更新来源支持两种模式:
#  A. 本地更新包(默认): 把"更新包_Vx.x.zip"丢进代理目录(或配置里填其路径),
#     代理从 zip 里读 version.json 清单, 一键替换文件——全程不联网。
#  B. 远程清单: update_manifest_url 填 https 地址(GitHub raw/对象存储), 适合发布后分发。
# 两种模式共用同一套 安全校验(https源/白名单扩展名/禁路径穿越) 与 旧文件备份。
def _version():
    try:
        return (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except Exception:
        return "0.0.0"


def _ver_tuple(s):
    nums = re.findall(r"\d+", str(s))
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def _resolve_manifest_src(cfg):
    """确定更新来源: 配置优先; 留空则自动扫描代理目录下最新的 .zip 更新包"""
    src = (cfg.get("update_manifest_url") or "").strip()
    if src:
        return src
    try:
        zips = [p for p in BASE_DIR.glob("*.zip") if p.is_file()]
        if zips:
            return str(max(zips, key=lambda p: p.stat().st_mtime))
    except Exception:
        pass
    return ""


def _local_path(src):
    p = Path(src)
    return p if p.is_absolute() else (BASE_DIR / src)


def _fetch_manifest(cfg):
    """读取更新清单: https 远程地址 / 本地 .zip 更新包 / 本地 .json / 本地目录"""
    src = _resolve_manifest_src(cfg)
    if not src:
        return None, ("未找到更新来源: 把「更新包_Vx.x.zip」放进代理目录, "
                      "或在 accounts_config.json 的 update_manifest_url 填路径/https地址")
    if src.startswith("https://"):
        try:
            req = urllib.request.Request(src, headers={"User-Agent": "quota-guardian-proxy"})
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except Exception as e:
            return None, f"拉取远程清单失败: {_friendly_err(e)}"
    p = _local_path(src)
    if p.is_dir():
        p = p / "version.json"
    if p.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(p) as zf:
                return json.loads(zf.read("version.json").decode("utf-8")), None
        except KeyError:
            return None, "更新包里没有 version.json——这不是一个有效的更新包"
        except Exception as e:
            return None, f"读取更新包失败: {e}"
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except Exception as e:
        return None, f"读取更新清单失败: {e}"


def _read_update_file(cfg, furl):
    """读取清单里某个文件的内容: https 下载 / 更新包(zip)内文件 / 本地路径"""
    if furl.startswith("https://"):
        req = urllib.request.Request(furl, headers={"User-Agent": "quota-guardian-proxy"})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return resp.read()
    src = _resolve_manifest_src(cfg)
    p = _local_path(src)
    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as zf:
            return zf.read(furl)
    fp = Path(furl)
    if not fp.is_absolute():
        fp = p.parent / furl  # 相对清单所在目录
    return fp.read_bytes()


def update_check(cfg):
    """检查是否有新版本(本地更新包或远程清单)"""
    cur = _version()
    man, err = _fetch_manifest(cfg)
    if err:
        return {"current": cur, "has_update": False, "detail": err}
    latest = str(man.get("version") or "")
    return {
        "current": cur,
        "latest": latest,
        "has_update": _ver_tuple(latest) > _ver_tuple(cur),
        "date": man.get("date", ""),
        "notes": man.get("notes", []),
        "file_count": len(man.get("proxy_files", [])) + len(man.get("app_files", [])),
        "package_zip": man.get("package_zip", ""),
    }


def update_apply(cfg):
    """一键更新: 按清单替换 代理目录(proxy_files) 与 前端源码目录(app_files) 内的文件。
    白名单扩展名, 旧文件备份到 备份/V<旧版本>/, 前端 dev 模式改完刷新即生效。"""
    cur = _version()
    man, err = _fetch_manifest(cfg)
    if err:
        return {"ok": False, "detail": err}
    latest = str(man.get("version") or "")
    if _ver_tuple(latest) <= _ver_tuple(cur):
        return {"ok": True, "detail": f"已是最新版本 V{cur}", "updated": []}
    proxy_files = man.get("proxy_files") or []
    app_files = man.get("app_files") or []
    if not proxy_files and not app_files:
        return {"ok": False, "detail": "更新清单里没有可更新文件(proxy_files/app_files 均为空)"}
    APP_DIR = BASE_DIR.parent / "app"
    backup_dir = BASE_DIR / "备份" / f"V{cur}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    updated, failed = [], []
    # 白名单: 代理文件 + 前端源码文件
    allowed_ext = {".py", ".md", ".json", ".txt", ""} | {".ts", ".tsx", ".css", ".js", ".jsx", ".html"}

    def _apply(files, base, prefix):
        for f in files:
            rel = (f.get("path") or "").strip()
            furl = (f.get("url") or "").strip()
            # 安全: 目标路径禁止路径穿越/绝对路径; 远程文件仅允许 https 源; 白名单扩展名
            if not rel or not furl or ".." in rel or rel.startswith(("/", "\\")):
                failed.append(f"{rel or '?'}: 非法路径")
                continue
            if not furl.startswith("https://") and (furl.startswith("http://") or ".." in furl):
                failed.append(f"{rel}: 非法下载源")
                continue
            if os.path.splitext(rel)[1].lower() not in allowed_ext:
                failed.append(f"{rel}: 不允许的文件类型")
                continue
            target = base / rel
            try:
                content = _read_update_file(cfg, furl)
                if target.exists():
                    bak = backup_dir / prefix / rel
                    bak.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(target), str(bak))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                updated.append(f"{prefix}/{rel}" if prefix else rel)
            except Exception as e:
                failed.append(f"{rel}: {e}")

    _apply(proxy_files, BASE_DIR, "")
    _apply(app_files, APP_DIR, "app")
    if failed:
        return {"ok": False, "updated": updated,
                "detail": "部分失败: " + "; ".join(failed)}
    detail = f"已更新 {len(updated)} 个文件到 V{latest}, 旧文件备份在 备份/V{cur}/"
    if app_files:
        detail += ("; 前端文件已在 dev 模式热更新, 刷新浏览器即生效"
                   "(如改了依赖/配置, 守护会自动重启网页服务)")
    else:
        detail += ", 重启代理后生效"
    return {"ok": True, "updated": updated, "detail": detail}


# ---------------------------------------------------------------- 状态判定
def status_of(remaining, kind=None):
    if kind == "percent":
        if remaining <= 10:
            return "danger", "配额即将用尽"
        if remaining <= 30:
            return "warning", "配额消耗偏快"
        return "success", "正常"
    if remaining <= 0:
        return "danger", "余额已用尽"
    if remaining < 100:
        return "warning", "余额偏低"
    return "success", "正常"


# ---------------------------------------------------------------- 账号输出
def build_accounts(cfg=None):
    cfg = cfg or load_config()
    out = []
    for i, acc in enumerate(cfg.get("accounts", []), start=1):
        r = query_account({"id": i, **acc})
        remaining = r["remaining"]
        kind = r.get("kind")
        is_quota = acc.get("query") == "quota"
        api_type = "sub" if is_quota else "payg"
        status, status_text = status_of(remaining, kind)
        name = acc.get("name", f"账号{i}")
        platform = acc.get("platform", "custom")
        rate = rate_of(acc)
        api_month_used = r.get("monthlyUsed")   # 平台自己算出的本月费用(如智谱按量)
        if api_month_used is not None:
            month_used = float(api_month_used)
        else:
            month_used = (float(acc.get("monthlyUsed", 0))
                          + account_month_used(name, platform, rate, convert=False))
        recharge = float(acc.get("totalRecharge", 0))
        percent = round(month_used / recharge * 100) if (recharge > 0 and not is_quota) else 0
        # 官方未开放 API: 与「临时查询失败」区分开, 界面显示「官方未开放」而非误导性的「手动录入」
        no_api = (not r["live"]) and ("未开放" in r.get("note", ""))
        if is_quota:
            ad_label = "API直连"
        elif r["live"]:
            ad_label = "API直连"
        elif no_api:
            ad_label = "官方未开放"
            status, status_text = "warning", "需手动维护"
        else:
            ad_label = "手动录入"
        out.append({
            "id": i,
            "platformId": platform,
            "platform": platform,
            "name": name,
            "group": acc.get("group", "默认"),
            # Key 隐私: 不输出任何 Key 内容
            "apiType": api_type,
            "remaining": remaining,
            "monthlyUsed": round(month_used, 2),
            "totalRecharge": recharge,
            "customName": acc.get("customName") or "",   # 同号去重(V3.34.1)
            "phone": acc.get("phone") or "",
            "percent": min(percent, 999),
            "collectMethod": ad_label,
            "status": status,
            "statusText": status_text,
            "lastCollect": time.strftime("%H:%M:%S") if r["live"] else "—",
            "collectFreq": acc.get("collectFreq", "实时" if r["live"] else "—"),
            "live": r["live"],
            "note": r["note"] + (f", 有效期至 {acc['validUntil']}" if acc.get("validUntil") else ""),
            "unit": "%" if kind == "percent" else (acc.get("unit") or ""),
            # 订阅账号附加字段, 供 V3.1 前端直接映射
            "quotaPercent": remaining if is_quota else None,
            "monthlyFee": float(acc.get("monthlyFee", 0)),
            "validUntil": acc.get("validUntil"),
            # 额度窗口(V3.17.11): 5小时/7天进度+重置时间, 只展示不计费
            "plan": r.get("plan") or "",
            "windows": r.get("windows") or [],
            # 多币种(V3.19.0): 金额字段是「原币」, 主程序据此换算展示。
            # 优先级: 账号里用户显式设的币种 > 适配器按平台判定 > 平台默认
            "currency": (str(acc.get("currency") or "").strip().upper()
                         or r.get("currency") or currency_of(acc)),
        })
    return out


def upsert_account(payload):
    """页面表单 -> 配置文件。action: add / update / delete"""
    cfg = load_config()
    accounts = cfg.setdefault("accounts", [])
    action = payload.get("action", "add")

    if action == "delete":
        idx = int(payload.get("id", 0)) - 1
        if 0 <= idx < len(accounts):
            removed = accounts.pop(idx)
            save_config(cfg)
            _cache.clear()
            return {"ok": True, "removed": removed.get("name")}
        return {"ok": False, "error": "账号不存在"}

    entry = {
        "platform": payload.get("platform", "custom"),
        "name": payload.get("name", ""),
        "api_key": payload.get("api_key", ""),
        "group": payload.get("group", "默认"),
        "collectFreq": payload.get("collectFreq", ""),
        "totalRecharge": float(payload.get("totalRecharge") or 0),
        # 同号去重(V3.34.1): 身份字段, 与主程序 data.json 同字段
        "customName": str(payload.get("customName") or "").strip(),
        "phone": str(payload.get("phone") or "").strip(),
        # 多币种(V3.19.0): 没显式指定就按平台默认(海外平台 USD, 其余 CNY)
        "currency": (str(payload.get("currency") or "").strip().upper()
                     or PLATFORM_CURRENCY.get(payload.get("platform", ""), "CNY")),
    }
    if not entry["name"].strip():
        return {"ok": False, "error": "缺少账号名称"}
    # 订阅制账号可选字段(网页「添加API账号」会传): query=quota / monthlyFee / validUntil
    if payload.get("query"):
        entry["query"] = payload["query"]
    if payload.get("monthlyFee") is not None:
        entry["monthlyFee"] = float(payload.get("monthlyFee") or 0)
    if payload.get("validUntil"):
        entry["validUntil"] = str(payload["validUntil"])
    # 自定义 API 地址(V3.18.0: ChatAnywhere 等转发站/海外 Host 用; 留空=平台默认)
    if str(payload.get("base_url") or "").strip():
        entry["base_url"] = str(payload["base_url"]).strip()
    if action == "update":
        idx = int(payload.get("id", 0)) - 1
        if 0 <= idx < len(accounts):
            if not entry["api_key"]:  # 未改 Key 则保留原 Key(已是混淆形式)
                entry["api_key"] = accounts[idx].get("api_key", "")
            old = accounts[idx]
            for k in ("remaining", "monthlyUsed", "rate", "unit",
                      "query", "monthlyFee", "validUntil", "note", "base_url",
                      "customName", "phone"):
                if k in old:
                    entry[k] = old[k]
            # 没在表单里改币种 → 保留原值(不能因为表单没这个字段就被平台默认值冲掉)
            if not str(payload.get("currency") or "").strip() and old.get("currency"):
                entry["currency"] = old["currency"]
            accounts[idx] = entry
        else:
            return {"ok": False, "error": "账号不存在"}
    else:
        # 重复检测(V3.30.6): 同平台同 Key = 同一个账号, 拒绝录入避免重复计费/重复显示。
        # Key 先混淆再比(配置里存的都是 enc1: 形式); update 走上面分支不受影响。
        new_key_obf = obfuscate_key(entry.get("api_key", ""))
        for ex in accounts:
            if (ex.get("platform") == entry["platform"]
                    and ex.get("api_key") == new_key_obf):
                return {"ok": False, "error":
                        f"这个 Key 已经添加过了（账号「{ex.get('name')}」）。"
                        "同一平台同一个 Key 重复添加会导致额度显示两条、月费重复计, 如需改月费请编辑原账号。"}
        entry["remaining"] = entry["totalRecharge"]
        entry.setdefault("monthlyUsed", 0)
        accounts.append(entry)
    # Key 混淆存储: 配置文件不落明文(对已是 enc1: 的值幂等)
    entry["api_key"] = obfuscate_key(entry.get("api_key", ""))
    save_config(cfg)
    _cache.clear()
    return {"ok": True}


# ---------------------------------------------------------------- 后台快照线程
def snapshot_loop(stop_event=None):
    """采集+评估循环。V3.29.0: 支持 stop_event 热停止(界面改配置后立即生效, 不必重启软件)。"""
    while not (stop_event and stop_event.is_set()):
        try:
            cfg = load_config()
            for i, acc in enumerate(cfg.get("accounts", []), start=1):
                if auto_collectable(acc):
                    query_account({"id": i, **acc}, force=True)
            # 每轮采集后评估预警(异常消耗熔断/余额过低/日报), 失败不影响采集
            try:
                evaluate_alerts(cfg)
            except Exception as e:
                print(f"[预警] 评估出错(不影响采集): {e}")
            wait = poll_seconds(cfg)
            # 分片等待: 便于 stop_event 及时生效
            for _ in range(int(wait)):
                if stop_event and stop_event.is_set():
                    return
                time.sleep(1)
        except Exception:
            for _ in range(60):
                if stop_event and stop_event.is_set():
                    return
                time.sleep(1)


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        try:
            if self.path.startswith("/api/health"):
                self._send(200, {"ok": True, "ts": time.time()})
            elif self.path.startswith("/api/notify/test"):
                ok, detail = push_alert(load_config(),
                    "AI额度管家测试",
                    "这是一条测试消息: 异常消耗熔断/余额预警/每日日报都会推送到这里。")
                self._send(200, {"ok": ok, "detail": detail})
            elif self.path.startswith("/api/version"):
                self._send(200, {"version": _version()})
            elif self.path.startswith("/api/update/check"):
                self._send(200, update_check(load_config()))
            elif self.path.startswith("/api/accounts"):
                self._send(200, {"accounts": build_accounts()})
            else:
                self._send(404, {"error": "unknown path"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
            if self.path.startswith("/api/update/apply"):
                self._send(200, update_apply(load_config()))
            elif self.path.startswith("/api/accounts"):
                self._send(200, upsert_account(payload))
            elif self.path.startswith("/api/collect"):
                acc_id = payload.get("id")
                cfg = load_config()
                accounts = cfg.get("accounts", [])
                if acc_id is not None:
                    idx = int(acc_id) - 1
                    if not (0 <= idx < len(accounts)):
                        self._send(200, {"ok": False, "note": "账号不存在"})
                        return
                    acc = accounts[idx]
                    if not auto_collectable(acc):
                        self._send(200, {"ok": False, "live": False,
                                         "note": "未配置自动采集: 请在配置里填入该平台 API Key, 或手动维护余额"})
                        return
                    r = query_account({"id": idx + 1, **acc}, force=True)
                    self._send(200, {"ok": r["live"], "live": r["live"],
                                     "remaining": round(r["remaining"], 2),
                                     "apiType": "sub" if acc.get("query") == "quota" else "payg",
                                     "unit": "%" if r.get("kind") == "percent" else "",
                                     "note": r["note"]})
                else:
                    _cache.clear()
                    done, failed = 0, []
                    for i, acc in enumerate(accounts, start=1):
                        if auto_collectable(acc):
                            r = query_account({"id": i, **acc}, force=True)
                            if r["live"]:
                                done += 1
                            else:
                                failed.append(acc.get("name", f"账号{i}"))
                    self._send(200, {"ok": not failed, "done": done,
                                     "note": ("全部刷新成功" if not failed
                                              else "部分失败: " + "、".join(failed))})
            else:
                self._send(404, {"error": "unknown path"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {fmt % args}")


if __name__ == "__main__":
    # Key 隐私迁移: 把旧配置里明文保存的 api_key 一次性转成混淆存储(enc1:)
    try:
        _cfg = load_config()
        _dirty = False
        _migrated = 0
        for _a in _cfg.get("accounts", []):
            _k = (_a.get("api_key") or "").strip()
            if _k and not _k.startswith("enc1:"):
                _a["api_key"] = obfuscate_key(_k)
                _dirty = True
                _migrated += 1
        if _dirty:
            save_config(_cfg)
            print(f"[隐私] 已将 {_migrated} 个明文 API Key 转为混淆存储(enc1:), 配置文件不再含明文 Key")
    except Exception as _e:
        print(f"[隐私] Key 迁移跳过: {_e}")
    threading.Thread(target=snapshot_loop, daemon=True).start()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"启动失败: 端口 {PORT} 被占用——多半已经有一个代理在运行。")
        print("请先关掉旧的代理窗口(或结束旧的 python 进程), 再重新启动。")
        raise SystemExit(1)
    print(f"额度管家本地代理 V{_version()}(纯API版) 已启动: http://{HOST}:{PORT}")
    print(f"配置文件: {CONFIG_PATH}")
    print(f"余额历史: {HISTORY_PATH}")
    print(f"检查更新: http://{HOST}:{PORT}/api/update/check")
    print("按 Ctrl+C 停止")
    server.serve_forever()
