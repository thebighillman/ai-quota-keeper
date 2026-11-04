#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI额度管家 · 一键启动器 (V3.1 单机版)
====================================
双击运行(或 python3 启动额度管家.py)即可:
  1. 启动本机服务(只监听 127.0.0.1:8787, 外网访问不到)
  2. 自动打开浏览器进入网页
  3. 数据保存在用户数据目录(默认 %LOCALAPPDATA%\\AI额度管家\\), 不需要安装数据库 / Node.js

V3.22.0 起程序目录只读可分发:
  - 首次启动会自动把程序目录里的 data.json / accounts_config.json 等搬到数据目录
  - Key 与账本永不进更新包; 「关于系统 → 数据位置」可查看当前路径
  - 可用环境变量 QUOTA_DATA_HOME 改到别处(便携盘/同步盘)
"""

import bisect
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# pythonw 静默运行(V3.25.1): 无控制台时 stdout/stderr 为 None, print/异常写栈会直接崩。
# 检测到就重定向到数据目录的 运行日志.log, 双击桌面图标后完全无窗口、出问题也能查。
if sys.stdout is None or sys.stderr is None:
    _LOG_DIR = Path(str(os.environ.get("QUOTA_DATA_HOME")
                        or (Path(str(os.environ.get("LOCALAPPDATA") or "")) / "AI额度管家")))
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        _lf = open(_LOG_DIR / "运行日志.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = _lf
        sys.stderr = _lf
    except OSError:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = sys.stdout

HOST = "127.0.0.1"
# 端口可用环境变量覆盖(V3.25.0): 便于内测并行跑多个实例, 或 8788 被占用时换口
try:
    PORT = int(str(os.environ.get("QUOTA_PORT") or "").strip() or 8788)
except ValueError:
    PORT = 8788
BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist"

# 平台适配层(V3.26.0): 路径/自启/加密等平台差异统一走 plat。
# 导入失败(包没带 plat/)时回退到内置 Windows 逻辑, 保证老更新包升级不断档。
try:
    sys.path.insert(0, str(BASE_DIR))
    import plat as _plat
except Exception:                                 # pragma: no cover
    _plat = None


def _data_home():
    """用户数据目录(V3.22.0): 程序目录只读可分发, Key/账本统一放这里。

    默认 Windows %LOCALAPPDATA%\\AI额度管家\\, Linux ~/.local/share/aiquota,
    Mac ~/Library/Application Support/AI额度管家; 可用 QUOTA_DATA_HOME 覆盖(测试/便携部署)。
    """
    if _plat is not None:
        return _plat.data_home()
    env = str(os.environ.get("QUOTA_DATA_HOME") or "").strip()
    if env:
        return Path(env)
    base = str(os.environ.get("LOCALAPPDATA") or "").strip()
    return Path(base or (Path.home() / "AppData" / "Local")) / "AI额度管家"


DATA_HOME = _data_home()

# 首次启动要从程序目录搬走的用户数据(白名单): 只搬已知文件名, 不碰其它东西
# V3.28.0: 补 license.json/install.json(授权状态与试用起点, 漏了会导致升级后授权丢失)
_DATA_FILES = ("data.json", "data.json.bak", "accounts_config.json",
               "价格政策.json", "价格政策.核对.json", "balance_history.jsonl",
               "alert_state.json", "license.json", "install.json",
               "运行日志.log")
_MIGRATED = []          # 本次启动实际迁移的文件(供「关于系统」展示)


def _migrate_user_data():
    """把程序目录里的用户数据搬到 DATA_HOME(老用户无感升级)。

    - 目标不存在 → 复制 + 删源, 并写 .migrated 标记
    - 目标已存在 → 以数据目录为准, 源改名 .migrated-dup 保留(绝不覆盖用户数据)
    - 只处理白名单里的文件; 备份目录/程序文件不动
    """
    try:
        DATA_HOME.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    moved, dup = [], []
    for name in _DATA_FILES:
        src = BASE_DIR / name
        if not src.is_file():
            continue
        dst = DATA_HOME / name
        try:
            if dst.exists():
                alt = src.with_name(src.name + ".migrated-dup")
                if not alt.exists():
                    src.replace(alt)
                dup.append(name)
            else:
                shutil.copy2(str(src), str(dst))
                src.unlink()
                moved.append(name)
        except OSError:
            continue
    if moved or dup:
        _MIGRATED.extend(moved)
        try:
            (DATA_HOME / ".migrated").write_text(
                "migrated_at=%s\nmoved=%s\nduplicates=%s\n"
                % (datetime.now().isoformat(timespec="seconds"),
                   ",".join(moved) or "-", ",".join(dup) or "-"),
                encoding="utf-8")
        except OSError:
            pass


_migrate_user_data()


def _migrate_legacy_home():
    """V3.28.0 反向迁移: 旧 %LOCALAPPDATA%\\AI额度管家 → 程序目录\\data。

    - 仅当 DATA_HOME 是程序目录\\data 且旧目录存在时执行
    - data\\ 里没有数据文件而旧目录有 → 整体搬过来(旧目录改名 .moved-旧目录 保留原地)
    - data\\ 里已有数据 → 以 data\\ 为准, 旧目录改名保留(绝不覆盖, 用户可手动清理)
    """
    if _plat is None:
        return
    try:
        new_home = _plat.data_home()
        if str(new_home) != str(BASE_DIR / "data"):
            return                                   # 回退到了 LOCALAPPDATA 或自定义, 无需迁
        legacy = _plat.legacy_windows_home()
        if not legacy.is_dir() or legacy == new_home:
            return
        # 新目录已有真实数据? (以 data.json 为准)
        new_has_data = (new_home / "data.json").is_file()
        legacy_has_data = (legacy / "data.json").is_file()
        if not legacy_has_data:
            return                                   # 旧目录本来就空, 不动
        if new_has_data:
            # 双边都有: 以新为准, 旧目录整体改名留证
            alt = legacy.with_name(legacy.name + ".有旧数据-已迁移到程序目录data")
            if not alt.exists():
                legacy.rename(alt)
            return
        # 新目录无数据 → 搬
        new_home.mkdir(parents=True, exist_ok=True)
        moved = False
        for name in _DATA_FILES:
            src = legacy / name
            if src.is_file() and not (new_home / name).exists():
                shutil.copy2(str(src), str(new_home / name))
                moved = True
        if moved:
            alt = legacy.with_name(legacy.name + ".有旧数据-已迁移到程序目录data")
            if not alt.exists():
                legacy.rename(alt)
            _MIGRATED.append(f"(自 {legacy} 迁移)")
    except Exception:
        pass


_migrate_legacy_home()
DATA_PATH = DATA_HOME / "data.json"
_lock = threading.Lock()

MIME = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript",
    ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".map": "application/json",
}

# ---------------------------------------------------------------- 存储
# 数据安全设计(2026-10-16 加固):
#   ① 每次写盘前把上一版留成 data.json.bak(滚动一版), 保证"最多丢一次操作"而不是全丢
#      且空账本永远不会覆盖有价值的备份(见 _save)
#   ② 写临时文件 + os.replace 原子替换: 断电/崩溃时要么是旧文件、要么是新文件, 不会出现半截
#   ③ 读-改-写整体持锁(_DATA_TX, 可重入): 消除多线程并发覆盖丢数据
#   ④ 读失败不再静默返回空结构: 标记 _DEGRADED, 拒绝写盘并尝试从 .bak 恢复, 避免"空结构覆盖全量"
#   ⑤ 文件"不存在"也先尝试 .bak 恢复(不等同于首次启动), 否则空账本会在两次写盘后吞掉真备份
_EMPTY = {"nextId": 1, "accounts": [], "snapshots": [], "settings": {}}
_DATA_TX = threading.RLock()   # 可重入: 业务层统一用 `with _DATA_TX:` 包住 读-改-写
_DEGRADED = {"on": False, "reason": ""}   # 数据文件损坏标志(启动自检/读失败时置位)


def _read_json_lenient(txt):
    """宽松解析: 容忍历史遗留的尾随内容/多余空白, 尽量把数据救回来"""
    try:
        return json.loads(txt)
    except Exception:
        pass
    # 兜底: 截到最后一个 '}' 再试(应对写一半的残留)
    idx = txt.rfind("}")
    if idx > 0:
        try:
            return json.loads(txt[:idx + 1])
        except Exception:
            pass
    return None


def _looks_empty(d):
    """判断一份账本是否"疑似被清空"(既没有账号也没有任何快照)。
    用于防呆: 不让空账本把有价值的备份覆盖掉。"""
    try:
        return not d.get("accounts") and not d.get("snapshots")
    except Exception:
        return False


def _atomic_write(path, text):
    """原子写单文件: 写临时文件 -> os.replace。避免断电/崩溃留下半截文件。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _load():
    """读取账本。读失败 -> 置降级标志(拒绝写盘) 并尝试从 .bak 恢复, 绝不静默返回空结构。"""
    global _DEGRADED
    with _DATA_TX:
        raw = None
        try:
            raw = DATA_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            # 文件"不见了"(误删 / 同步盘冲突 / 被杀软清理) —— 不要当成"首次启动":
            # 第 1 次写盘会把空账本写回 data.json, 第 2 次写盘又拿空账本当备份,
            # 于是唯一的真备份也被覆盖, 数据彻底找不回。这里必须先尝试从 .bak 恢复。
            bak = _bak_path()
            if bak.is_file():
                try:
                    bd = _read_json_lenient(bak.read_text(encoding="utf-8"))
                    if isinstance(bd, dict) and "accounts" in bd and not _looks_empty(bd):
                        _atomic_write(DATA_PATH, json.dumps(bd, ensure_ascii=False, indent=1))
                        _DEGRADED = {"on": False, "reason": ""}
                        print("  [数据保护] data.json 不存在, 已从 data.json.bak 自动恢复"
                              f"({len(bd.get('accounts', []))} 个账号 / {len(bd.get('snapshots', []))} 条快照) ✅",
                              flush=True)
                        return bd
                except Exception as e:
                    print(f"  [数据保护] data.json 不存在, 且备份恢复失败: {e}", flush=True)
            _DEGRADED = {"on": False, "reason": ""}
            return json.loads(json.dumps(_EMPTY))   # 确实是首次启动(无文件也无备份)
        except Exception as e:
            _DEGRADED = {"on": True, "reason": f"读取 data.json 失败: {e}"}
            print(f"  [数据保护] {_DEGRADED['reason']} —— 已停止写入, 请检查文件占用/权限", flush=True)
            return json.loads(json.dumps(_EMPTY))

        d = _read_json_lenient(raw)
        if isinstance(d, dict) and "accounts" in d:
            _DEGRADED = {"on": False, "reason": ""}
            return d

        # ---- 解析失败: 数据文件已损坏 ----
        _DEGRADED = {"on": True, "reason": "data.json 内容不是合法账本(损坏或被截断)"}
        print(f"  [数据保护] {_DEGRADED['reason']}; 原文件已保留, 正在尝试从备份恢复…", flush=True)
        bak = _bak_path()
        if bak.is_file():
            try:
                bd = _read_json_lenient(bak.read_text(encoding="utf-8"))
                if isinstance(bd, dict) and "accounts" in bd:
                    # 把损坏文件另存为证据, 再用备份顶上
                    try:
                        _atomic_write(DATA_PATH.with_name(
                            f"data.json.损坏-{time.strftime('%Y%m%d-%H%M%S')}"), raw)
                    except Exception:
                        pass
                    _atomic_write(DATA_PATH, json.dumps(bd, ensure_ascii=False, indent=1))
                    _DEGRADED = {"on": False, "reason": ""}
                    print("  [数据保护] 已从 data.json.bak 自动恢复 ✅", flush=True)
                    return bd
            except Exception as e:
                print(f"  [数据保护] 备份恢复失败: {e}", flush=True)
        print("  [数据保护] 无可用的自动备份。为防覆盖损坏数据, 本次运行将【拒绝写入】;"
              f"数据目录是 {DATA_HOME}, 请手动从 备份/ 目录还原 data.json 到该目录后重启。", flush=True)
        return json.loads(json.dumps(_EMPTY))


def _bak_path():
    return DATA_PATH.with_name("data.json.bak")


def _save(d):
    """原子写: 先滚动 .bak, 再写临时文件, 最后 os.replace 原子替换。
    降级状态下直接拒绝写盘(宁可本次操作失败, 也不让空结构覆盖真实数据)。"""
    if _DEGRADED.get("on"):
        raise RuntimeError(
            f"数据文件异常({_DEGRADED.get('reason') or '未知'}), 已拒绝写入以防覆盖。"
            "请从 备份/ 目录还原 data.json 后重启程序。")
    with _DATA_TX:
        payload = json.dumps(d, ensure_ascii=False, indent=1)
        # ① 滚动备份: 仅当当前文件是合法账本时才留作 .bak(避免把损坏内容当备份)
        #    ★ 防呆: 若"当前文件"是空账本、而 .bak 里还有真数据, 就不滚动 ——
        #      否则一次异常的清空会顺着滚动把唯一的真备份也抹掉, 数据再也找不回。
        try:
            if DATA_PATH.is_file():
                cur = DATA_PATH.read_text(encoding="utf-8")
                cur_d = _read_json_lenient(cur)
                if isinstance(cur_d, dict):
                    keep_bak = True
                    if _looks_empty(cur_d):
                        try:
                            if _bak_path().is_file():
                                old = _read_json_lenient(_bak_path().read_text(encoding="utf-8"))
                                if isinstance(old, dict) and not _looks_empty(old):
                                    keep_bak = False   # 拒绝用空账本覆盖有价值的备份
                        except Exception:
                            pass
                    if keep_bak:
                        _atomic_write(_bak_path(), cur)
        except Exception:
            pass
        # ② 写临时文件 + 原子替换
        _atomic_write(DATA_PATH, payload)


def _now():
    return datetime.now().isoformat(timespec="seconds")

# ---------------------------------------------------------------- 业务逻辑(与云端版一致)
def derive_status(a):
    if a.get("apiType") == "sub":
        vu = a.get("validUntil")
        if vu:
            try:
                if datetime.fromisoformat(vu) < datetime.now():
                    return "danger", "订阅已到期"
            except ValueError:
                pass
        qp = a.get("quotaPercent")
        if qp is None:
            return "success", "正常"
        if qp <= 10:
            return "danger", "配额即将用尽"
        if qp <= 30:
            return "warning", "配额消耗偏快"
        return "success", "正常"
    rem = float(a.get("remaining", 0)); used = float(a.get("monthlyUsed", 0))
    tot = float(a.get("totalRecharge", 0))
    pct = used / tot * 100 if tot > 0 else 0
    if rem <= 0 and tot > 0:
        return "danger", "余额已用尽"
    if (tot > 0 and rem <= tot * 0.1) or pct >= 90:
        return "danger", "余额不足"
    if pct >= 70:
        return "warning", "消耗偏快"
    return "success", "正常"

def normalize(d):
    d = dict(d)
    if d.get("apiType") == "sub":
        d["remaining"] = 0; d["monthlyUsed"] = 0; d["totalRecharge"] = 0
    else:
        d["apiType"] = "payg"; d["quotaPercent"] = None; d["monthlyFee"] = 0
        d["resetCycle"] = None; d["resetDay"] = None      # 重置周期只对订阅有意义
        d["subSince"] = None                              # 订阅开始日只对订阅有意义
    # 多币种(V3.19.0): 非法币种一律回落 CNY, 防止脏数据把换算系数变成 0
    d["currency"] = _norm_currency(d.get("currency"))
    return d


# ---------------------------------------------------------------- 同号去重(V3.34.1)
def _identity_key(a):
    """同一真实账户的身份键 = 平台 + 账户名称 + 电话(两字段都填了才算)。
    任何一项留空都视为「无法判定重复」, 返回 None(不参与去重)。"""
    cn = str(a.get("customName") or "").strip().lower()
    ph = "".join(ch for ch in str(a.get("phone") or "") if ch.isdigit())
    if not cn or not ph:
        return None
    return (str(a.get("platform") or "").strip().lower(), cn, ph)


def _identity_counts(accounts):
    """每个身份键出现次数(>1 即为疑似同一真实账户重复登记)。"""
    counts = {}
    for a in accounts:
        k = _identity_key(a)
        if k:
            counts[k] = counts.get(k, 0) + 1
    return counts


def _dedup_sum(accounts, amount_fn):
    """按身份键去重后求和: 同一身份键的多个账号只计一次(取最先出现的)。"""
    seen, total = set(), 0.0
    for a in accounts:
        k = _identity_key(a)
        if k is not None:
            if k in seen:
                continue
            seen.add(k)
        total += float(amount_fn(a) or 0)
    return total




DEFAULTS = {"platform": "custom", "name": "", "groupName": "默认", "apiType": "payg",
            "remaining": 0, "quotaPercent": None, "monthlyUsed": 0, "monthlyFee": 0,
            "totalRecharge": 0, "budget": 0, "collectMethod": "手动录入",
            "note": None, "validUntil": None,
            # V3.34.1: 同号去重的身份字段(选填) —— 用户在该平台的自定义名称 + 电话,
            # 平台+两字段完全相同视为同一真实账户, 累计充值/总投入只计一次
            "customName": "", "phone": "",
            # 订阅配额重置周期(仅 sub 有意义): weekly=每周 / monthly=每月; resetDay: 周=0-6(周一0) 月=1-31
            "resetCycle": None, "resetDay": None,
            # 订阅开始日(仅 sub 有意义, YYYY-MM-DD): 用于算「已计费周期数」。空=用最早快照日兜底
            "subSince": None,
            # 账号原币(V3.19.0): 存储永远原币, 换算只在展示层。老账号缺省 CNY
            "currency": "CNY",
            # 在线订阅手动额度窗口(V3.21.0): 存「剩余%」0-100, 只展示不计费
            "win5h": None, "win7d": None, "winUpdatedAt": None,
            }

# 在线订阅(V3.21.0): 官网买的消费级会员, 也是模型成本的一部分。
# 边界: 只收「能调模型的钱」(ChatGPT/Claude/Gemini), 视频/网盘/音乐会员一律不收。
ONLINE_SUB_PLATFORMS = {"chatgpt", "claude_sub", "gemini_sub"}


def _acct_kind(a):
    """账目分类: payg=按量 / sub=订阅API(有配额接口) / online=在线会员(消费级订阅)。"""
    if (a.get("apiType") or "payg") != "sub":
        return "payg"
    return "online" if (a.get("platform") or "") in ONLINE_SUB_PLATFORMS else "sub"

# ---------------------------------------------------------------- 多币种(V3.19.0)
# 原则: 存储永远原币, 换算只在展示层。
# 额度监控是「量级参考」工具, 不是记账软件 → 内置静态汇率足够; 在线刷新留到 P3 可选项,
# 且必须能失败回退到这张表(不能因为汇率接口挂了就打不开页面)。
FX_RATES = {"CNY": 1.0, "USD": 7.10, "EUR": 7.72, "JPY": 0.0475, "HKD": 0.912, "GBP": 9.05}
FX_SYMBOLS = {"CNY": "¥", "USD": "$", "EUR": "€", "JPY": "¥", "HKD": "HK$", "GBP": "£"}
FX_UPDATED = "2026-10-30"


def _norm_currency(cur):
    """币种归一: 非法/空值一律按 CNY, 防止脏数据把汇率算成 0。"""
    c = str(cur or "CNY").strip().upper()
    return c if c in FX_RATES else "CNY"


def _disp_currency(d=None):
    """当前「显示货币」设置(默认 CNY)。"""
    try:
        s = (d if d is not None else _load()).get("settings") or {}
    except Exception:
        s = {}
    return _norm_currency(s.get("displayCurrency") or "CNY")


def _fx_rate(cur, disp="CNY"):
    """原币 -> 显示币 的换算系数(同币种恒为 1)。"""
    c, t = _norm_currency(cur), _norm_currency(disp)
    return FX_RATES[c] / FX_RATES[t]


def _fx(amount, cur, disp="CNY"):
    """把原币金额换算成显示币金额; 任何异常都退回 0, 不让页面整体崩。"""
    try:
        return float(amount or 0.0) * _fx_rate(cur, disp)
    except (TypeError, ValueError):
        return 0.0


# 开源版(V3.34.0): 无授权门禁, 所有功能永久开放。
def _license_state(force=False):
    return {"state": "open", "source": "open", "exp": None, "daysLeft": None,
            "name": "", "plan": "open-source", "sub": "", "trialEnd": None,
            "stacked": False, "readonly": False, "graceDays": 0, "trialDays": 0}


# ---------------------------------------------------------------- 更新包签名校验
# 开源版说明: 这里只做「更新包完整性验签」(防篡改), 与旧版的授权激活无关。
# 若你 fork 本项目并自行分发更新包, 换成你自己的 Ed25519 公钥即可
# (生成: python tools/ed25519_ref.py 有自测; 私钥务必离线保存)。
PACK_PUBKEY_HEX = "f2bedee8a735686caa0feb7aff57d26ee4c1228d4c23175948f81c3b0a146309"

# --- Ed25519 验签(仅 verify; 与 tools/ed25519_ref.py 同源的纯实现) ---
_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_inv(x):
    return pow(x, _ED_P - 2, _ED_P)


def _ed_xrecover(y):
    xx = (y * y - 1) * _ed_inv(_ED_D * y * y + 1)
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = (x * _ED_I) % _ED_P
    if x % 2 != 0:
        x = _ED_P - x
    return x


_ED_By = 4 * _ed_inv(5) % _ED_P
_ED_B = (_ed_xrecover(_ED_By) % _ED_P, _ED_By, 1, (_ed_xrecover(_ED_By) * _ED_By) % _ED_P)


def _ed_add(p, q):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _ED_P
    b = (y1 + x1) * (y2 + x2) % _ED_P
    c = t1 * 2 * _ED_D * t2 % _ED_P
    dd = z1 * 2 * z2 % _ED_P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_mul(p, e):
    q = (0, 1, 1, 0)
    while e > 0:
        if e & 1:
            q = _ed_add(q, p)
        p = _ed_add(p, p)
        e >>= 1
    return q


def _ed_encode(p):
    x, y, z, _t = p
    zi = _ed_inv(z)
    x, y = x * zi % _ED_P, y * zi % _ED_P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _ed_decode(s):
    if len(s) != 32:
        raise ValueError("bad point")
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _ed_xrecover(y)
    if (x & 1) != sign:
        x = _ED_P - x
    return (x, y, 1, x * y % _ED_P)


def _ed_verify(pk: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pk) != 32 or len(sig) != 64:
        return False
    try:
        A = _ed_decode(pk)
        R = _ed_decode(sig[:32])
        S = int.from_bytes(sig[32:], "little")
        if S >= _ED_L:
            return False
        k = int.from_bytes(hashlib.sha512(sig[:32] + pk + msg).digest(), "little") % _ED_L
        return _ed_encode(_ed_mul(_ED_B, S)) == _ed_encode(_ed_add(R, _ed_mul(A, k)))
    except Exception:
        return False


def _b64d(s):
    s = str(s or "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# 账本列表缓存(2026-10-25 性能优化): data.json 未变则复用上次结果
# 背景: 快照累积到 2.7 万条后, 每次打开页面都要按账号重算一遍(约 25 万次循环) → 首屏 4 秒
# 失效条件: data.json 的 mtime/大小变化(任何写盘都会变) 或超过 60 秒
_LIST_CACHE = {"key": None, "ts": 0.0, "data": None}
_LIST_LOCK = threading.Lock()
_LIST_TTL = 60.0


def _data_stamp():
    try:
        st = DATA_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def proc_list(_inp):
    stamp = _data_stamp()
    now = time.time()
    with _LIST_LOCK:
        if (_LIST_CACHE["data"] is not None and _LIST_CACHE["key"] == stamp
                and now - _LIST_CACHE["ts"] < _LIST_TTL):
            return _LIST_CACHE["data"]
    d = _load()
    # 账目口径只算一次, 供所有订阅账号共享「本月摊销」(避免每个账号都重算一遍 _ledger_data)
    led = _ledger_data(d)
    cur_ym = datetime.now().strftime("%Y-%m")
    cur = next((m for m in led.get("months", []) if m["month"] == cur_ym), None)
    cur_amt = {x["accountId"]: float(x.get("amount") or 0.0)
               for x in (cur or {}).get("detail", [])}
    _out = [_account_view(d, a, cur_amt)
            for a in sorted(d["accounts"], key=lambda a: a["id"])]
    with _LIST_LOCK:
        _LIST_CACHE["key"], _LIST_CACHE["ts"], _LIST_CACHE["data"] = stamp, time.time(), _out
    return _out


# ---------------------------------------------------------------- 快照索引(2026-10-25 方案 B)
# 背景: _sub_cycle_used 对「每个计费周期」都要全量遍历 2.7 万条快照,
#       minimax(5 小时周期) 单账号就要 1.76 秒, 整页 3.8 秒。
# 做法: 同一份账本只建一次「按账号分组 + 时间升序」索引, 用二分切片取周期内的值。
# 缓存键用对象身份(is): 每次 _load() 都是新 dict, 天然按请求隔离, 不会读到过期数据。
_SNAPIDX = {"d": None, "idx": None}
_SNAPIDX_LOCK = threading.Lock()


def _snap_index(d):
    """{accountId: (times[], values[])}，times 升序。同一个 d 对象只构建一次。"""
    if _SNAPIDX["d"] is d and _SNAPIDX["idx"] is not None:
        return _SNAPIDX["idx"]
    tmp = {}
    for s in (d.get("snapshots") or []):
        try:
            t = datetime.fromisoformat(str(s.get("collectedAt", "")))
            v = float(s.get("remaining"))
        except (TypeError, ValueError):
            continue
        tmp.setdefault(s.get("accountId"), []).append((t, v))
    idx = {}
    for aid, arr in tmp.items():
        arr.sort(key=lambda x: x[0])
        idx[aid] = ([p[0] for p in arr], [p[1] for p in arr])
    with _SNAPIDX_LOCK:
        _SNAPIDX["d"], _SNAPIDX["idx"] = d, idx
    return idx


def _sub_since(d, a):
    """订阅开始日: 手填 subSince 优先; 否则用该账号最早快照日; 再退 createdAt; 最后退今天。"""
    raw = str(a.get("subSince") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw[:10])
        except ValueError:
            pass
    _times = _snap_index(d).get(a.get("id"), ([], []))[0]
    if _times:
        return _times[0]          # 索引已按时间升序，第一个即最早
    try:
        return datetime.fromisoformat(str(a.get("createdAt") or ""))
    except ValueError:
        return datetime.now()


def _sub_billed_periods(since, cycle, today=None):
    """已计费周期数(含当前周期): 月度=跨自然月数(含当月); 周度=经过的整周数+1(含当周)。"""
    today = today or datetime.now()
    if since > today:
        return 1
    if cycle == "weekly":
        return (today - since).days // 7 + 1
    # 月度 / interval5h(按5小时窗口计量但按月付费): 按自然月数计
    return (today.year - since.year) * 12 + (today.month - since.month) + 1


def _sub_total_recharge(a, cycle, periods):
    """订阅累计充值(实际已付的钱):
    年付(套餐截止日 validUntil 距今 >90 天, 说明一次性买断) = 月费×12;
    月付滚动(无截止日/截止日临近) = 已计费周期数 × 周期费(付了几个月算几个月)。
    """
    fee = float(a.get("monthlyFee") or 0)
    period_fee = fee / 4.0 if cycle == "weekly" else fee
    vu = str(a.get("validUntil") or "").strip()
    if vu:
        try:
            vu_dt = datetime.fromisoformat(vu[:10])
            if (vu_dt - datetime.now()).days > 90:
                return round(fee * 12, 2)  # 年付: 一次性付清全年
        except ValueError:
            pass
    return round(periods * period_fee, 2)  # 月付滚动


def _sub_time_costs(d, a):
    """订阅按天摊销的成本(V3.17.37 唯一定稿口径)。

    用户拍板: 订阅月费固定, 金额只跟时间走, 与配额窗口完全解绑。
    返回 (total_used, month_used, per_day):
      total_used  累计消耗 = Σ各月摊销(从订阅日起至今天)
      month_used  本月消耗 = 当月摊销(月费×本月在订天数/当月总天数)
      per_day     [(date, 金额)] 逐日明细, 供趋势图/账目按天归月
    """
    fee = float(a.get("monthlyFee") or 0)
    if fee <= 0:
        return 0.0, 0.0, []
    since = _sub_since(d, a)
    alloc = _sub_month_alloc(since, fee)
    total_used = round(sum(amt for _, amt, _ in alloc), 2)
    cur_ym = datetime.now().strftime("%Y-%m")
    month_used = round(sum(amt for ym, amt, _ in alloc if ym == cur_ym), 2)
    per_day = []
    for ym, amt, days in alloc:
        y, m = int(ym[:4]), int(ym[5:7])
        dim = _days_in_month(y, m)
        daily = fee / dim
        today0 = datetime(datetime.now().year, datetime.now().month, datetime.now().day)
        # 摊入的每一天: 从 max(订阅日, 月初) 到 min(今天, 月末)
        m_start = datetime(y, m, 1)
        start = since if (since.year, since.month) == (y, m) else m_start
        if start < m_start:
            start = m_start
        for day_off in range(dim):
            t = m_start + timedelta(days=day_off)
            if t < start or t > today0:
                continue
            per_day.append((t.strftime("%Y-%m-%d"), round(daily, 6)))
    return total_used, month_used, per_day


def _account_view(d, a, cur_amt):
    """给账号附加界面要用的计算口径字段(不改动原字段, 只新增)。

    新增:
      monthUsedReal   本月真实消耗(元): 按量=自然月1号起余额快照下降额累加;
                      订阅=本月按天摊销(与「账目统计」页当前月明细同口径)
      totalRechargeShow 累计充值展示(元): 按量=手填累计充值; 订阅=年付月费×12 / 月付=已计费月数×月费
      totalUsed       累计消耗(元): 按量=累计充值-剩余余额;
                      订阅=月费按天摊销累计(V3.17.37 起不再绑定配额%)
      resetCycle/resetDay/resetLabel/nextReset  订阅配额重置周期(自动识别/手动)
    """
    out = dict(a)
    # 多币种(V3.19.0): 所有金额字段保持「原币」不变(账本要存原币),
    # 前端用 currency + fxRate 自己换算展示; 汇总类接口由后端换算后给显示币金额。
    out["currency"] = _norm_currency(a.get("currency"))
    out["fxRate"] = round(_fx_rate(out["currency"], _disp_currency(d)), 6)
    if a.get("apiType") == "sub":
        fee = float(a.get("monthlyFee") or 0)
        info = _sub_reset_info(d, a)
        since = _sub_since(d, a)
        # 累计充值 = 实际已付(年付=月费×12; 月付=已计费月数×月费)
        periods = _sub_billed_periods(since, "monthly")
        out["totalRechargeShow"] = _sub_total_recharge(a, "monthly", periods)
        # 累计消耗 / 本月消耗 = 月费按天摊销(V3.17.37: 不再绑定配额%)
        total_used, month_used, _per_day = _sub_time_costs(d, a)
        out["totalUsed"] = total_used
        out["monthUsedFee"] = month_used
        out["billedPeriods"] = periods
        # 当前余额 = 已付 − 已用(按时间摊销)
        paid = out["totalRechargeShow"]
        out["remainingMoney"] = round(max(0.0, paid - total_used), 2)
        out["monthUsedReal"] = round(float(cur_amt.get(a.get("id"), 0.0)), 2)
        out["resetCycle"] = info.get("cycle")
        out["resetDay"] = info.get("day")
        out["resetAuto"] = info.get("auto")
        out["resetLabel"] = info.get("label")
        out["nextReset"] = _next_reset_date(info)
        # 计费周期口径(供界面标明是周还是月, 避免跨平台数字误读)
        kind = _sub_cycle_kind(a)
        out["subCycleKind"] = kind
        if kind == "interval5h":
            out["subCycleLabel"] = "月付(5小时窗)"
            out["subQuotaTag"] = "5h折月"
        elif kind == "weekly":
            out["subCycleLabel"] = "周额度(7天)"
            out["subQuotaTag"] = "周"
        else:
            out["subCycleLabel"] = "月度额度"
            out["subQuotaTag"] = "月"
    else:
        tot = float(a.get("totalRecharge") or 0)
        rem = float(a.get("remaining") or 0)
        out["totalRechargeShow"] = round(tot, 2)
        out["totalUsed"] = round(tot - rem, 2)
        out["remainingMoney"] = round(rem, 2)
        # 本月真实消耗: 自然月1号起, 余额快照「下降额」累加(与账目统计同口径)
        out["monthUsedReal"] = round(_month_used_from_snapshots(d, a["id"]), 2)
    # 同号去重标记(V3.34.1): 同平台另一账号的身份键相同(账户名称+电话都填且一致)
    ik = _identity_key(a)
    out["dupIdentity"] = bool(ik) and _identity_counts(d.get("accounts", [])).get(ik, 0) > 1
    return out


def _upsert_one(d, inp):
    data = normalize({**DEFAULTS, **{k: v for k, v in inp.items() if k in DEFAULTS}})
    st, stx = derive_status(data)
    data["status"], data["statusText"] = st, stx
    data["lastCollectAt"] = _now()
    snap_val = (data["quotaPercent"] if data["quotaPercent"] is not None else 100) if data["apiType"] == "sub" else data["remaining"]
    if inp.get("id"):
        for i, a in enumerate(d["accounts"]):
            if a["id"] == inp["id"]:
                data["id"] = a["id"]; data["createdAt"] = a.get("createdAt", _now())
                d["accounts"][i] = data
                return a["id"], None
        raise RuntimeError("账号不存在")
    nid = d["nextId"]; d["nextId"] += 1
    data["id"] = nid; data["createdAt"] = _now()
    d["accounts"].append(data)
    return nid, snap_val

def proc_upsert(inp):
    with _DATA_TX:                      # 读-改-写整体持锁: 防并发覆盖丢数据
        d = _load()
        nid, snap_val = _upsert_one(d, inp)
        if snap_val is not None:
            d["snapshots"].append({"accountId": nid, "remaining": snap_val, "collectedAt": _now()})
        _save(d)
    return {"id": nid}

def proc_remove(inp):
    """删除账号 —— 硬删除(一刀 2026-10-25 要求: 删掉的账号内容不保存, 直接删除)

    与旧版「软删除」的区别:
      旧版保留快照 + deletedAccounts 归档, 以便账目仍能反映历史花销;
      新版把账号、它名下的所有快照、以及 deletedAccounts 里的归档记录一并清除。
    注意: 该账号的历史消耗将不再计入账目统计。
    """
    with _DATA_TX:
        d = _load()
        tid = inp["id"]
        removed = next((a for a in d["accounts"] if a["id"] == tid), None)
        d["accounts"] = [a for a in d["accounts"] if a["id"] != tid]
        before_n = len(d.get("snapshots", []))
        d["snapshots"] = [s for s in d.get("snapshots", []) if s.get("accountId") != tid]
        dropped = before_n - len(d["snapshots"])
        arch = d.get("deletedAccounts") or {}
        arch.pop(str(tid), None)
        d["deletedAccounts"] = arch
        _save(d)
    # 联动删除代理配置里的同名账号, 防止自动同步把已删账号又带回来
    if removed:
        _remove_from_proxy_config(removed.get("platform"), removed.get("name"))
    return {"ok": True, "removedSnapshots": dropped}

def _remove_from_proxy_config(platform, name):
    """网页删除账号时, 同步从 accounts_config.json 移除同名同平台账号"""
    try:
        mod = _load_proxy_module()
        if not mod:
            return
        cfg = mod.load_config()
        before = len(cfg.get("accounts", []))
        cfg["accounts"] = [a for a in cfg.get("accounts", [])
                           if not (a.get("platform") == platform and a.get("name") == name)]
        if len(cfg["accounts"]) < before:
            mod.save_config(cfg)
            print(f"  [联动删除] 已从代理配置移除: {name}", flush=True)
    except Exception as e:
        print(f"  [联动删除] 配置清理失败({e}), 仅网页端删除", flush=True)

def proc_collect(inp):
    with _DATA_TX:
        d = _load()
        for a in d["accounts"]:
            if a["id"] == inp["id"]:
                val = float(inp["value"])
                if a.get("apiType") == "sub":
                    a["quotaPercent"] = max(0, min(100, val))
                else:
                    a["remaining"] = val
                if inp.get("note"):
                    a["note"] = inp["note"]
                st, stx = derive_status(a)
                a["status"], a["statusText"] = st, stx
                a["lastCollectAt"] = _now()
                d["snapshots"].append({"accountId": a["id"], "remaining": val, "collectedAt": _now()})
                _save(d)
                return {"ok": True, "status": st, "statusText": stx}
        raise RuntimeError("账号不存在")

def _month_used_from_snapshots(d, account_id):
    """本月真实消耗 = 「相邻两次快照余额下降额」中, 区间中点落在本自然月的部分累加。

    口径与 账目统计(_ledger_data) 完全一致 —— 全量快照排序后逐对比较,
    按区间中点归月, 只累加中点在本月的下降额。
    旧版只取本月快照逐对比较, 会丢掉「上月末→本月初」跨月区间的消耗
    (该区间在本月快照里没有前驱点); 也保证上月消耗绝不混入本月。
    """
    month_prefix = datetime.now().strftime("%Y-%m")
    _times, _vals = _snap_index(d).get(account_id, ([], []))
    total = 0.0
    for i in range(1, len(_times)):
        t0, t1 = _times[i - 1], _times[i]
        delta = _vals[i] - _vals[i - 1]
        if delta >= 0:
            continue  # 余额不变/上升(充值) 不计消耗
        mid = t0 + (t1 - t0) / 2
        if mid.strftime("%Y-%m") == month_prefix:
            total += -delta
    return round(total, 4)


def proc_import_all(inp):
    with _DATA_TX:
        d = _load()
        added = 0
        for raw in inp.get("items", []):
            data = normalize({**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}})
            st, stx = derive_status(data)
            data["status"], data["statusText"] = st, stx
            data["lastCollectAt"] = _now()
            exist = next((a for a in d["accounts"] if a["platform"] == data["platform"] and a["name"] == data["name"]), None)
            # 采集失败(live=False 且 note 是回退口径)时: 不覆盖最后一次真实配额/余额,
            # 只保留 note 提示 —— 防止偶发失败把真实值(如 74%)冲成 0 造成"配额用尽"误报
            collect_failed = ("查询失败" in (data.get("note") or "")) or ("无法连接" in (data.get("note") or ""))
            if collect_failed and exist is not None:
                exist["note"] = data.get("note") or exist.get("note")
                exist["lastCollectAt"] = data["lastCollectAt"]
                exist["collectMethod"] = data.get("collectMethod") or exist.get("collectMethod")
                continue  # 不写快照、不动 quotaPercent/remaining, 下个周期重试
            snap_val = (data["quotaPercent"] if data["quotaPercent"] is not None else 100) if data["apiType"] == "sub" else data["remaining"]
            if exist:
                data["id"] = exist["id"]; data["createdAt"] = exist.get("createdAt", _now())
                # 用户手动设置的字段不被同步覆盖(预算/分组/累计充值/月费/到期日/
                # 订阅开始日/重置周期与重置日 —— 代理不掌握这些, 同步必须原样保留)
                data["budget"] = exist.get("budget", 0)
                data["groupName"] = exist.get("groupName", data["groupName"])
                # 币种由用户决定(界面上可改), 代理同步不得覆盖; 老账号缺失时用同步值
                if exist.get("currency"):
                    data["currency"] = exist["currency"]
                if exist.get("totalRecharge"):
                    data["totalRecharge"] = exist["totalRecharge"]
                # 同号去重身份字段(V3.34.1): 代理不掌握, 同步必须原样保留
                data["customName"] = exist.get("customName") or ""
                data["phone"] = exist.get("phone") or ""
                if exist.get("monthlyFee"):
                    data["monthlyFee"] = exist["monthlyFee"]
                if exist.get("validUntil"):
                    data["validUntil"] = exist["validUntil"]
                if exist.get("subSince"):
                    data["subSince"] = exist["subSince"]
                if exist.get("resetCycle"):
                    data["resetCycle"] = exist["resetCycle"]
                    data["resetDay"] = exist.get("resetDay")
                elif exist.get("resetDay") is not None and data.get("resetDay") is None:
                    data["resetDay"] = exist["resetDay"]
                # 本月消耗: 平台接口没给(=0)时, 用系统快照自动计算(真实值)
                if data.get("apiType") != "sub" and not data.get("monthlyUsed"):
                    data["monthlyUsed"] = _month_used_from_snapshots(d, exist["id"])
                # 手动录入余额的平台(MiniMax 按量/智谱按量): 官方接口拿不到自动余额,
                # 代理每次同步都会回传一个「基于配置推算的值」, 把用户手填的余额冲掉。
                # 这里强制保留用户手填值, 只更新采集时间等元信息。
                manual_balance_plat = data.get("platform") in ("minimax", "glm", "chatanywhere") and data.get("apiType") == "payg"
                if manual_balance_plat:
                    data["remaining"] = exist.get("remaining", data.get("remaining", 0))
                    # note 也保留用户手填的「上次记录 ¥X(时间)」提示, 不被代理的通用文案覆盖
                    if exist.get("note") and "上次记录" in exist["note"]:
                        data["note"] = exist["note"]
                d["accounts"][d["accounts"].index(exist)] = data
                aid = exist["id"]
            else:
                aid = d["nextId"]; d["nextId"] += 1
                data["id"] = aid; data["createdAt"] = _now()
                d["accounts"].append(data)
                added += 1
            # 手动录入余额的平台: 快照也用手填值, 别让代理推算值进账本
            if data.get("platform") in ("minimax", "glm", "chatanywhere") and data.get("apiType") == "payg" and exist is not None:
                snap_val = float(exist.get("remaining", snap_val))
            d["snapshots"].append({"accountId": aid, "remaining": snap_val, "collectedAt": _now()})
        _save(d)
    return {"ok": True, "added": added, "total": len(inp.get("items", []))}

def month_cost(a):
    """【已废弃 · 仅保留兼容】订阅成本旧口径(月费×已用配额%)。
    V3.17.12/13 起订阅金额一律按时间摊销(_sub_month_alloc / monthUsedFee),
    不再用配额%折算 —— 新代码请勿调用本函数。"""
    if a.get("apiType") == "sub":
        fee = float(a.get("monthlyFee") or 0)
        qp = a.get("quotaPercent")
        if not fee or qp is None:
            return 0.0
        used_pct = max(0.0, min(100.0, 100.0 - float(qp)))
        return round(fee * used_pct / 100.0, 2)
    return float(a.get("monthlyUsed") or 0)


def sub_used_pct(a):
    """【已废弃 · 仅保留兼容】订阅「已用配额%」旧口径(基于 5 小时/7 天窗口)。
    V3.17.13 起界面统一用月窗口(monthWindowPct), 本函数不再被调用。"""
    qp = a.get("quotaPercent")
    if qp is None:
        return None
    return max(0.0, min(100.0, 100.0 - float(qp)))


def proc_summary(_inp):
    """消耗总览。

    「本月总消耗」与各平台「本月消耗」= 账目统计(_ledger_data)的同口径结果
    (快照相邻下降额累加 + 订阅按在订天数折算)，保证总览页与账目统计页数字一致。
    """
    d = _load()
    lst = d["accounts"]
    disp = _disp_currency(d)
    led = _ledger_data(d)
    cur_ym = datetime.now().strftime("%Y-%m")
    cur_month = next((m for m in led.get("months", []) if m["month"] == cur_ym), None)
    cur_plat = (cur_month or {}).get("byPlatform", {}) or {}
    month_total = float((cur_month or {}).get("total", 0.0))
    # 当前余额(元, V3.17.10): 按量=余额; 订阅=已付−已用(月付=周期费×剩余%, 年付=年费−已用
    # 含未来周期价值, 与总投入口径对齐) —— 与「账目总计」当前余额完全同源(_account_view)
    remaining_total = 0.0
    for a in lst:
        try:
            remaining_total += _fx(_account_view(d, a, {}).get("remainingMoney") or 0.0,
                                   a.get("currency"), disp)
        except Exception:
            if a.get("apiType") != "sub":
                remaining_total += _fx(a.get("remaining") or 0, a.get("currency"), disp)
    # 按量账号累计充值合计(总投入额度的参考值之一) —— 统一换成显示币
    # V3.34.1: 同号去重(平台+账户名称+电话完全一致只计一次)
    recharge_total = _dedup_sum(
        [a for a in lst if a.get("apiType") != "sub"],
        lambda a: _fx(a.get("totalRecharge") or 0, a.get("currency"), disp))
    by_plat = {}
    for a in lst:
        cur = by_plat.setdefault(a["platform"], {
            "platform": a["platform"], "used": 0, "budget": 0, "remaining": 0,
            "subQuota": None, "subUsedPct": None, "subCount": 0, "subFee": 0,
            "paygUsed": 0, "subCost": 0, "subCycleKind": None, "subQuotaLabel": "",
        })
        cur["budget"] += _fx(a.get("budget") or 0, a.get("currency"), disp)
        if a.get("apiType") == "sub":
            cur["subCount"] += 1
            cur["subFee"] += _fx(a.get("monthlyFee") or 0, a.get("currency"), disp)
            # V3.17.37 起: 订阅金额只按天摊销, 与配额窗口完全解绑 ——
            #   subUsedPct = 金额摊销进度(本月已摊金额/月费, 按天累加)
            #   subQuota   = 真实配额剩余%(仅展示参考; 额度窗口请看总览面板)
            v = _account_view(d, a, {})
            fee = float(a.get("monthlyFee") or 0)
            used_fee = float(v.get("monthUsedFee") or 0)
            cur["subCost"] += _fx(used_fee, a.get("currency"), disp)
            # 周期口径(周/月) —— 同一平台多个订阅账号时以第一个为准
            kind = _sub_cycle_kind(a)
            cur["subCycleKind"] = kind
            cur["subQuotaLabel"] = ("月付(不限量)" if kind == "flat"
                                    else ("周额度(7天)" if kind == "weekly" else "月度额度"))
            cur["subQuotaTag"] = "月" if kind in ("monthly", "flat") else "周"
            if fee > 0:
                cur["_up"] = cur.get("_up", []) + [
                    max(0.0, min(100.0, used_fee / fee * 100.0))]
            qp = a.get("quotaPercent")
            if qp is not None:
                cur["_q"] = cur.get("_q", []) + [float(qp)]
        else:
            cur["remaining"] += _fx(a.get("remaining") or 0, a.get("currency"), disp)
            cur["paygUsed"] += _fx(a.get("monthlyUsed") or 0, a.get("currency"), disp)
    for p, c in by_plat.items():
        ups = c.pop("_up", [])
        if ups:
            c["subUsedPct"] = round(sum(ups) / len(ups), 1)     # 金额摊销进度
        qs = c.pop("_q", [])
        if qs:
            c["subQuota"] = round(sum(qs) / len(qs), 1)         # 真实月度额度剩余%
        # 本月消耗改用账目统计口径(与「账目统计」页完全一致)
        c["used"] = round(float(cur_plat.get(p, 0.0)), 2)
    alerts = [{"id": a["id"], "level": a.get("status") or "success", "platform": a.get("platform") or "custom",
               "name": a.get("name") or "?", "title": f"{a.get('name') or '?'} 额度预警",
               "desc": a.get("statusText") or ""}
              for a in lst if (a.get("status") or "success") != "success"]
    # 订阅配额总览(用于占比区展示订阅口径) —— V3.17.13 起统一月窗口口径
    sub_list = []
    for a in lst:
        if a.get("apiType") == "sub":
            v = _account_view(d, a, {})
            qp = a.get("quotaPercent")
            sub_list.append({"name": a["name"], "platform": a["platform"],
                             "usedPct": (None if qp is None else round(max(0.0, 100.0 - float(qp)), 1)),
                             "fee": round(_fx(a.get("monthlyFee") or 0, a.get("currency"), disp), 2),
                             "cost": round(_fx(v.get("monthUsedFee") or 0, a.get("currency"), disp), 2)})
    # 在线订阅(V3.21.0): 消费级会员也进成本账本, 三视图(按量/订阅/在线订阅)齐了
    by_cat = {
        "payg": round(float((cur_month or {}).get("paygTotal", 0.0)), 2),
        "sub": round(float((cur_month or {}).get("subTotal", 0.0)), 2),
        "online": round(float((cur_month or {}).get("onlineTotal", 0.0)), 2),
    }
    online_list = []
    for a in lst:
        if _acct_kind(a) != "online":
            continue
        v = _account_view(d, a, {})
        online_list.append({
            "id": a.get("id"), "name": a.get("name") or "?",
            "platform": a.get("platform"), "currency": _norm_currency(a.get("currency")),
            "fxRate": v.get("fxRate"), "monthlyFee": float(a.get("monthlyFee") or 0),
            "monthCost": round(float(v.get("monthUsedFee") or 0), 2),
            "remainingMoney": round(float(v.get("remainingMoney") or 0), 2),
            "win5h": a.get("win5h"), "win7d": a.get("win7d"),
            "winUpdatedAt": a.get("winUpdatedAt"),
        })
    return {"monthTotal": round(month_total, 2), "remainingTotal": round(remaining_total, 2),
            "rechargeTotal": round(recharge_total, 2),
            "month": cur_ym, "displayCurrency": disp,
            "byCategory": by_cat, "onlineAccounts": online_list,
            "accountCount": len(lst), "alertCount": len(alerts),
            "byPlatform": list(by_plat.values()), "alerts": alerts, "subAccounts": sub_list}

def proc_trend(inp):
    """近 N 天消耗趋势。

    口径与「账目统计(quota.ledger)」保持一致: 消耗 = 相邻两次快照「余额下降额」累加,
    余额上升视为充值不计入; 每笔按区间中点归属到某一天。
    旧版用「同日首末快照差值」, 会把充值当天的消耗漏算、跨天断档算错 —— 已修正。
    """
    days = int((inp or {}).get("days", 30))
    d = _load()
    disp = _disp_currency(d)
    accs = {a["id"]: a for a in d["accounts"]}
    since = datetime.now() - timedelta(days=days)
    by_acc = {}
    for s in d.get("snapshots", []):
        try:
            t = datetime.fromisoformat(str(s.get("collectedAt", "")))
        except ValueError:
            continue
        by_acc.setdefault(s.get("accountId"), []).append((t, s.get("remaining")))

    daily_used = {}
    daily_quota = {}
    for aid, arr in by_acc.items():
        a = accs.get(aid)
        if not a:
            continue
        arr.sort(key=lambda x: x[0])
        if a.get("apiType") == "sub":
            # 订阅账号: 每日已消耗金额 = 月费按天摊销(V3.17.37 定稿口径),
            # 与「模型API管理」的本月消耗完全同源; 配额窗口只在总览面板展示, 不进趋势。
            _tu, _mu, per_day = _sub_time_costs(d, a)
            for dstr, amt in per_day:
                try:
                    t = datetime.fromisoformat(dstr)
                except ValueError:
                    continue
                if t < since:
                    continue
                dq = daily_quota.setdefault(dstr, {})
                dq[a["platform"]] = dq.get(a["platform"], 0.0) + _fx(amt, a.get("currency"), disp)
            continue
        for i in range(1, len(arr)):
            t0, r0 = arr[i - 1]
            t1, r1 = arr[i]
            try:
                delta = float(r1) - float(r0)
            except (TypeError, ValueError):
                continue
            if delta >= 0:
                continue                      # 充值/不变 → 不计消耗
            mid = t0 + (t1 - t0) / 2
            if mid < since:
                continue
            du = daily_used.setdefault(mid.date().isoformat(), {})
            du[a["platform"]] = du.get(a["platform"], 0.0) + _fx(-delta, a.get("currency"), disp)
    payg_plats = sorted({a["platform"] for a in accs.values() if a.get("apiType") != "sub"})
    sub_plats = sorted({a["platform"] for a in accs.values() if a.get("apiType") == "sub"})
    out, quota_out = [], []
    for i in range(days - 1, -1, -1):
        dstr = (datetime.now() - timedelta(days=i)).date().isoformat()
        row = {"date": dstr}
        pd = daily_used.get(dstr, {})
        for p in payg_plats:
            row[p] = round(pd.get(p, 0), 2)
        out.append(row)
        qrow = {"date": dstr}
        qd = daily_quota.get(dstr, {})
        for p in sub_plats:
            v = qd.get(p)
            qrow[p] = round(float(v), 2) if v is not None else None
        quota_out.append(qrow)
    return {"days": out, "platforms": payg_plats, "quotaDays": quota_out,
            "subPlatforms": sub_plats, "displayCurrency": disp}

def _days_in_month(y, m):
    nxt = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    return (nxt - datetime(y, m, 1)).days


def _sub_month_alloc(since, fee, today=None):
    """订阅按「月度」摊销的逐月明细: [(YYYY-MM, 金额, 在订天数)]。

    V3.17.37 起订阅金额唯一口径(用户拍板): 月费固定 → 只按天摊销。
      当月金额 = 月费 × 当月实际在订天数 / 当月总天数
    所有页面(账目统计/累计消耗/本月消耗/趋势图)共用本函数 —— 单一口径来源, 处处一致。
    配额窗口(5小时/7天/月)只在「总览 → 订阅额度窗口」面板展示, 不参与任何金额计算。
    """
    out = []
    fee = float(fee or 0)
    if fee <= 0:
        return out
    today = today or datetime.now()
    today0 = datetime(today.year, today.month, today.day)
    start = datetime(since.year, since.month, since.day)
    if start > today0:
        return out
    cur = datetime(start.year, start.month, 1)
    guard = 0
    while cur <= today0 and guard < 600:
        guard += 1
        y, m = cur.year, cur.month
        n_days = _days_in_month(y, m)
        m_start = datetime(y, m, 1)
        m_last = m_start + timedelta(days=n_days - 1)
        lo = max(start, m_start)
        hi = min(today0, m_last)
        active = (hi - lo).days + 1 if hi >= lo else 0
        if active > 0:
            out.append((f"{y:04d}-{m:02d}", round(fee * active / n_days, 6), active))
        cur = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    return out


_WEEK_CN = ["一", "二", "三", "四", "五", "六", "日"]


# ---- 订阅「按配额计费」(V3.17.16) -------------------------------------------
# 用户拍板: Kimi / 智谱 都按周额度计费, 方舟按月额度计费。
#   每个周期消耗 = 该周期已用配额% × 周期费(周=月费/4, 月=月费)
#   累计消耗 = 各周期累加(周重置不会清零); 本月消耗 = 各周期成本按天摊到本月。
def _sub_cycle_kind(a):
    """计费周期(= 套餐的付费周期):
      monthly —— 方舟(官方月度额度)、MiniMax(官方月付套餐, 只有5小时/周两个限流窗口, 没有月度配额)
      weekly  —— Kimi/智谱(官方周额度)
    ⚠️ 计费周期看的是「用户付多少钱」, 与「配额窗口是5小时还是周」是两件事:
       MiniMax 官方没有月度配额窗口, 但它是按月付费的, 所以金额按月摊。
    """
    plat = a.get("platform") or ""
    if plat == "minimax":
        # MiniMax 月付但只有 5 小时限流窗口(用完即重置, 周窗口不计量)。
        # 用户口径: 30天总费用固定 → 折算到每个 5 小时窗口的费用
        # (30天=144个5小时窗) → 按各窗口实际消耗累加成本。
        return "interval5h"
    return "monthly" if plat in ("doubao", "qwenplan") else "weekly"


def _sub_cycle_bounds(since, kind, now=None):
    """按计费周期切分 [(start, end)] —— 周期锚定订阅开始日。

    注意: 最后一个(当前)周期的 end 用「名义结束日」(周=+7天 / 月=下月同日),
    不是 now —— 否则按天摊到本月时会把不足一天的尾差算成多一天。
    interval5h(MiniMax): 官方窗口是整点起 5 小时一班(00/05/10/15/20),
    锚定当天 00:00 而不是 subSince, 与官方班次对齐。
    """
    now = now or datetime.now()
    starts = []
    if kind == "interval5h":
        cur = datetime(since.year, since.month, since.day)  # 从订阅日起, 但对齐整点班
    else:
        cur = datetime(since.year, since.month, since.day)
    guard = 0
    while cur <= now and guard < 2000:
        guard += 1
        starts.append(cur)
        if kind == "weekly":
            cur = cur + timedelta(days=7)
        elif kind == "interval5h":
            cur = cur + timedelta(hours=5)
        else:   # monthly 按自然月切
            y = cur.year + (1 if cur.month == 12 else 0)
            m = 1 if cur.month == 12 else cur.month + 1
            cur = datetime(y, m, min(cur.day, _days_in_month(y, m)))
    out = []
    for st in starts:
        if kind == "weekly":
            en = st + timedelta(days=7)
        elif kind == "interval5h":
            en = st + timedelta(hours=5)
        else:
            y = st.year + (1 if st.month == 12 else 0)
            m = 1 if st.month == 12 else st.month + 1
            en = datetime(y, m, min(st.day, _days_in_month(y, m)))
        out.append((st, en))
    return out


def _first_snapshot_ts(d, a):
    """该账号第一条快照时间(监控起点); 没有返回 None"""
    best = None
    for s in d.get("snapshots", []):
        if s.get("accountId") != a.get("id"):
            continue
        try:
            t = datetime.fromisoformat(str(s.get("collectedAt", "")))
        except (TypeError, ValueError):
            continue
        if best is None or t < best:
            best = t
    return best


def _sub_cycle_used(d, a, st, en, cur_pct, is_current):
    """该周期已用配额%: 有快照取「100-周期内最低剩余」, 当前周期用实时值。
    没有任何快照的历史周期返回 0 —— 那是「系统还没接入监控」的时间段,
    按 100% 计会把累计消耗算成天价(实测 MiniMax 月付59.5 却被按周摊成 463.5)。
    flat 档(月付不限量): 没有额度窗口可推算, 按「周期已过天数占比」计。"""
    if _sub_cycle_kind(a) == "interval5h":
        # MiniMax 月付固定费用: 监控起点之前的时间「钱已经付了、额度已经买下」,
        # 一律按满额计(窗口 100% 视为已消耗), 这样累计消耗能覆盖订阅至今的每个月。
        # 监控起点之后按 5h 窗口实际消耗计。
        first_ts = _first_snapshot_ts(d, a)
        if first_ts is not None and en <= first_ts:
            return 100.0
    if _sub_cycle_kind(a) == "flat":
        total_days = max(1, (en - st).days)
        if is_current:
            passed = max(0, min(total_days, (datetime.now() - st).days))
        else:
            passed = total_days
        return round(passed * 100.0 / total_days, 1)
    if is_current and cur_pct is not None:
        return max(0.0, min(100.0, 100.0 - float(cur_pct)))
    _times, _vals = _snap_index(d).get(a.get("id"), ([], []))
    _lo = bisect.bisect_left(_times, st)
    _hi = bisect.bisect_left(_times, en)   # 左闭右开，与原逻辑一致
    if _hi <= _lo:
        return 0.0
    return max(0.0, min(100.0, 100.0 - min(_vals[_lo:_hi])))


def _sub_cost_window(a):
    """成本核算用的额度窗口:
      weekly  —— 用「7天」窗口的额度消耗率推算月度成本(周额度平台: Kimi/智谱)
      monthly —— 用「月」窗口的额度消耗率推算成本(月度额度平台: 方舟; 月付不限量: MiniMax)
    ⚠️ 绝不用 5 小时窗口做成本核算 —— 5 小时是限流窗口, 用完就重置,
       拿它推算月度成本会严重失真(用量集中时算成 100%, 分散时又算成 0)。
    5 小时窗口只在「总览 → 订阅额度窗口」面板展示, 供日常选模型参考。"""
    return "weekly" if _sub_cycle_kind(a) == "weekly" else "monthly"


def _sub_monthly_cost_from_window(d, a, window_pct, kind):
    """由额度窗口消耗率推算月度成本(元)。

    统一公式(用户口径): 30 天总费用固定 → 折算到该窗口的周期费用
      → 用该窗口的额度消耗率 → 推算月度总消耗。
    weekly 口径: 周周期费 = 月费/4, 该周已用 X% → 本月成本 = 周周期费 × X%
    monthly 口径: 周期费 = 月费, 该月已用 X% → 本月成本 = 月费 × X%
    """
    fee = float(a.get("monthlyFee") or 0)
    if fee <= 0 or window_pct is None:
        return 0.0
    used = max(0.0, min(100.0, 100.0 - float(window_pct)))
    if kind == "weekly":
        return round(fee / 4.0 * used / 100.0, 2)
    return round(fee * used / 100.0, 2)


def _sub_cycle_costs(d, a):
    """订阅各计费周期的消耗明细: [{start, end, used, cost, fee}]"""
    fee = float(a.get("monthlyFee") or 0)
    if fee <= 0:
        return []
    kind = _sub_cycle_kind(a)
    if kind == "weekly":
        cycle_fee = fee / 4.0
    elif kind == "interval5h":
        cycle_fee = fee / 144.0   # 30天 = 144 个 5 小时窗口
    else:
        cycle_fee = fee           # monthly 整月费
    since = _sub_since(d, a)
    bounds = _sub_cycle_bounds(since, kind, datetime.now())
    out = []
    for i, (st, en) in enumerate(bounds):
        is_cur = i == len(bounds) - 1
        used = _sub_cycle_used(d, a, st, en, a.get("quotaPercent"), is_cur)
        # interval5h: 窗口费按「窗口所在自然月的实际天数」算, 保证每个月用满 = 恰好一个月费
        # (31天的月有 148.8 个窗, 30天的月有 144 个窗; 用固定 144 会让大月多算 3.4%)
        win_fee = cycle_fee
        if kind == "interval5h":
            dim = _days_in_month(st.year, st.month)
            win_fee = fee / (dim * 24.0 / 5.0)
        out.append({"start": st, "end": en, "used": round(used, 1),
                    "cost": round(win_fee * used / 100.0, 4),
                    "fee": round(win_fee, 4)})
    return out



def _sub_reset_info(d, a):
    """识别订阅账号的配额重置周期(周度 / 月度)。

    返回 {"cycle": "weekly"|"monthly"|None, "day": int|None, "auto": bool, "label": str}
      cycle: weekly=每周重置 / monthly=每月重置 / None=未识别
      day:   weekly 时是星期几(周一=0..周日=6); monthly 时是几号(1-31); None=未识别
      auto:  True=系统自动识别 / False=用户手动设置
    识别逻辑: 手动设置(resetCycle/resetDay)优先; 否则从该账号的配额快照里找
    「配额%突然回升(>=25个点)」的日期 —— 那就是重置发生的时刻, 再按间隔推断周/月。
    """
    rc = a.get("resetCycle")
    rd = a.get("resetDay")
    if rc in ("weekly", "monthly") and rd is not None:
        try:
            rd = int(rd)
        except (TypeError, ValueError):
            rd = None
        if rd is not None:
            label = (f"每周{_WEEK_CN[rd]}重置" if rc == "weekly" else f"每月{rd}号重置")
            return {"cycle": rc, "day": rd, "auto": False, "label": label}

    # 自动识别: 收集配额%快照, 找回升点
    _times, _vals = _snap_index(d).get(a.get("id"), ([], []))
    pts = list(zip(_times, _vals))   # 索引本身已按时间升序
    # ① 先找出所有「配额%大幅回升(>=25个点)」的时刻 —— 这些里混着两种东西:
    #    真正的周期重置(每周/每月), 以及 5小时滚动窗口的正常回升(几小时一次)。
    raw_resets = []
    for i in range(1, len(pts)):
        if pts[i][1] - pts[i - 1][1] >= 25:
            raw_resets.append(pts[i][0])
    # ② 把相邻间隔 < 4 天的回升合并成一簇(只留簇内最后一次) —— 剔除 5h 窗口噪音,
    #    剩下的才是「跨多天的周期重置」。Coding Plan 多为「5h频限+周/月限」双窗口,
    #    5h 窗口每隔几小时就回升一次, 不合并会被误判成高频重置。
    resets = []
    for r in raw_resets:
        if resets and (r - resets[-1]).days < 4:
            resets[-1] = r
        else:
            resets.append(r)
    if len(resets) >= 2:
        gaps = [(resets[i] - resets[i - 1]).days for i in range(1, len(resets))]
        gaps = [g for g in gaps if g > 0]
        if gaps:
            gaps.sort()
            med = gaps[len(gaps) // 2]
            if 5 <= med <= 9:      # 大约一周 -> 周度
                wd = resets[-1].weekday()
                return {"cycle": "weekly", "day": wd, "auto": True,
                        "label": f"每周{_WEEK_CN[wd]}重置(自动)"}
            if 25 <= med <= 33:    # 大约一月 -> 月度
                dom = resets[-1].day
                return {"cycle": "monthly", "day": dom, "auto": True,
                        "label": f"每月{dom}号重置(自动)"}
    elif len(resets) == 1:
        # 只观察到一次重置, 无法判断周期, 但至少知道一个重置点 -> 当作参考
        r = resets[0]
        return {"cycle": None, "day": r.day, "auto": True,
                "label": f"上次重置 {r.strftime('%m-%d')}(仅一次, 未判周期)"}
    return {"cycle": None, "day": None, "auto": True, "label": "未识别"}


def _next_reset_date(info, base=None):
    """根据重置周期信息, 算出下一次重置的日期(用于前端展示「下次重置」)。"""
    if base is None:
        base = datetime.now()
    cyc = info.get("cycle")
    day = info.get("day")
    if cyc == "weekly" and day is not None:
        cur_wd = base.weekday()
        ahead = (day - cur_wd) % 7
        if ahead == 0:
            ahead = 7                    # 今天就是重置日 -> 下次是下周
        return (base + timedelta(days=ahead)).strftime("%m-%d")
    if cyc == "monthly" and day is not None:
        y, m = base.year, base.month
        nd = min(day, _days_in_month(y, m))
        cand = datetime(y, m, nd)
        if cand.date() <= base.date():   # 本月重置日已过 -> 下个月
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            nd = min(day, _days_in_month(y, m))
            cand = datetime(y, m, nd)
        return cand.strftime("%m-%d")
    return ""


def _ledger_data(d):
    """自然月 / 自然年 账目统计（按模型公司/平台拆分）—— 纯计算, 不含读取。

    口径（只统计真实支出）:
      按量账号: 消耗 = 相邻两次快照「余额下降额」累加（余额上升视为充值，不计入），
                归入该区间「中点」所在的自然月 —— 跨天/跨月断档也能正确归月，
                且不会像「每日首末差值」那样把充值当天的消耗漏算成 0。
      订阅账号: 消耗 = 月费。默认按当月「已订阅天数 / 当月总天数」折算；
                另返回「全月口径」（当月只要在订就算整月月费）供前端切换。
    免费额度 / 赠送额度不产生金额（单价 0）。

    本函数是「账目统计」与「消耗总览」共用的唯一口径来源 —— 两处数字必须一致。
    """
    accs = {a["id"]: a for a in d.get("accounts", [])}
    # 已软删除账号也纳入统计: 它名下的历史消耗必须留在账本里(否则删账号=花销凭空蒸发)
    for k, v in (d.get("deletedAccounts") or {}).items():
        try:
            aid = int(k)
        except (TypeError, ValueError):
            continue
        accs.setdefault(aid, {**v, "_deleted": True})
    today = datetime.now()
    disp = _disp_currency(d)

    # "YYYY-MM" -> {platform: 显示币金额}(跨币种平台合计必须先换算, 否则 100 美元会当 100 元)
    m_plat = {}
    m_plat_full = {}
    # "YYYY-MM" -> {accountId: 原币金额} —— 明细保留原币, 前端按账号币种展示
    m_acct = {}
    m_acct_full = {}
    # "YYYY-MM" -> {"payg"/"sub": 显示币金额} —— 总额/分类小计必须同币种
    m_kind = {}
    m_kind_full = {}

    def _add(mkey, aid, plat, amount, cur="CNY", kind="payg", full=None):
        if amount <= 0:
            return
        conv = _fx(amount, cur, disp)
        mp = m_plat.setdefault(mkey, {})
        mp[plat] = mp.get(plat, 0.0) + conv
        mk = m_kind.setdefault(mkey, {})
        mk[kind] = mk.get(kind, 0.0) + conv
        ma = m_acct.setdefault(mkey, {})
        ma[aid] = ma.get(aid, 0.0) + amount
        if full is not None:
            convf = _fx(full, cur, disp)
            mpf = m_plat_full.setdefault(mkey, {})
            mpf[plat] = mpf.get(plat, 0.0) + convf
            mkf = m_kind_full.setdefault(mkey, {})
            mkf[kind] = mkf.get(kind, 0.0) + convf
            maf = m_acct_full.setdefault(mkey, {})
            maf[aid] = maf.get(aid, 0.0) + full

    # ---- 1) 按量账号: 从快照累加真实扣费 ----
    by_acc = {}
    for s in d.get("snapshots", []):
        by_acc.setdefault(s.get("accountId"), []).append(s)

    for aid, arr in by_acc.items():
        a = accs.get(aid)
        if not a or a.get("apiType") == "sub":
            continue
        plat = a.get("platform") or "custom"
        try:
            arr = sorted(arr, key=lambda x: str(x.get("collectedAt", "")))
        except Exception:
            continue
        for i in range(1, len(arr)):
            try:
                t0 = datetime.fromisoformat(str(arr[i - 1].get("collectedAt", "")))
                t1 = datetime.fromisoformat(str(arr[i].get("collectedAt", "")))
                delta = float(arr[i]["remaining"]) - float(arr[i - 1]["remaining"])
            except (ValueError, TypeError, KeyError):
                continue
            if delta >= 0:
                continue  # 余额不变 / 上升(充值) → 不计消耗
            mid = t0 + (t1 - t0) / 2
            _add(mid.strftime("%Y-%m"), aid, plat, -delta,
                 cur=a.get("currency"), kind="payg")

    # ---- 2) 订阅账号: 月费固定 → 按天摊销(V3.17.37 定稿口径) ----
    # 用户拍板: 订阅金额只跟时间走, 与配额窗口(5h/7天/月)完全解绑;
    # 当日金额 = 月费 / 当月总天数, 归当天所在自然月; 历史月份摊满整月。
    for aid, a in accs.items():
        if a.get("apiType") != "sub":
            continue
        fee = float(a.get("monthlyFee") or 0)
        if fee <= 0:
            continue
        plat = a.get("platform") or "custom"
        _tu, _mu, per_day = _sub_time_costs(d, a)
        for dstr, amt in per_day:
            _add(dstr[:7], aid, plat, amt, cur=a.get("currency"), kind=_acct_kind(a))

    keys = sorted(set(m_plat) | set(m_plat_full))
    if not keys:
        return {"months": [], "years": [], "platforms": [], "generatedAt": _now(),
                "hasData": False, "displayCurrency": disp, "fxUpdated": FX_UPDATED}
    y0, m0 = int(keys[0][:4]), int(keys[0][5:7])
    y1, m1 = int(keys[-1][:4]), int(keys[-1][5:7])
    seq = []
    cy, cm = y0, m0
    while (cy, cm) <= (y1, m1):
        seq.append(f"{cy:04d}-{cm:02d}")
        cy, cm = (cy + 1, 1) if cm == 12 else (cy, cm + 1)
    platforms = sorted({p for mp in list(m_plat.values()) + list(m_plat_full.values())
                        for p in mp})

    def _detail(mkey, acct_map):
        out = []
        for aid, v in (acct_map.get(mkey) or {}).items():
            a = accs.get(aid) or {}
            cur_a = _norm_currency(a.get("currency"))
            out.append({"accountId": aid, "name": a.get("name") or "?",
                        "platform": a.get("platform") or "custom",
                        "apiType": a.get("apiType") or "payg",
                        "category": _acct_kind(a),
                        "monthlyFee": round(float(a.get("monthlyFee") or 0), 2),
                        # 明细金额是「原币」, 前端用 currency+fxRate 换算展示
                        "amount": round(v, 2), "currency": cur_a,
                        "fxRate": round(_fx_rate(cur_a, disp), 6)})
        out.sort(key=lambda x: -x["amount"])
        return out

    months = []
    for mk in seq:
        mp = m_plat.get(mk, {})
        mpf = m_plat_full.get(mk, {})
        kd = m_kind.get(mk, {})
        kdf = m_kind_full.get(mk, {})
        payg = kd.get("payg", 0.0)
        sub = kd.get("sub", 0.0)
        online = kd.get("online", 0.0)
        sub_full = kdf.get("sub", sub)
        online_full = kdf.get("online", online)
        months.append({
            "month": mk,
            "byPlatform": {p: round(mp.get(p, 0.0), 2) for p in platforms},
            "byPlatformFull": {p: round(mpf.get(p, 0.0), 2) for p in platforms},
            "paygTotal": round(payg, 2),
            "subTotal": round(sub, 2),
            "onlineTotal": round(online, 2),
            "subTotalFull": round(sub_full, 2),
            "onlineTotalFull": round(online_full, 2),
            "total": round(sum(mp.values()), 2),
            "totalFull": round(sum(mpf.values()), 2),
            "detail": _detail(mk, m_acct),
            "detailFull": _detail(mk, m_acct_full),
        })

    ymap, ymap_full = {}, {}
    for mk in seq:
        y = mk[:4]
        ym = ymap.setdefault(y, {})
        for p, v in (m_plat.get(mk) or {}).items():
            ym[p] = ym.get(p, 0.0) + v
        ymf = ymap_full.setdefault(y, {})
        for p, v in (m_plat_full.get(mk) or {}).items():
            ymf[p] = ymf.get(p, 0.0) + v
    years = []
    for y in sorted(set(ymap) | set(ymap_full)):
        ym = ymap.get(y, {})
        ymf = ymap_full.get(y, {})
        years.append({
            "year": y,
            "byPlatform": {p: round(ym.get(p, 0.0), 2) for p in platforms},
            "byPlatformFull": {p: round(ymf.get(p, 0.0), 2) for p in platforms},
            "paygTotal": round(sum(m["paygTotal"] for m in months if m["month"][:4] == y), 2),
            "subTotal": round(sum(m["subTotal"] for m in months if m["month"][:4] == y), 2),
            "onlineTotal": round(sum(m["onlineTotal"] for m in months if m["month"][:4] == y), 2),
            "total": round(sum(ym.values()), 2),
            "totalFull": round(sum(ymf.values()), 2),
            "monthCount": sum(1 for mk in seq if mk[:4] == y),
        })
    return {"months": months, "years": years, "platforms": platforms,
            "generatedAt": _now(), "hasData": True,
            "displayCurrency": disp, "fxUpdated": FX_UPDATED}


def proc_ledger(_inp):
    """账目统计接口: 读账本 → 交给 _ledger_data 计算(与消耗总览同口径)。"""
    return _ledger_data(_load())


def _auto_total_quota(d):
    """自动总投入额度 = Σ按量账号累计充值 + Σ订阅账号实际已付(累计充值口径)。

    订阅实际已付: 年付(截止日距今>90天)=月费×12; 月付滚动=已计费周期数×周期费。
    与「模型API管理」页的累计充值列完全同口径(同一组函数)。
    V3.34.1: 同号去重 —— 平台+账户名称+电话完全相同的账号, 累计充值只计一次。
    """
    disp = _disp_currency(d)
    def _amt(a):
        if a.get("apiType") == "sub":
            info = _sub_reset_info(d, a)
            periods = _sub_billed_periods(_sub_since(d, a), info.get("cycle"))
            return _fx(_sub_total_recharge(a, info.get("cycle"), periods),
                       a.get("currency"), disp)
        return _fx(a.get("totalRecharge") or 0, a.get("currency"), disp)
    return round(_dedup_sum(d.get("accounts", []), _amt), 2)


def proc_get_total_quota(_inp):
    """总投入额度: 自动计算(Σ按量累计充值 + Σ订阅实际已付), 用户可手动覆盖。

    用户手填了 totalQuota 就用手填的(isAuto=False); 没填/清空则用自动值(isAuto=True)。
    联动指标(与「模型API管理」完全同口径, 直接复用 _account_view 逐账号聚合):
      totalUsedAll  累计消耗合计 = Σ各账号累计消耗
      monthUsedAll  本月消耗合计 = Σ各账号本月消耗(自然月1号起)
      rechargeTotal 按量累计充值(参考)
    """
    d = _load()
    s = d.get("settings", {})
    disp = _disp_currency(d)
    raw = s.get("totalQuota", "")
    auto = _auto_total_quota(d)
    led = _ledger_data(d)
    spent = round(sum(float(y.get("total", 0.0)) for y in led.get("years", [])), 2)
    recharge = _dedup_sum([a for a in d.get("accounts", []) if a.get("apiType") != "sub"],
                          lambda a: _fx(a.get("totalRecharge") or 0, a.get("currency"), disp))
    # 与账号列表同口径的聚合: 本月摊销共享一次 _ledger_data 当前月明细
    cur_ym = datetime.now().strftime("%Y-%m")
    cur = next((m for m in led.get("months", []) if m["month"] == cur_ym), None)
    cur_amt = {x["accountId"]: float(x.get("amount") or 0.0)
               for x in (cur or {}).get("detail", [])}
    total_used_all = 0.0
    remaining_all = 0.0
    for a in d.get("accounts", []):
        v = _account_view(d, a, cur_amt)
        total_used_all += _fx(v.get("totalUsed") or 0.0, a.get("currency"), disp)
        remaining_all += _fx(v.get("remainingMoney") or 0.0, a.get("currency"), disp)
    # 本月消耗 = 账本当月总额(与「当前总览-本月总消耗」完全同源, 含已删除账号的历史摊销,
    # 两处数字永远一致; 各账号明细仍按 _account_view 在「模型API管理」展示)
    month_used_all = float((cur or {}).get("total", 0.0))
    base = {"autoTotal": auto, "spentTotal": spent,
            "rechargeTotal": round(recharge, 2),
            "totalUsedAll": round(total_used_all, 2),
            "remainingAll": round(remaining_all, 2),
            "monthUsedAll": round(month_used_all, 2),
            "displayCurrency": disp}
    if raw not in ("", None):
        try:
            manual = float(raw)
        except (TypeError, ValueError):
            manual = 0.0
        return {"totalQuota": manual, "hasValue": True, "isAuto": False, **base}
    # 未手动填写 -> 用自动计算值
    return {"totalQuota": auto, "hasValue": auto > 0, "isAuto": True, **base}


def proc_set_total_quota(inp):
    """手动覆盖总投入额度。传空字符串/0 表示「恢复自动计算」。"""
    with _DATA_TX:
        d = _load()
        v = inp.get("totalQuota", "")
        try:
            fv = float(v)
        except (TypeError, ValueError):
            fv = 0.0
        if fv < 0:
            fv = 0.0
        # 清空/0 = 恢复自动(不写 totalQuota)
        if fv <= 0:
            d.setdefault("settings", {}).pop("totalQuota", None)
        else:
            d.setdefault("settings", {})["totalQuota"] = str(fv)
        _save(d)
    return {"ok": True, "totalQuota": fv, "autoTotal": _auto_total_quota(d)}


# ---------------------------------------------------------------- 手机/远程访问入口
def _probe_frp_url():
    """尽力从本机 frp 客户端配置里读出本服务的公网映射地址(只为在界面上显示地址)。

    找 localPort == 本服务端口 的那条 proxy, 拼成 http://{serverAddr}:{remotePort}。
    读不到就返回空 —— 不影响任何功能, 手机仍然可以手动输地址访问。
    """
    # 依次探测常见 frpc 配置位置; 也支持环境变量 QUOTA_FRPC_CONFIG 显式指定
    cands = [
        BASE_DIR / "frpc.toml",
        BASE_DIR / "_tools" / "frp" / "frpc.toml",
        Path.home() / ".config" / "frp" / "frpc.toml",
    ]
    env_cfg = str(os.environ.get("QUOTA_FRPC_CONFIG") or "").strip()
    if env_cfg:
        cands.insert(0, Path(env_cfg))
    for p in cands:
        try:
            if not p.is_file():
                continue
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        addr = ""
        for m in re.finditer(r'serverAddr\s*=\s*"([^"]+)"', txt):
            addr = m.group(1)
            break
        local_port = remote_port = ""
        for block in re.split(r"\[\[proxies\]\]", txt)[1:]:
            m = re.search(r"localPort\s*=\s*(\d+)", block)
            lp = m.group(1) if m else ""
            if lp != str(PORT):
                continue
            m2 = re.search(r"remotePort\s*=\s*(\d+)", block)
            if m2:
                remote_port = m2.group(1)
            local_port = lp
            break
        if addr and remote_port:
            return f"http://{addr}:{remote_port}"
    return ""


def _remote_url():
    """远程访问地址: 优先取用户手填的, 否则尽力自动探测内网穿透配置。"""
    try:
        manual = str(_load().get("settings", {}).get("remoteUrl") or "").strip()
    except Exception:
        manual = ""
    if manual:
        if not manual.lower().startswith("http"):
            manual = "http://" + manual
        return manual.rstrip("/")
    return _probe_frp_url()


def _access_info():
    """手机/远程访问信息(可用地址)。已取消访问口令 —— 手机直连即可, 无需口令。"""
    lan = _lan_ip()
    return {
        "password": "",
        "enabled": False,
        "port": PORT,
        "loopbackUrl": f"http://{HOST}:{PORT}",
        "lanUrl": (f"http://{lan}:{PORT}" if lan else ""),
        "lanIp": lan,
        "remoteUrl": _remote_url(),
        "autoDetected": not bool(str(_load().get("settings", {}).get("remoteUrl") or "").strip()),
    }


def proc_get_access(_inp):
    """「关于系统」页用: 查看手机访问口令与地址。"""
    return _access_info()


def proc_set_remote_password(inp):
    """设置/更换手机访问口令。传空字符串 = 关闭远程访问(手机将打不开)。"""
    pw = str(inp.get("password") or "")
    pw = re.sub(r"\s+", "", pw)[:32]
    with _DATA_TX:
        d = _load()
        d.setdefault("settings", {})["remotePassword"] = pw
        _save(d)
    return {"ok": True, "password": pw, "enabled": bool(pw)}


def proc_gen_remote_password(_inp):
    """随机生成一个新口令(用户记不住时用)。"""
    pw = _gen_token()
    with _DATA_TX:
        d = _load()
        d.setdefault("settings", {})["remotePassword"] = pw
        _save(d)
    return {"ok": True, "password": pw, "enabled": True}


def proc_set_remote_url(inp):
    """手填远程访问地址(内网穿透的公网地址), 留空 = 回到自动探测。"""
    url = str(inp.get("remoteUrl") or "").strip()
    with _DATA_TX:
        d = _load()
        d.setdefault("settings", {})["remoteUrl"] = url
        _save(d)
    return {"ok": True, "remoteUrl": _remote_url()}


def proc_get_budget(_inp):
    s = _load()["settings"]
    return {"monthlyTotal": float(s.get("monthlyTotal", 1800)), "warnPercent": float(s.get("warnPercent", 80))}

def proc_set_budget(inp):
    with _DATA_TX:
        d = _load()
        d["settings"]["monthlyTotal"] = str(inp.get("monthlyTotal", 0))
        d["settings"]["warnPercent"] = str(inp.get("warnPercent", 80))
        _save(d)
    return {"ok": True}

def proc_set_platform_budget(inp):
    with _DATA_TX:
        d = _load()
        for a in d["accounts"]:
            if a["platform"] == inp["platform"]:
                a["budget"] = inp.get("budget", 0)
        _save(d)
    return {"ok": True}


def proc_get_currency(_inp):
    """多币种(V3.19.0): 当前显示货币 + 内置汇率表(前端换算用)。"""
    d = _load()
    return {"displayCurrency": _disp_currency(d), "rates": FX_RATES,
            "symbols": FX_SYMBOLS, "updated": FX_UPDATED}


def proc_set_currency(inp):
    """切换显示货币。只改展示口径, 不动任何账号金额(存储永远原币)。"""
    c = _norm_currency(inp.get("displayCurrency"))
    with _DATA_TX:
        d = _load()
        d.setdefault("settings", {})["displayCurrency"] = c
        _save(d)
    return {"ok": True, "displayCurrency": c, "rates": FX_RATES}


def proc_set_windows(inp):
    """在线订阅手动录入额度窗口(V3.21.0): 存「剩余%」, 只进总览额度窗口, 不参与金额。"""
    try:
        aid = int(inp.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "账号 id 无效"}

    def _pct(v):
        if v is None or v == "":
            return None
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return round(max(0.0, min(100.0, x)), 1)

    with _DATA_TX:
        d = _load()
        for a in d["accounts"]:
            if a.get("id") == aid:
                a["win5h"] = _pct(inp.get("win5h"))
                a["win7d"] = _pct(inp.get("win7d"))
                a["winUpdatedAt"] = _now()
                _save(d)
                return {"ok": True, "win5h": a["win5h"], "win7d": a["win7d"],
                        "winUpdatedAt": a["winUpdatedAt"]}
    return {"ok": False, "error": "账号不存在"}


PROCS = {
    "quota.list": proc_list, "quota.upsert": proc_upsert, "quota.remove": proc_remove,
    "quota.collect": proc_collect, "quota.importAll": proc_import_all,
    "quota.summary": proc_summary, "quota.trend": proc_trend,
    "quota.ledger": proc_ledger,
    "quota.getTotalQuota": proc_get_total_quota, "quota.setTotalQuota": proc_set_total_quota,
    "quota.getAccess": proc_get_access,
    "quota.setRemotePassword": proc_set_remote_password,
    "quota.genRemotePassword": proc_gen_remote_password,
    "quota.setRemoteUrl": proc_set_remote_url,
    "quota.getBudget": proc_get_budget, "quota.setBudget": proc_set_budget,
    "quota.setPlatformBudget": proc_set_platform_budget,
    "quota.getCurrency": proc_get_currency, "quota.setCurrency": proc_set_currency,
    "quota.setWindows": proc_set_windows,
}

# ---------------------------------------------------------------- 代理采集(可选)
def proxy_accounts():
    """同目录存在 额度管家_本地代理.py 时, 调用它查询真实余额/配额(模块走缓存)"""
    try:
        mod = _load_proxy_module()
        if not mod:
            return {"accounts": [], "hint": "未找到 额度管家_本地代理.py"}
        return {"accounts": mod.build_accounts()}
    except Exception as e:
        return {"accounts": [], "error": str(e)}


def proxy_upsert(payload):
    """网页添加 API 账号: 转发给本地代理模块(Key 混淆存储到 accounts_config.json)"""
    proxy_file = BASE_DIR / "额度管家_本地代理.py"
    if not proxy_file.exists():
        return {"ok": False, "error": "未找到 额度管家_本地代理.py"}
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("local_proxy", proxy_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.upsert_account(payload)
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------- 额度窗口(5小时/7天)只读采集 ----------------
# V3.17.11: 给 Kimi Coding / 智谱 Coding / 方舟 Agent Plan 展示「5小时 + 7天」窗口进度
# 与重置时间。**只读展示, 不写快照、不进账目计算**, 与付费口径完全隔离。
_WINDOW_CACHE = {"ts": 0.0, "data": None}
_WINDOW_LOCK = threading.Lock()
_WINDOW_PLATFORMS = {"kimicode", "kimi", "glm", "minimax", "doubao", "qwenplan"}


def quota_windows(inp=None):
    force = bool((inp or {}).get("force"))
    now = time.time()
    with _WINDOW_LOCK:
        if not force and _WINDOW_CACHE["data"] and now - _WINDOW_CACHE["ts"] < 90:
            return _WINDOW_CACHE["data"]
    items = []
    try:
        mod = _load_proxy_module()
        cfg = mod.load_config() if mod else {}
    except Exception as e:
        return {"ok": False, "detail": f"本地代理模块加载失败: {e}", "items": []}
    for i, acc in enumerate(cfg.get("accounts", []), start=1):
        if acc.get("query") != "quota":
            continue
        plat = acc.get("platform", "")
        if plat not in _WINDOW_PLATFORMS:
            continue
        try:
            r = mod.query_account({"id": i, **acc}, force=force)
        except Exception as e:
            r = {"live": False, "note": f"查询失败: {e}", "windows": []}
        items.append({
            "platform": plat,
            "name": acc.get("name", ""),
            "group": acc.get("group", "默认"),
            "plan": r.get("plan") or "",
            "windows": r.get("windows") or [],
            "live": bool(r.get("live")),
            "ok": bool(r.get("windows")),
            "note": r.get("note", ""),
        })
    # 在线订阅(V3.21.0): 官方无接口, 把用户手动录入的 5h/7d 剩余% 并进同一面板
    try:
        d = _load()
        for a in d.get("accounts", []):
            if (a.get("platform") or "") not in ONLINE_SUB_PLATFORMS:
                continue
            ws = []
            if a.get("win5h") is not None:
                p = max(0.0, min(100.0, float(a["win5h"])))
                ws.append({"key": "5h", "label": "5 小时", "usedPct": round(100 - p, 1),
                           "remainingPct": round(p, 1)})
            if a.get("win7d") is not None:
                p = max(0.0, min(100.0, float(a["win7d"])))
                ws.append({"key": "7d", "label": "7 天", "usedPct": round(100 - p, 1),
                           "remainingPct": round(p, 1)})
            if not ws:
                continue
            items.append({
                "platform": a.get("platform"), "name": a.get("name", ""),
                "group": a.get("groupName", "默认"), "plan": "手动录入",
                "windows": ws, "live": False, "ok": True,
                "note": f"手动录入(更新于 {a.get('winUpdatedAt') or '—'}), 只展示不计费",
            })
    except Exception:
        pass
    out = {"ok": True, "items": items,
           "fetchedAt": int(time.time() * 1000)}
    with _WINDOW_LOCK:
        _WINDOW_CACHE["ts"], _WINDOW_CACHE["data"] = time.time(), out
    return out


# ---------------------------------------------------------------- 版本与系统更新
# 用户数据保护名单(更新包与上传接口一律不得触碰 —— 这是"账本可能被清空"的根源)
# 说明: 2026-10-16 加固 —— 此前 protect 只对 deletes 生效(files 可覆盖账本),
#       且用字符串精确比较, 可被 "./data.json" 绕过; 现统一走 _norm_rel 归一化后再比对。
#   程序文件(启动额度管家.py / VERSION / dist 产物 等) 不在此列 —— 更新包的本意就是要
#   能升级它们; 防线在于"谁有权触发更新": 外部网站跨域已被 Origin 拦截, 上传必须是合法 zip。
_PROTECTED_FILES = {
    "accounts_config.json",      # 账号与 Key 配置
    "data.json",                 # 账本(账号/快照/设置)
    "data.json.bak",             # 账本自动备份
    "balance_history.jsonl",     # 余额历史
    "价格政策.json",             # 人工整理的价格政策数据
    "价格政策.核对.json",        # 政策页指纹基线(在线核查用, 本地生成)
    "使用说明.md",
}
# 上传接口额外禁止: 这些名字即使是 .zip 也一律拒绝(防 name 直写核心文件)
_UPLOAD_DENY = _PROTECTED_FILES | {"VERSION", "version.json", "启动额度管家.py", "额度管家_本地代理.py"}
# (version_history.json 是程序自带的版本历史, 允许被更新包覆盖, 故不在保护名单里)


def _norm_rel(rel):
    """把更新清单里的相对路径归一化, 用于和核心文件名单比对。
    './data.json'、'dist/../data.json'、'data.json' 都会归一成 'data.json'。"""
    s = str(rel or "").strip().replace("\\", "/")
    if not s:
        return ""
    parts = []
    for seg in s.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            else:
                parts.append("..")
            continue
        parts.append(seg)
    return "/".join(parts)


def _is_protected(rel):
    """是否为受保护的用户数据(更新包绝对不能改/删)"""
    n = _norm_rel(rel)
    if not n:
        return True
    if n in _PROTECTED_FILES:
        return True
    # 备份目录/临时文件也不允许更新包触碰
    if n.startswith("备份/") or n == "_upload_tmp.zip":
        return True
    return False


def _version():
    try:
        return (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except Exception:
        return "0.0.0"


def security_info():
    """密钥保护状态(V3.24.0): accounts_config.json 是否已用 Windows DPAPI 加密。"""
    try:
        mod = _load_proxy_module()
        if not mod:
            return {"ok": False, "error": "本地代理模块未加载"}
        return {"ok": True, **mod.security_state()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def set_security(inp):
    """开/关 DPAPI 加密。只动 accounts_config.json, 不碰 data.json 账本。"""
    try:
        mod = _load_proxy_module()
        if not mod:
            return {"ok": False, "error": "本地代理模块未加载"}
        return mod.set_encryption(bool((inp or {}).get("encryptAccounts")))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def set_notify(inp):
    """保存微信预警设置(V3.29.0)并热启动/热停止预警线程(不必重启软件)。

    inp: enabled / channel(wecom|serverchan) / webhook / sendkey / 阈值若干
    webhook 与 sendkey 传空字符串 = 保持原值不变(界面不回显敏感值, 避免误清空)。
    """
    try:
        mod = _load_proxy_module()
        if not mod:
            return {"ok": False, "error": "未找到 额度管家_本地代理.py"}
        cfg = mod.load_config()
        n = dict(cfg.get("notify") or {})
        if "enabled" in inp:
            n["enabled"] = bool(inp.get("enabled"))
        if inp.get("channel"):
            n["channel"] = "serverchan" if inp["channel"] == "serverchan" else "wecom"
        # 敏感字段: 有值才覆盖, 空值保留原样
        if str(inp.get("webhook") or "").strip():
            n["webhook"] = str(inp["webhook"]).strip()
        if str(inp.get("sendkey") or "").strip():
            n["sendkey"] = str(inp["sendkey"]).strip()
        _FIELD_MAP = {"burnThresholdPerHour": ("burn_threshold_per_hour", float),
                      "lowBalanceThreshold": ("low_balance_threshold", float),
                      "lowQuotaPercent": ("low_quota_percent", float),
                      "cooldown": ("cooldown", int)}
        for k, (nk, cast) in _FIELD_MAP.items():
            if inp.get(k) is not None:
                try:
                    n[nk] = cast(inp[k])
                except (TypeError, ValueError):
                    pass
        # V3.31.1: 自定义规则整表覆盖(前端全量提交, 不做增量合并)
        if isinstance(inp.get("rules"), list):
            cleaned = []
            for r in inp["rules"]:
                if not isinstance(r, dict) or not r.get("type"):
                    continue
                if r["type"] not in mod.RULE_TYPES:
                    continue
                scope = r.get("scope") or mod.RULE_TYPES[r["type"]]["scopes"]
                scope = [x for x in scope if x in ("payg", "sub", "online")] or \
                        list(mod.RULE_TYPES[r["type"]]["scopes"])
                try:
                    th = float(r.get("threshold"))
                except (TypeError, ValueError):
                    th = float(mod.RULE_TYPES[r["type"]]["default"])
                accs = r.get("accounts") or []
                if not isinstance(accs, list):
                    accs = []
                accs = [str(x)[:60] for x in accs if str(x).strip()][:200]
                cleaned.append({
                    "id": str(r.get("id") or f"r{len(cleaned)+1}"),
                    "type": r["type"],
                    "scope": scope,
                    "accounts": accs,          # V3.32.4: 指定账号, 空=该类别全部
                    "threshold": th,
                    "enabled": bool(r.get("enabled", True)),
                    "note": str(r.get("note") or "")[:60],
                })
            n["rules"] = cleaned
        cfg["notify"] = n
        mod.save_config(cfg)                 # 自带加密态保护(DPAPI 时写密文)
        started = _start_alert_thread(mod, cfg)   # 热启动/热切
        return {"ok": True, "started": started,
                "detail": "已开启，开始监控" if started else "已关闭监控"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def storage_info():
    """数据目录信息(V3.22.0): 供「关于系统」展示数据位置、迁移状态与文件清单。"""
    files = []
    try:
        for p in sorted(DATA_HOME.glob("*")):
            if not p.is_file():
                continue
            st = p.stat()
            files.append({"name": p.name, "size": st.st_size,
                          "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    except OSError:
        pass
    legacy = [n for n in _DATA_FILES if (BASE_DIR / n).is_file()]
    return {
        "dataHome": str(DATA_HOME),
        "programDir": str(BASE_DIR),
        "migratedThisRun": list(_MIGRATED),
        "legacyLeft": legacy,
        "files": files,
        "note": ("Key 与账本都放在数据目录, 更新包/重装程序不会碰它。"
                 "可用环境变量 QUOTA_DATA_HOME 改到别处(便携盘/同步盘)。"),
    }


def _ver_tuple(s):
    nums = re.findall(r"\d+", str(s))
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def _find_pack():
    """找目录内的更新包。收紧规则(2026-10-16 加固):
    ① 文件名必须形如 更新包_V*.zip / 更新包*.zip (不再乱认目录里任何 zip, 避免把用户的备份、
       下载、别人发的压缩包当成更新包自动应用);
    ② 必须能解析出清单(version.json 或 VERSION), 否则不算;
    ③ 多个候选取版本号最高的, 而不是 mtime 最新的(改文件时间不会误判)。
    """
    try:
        cands = []
        for p in BASE_DIR.glob("*.zip"):
            if not p.is_file():
                continue
            # 收紧: 必须形如 更新包_V3.15.0.zip / 更新包V3.15.zip / 更新包_3.15.0.zip
            # 末尾必须是 .zip, 且版本号后不再有其它字符 —— 避免 "更新包_2025备份.zip"、
            # "更新包_V2价格表.zip" 这类同前缀文件被误认成更新包。
            if not re.match(r"^更新包[_-]?V?\d+(?:\.\d+)*\.zip$", p.name, re.IGNORECASE):
                continue
            try:
                man = _read_manifest(p)
            except Exception:
                continue
            cands.append((_ver_tuple(str(man.get("version") or "")), p))
        if not cands:
            return None
        return max(cands, key=lambda x: x[0])[1]
    except Exception:
        return None


def _read_manifest(pack):
    """读取更新清单。两种包都支持:
    A. 增量更新包(带 version.json 清单); B. 完整安装包(无清单, 用 VERSION + 自动构建清单,
       自动排除 用户配置accounts_config.json/数据data.json, 只更新程序文件)"""
    with zipfile.ZipFile(pack) as zf:
        names = zf.namelist()
        if "version.json" in names:
            return json.loads(zf.read("version.json").decode("utf-8"))
        if "VERSION" in names:
            ver = zf.read("VERSION").decode("utf-8").strip()
            # ★ 自动清单必须直接跳过受保护的用户数据 —— 否则这些文件会被列进 files,
            #   到 apply 阶段逐个被判「受保护, 已跳过」→ 整个包误报「部分失败」,
            #   而程序其实已经升级成功, 用户会以为升级失败。
            files = [{"path": n, "url": n} for n in names
                     if not n.endswith("/") and not _is_protected(n)
                     and "__pycache__" not in n]
            return {"version": ver, "date": "", "notes": ["完整包更新(保留你的账号配置与数据)"],
                    "files": files}
    raise KeyError("version.json")


def update_check_local():
    cur = _version()
    pack = _find_pack()
    if not pack:
        return {"current": cur, "has_update": False,
                "detail": "未找到更新包: 把「更新包_Vx.x.zip」放到本目录, 或点本页上传"}
    try:
        man = _read_manifest(pack)
    except KeyError:
        return {"current": cur, "has_update": False,
                "detail": f"{pack.name} 里既没有 version.json 也没有 VERSION, 不是本程序的更新包/安装包"}
    except Exception as e:
        return {"current": cur, "has_update": False, "detail": f"读取更新包失败: {e}"}
    latest = str(man.get("version") or "")
    # V3.33.6: 目录里常堆着历史更新包(3.17.x/3.28.0…)。若包里版本 <= 当前版本,
    # 说明这不是"待更新包", 绝不能把它的 notes 当成本版更新内容显示给用户。
    if latest and _ver_tuple(latest) <= _ver_tuple(cur):
        return {"current": cur, "latest": cur, "has_update": False,
                "date": "", "notes": [],
                "detail": f"目录里的 {pack.name} 是 V{latest}（不高于当前 V{cur}），已忽略"}
    fs = man.get("files") or man.get("proxy_files") or []
    dele = man.get("deletes") or []
    # 只统计真实存在、且非受保护/非目录的待删项, 给用户一个准确的"将清理几个"
    del_live = 0
    for rel in dele:
        if _is_protected(rel):
            continue
        t = BASE_DIR / _norm_rel(rel)
        if t.exists() and t.is_file():
            del_live += 1
    out = {"current": cur, "latest": latest,
           "has_update": _ver_tuple(latest) > _ver_tuple(cur),
           "date": man.get("date", ""), "notes": man.get("notes", []),
           "file_count": len(fs)}
    if del_live:
        out["delete_count"] = del_live
    return out


def _schedule_self_restart(delay=1.5):
    """更新主程序后自我重启(V3.17.41): 延迟执行让 HTTP 响应先发出。
    os.execv 同进程镜像替换: 原命令行窗口/守护脚本都不受影响, 用户无感。"""
    def _do():
        try:
            print("\n[自动更新] 主程序已更新, 正在自动重启生效…", flush=True)
            os.execv(sys.executable,
                     [sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:])
        except Exception as e:
            print(f"[自动更新] 自动重启失败({e}), 请手动重启本程序", flush=True)
    threading.Timer(delay, _do).start()


def _pack_signature_ok(man):
    """校验更新包签名(V3.23.0): 签名对象 = 去掉 sig 后的规范化 JSON。"""
    sig = str((man or {}).get("sig") or "")
    body = {k: v for k, v in (man or {}).items() if k != "sig"}
    try:
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
        return _ed_verify(bytes.fromhex(PACK_PUBKEY_HEX), raw, _b64d(sig))
    except Exception:
        return False


def apply_update_pack(pack):
    """应用更新包: 校验签名 -> 校验版本 -> 备份旧文件 -> 解压替换 + 删除废弃文件。
    V3.17.41 起: 若更新了主程序(启动额度管家.py), 完成后自动重启, 无需用户手动操作。
    V3.23.0 起: 必须是我们签过名的包才应用(堵"任意代码执行"洞);
    开发测试可用环境变量 QUOTA_ALLOW_UNSIGNED_UPDATE=1 临时放行未签名包。"""
    cur = _version()
    try:
        man = _read_manifest(pack)
    except KeyError:
        return False, "更新包里没有 version.json, 不是有效的更新包"
    except Exception as e:
        return False, f"读取更新包失败: {e}"
    if not _pack_signature_ok(man):
        if str(os.environ.get("QUOTA_ALLOW_UNSIGNED_UPDATE") or "") == "1":
            print("  [更新] 未签名/签名无效, 但已显式放行(QUOTA_ALLOW_UNSIGNED_UPDATE=1)", flush=True)
        elif not str(man.get("sig") or ""):
            return False, ("更新包未签名, 已拒绝应用。请使用官方发布的更新包"
                           "(本地开发可设环境变量 QUOTA_ALLOW_UNSIGNED_UPDATE=1 临时放行)")
        else:
            return False, "更新包签名校验失败: 文件被改动过或不是官方包, 已拒绝应用"
    latest = str(man.get("version") or "")
    if _ver_tuple(latest) <= _ver_tuple(cur):
        return False, f"当前已是最新版本 V{cur}(更新包为 V{latest}), 无需更新"
    files = man.get("files") or man.get("proxy_files") or []
    deletes = man.get("deletes") or []
    if not files and not deletes:
        return False, "更新清单里既没有要更新的文件, 也没有要删除的文件"
    # V3.27.0: 补 .pyw(桌面壳) / .sh(Linux) / .ico(favicon) / .css 已有——扩展名白名单跟不上新文件类型会静默拒绝
    allowed = {".py", ".pyw", ".sh", ".md", ".json", ".txt", ".html", ".js", ".css", ".ico", ".bat", ".command", ""}
    backup = BASE_DIR / "备份" / f"V{cur}"
    backup.mkdir(parents=True, exist_ok=True)
    updated, removed, failed = [], [], []
    with zipfile.ZipFile(pack) as zf:
        names = set(zf.namelist())
        for f in files:
            if not isinstance(f, dict):
                failed.append(f"{f!r}: 清单项格式错误")
                continue
            rel = (f.get("path") or "").strip()
            src = (f.get("url") or rel).strip()
            nrel = _norm_rel(rel)
            # 安全: 禁路径穿越/绝对路径/非白名单类型
            if not rel or ".." in rel or rel.startswith(("/", "\\")):
                failed.append(f"{rel or '?'}: 非法路径")
                continue
            if Path(nrel).suffix.lower() not in allowed:
                failed.append(f"{rel}: 不允许的文件类型")
                continue
            # ★ 核心文件保护(files 也必须受保护, 否则恶意包可覆盖账本/主程序)
            if _is_protected(rel):
                failed.append(f"{rel}: 核心文件受保护, 已跳过")
                continue
            if src not in names:
                failed.append(f"{rel}: 更新包内未找到 {src}")
                continue
            target = BASE_DIR / nrel
            try:
                if target.exists():
                    bak = backup / nrel
                    bak.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(target), str(bak))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(src))
                updated.append(nrel)
            except Exception as e:
                failed.append(f"{rel}: {e}")
    # ---- 删除废弃文件(先备份再删, 不碰用户数据与核心文件) ----
    for rel in deletes:
        nrel = _norm_rel(rel)
        if not nrel or ".." in nrel or str(rel or "").strip().startswith(("/", "\\")):
            failed.append(f"删除 {rel or '?'}: 非法路径")
            continue
        if _is_protected(rel):
            failed.append(f"删除 {rel}: 核心文件受保护, 已跳过")
            continue
        target = BASE_DIR / nrel
        if not target.exists():
            continue  # 本就不存在, 无需删除(幂等)
        if target.is_dir():
            failed.append(f"删除 {rel}: 是目录, 出于安全已跳过")
            continue
        try:
            bak = backup / nrel
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(target), str(bak))  # 备份后再删
            target.unlink()
            removed.append(nrel)
        except Exception as e:
            failed.append(f"删除 {rel}: {e}")
    if failed:
        tail = ""
        if updated:
            tail += "; 已更新: " + ", ".join(updated)
        if removed:
            tail += "; 已删除: " + ", ".join(removed)
        return False, "部分失败: " + "; ".join(failed) + tail
    parts = []
    if updated:
        parts.append(f"已更新 {len(updated)} 个文件")
    if removed:
        parts.append(f"已清理 {len(removed)} 个废弃文件")
    # 主程序被更新 → 自动重启(V3.17.41): 不再需要用户手动关窗重启
    need_restart = any(u.replace("\\", "/").lower() == "启动额度管家.py" for u in updated)
    if need_restart:
        _schedule_self_restart(delay=1.5)
        return True, ("V%s 更新完成: %s。旧文件(含被删文件)备份在 备份/V%s/。"
                      "主程序已更新, 正在自动重启(约 3 秒), 页面稍后会自动刷新。"
                      % (latest, ", ".join(parts) or "无变化", cur))
    return True, ("V%s 更新完成: %s。旧文件(含被删文件)备份在 备份/V%s/。"
                  "前端文件刷新浏览器即生效。"
                  % (latest, ", ".join(parts) or "无变化", cur))


# 系统更新页面(自包含, 无需构建)
UPDATE_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>系统更新 · AI额度管家</title>
<style>
body{font-family:system-ui,'Microsoft YaHei',sans-serif;background:#F3F5F9;color:#1A2B40;margin:0;padding:40px 16px}
.card{max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:28px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{font-size:18px;margin:0 0 4px}.sub{color:#64748B;font-size:13px;margin-bottom:20px}
.ver{background:#F1F5F9;border-radius:8px;padding:10px 14px;font-size:14px;margin-bottom:16px}
.btn{background:#1B6B7A;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:14px;cursor:pointer}
.btn:disabled{opacity:.5;cursor:default}
.btn2{background:#fff;color:#1B6B7A;border:1px solid #1B6B7A}
.msg{margin-top:14px;font-size:13px;padding:10px 14px;border-radius:8px;background:#F1F5F9;white-space:pre-wrap}
ul{font-size:13px;color:#475569;padding-left:20px}
a{color:#1B6B7A}
</style></head><body><div class="card">
<h1>系统更新</h1>
<div class="sub">AI额度管家 · 单机版 — 上传新版本更新包, 自动完成更新(旧文件自动备份)</div>
<div class="ver">当前版本: <b id="cur">…</b></div>
<input type="file" id="file" accept=".zip" style="font-size:13px;margin-bottom:12px"><br>
<button class="btn" id="up" onclick="upload()">上传并更新</button>
<button class="btn btn2" onclick="check()">检查目录内更新包</button>
<a href="/" target="_top" style="float:right;font-size:13px;line-height:38px">← 返回主页</a>
<div id="out"></div>
<script>
async function ver(){const r=await fetch('/api/version');const d=await r.json();document.getElementById('cur').textContent='V'+d.version}
function show(t){document.getElementById('out').innerHTML='<div class="msg">'+t+'</div>'}
async function upload(){
  const f=document.getElementById('file').files[0];
  if(!f){show('请先选择更新包 zip 文件');return}
  document.getElementById('up').disabled=true;show('正在上传并应用更新...');
  try{
    const r=await fetch('/api/update/upload?name='+encodeURIComponent(f.name),{method:'POST',body:f});
    const d=await r.json();show((d.ok?'✅ ':'⚠️ ')+d.detail);
    if(d.ok){show('✅ '+d.detail+'<br><br>页面将在 8 秒后自动刷新…');setTimeout(()=>location.reload(),8000)}
    else ver();
  }catch(e){show('上传失败: '+e)}
  document.getElementById('up').disabled=false;
}
async function check(){
  show('正在检查...');
  const r=await fetch('/api/update/check');const d=await r.json();
  if(d.has_update){
    let h='发现新版本 <b>V'+d.latest+'</b>'+(d.date?' ('+d.date+'）':'')+'<br>';
    if(d.notes&&d.notes.length){h+='<ul>'+d.notes.map(n=>'<li>'+n+'</li>').join('')+'</ul>'}
    h+='<button class="btn" onclick="apply()">立即更新到 V'+d.latest+'</button>';show(h);
  }else show(d.detail||('当前已是最新版本 V'+d.current));
}
async function apply(){
  show('正在更新...');
  const r=await fetch('/api/update/apply',{method:'POST'});const d=await r.json();
  if(d.ok){show('✅ '+d.detail+'<br><br>页面将在 8 秒后自动刷新…');setTimeout(()=>location.reload(),8000)}
  else{show('⚠️ '+d.detail);ver()}
}
ver();
</script></div></body></html>"""

# 系统更新入口已并入「关于系统 → 版本与更新」页(支持上传更新包), 不再在主页注入浮动按钮。


# ---------------------------------------------------------------- 价格政策数据
PRICING_PATH = DATA_HOME / "价格政策.json"

# 内置政策数据(随主程序版本升级)。价格政策.json 属受保护的用户数据, 更新包不会覆盖它,
# 因此每次核对完价格政策, 必须同步更新这里的 _PRICING_BUILTIN 与 updated 日期 ——
# _load_pricing() 会取「本地文件 / 内置数据」中 updated 较新的一份。
_PRICING_BUILTIN = {
    "updated": "2026-10-30",
    "source": "各平台官网公开价格(人工核对整理)",
    "payg": [
        ["DeepSeek", "platform.deepseek.com", "V4-Flash 输入1/输出2;V4-Pro 3.1/6.2(元/百万token)", "预充值余额扣减, 欠费即停;缓存命中输入低至0.02元", "https://api-docs.deepseek.com/quick_start/pricing"],
        ["Kimi 开放平台", "platform.kimi.com", "K2.6/K2.7 Code 输入6.5/输出27;K3 20/100;联网搜索0.03元/次", "充值500元+送20-30%代金券;Batch 6折;缓存命中1.1-2元", "https://platform.kimi.com/docs/pricing"],
        ["智谱 GLM", "bigmodel.cn", "GLM-5 输入7.2/输出23;GLM-4.7 4.3/15.8(元/百万token)", "GLM-4.7-Flash、4.5-Flash 免费", "https://open.bigmodel.cn/pricing"],
        ["MiniMax", "platform.minimaxi.com", "M2 输入2.1/输出8.4;高速版4.2/16.8(元/百万token);图片0.025元/张", "付费默认500 RPM;缓存读取0.21元", "https://platform.minimaxi.com/document/price"],
        ["火山方舟(豆包)", "volcengine.com", "Seed 2.0 Mini 0.2/2 至 2.1 Pro 6/30(元/百万token)", "注册送50万token;Batch约5折", "https://www.volcengine.com/docs/82379/1099320"],
        ["ChatAnywhere(转发)", "api.chatanywhere.tech", "GPT/Claude/Gemini/DeepSeek/Qwen/Kimi 聚合, 各模型约为官方价 6-9 折", "满100元95折/满1000元9折;CA系列(Azure)更低", "https://chatanywhere.apifox.cn/"],
        ["SiliconFlow", "siliconflow.cn", "多模型聚合, 按各模型标价按量计费", "部分小模型免费;注册有体验金", "https://siliconflow.cn/pricing"],
        ["Mistral AI", "console.mistral.ai", "按 token 计费(美元); 有官方余额接口 /v1/credit_balance", "免费实验层(La Plateforme); 批量推理有折扣", "https://mistral.ai/pricing"],
        ["Claude(Anthropic)", "console.anthropic.com", "按 token 计费(美元); 无预付余额接口, Admin API 可查成本报表", "Prompt 缓存可降成本; Batch 约5折", "https://www.anthropic.com/pricing"],
        ["OpenAI", "platform.openai.com", "按 token 计费(美元); 公开余额接口已关闭, Admin Key 可查成本", "Batch API 约5折; 缓存输入折扣", "https://platform.openai.com/docs/pricing"],
        ["Gemini(Google)", "ai.google.dev", "按 token 计费(美元); AI Studio 提供免费额度", "免费层有速率限制; 上下文缓存可降成本", "https://ai.google.dev/pricing"],
        ["Grok(xAI)", "console.x.ai", "按 token 计费(美元); 官方无余额/用量接口", "无免费层; 新账号偶有赠送额度", "https://x.ai/api"],
        ["Groq", "console.groq.com", "按 token 计费(美元), 主打低延迟推理; 官方无用量/余额接口", "免费层有速率限制", "https://groq.com/pricing"],
    ],
    "sub": [
        ["智谱 Coding Plan", "49 / 149 / 469", "5小时窗口+周窗口双限流, 到期自动重置", "包年7折/包季8折(限时)", "https://open.bigmodel.cn/finance/codingplan"],
        ["Kimi Coding Plan", "49 / 99 / 199 / 699", "7天周限 + 5小时频限双窗口", "年付约8折: 39/79/159/559", "https://platform.kimi.com/docs/pricing"],
        ["MiniMax Token Plan", "29 / 49 / 119", "5小时窗口+周窗口双限流", "首月折扣(如 Starter 26.1元)", "https://platform.minimax.cn/docs/pricing/overview"],
        ["百炼 Token Plan(阿里)", "个人版/团队版多档", "Credits 计量, 5小时+周双窗口重置", "sk-sp- 专用 Key; 用量官方无 API, 需控制台查看", "https://help.aliyun.com/zh/model-studio/coding-plan"],
        ["火山方舟 Agent Plan", "Small 9.9(首两月)起 / Medium / Large / Max", "5小时+日+周+月四窗口 AFP 额度", "连续包季9.7折/包年9.5折", "https://www.volcengine.com/product/ark"],
        ["Work Buddy", "99 / 199 / 999", "积分每月刷新", "连续包月7折: 70/140/700", "https://www.workbuddy.cn/pricing"],
        ["豆包会员", "68 / 200 / 500", "会员周期内额度刷新", "连续包年688/2048/5088", "https://www.doubao.com/chat/subscribe"],
    ],
    "note": "智谱、Kimi、MiniMax、火山方舟同时有按量 API 与订阅套餐两种形态, 可分别添加两个不同类型的账号跟踪。Kimi 订阅 Key(sk-kimi-xxx, kimi.com/code/console 创建)与开放平台按量 Key 是两套; 火山方舟 Agent Plan 查用量需火山引擎 Access Key(AKLT 开头, 控制台 → API 访问密钥), 推理 Key 查不了。阿里百炼按量余额可用阿里云 AccessKey 采集, Token Plan 用量官方仅控制台可见。近期价格变动频繁, 录入预算前请以官网实时页面为准。",
    "urls_note": "各平台政策页地址收录在「政策地址」列, 点击直达官网核对最新价格。",
}


def _pricing_version(d):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str((d or {}).get("updated") or ""))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _load_pricing():
    """读取政策数据: 本地文件与内置数据取 updated 较新的一份(内置随版本升级)。
    本地文件损坏/缺失时用内置兜底, 不再返回空表。"""
    data = None
    try:
        data = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("payg"), list):
            data = None
    except Exception:
        data = None
    if data is None or _pricing_version(_PRICING_BUILTIN) > _pricing_version(data):
        out = dict(_PRICING_BUILTIN)
        out["_local"] = True
        out["_builtin"] = True
        return out
    data["_local"] = True
    return data

_PRICING_CHECK_PATH = DATA_HOME / "价格政策.核对.json"
_PRICING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _pricing_urls(data):
    """从政策数据里取出 [(平台, 政策地址)], 去重保序"""
    out, seen = [], set()
    for row in (data.get("payg") or []) + (data.get("sub") or []):
        if not isinstance(row, (list, tuple)) or not row:
            continue
        u = str(row[-1] or "").strip()
        if u.startswith("http") and u not in seen:
            seen.add(u)
            out.append((str(row[0]), u))
    return out


def _page_fingerprint(url):
    """抓取官网政策页 -> (正文指纹, 正文长度)。去掉脚本/样式/标签后取哈希,
    页面价格或政策文字变动 -> 指纹变; 纯动态广告位一般不影响正文。"""
    req = urllib.request.Request(url, headers={"User-Agent": _PRICING_UA,
                                               "Accept": "text/html,*/*",
                                               "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context()) as r:
        raw = r.read(900_000)
    html = raw.decode("utf-8", "ignore")
    txt = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"&[a-zA-Z#0-9]+;", " ", txt)
    txt = re.sub(r"\s+", "", txt)
    # 纯前端渲染的页面只返回一句 "need to enable JavaScript", 正文为空 ——
    # 拿来比对必然误报「有变动」, 这类页面只确认「能打开」, 不做指纹比对
    if len(txt) < 200 or "enableJavaScript" in txt:
        return None, len(txt)
    return hashlib.sha256(txt.encode("utf-8")).hexdigest()[:16], len(txt)


def _pricing_online_check(data):
    """在线核查各平台政策页是否变动(只比对正文指纹, 不改本地价格数据)"""
    urls = _pricing_urls(data)
    try:
        prev = json.loads(_PRICING_CHECK_PATH.read_text(encoding="utf-8"))
        pages = prev.get("pages", {}) if isinstance(prev, dict) else {}
    except Exception:
        pages = {}

    def one(item):
        name, url = item
        try:
            h, n = _page_fingerprint(url)
            if h is None:
                return {"platform": name, "url": url, "status": "shell", "size": n}
            old = (pages.get(url) or {}).get("hash")
            status = "same" if old == h else ("new" if not old else "changed")
            return {"platform": name, "url": url, "status": status, "hash": h, "size": n}
        except Exception as e:
            return {"platform": name, "url": url, "status": "fail",
                    "detail": _short_err(e)}

    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(one, urls))
    except Exception as e:
        return [{"platform": "全部", "url": "", "status": "fail", "detail": _short_err(e)}]
    now_pages = dict(pages)
    for r in results:
        if r.get("hash"):
            now_pages[r["url"]] = {"hash": r["hash"], "at": _now()}
    try:
        _PRICING_CHECK_PATH.write_text(json.dumps(
            {"pages": now_pages, "checkedAt": _now()}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:
        pass
    return results


def _short_err(e):
    """异常 → 短文案。⚠️ V3.32.8: reason 可能是异常对象(如 TimeoutError/SSLError)而非字符串,
    直接切片会 TypeError 把整个核查拖垮 —— 先统一转 str 再截断。"""
    s = str(e)
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        reason = getattr(e, "reason", None)
        if isinstance(reason, BaseException):
            reason = f"{type(reason).__name__}: {reason}"[:60]
        elif reason is not None:
            reason = str(reason)[:60]
        else:
            reason = s[:60]
        return f"无法连接({reason})"
    if "timed out" in s.lower() or "timeout" in s.lower():
        return "超时"
    return s[:90]


def refresh_pricing():
    """检查政策更新(V3.17.11 重做):
    ① 总是重新读盘(不缓存), 保证新版本带来的价格政策立即生效;
    ② 配了 pricing_url 的, 从远程源拉取并覆盖本地(真·在线更新);
    ③ 没配远程源的, 在线核查各平台官网政策页正文指纹, 报告哪些页面有变动。
    返回里带 data(最新完整数据), 前端据此立即重绘表格。"""
    data = _load_pricing()
    url = ""
    try:
        mod = _load_proxy_module()
        if mod:
            url = (mod.load_config().get("pricing_url") or "").strip()
    except Exception:
        url = ""
    if url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "quota-guardian"})
            with urllib.request.urlopen(req, timeout=15,
                                        context=ssl.create_default_context()) as r:
                remote = json.loads(r.read().decode("utf-8"))
            if not isinstance(remote.get("payg"), list) or not isinstance(remote.get("sub"), list):
                return {"ok": False, "detail": "远程政策源格式不正确(缺少 payg/sub 列表)",
                        "data": data}
            old_updated = data.get("updated", "")
            PRICING_PATH.write_text(json.dumps(remote, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
            new_updated = remote.get("updated", "?")
            changed = new_updated != old_updated
            return {"ok": True, "local": False, "changed": changed,
                    "updated": new_updated, "data": _load_pricing(),
                    "detail": (f"已从远程源更新政策数据: {old_updated} → {new_updated}"
                               if changed else f"远程源已是最新政策数据({new_updated})")}
        except Exception as e:
            return {"ok": False, "data": data,
                    "detail": f"远程源拉取失败({_short_err(e)}), 已保留本地政策数据"}

    checks = _pricing_online_check(data)
    changed = [c for c in checks if c["status"] == "changed"]
    failed = [c for c in checks if c["status"] == "fail"]
    fresh = [c for c in checks if c["status"] == "new"]
    shells = [c for c in checks if c["status"] == "shell"]
    head = f"已重新读取政策数据(更新于 {data.get('updated')})"
    tail = []
    if fresh:
        tail.append(f"{len(fresh)} 个首次核查已记下基线")
    if shells:
        tail.append(f"{len(shells)} 个为前端渲染页(只确认可访问)")
    if failed:
        tail.append(f"{len(failed)} 个打不开")
    if changed:
        detail = (f"{head}；在线核查 {len(checks)} 个官网政策页，"
                  f"{len(changed)} 个页面内容有变动：" +
                  "、".join(c["platform"] for c in changed[:5]) +
                  "。请点「政策地址」核对最新条款。")
    elif checks and len(failed) == len(checks):
        detail = f"{head}；官网核查全部失败(可能断网或被拦截)，本次以本地数据为准。"
    else:
        detail = (f"{head}；在线核查 {len(checks)} 个官网政策页，未发现政策变动" +
                  (f"（{'，'.join(tail)}）" if tail else "") + "。")
    return {"ok": True, "local": True, "changed": len(changed), "updated": data.get("updated"),
            "data": data, "checks": checks, "detail": detail, "checkedAt": _now()}


# ---------------------------------------------------------------- HTTP
# 安全加固(2026-10-16): 本机服务也必须防「任意网页跨域读写」与「DNS rebinding」
#   ★ 2026-10-16 二次收紧: 只认「回环主机 + 本服务端口」这一种源。
#     第一版只校验主机名, 于是 http://127.0.0.1:8899(本机另一个端口上的网页) 也被当成
#     "同源"放行 —— 而本地 dev server / 别人发来的 html 恰恰都跑在别的端口上, 等于防线旁路。
#     另外 urlsplit().hostname 会把 "127.0.0.1:8787.evil.com" 解析出 "127.0.0.1" 而误放行,
#     所以改为对 netloc(主机:端口) 做显式字符串比对。
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1", "0.0.0.0"}


def _self_netlocs():
    """本服务在「本机回环」下的合法 netloc(主机:端口)。
    ★ 运行时按当前 PORT 计算, 不要固化成模块级常量 —— 否则端口一变防线就失效。
    """
    return {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}", f"::1:{PORT}"}


def _self_origins():
    """本机回环下的合法 Origin 集合(含无端口写法, 便于手工调试)。"""
    outs = {"http://" + n for n in _self_netlocs()}
    outs |= {"http://" + h for h in ("127.0.0.1", "localhost", "[::1]", "::1")}
    return outs


def _split_hostport(h):
    """把 Host 头 / netloc 拆成 (host, port)。IPv6 形如 [::1]:8787。"""
    h = (h or "").strip()
    if not h:
        return "", ""
    if h.startswith("["):
        host = h.split("]")[0] + "]"
        rest = h.split("]", 1)[1].lstrip(":")
        return host.lower(), rest.strip()
    if ":" in h:
        host, rest = h.rsplit(":", 1)
        return host.lower(), rest.strip()
    return h.lower(), ""


def _origin_allowed(origin, host=None):
    """跨域/CSRF 防线 —— 只放行「与本次请求的 Host 同源」的页面。

    ★ 2026-10-16 三次修正(重要): 改「按请求的 Host 比对」, 而不是写死回环名单。
      原因: 手机端经 FRP 内网穿透访问时, 浏览器发来的 Host/Origin 是公网地址
      (如 203.0.113.10:18001), 写死回环名单会把它当"外站"拒掉 —— 这正是手机端
      突然打不开的根因。改为与 Host 比对后: 本机访问、局域网访问、内网穿透访问
      都能正常工作, 而真正的跨站页面(Origin 与 Host 不同源)依旧被拦。

    还能挡住的情形:
      - 本机别的端口上的网页(本地 dev server / 别人发来的 html): 主机同名但端口不同 -> 拒
      - http://127.0.0.1:8787.域名 这类宽松解析: 按「主机:端口」逐字符比对 -> 拒
      - DNS rebinding(域名解析到 127.0.0.1): Host 与 Origin 虽然同名, 但 Host 非回环,
        会被下面的「远程访问口令」拦下。

    不给 host 时(旧调用 / 单测)退化为「只认本机回环 + 本服务端口」。
    """
    if not origin:
        return True                     # 同源导航 / curl / 本地程序不带 Origin
    o = origin.strip().rstrip("/")
    if not o:
        return True
    try:
        u = urllib.parse.urlsplit(o)
    except Exception:
        return False
    if (u.scheme or "").lower() != "http":
        return False                    # https / file / null 等一律不放行
    netloc = (u.netloc or "").lower()
    if not netloc or "@" in netloc:
        return False                    # 带 userinfo 的一律拒

    if host:
        oh, op = _split_hostport(netloc)
        hh, hp = _split_hostport(host)
        if not oh or not hh:
            return False
        if oh != hh:
            return False
        if op and hp and op != hp:
            return False                # 同主机不同端口 -> 本机另一个网页, 拒
        return True

    if o.lower() in {s.lower() for s in _self_origins()}:
        return True
    return netloc in _self_netlocs()


# ---------------------------------------------------------------- 远程访问口令
# 背景: 手机经 FRP 内网穿透(或局域网)访问本服务, Host 不是回环地址。
# 既要放行这条正常路径, 又不能把服务敞开给任何知道地址的人, 故加一道访问口令:
#   - 本机回环访问: 不需要口令(就是你自己的电脑)
#   - 非回环访问(手机/穿透/局域网): 必须带对口令, 否则给出口令输入页
#   - 口令为空 = 关闭远程访问
# 顺带: 这也把 DNS rebinding 挡在门外(伪造域名的请求是非回环且没口令)。
_TOKEN_COOKIE = "aq_key"
_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"  # 去易混字符


def _gen_token(n=6):
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(n))


def _remote_password():
    """当前远程访问口令(存 data.json 的 settings.remotePassword)。空 = 关闭远程访问。"""
    try:
        return str(_load().get("settings", {}).get("remotePassword") or "")
    except Exception:
        return ""


def _ensure_remote_password():
    """首次启动自动生成一个手机访问口令(可在「关于系统」页查看/修改)。"""
    with _DATA_TX:
        d = _load()
        st = d.setdefault("settings", {})
        if "remotePassword" not in st:
            st["remotePassword"] = _gen_token()
            _save(d)
        return str(st.get("remotePassword") or "")


def _lan_ip():
    """尽力取本机局域网 IPv4(取不到就返回空)。"""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


def _password_page(detail=""):
    """远程访问口令输入页(手机端首屏)。"""
    msg = f'<div class="err">{_html_escape(detail)}</div>' if detail else ""
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI额度管家 · 访问口令</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#F3F5F9;color:#1A2B40;font:16px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:20px}}
.card{{width:100%;max-width:340px;background:#fff;border-radius:14px;padding:26px 22px;
box-shadow:0 6px 24px rgba(18,35,58,.10);text-align:center}}
h1{{font-size:18px;margin:0 0 6px}}
p{{font-size:13px;color:#64748B;margin:0 0 18px}}
input{{width:100%;font-size:18px;letter-spacing:3px;text-align:center;padding:12px;
border:1px solid #CBD5E1;border-radius:10px;outline:none}}
input:focus{{border-color:#1B6B7A}}
button{{width:100%;margin-top:14px;padding:13px;font-size:16px;color:#fff;background:#1B6B7A;
border:0;border-radius:10px}}
.err{{margin-top:12px;font-size:13px;color:#DC2626}}
.tip{{margin-top:16px;font-size:12px;color:#94A3B8;line-height:1.7}}
</style></head><body>
<div class="card">
  <h1>AI额度管家</h1>
  <p>请输入访问口令</p>
  <form method="get" action="/">
    <input name="k" type="password" inputmode="latin" autocomplete="off" placeholder="访问口令" autofocus>
    <button type="submit">进入</button>
  </form>
  {msg}
  <div class="tip">口令在电脑端「关于系统」页可以查看和修改。<br>在电脑上打开本服务无需口令。</div>
</div></body></html>"""


def _html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _url_has_k(url):
    """URL 的 query 里是否带了 k 参数(口令)。"""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url or "").query)
        return "k" in q
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    def _host_parts(self):
        return _split_hostport(self.headers.get("Host"))

    def _is_remote(self):
        """本次请求是否来自「非本机回环」的地址(内网穿透 / 局域网 / 域名)。"""
        host, _p = self._host_parts()
        if not host:
            return False                # 无 Host(极少数本地程序) 视为本机
        return host not in _LOOPBACK_HOSTS

    def _client_token(self):
        """从 query(?k=) 或 cookie 取访问口令。"""
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            k = (q.get("k", [""])[0] or "").strip()
        except Exception:
            k = ""
        if k:
            return k
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                n, v = part.split("=", 1)
                if n.strip() == _TOKEN_COOKIE:
                    return v.strip()
        return ""

    def _wants_html(self):
        """这次请求是不是「要页面」(而非接口)。"""
        p = urllib.parse.urlsplit(self.path).path
        return not p.startswith("/api/")

    def _is_first_screen(self):
        """是不是「首屏导航」(根路径 / 前端路由), 而非 css/js/图片等子资源。"""
        p = urllib.parse.urlsplit(self.path).path
        rel = p.lstrip("/") or "index.html"
        name = Path(rel).name
        return "." not in name or name in ("index.html", "index.htm")

    def _redirect(self, loc):
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Cache-Control", "no-store")
        self._maybe_set_cookie()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _guard(self):
        """统一来源防线。True=放行; False=已回写响应。"""
        self._set_cookie = False        # 每次请求重新判定(实例可能被 keep-alive 复用)
        self._strip_k = False
        host = self.headers.get("Host")
        # ① 跨域/CSRF: Origin 必须与本次请求的 Host 同源
        #    (本机 / 同WiFi / 内网穿透都能正常打开; 其它网站的跨域请求一律拒绝)
        if not _origin_allowed(self.headers.get("Origin"), host):
            self._json(403, {"error": "跨域请求已拒绝",
                             "detail": "本服务只接受同源页面请求, 不接受来自其它网站的请求"})
            return False
        return True

    def _local_only(self):
        """更新/管理接口仅限真正的本机浏览器操作。True=放行; False=已回写响应。
        安全(2026-10-28): 经 nginx+frp 远程进来的请求, 对后端而言来源也是 127.0.0.1
        (frpc 本地回连), 仅看 client_address 无法区分。但 nginx 反代会带上
        X-Real-IP / X-Forwarded-For 头, 本机直连浏览器则没有 —— 以此甄别。
        与服务器 nginx 的 location /api/update/ {return 403} 互为双保险。"""
        if self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP"):
            self._json(403, {"ok": False, "error": "该接口仅限本机操作",
                             "detail": "检测到远程访问特征, 已拒绝。请在部署本机的浏览器上操作。"})
            return False
        return True

    def _redirect_clean(self):
        """把地址栏里的 ?k=口令 去掉(口令已存进 cookie)。"""
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
        rest = [(n, v) for (n, v) in q if n != "k"]
        target = u.path or "/"
        if rest:
            target += "?" + urllib.parse.urlencode(rest)
        self._redirect(target)

    def _send_html(self, code, html):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._maybe_set_cookie()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _maybe_set_cookie(self):
        if getattr(self, "_set_cookie", False):
            pw = _remote_password()
            if pw:
                self.send_header("Set-Cookie",
                                 f"{_TOKEN_COOKIE}={pw}; Path=/; Max-Age=31536000; SameSite=Lax")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # 只对同源页面开放跨域; 不使用 "*", 也不使用 Allow-Private-Network
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if origin and _origin_allowed(origin, self.headers.get("Host")):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self._maybe_set_cookie()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _trpc_ok(self, data, batch):
        item = {"result": {"data": {"json": data}}}
        self._json(200, [item] if batch else item)

    def _trpc_err(self, msg, batch):
        item = {"error": {"json": {"message": msg, "code": -32603,
                "data": {"code": "INTERNAL_SERVER_ERROR", "httpStatus": 500}}}}
        self._json(500, [item] if batch else item)

    def _handle_trpc(self, procs, inputs, batch):
        results = []
        for i, proc in enumerate(procs):
            fn = PROCS.get(proc)
            if not fn:
                results.append(None)
                continue
            inp = inputs.get(str(i), {})
            if isinstance(inp, dict) and "json" in inp:
                inp = inp["json"]
            try:
                results.append(("ok", fn(inp if isinstance(inp, dict) else {})))
            except Exception as e:
                results.append(("err", str(e)))
        if batch:
            out = []
            for r in results:
                if r is None:
                    out.append({"error": {"json": {"message": "unknown procedure", "code": -32601, "data": {"code": "NOT_FOUND", "httpStatus": 404}}}})
                elif r[0] == "ok":
                    out.append({"result": {"data": {"json": r[1]}}})
                else:
                    out.append({"error": {"json": {"message": r[1], "code": -32603, "data": {"code": "INTERNAL_SERVER_ERROR", "httpStatus": 500}}}})
            self._json(200, out)
        else:
            r = results[0]
            if r and r[0] == "ok":
                self._trpc_ok(r[1], False)
            else:
                self._trpc_err(r[1] if r else "unknown procedure", False)

    def do_OPTIONS(self):
        if not self._guard():
            return
        self._json(204, {})

    def do_GET(self):
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        # 安全(2026-10-28): 更新接口(GET check / POST upload+apply)仅限本机
        if path.startswith("/api/update/") and not self._local_only():
            return
        # 口令是通过网址带进来的(口令页提交 ?k=) -> 已存 cookie, 换成干净地址再渲染
        if getattr(self, "_strip_k", False) and not path.startswith("/api/"):
            self._redirect_clean()
            return
        if path.startswith("/api/trpc/"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            batch = q.get("batch", ["0"])[0] == "1"
            procs = path[len("/api/trpc/"):].split(",")
            inputs = {}
            if "input" in q:
                try:
                    inputs = json.loads(q["input"][0])
                except Exception:
                    inputs = {}
            self._handle_trpc(procs, inputs, batch or len(procs) > 1)
            return
        if path.startswith("/api/accounts"):
            self._json(200, proxy_accounts())
            return
        if path.startswith("/api/quota/windows"):
            # 订阅额度窗口(5小时/7天)只读展示: 不写快照、不计费
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            force = q.get("force", ["0"])[0] in ("1", "true", "yes")
            self._json(200, quota_windows({"force": force}))
            return
        if path.startswith("/api/notify/test"):
            # 微信推送配置验证: 发一条测试消息
            try:
                mod = _load_proxy_module()
                if not mod:
                    self._json(200, {"ok": False, "detail": "未找到 额度管家_本地代理.py"})
                    return
                ok, detail = mod.push_alert(mod.load_config(),
                    "AI额度管家 · 测试消息",
                    "微信推送配置成功! 异常消耗熔断/余额预警/每日日报都会推送到这里。")
                self._json(200, {"ok": ok, "detail": detail})
            except Exception as e:
                self._json(200, {"ok": False, "detail": str(e)})
            return
        if path.startswith("/api/notify"):
            # 预警设置读取(V3.29.0): 仅返回 notify 块 + 是否支持, 不含 Key
            try:
                mod = _load_proxy_module()
                cfg = mod.load_config() if mod else {}
                n = cfg.get("notify") or {}
                # 只回传可控字段, webhook/sendkey 打码(界面回显时留空表示"不修改")
                self._json(200, {
                    "enabled": bool(n.get("enabled")),
                    "channel": n.get("channel", "wecom"),
                    "hasWebhook": bool((n.get("webhook") or "").strip()),
                    "hasSendKey": bool((n.get("sendkey") or "").strip()),
                    "burnThresholdPerHour": float(n.get("burn_threshold_per_hour", 20)),
                    "lowBalanceThreshold": float(n.get("low_balance_threshold", 50)),
                    "lowQuotaPercent": float(n.get("low_quota_percent", 20)),
                    "cooldown": int(n.get("cooldown", 7200)),
                    "pollSeconds": mod.poll_seconds(cfg) if mod else 300,
                    # V3.31.1: 自定义预警规则 + 可用规则类型(供前端渲染表单)
                    "rules": mod.get_rules(n) if mod else [],
                    "ruleTypes": ([{"id": k, **v} for k, v in mod.RULE_TYPES.items()]
                                  if mod else []),
                })
            except Exception as e:
                self._json(200, {"error": str(e)})
            return
        if path.startswith("/api/health"):
            self._json(200, {"ok": True, "ts": time.time()})
            return
        if path.startswith("/api/version/history"):
            # 完整版本历史(程序目录 version_history.json), 供「关于系统」页滚动展示
            # ⚠️ 必须排在 /api/version 之前, 否则被前缀匹配吃掉(V3.17.36 修过, 后回退, V3.17.40 再修)
            try:
                hist = json.loads((BASE_DIR / "version_history.json").read_text(encoding="utf-8"))
            except Exception:
                hist = {"count": 0, "items": []}
            self._json(200, hist)
            return
        if path.startswith("/api/security"):
            self._json(200, security_info())
            return
        if path.startswith("/api/storage"):
            self._json(200, storage_info())
            return
        if path.startswith("/api/version"):
            # 附带本版本说明(程序目录 version.json), 供「关于系统」页展示当前版本更新内容
            info = {"version": _version()}
            try:
                man = json.loads((BASE_DIR / "version.json").read_text(encoding="utf-8"))
                info["date"] = man.get("date") or ""
                info["notes"] = man.get("notes") or []
            except Exception:
                info["date"] = ""
                info["notes"] = []
            self._json(200, info)
            return
        if path.startswith("/api/update/check"):
            self._json(200, update_check_local())
            return
        if path == "/api/pricing":
            data = _load_pricing()
            # 附带「上次联网核查时间」(来自 _pricing_check.json), 供页面展示
            try:
                prev = json.loads(_PRICING_CHECK_PATH.read_text(encoding="utf-8"))
                if isinstance(prev, dict) and prev.get("checkedAt"):
                    data = dict(data)
                    data["checkedAt"] = prev["checkedAt"]
            except Exception:
                pass
            self._json(200, data)
            return
        if path == "/update":
            body = UPDATE_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 静态文件
        rel = path.lstrip("/") or "index.html"
        f = (DIST_DIR / rel).resolve()
        if not str(f).startswith(str(DIST_DIR.resolve())):
            # 越界/路径穿越: 明确的 404, 不再静默回落到首页
            self._json(404, {"error": "not found"})
            return
        if not f.is_file():
            # 带扩展名的请求(js/css/图片)找不到 -> 真 404(避免前端拿到 HTML 当 JS 用)
            if path != "/" and "." in Path(rel).name:
                self._json(404, {"error": f"not found: {rel}"})
                return
            # 无扩展名的前端路由(SPA)才回落首页
            f = DIST_DIR / "index.html"
        if not f.exists():
            self._json(404, {"error": "dist 目录缺失, 请确认安装包完整"})
            return
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(f.suffix, "application/octet-stream"))
        self.send_header("Cache-Control", "no-store" if f.suffix == ".html" else "max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        # 安全(2026-10-28): 更新接口仅限本机
        if path.startswith("/api/update/") and not self._local_only():
            return
        # 退出软件(V3.32.2): 界面上给一个明确的退出口, 不依赖托盘图标是否正常
        if path == "/api/app/quit":
            try:
                def _bye():
                    time.sleep(0.5)
                    os._exit(0)
                threading.Thread(target=_bye, daemon=True).start()
                self._json(200, {"ok": True, "detail": "软件将在 1 秒内退出"})
            except Exception as e:
                self._json(200, {"ok": False, "detail": str(e)})
            return

        # 预警设置写入(V3.29.0): 保存 notify 块并热启动/热停止预警线程
        if path == "/api/notify":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(min(length, 16384)) if length > 0 else b"{}"
                inp = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                inp = {}
            self._json(200, set_notify(inp))
            return
        # 打开数据目录(V3.26.0): 仅限本机, 调 plat.open_path 用系统文件管理器打开
        if path == "/api/storage/open":
            if not self._local_only():
                return
            try:
                import plat as _plat
                ok = _plat.open_path(DATA_HOME)
            except Exception:
                ok = False
            self._json(200, {"ok": bool(ok), "path": str(DATA_HOME)})
            return
        if path == "/api/pricing/refresh":
            self._json(200, refresh_pricing())
            return
        # 密钥保护开关(V3.24.0): 加密/解密 accounts_config.json(DPAPI)
        if path == "/api/security":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(min(length, 8192)) if length > 0 else b"{}"
                inp = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                inp = {}
            self._json(200, set_security(inp))
            return
        if path.startswith("/api/update/upload"):
            # 上传更新包(raw body 即 zip 内容)
            # 安全(2026-10-16): ① 先写临时文件, 校验通过才落到正式位置 —— 绝不先落盘再校验
            #                  ② name 必须 .zip 结尾且不得是核心文件名(防止 name=data.json 直写账本)
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            name = q.get("name", ["更新包_upload.zip"])[0]
            name = re.sub(r"[^\w.\-一-鿿]", "_", name) or "更新包_upload.zip"
            # 路径合法性(注: 上面的清洗已把 / 和 \ 换成 _, 这里仍保留判断作为二次兜底)
            if not name.lower().endswith(".zip"):
                self._json(200, {"ok": False, "detail": "只接受 .zip 更新包"})
                return
            if ".." in name or "/" in name or "\\" in name:
                self._json(200, {"ok": False, "detail": "更新包文件名非法"})
                return
            # 核心文件不可被上传写覆盖(防 name=data.json 直接砸坏账本)
            if _norm_rel(name) in _UPLOAD_DENY:
                self._json(200, {"ok": False, "detail": f"{name} 是程序核心文件, 不允许通过上传覆盖"})
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0 or length > 100 * 1024 * 1024:
                self._json(200, {"ok": False, "detail": "文件为空或超过 100MB 限制"})
                return
            payload = self.rfile.read(length)
            tmp = BASE_DIR / "_upload_tmp.zip"
            try:
                tmp.write_bytes(payload)
            except Exception as e:
                self._json(200, {"ok": False, "detail": f"保存上传内容失败: {e}"})
                return
            # 先校验是不是合法更新包(能读出清单), 通过才挪到正式文件名
            try:
                _read_manifest(tmp)
            except Exception as e:
                try:
                    tmp.unlink()
                except Exception:
                    pass
                self._json(200, {"ok": False, "detail": f"这不是有效的更新包(zip 内缺少 version.json / VERSION): {e}"})
                return
            pack = BASE_DIR / name
            try:
                if pack.exists():
                    pack.unlink()
                os.replace(str(tmp), str(pack))
            except Exception as e:
                self._json(200, {"ok": False, "detail": f"放置更新包失败: {e}"})
                return
            ok, detail = apply_update_pack(pack)
            self._json(200, {"ok": ok, "detail": detail})
            return
        if path.startswith("/api/update/apply"):
            pack = _find_pack()
            if not pack:
                self._json(200, {"ok": False, "detail": "未找到更新包: 请把「更新包_Vx.x.zip」放到程序目录, 或在上传页选择文件"})
                return
            ok, detail = apply_update_pack(pack)
            self._json(200, {"ok": ok, "detail": detail})
            return
        if path.startswith("/api/accounts"):
            # 网页添加 API 账号(输 Key), 转发给本地代理写入配置
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception:
                payload = {}
            self._json(200, proxy_upsert(payload))
            return
        if path.startswith("/api/trpc/"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            batch = q.get("batch", ["0"])[0] == "1"
            procs = path[len("/api/trpc/"):].split(",")
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception:
                body = {}
            inputs = body if batch or len(procs) > 1 else {"0": body}
            self._handle_trpc(procs, inputs, batch or len(procs) > 1)
            return
        self._json(404, {"error": "unknown path"})

    def log_message(self, fmt, *args):
        pass  # 保持控制台干净

# 代理模块进程级缓存(2026-10-25): 每次请求都 exec_module 会让模块内的查询缓存失效,
# 导致每次打开页面都重新实时查 4 个平台(~1.5s)。文件未变则复用已加载模块。
_PROXY_MOD_CACHE = {"mod": None, "stamp": None}
_PROXY_MOD_LOCK = threading.Lock()


def _load_proxy_module():
    """加载本地代理模块(查询余额/预警推送引擎) —— 带进程级缓存。

    发布包/内测包里代理在主程序同目录; 开发台里在 本地代理\\ 子目录, 两者都支持。
    """
    proxy_file = BASE_DIR / "额度管家_本地代理.py"
    if not proxy_file.exists():
        alt = BASE_DIR / "本地代理" / "额度管家_本地代理.py"
        if alt.exists():
            proxy_file = alt
    if not proxy_file.exists():
        return None
    try:
        st = proxy_file.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    with _PROXY_MOD_LOCK:
        if _PROXY_MOD_CACHE["mod"] is not None and _PROXY_MOD_CACHE["stamp"] == stamp:
            return _PROXY_MOD_CACHE["mod"]
        import importlib.util
        spec = importlib.util.spec_from_file_location("local_proxy", proxy_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PROXY_MOD_CACHE["mod"] = mod
        _PROXY_MOD_CACHE["stamp"] = stamp
        return mod


def _proxy_items(accounts):
    """build_accounts 输出 -> importAll items 格式(与前端同步逻辑一致)"""
    items = []
    for a in accounts:
        is_sub = a.get("apiType") == "sub"
        items.append({
            "platform": a.get("platformId") or a.get("platform", "custom"),
            "name": a.get("name", ""),
            "groupName": a.get("groupName") or "默认",
            "apiType": "sub" if is_sub else "payg",
            "remaining": 0 if is_sub else float(a.get("remaining") or 0),
            "quotaPercent": (float(a.get("quotaPercent")) if a.get("quotaPercent") is not None else 100) if is_sub else None,
            "monthlyUsed": float(a.get("monthlyUsed") or 0),
            "monthlyFee": float(a.get("monthlyFee") or 0) if is_sub else 0,
            "totalRecharge": 0 if is_sub else float(a.get("totalRecharge") or 0),
            "customName": a.get("customName") or "",     # V3.34.1: 同号去重身份字段
            "phone": a.get("phone") or "",
            "collectMethod": a.get("collectMethod") or "API直连",
            "note": a.get("note") or "",
            "validUntil": a.get("validUntil") or "",
        })
    return items


def _auto_sync_loop(mod, interval):
    """后台自动同步: 定时查询真实余额并写进 data.json —— 网页数字自动更新, 无需手动点同步。
    首次启动立即同步一次, 之后每 interval 秒一次。"""
    while True:
        try:
            cfg = mod.load_config()
            if any(mod.auto_collectable(a) for a in cfg.get("accounts", [])):
                accounts = mod.build_accounts()  # 实时查询各平台余额(60秒缓存)
                items = _proxy_items(accounts)
                if items:
                    r = proc_import_all({"items": items})
                    print(f"  [自动同步] {time.strftime('%H:%M:%S')} 已刷新 {r['total']} 个账号余额", flush=True)
        except Exception as e:
            print(f"  [自动同步] 本次失败({e}), 下个周期重试", flush=True)
        time.sleep(interval)


_ALERT_THREAD = {"t": None, "stop": None}


def _start_alert_thread(mod, cfg):
    """启动(或热重启)微信预警线程。V3.29.0: 界面改配置后立即生效, 不必重启软件。"""
    n = cfg.get("notify") or {}
    if not n.get("enabled"):
        return False
    old_stop = _ALERT_THREAD.get("stop")
    if old_stop is not None:
        old_stop.set()                      # 通知老线程下一轮退出
    stop = threading.Event()
    _ALERT_THREAD["stop"] = stop
    t = threading.Thread(target=mod.snapshot_loop, args=(stop,), daemon=True)
    t.start()
    _ALERT_THREAD["t"] = t
    return True


def _start_daemons():
    """启动后台线程: ①自动同步(无条件, 网页数字实时更新) ②微信预警(notify.enabled 时)"""
    try:
        mod = _load_proxy_module()
        if not mod:
            return
        cfg = mod.load_config()
        interval = int(cfg.get("poll_seconds", 300))
        # ① 自动同步(无条件启动): 定时查真实余额写进 data.json
        threading.Thread(target=_auto_sync_loop, args=(mod, interval), daemon=True).start()
        print(f"  自动同步: 已启动(每 {interval} 秒刷新真实余额到网页)")
        # ② 政策数据自动更新: 启动时+每天一次(配置 pricing_url 后生效)
        threading.Thread(target=_pricing_auto_loop, daemon=True).start()
        # ③ 微信预警(仅开启时)
        if _start_alert_thread(mod, cfg):
            print(f"  微信预警: 已启动(每 {mod.poll_seconds(cfg)} 秒采集+评估, 异常自动推送)")
        else:
            print("  微信预警: 未开启(可在「关于系统 → 微信预警」一键开启)")
    except Exception as e:
        print(f"  后台线程: 启动失败({e}), 不影响主程序")


def _pricing_auto_loop():
    """政策数据自动更新: 启动10秒后查一次, 之后每天查一次(pricing_url 有配置才拉取)"""
    time.sleep(10)
    while True:
        try:
            r = refresh_pricing()
            if r.get("ok"):
                print(f"  [政策数据] {r.get('detail', '')}", flush=True)
        except Exception:
            pass
        time.sleep(24 * 3600)


def _cleanup_orphan_assets():
    """启动自愈: 清理 dist/assets 里没被 dist/index.html 引用的孤儿产物。
    背景——前端产物每次改内容都会改名(规避 1 天静态缓存), 旧文件会一直堆着。
    更新包的 deletes 字段只能管住"新版本发的包", 管不住历史遗留, 所以这里兜底。
    安全约束: 只动 dist/assets 目录、只删未被 index.html 引用的文件、删前先备份。
    返回被清理的文件名列表。"""
    assets = BASE_DIR / "dist" / "assets"
    index = BASE_DIR / "dist" / "index.html"
    if not assets.is_dir() or not index.is_file():
        return []
    try:
        html = index.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    removed = []
    backup = BASE_DIR / "备份" / "孤儿产物清理"
    for f in assets.iterdir():
        if not f.is_file():
            continue
        # index.html 里引用了这个文件名 -> 保留
        if f.name in html:
            continue
        try:
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(f), str(backup / f.name))
            f.unlink()
            removed.append(f.name)
        except Exception:
            pass
    return removed


def _startup_selfcheck():
    """启动自检: 校验账本可读、备份健康、清理陈旧临时文件。返回 (ok, 提示语列表)。
    数据完整性出问题时明确告警, 而不是等用户发现数字不对。"""
    msgs = []
    ok = True
    # ① 清理上次异常残留的临时文件
    try:
        tmp = DATA_PATH.with_name("data.json.tmp")
        if tmp.exists():
            tmp.unlink()
            msgs.append("已清理上次未完成的临时文件 data.json.tmp")
    except Exception:
        pass
    try:
        up = BASE_DIR / "_upload_tmp.zip"
        if up.exists():
            up.unlink()
            msgs.append("已清理上次未完成的上传临时文件")
    except Exception:
        pass
    # ② 检查账本
    try:
        d = _load()
        n_acc = len(d.get("accounts", []))
        n_snap = len(d.get("snapshots", []))
        msgs.append(f"账本正常: {n_acc} 个账号 / {n_snap} 条余额快照")
        if _DEGRADED.get("on"):
            ok = False
            msgs.append(f"⚠️ 数据文件异常: {_DEGRADED.get('reason')}")
    except Exception as e:
        ok = False
        msgs.append(f"⚠️ 账本自检失败: {e}")
    # ③ 备份健康度: 没有 .bak 就立刻补一份
    try:
        if DATA_PATH.is_file() and not _bak_path().is_file():
            DATA_PATH.with_name("data.json.bak").write_text(
                DATA_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            msgs.append("已创建首份自动备份 data.json.bak")
    except Exception:
        pass
    return ok, msgs


def _open_ui():
    """打开主界面：优先 Edge/Chrome --app 独立窗口(类原生观感), 失败回退默认浏览器。

    设 QUOTA_NO_APP_WINDOW=1 可强制走默认浏览器(调试用)。
    """
    url = f"http://{HOST}:{PORT}/"
    if not str(os.environ.get("QUOTA_NO_APP_WINDOW") or "").strip():
        try:
            import plat as _plat
            if _plat.open_app_window(url):
                return
        except Exception:
            pass
    webbrowser.open(url)


def main():
    # 自动重启(V3.17.41)时旧进程刚退出、端口可能有几毫秒残留, 重试 3 次再认输
    server = None
    for _try in range(3):
        try:
            server = ThreadingHTTPServer((HOST, PORT), Handler)
            break
        except OSError:
            if _try < 2:
                time.sleep(1)
    if server is None:
        print(f"端口 {PORT} 被占用——可能已经启动了一个额度管家, 直接打开 http://{HOST}:{PORT} 即可")
        _open_ui()
        return
    print("=" * 46)
    print(f"  AI额度管家 V{_version()} 已启动")
    print(f"  本机地址: http://{HOST}:{PORT}")
    # 手机/远程访问: 直接显示三种地址(本机 / 同WiFi / 外网穿透), 不再设访问口令
    try:
        info = _access_info()
        lan = info.get("lanUrl") or ""
        rem = info.get("remoteUrl") or ""
        if lan:
            print(f"  手机(同局域网): {lan}")
        if rem:
            print(f"  手机(外网穿透): {rem}")
    except Exception:
        pass
    print("  数据文件: " + str(DATA_PATH))
    try:
        orphan = _cleanup_orphan_assets()
        if orphan:
            print("  已清理废弃前端产物: " + ", ".join(orphan)
                  + "(备份在 备份/孤儿产物清理/)")
    except Exception:
        pass
    try:
        _ok, _msgs = _startup_selfcheck()
        for _m in _msgs:
            print("  " + _m)
    except Exception:
        pass
    _start_daemons()
    print("  关闭本窗口即停止运行")
    print("=" * 46)
    threading.Timer(1.0, _open_ui).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
