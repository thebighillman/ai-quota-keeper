#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI额度管家 · 一键启动器 (V3.1 单机版)
====================================
双击运行(或 python3 启动额度管家.py)即可:
  1. 启动本机服务(只监听 127.0.0.1:8787, 外网访问不到)
  2. 自动打开浏览器进入网页
  3. 数据保存在同目录 data.json, 不需要安装数据库 / Node.js

可选: 把「额度管家_本地代理.py」和 accounts_config.json 放在同目录,
网页里点「从本地代理同步」就会把代理查到的真实余额/配额导入进来。
Key 与 Cookie 只存在你自己电脑上。
"""

import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST, PORT = "127.0.0.1", 8787
BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist"
DATA_PATH = BASE_DIR / "data.json"
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
              "请手动从 备份/ 目录还原 data.json 后重启。", flush=True)
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
    return d

DEFAULTS = {"platform": "custom", "name": "", "groupName": "默认", "apiType": "payg",
            "remaining": 0, "quotaPercent": None, "monthlyUsed": 0, "monthlyFee": 0,
            "totalRecharge": 0, "budget": 0, "collectMethod": "手动录入",
            "note": None, "validUntil": None}

def proc_list(_inp):
    return sorted(_load()["accounts"], key=lambda a: a["id"])

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
    """删除账号 —— 改为「软删除」:
    账号本身从列表移除(界面不再显示), 但它的历史快照全部保留、账号标记进 deletedAccounts,
    这样账目统计仍能反映"我历史上到底花了多少钱", 不会因为删账号让花销凭空蒸发。
    """
    with _DATA_TX:
        d = _load()
        removed = next((a for a in d["accounts"] if a["id"] == inp["id"]), None)
        if removed:
            # 归档账号信息(保留平台/名称, 供账目统计按平台归集)
            arch = d.setdefault("deletedAccounts", {})
            arch[str(removed["id"])] = {
                "id": removed["id"], "platform": removed.get("platform"),
                "name": removed.get("name"), "apiType": removed.get("apiType"),
                "monthlyFee": removed.get("monthlyFee") or 0,
                "createdAt": removed.get("createdAt"),
                "deletedAt": _now(),
            }
        d["accounts"] = [a for a in d["accounts"] if a["id"] != inp["id"]]
        # ★ 不再物理删除快照 —— 历史消耗必须留在账本里
        _save(d)
    # 联动删除代理配置里的同名账号, 防止自动同步把已删账号又带回来
    if removed:
        _remove_from_proxy_config(removed.get("platform"), removed.get("name"))
    return {"ok": True}

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
    """本月真实消耗 = 本月内「相邻两次快照余额下降额」累加(余额上升视为充值不计)。
    大多数平台(如 Kimi)没有本月用量接口, 但系统快照里就有答案。
    口径与 账目统计/趋势图 完全一致(2026-10-16 统一, 旧版"同日首末差值"会漏算充值当天消耗)。"""
    month_prefix = datetime.now().strftime("%Y-%m")
    rows = []
    for s in d.get("snapshots", []):
        if s.get("accountId") != account_id:
            continue
        t = str(s.get("collectedAt", ""))
        if not t.startswith(month_prefix):
            continue
        rows.append((t, s.get("remaining")))
    rows.sort(key=lambda x: x[0])
    total = 0.0
    for i in range(1, len(rows)):
        try:
            delta = float(rows[i][1]) - float(rows[i - 1][1])
        except (TypeError, ValueError):
            continue
        if delta < 0:
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
            snap_val = (data["quotaPercent"] if data["quotaPercent"] is not None else 100) if data["apiType"] == "sub" else data["remaining"]
            exist = next((a for a in d["accounts"] if a["platform"] == data["platform"] and a["name"] == data["name"]), None)
            if exist:
                data["id"] = exist["id"]; data["createdAt"] = exist.get("createdAt", _now())
                # 用户手动设置的字段不被同步覆盖(预算/分组/累计充值/月费/到期日)
                data["budget"] = exist.get("budget", 0)
                data["groupName"] = exist.get("groupName", data["groupName"])
                if exist.get("totalRecharge"):
                    data["totalRecharge"] = exist["totalRecharge"]
                if exist.get("monthlyFee"):
                    data["monthlyFee"] = exist["monthlyFee"]
                if exist.get("validUntil"):
                    data["validUntil"] = exist["validUntil"]
                # 本月消耗: 平台接口没给(=0)时, 用系统快照自动计算(真实值)
                if data.get("apiType") != "sub" and not data.get("monthlyUsed"):
                    data["monthlyUsed"] = _month_used_from_snapshots(d, exist["id"])
                d["accounts"][d["accounts"].index(exist)] = data
                aid = exist["id"]
            else:
                aid = d["nextId"]; d["nextId"] += 1
                data["id"] = aid; data["createdAt"] = _now()
                d["accounts"].append(data)
                added += 1
            d["snapshots"].append({"accountId": aid, "remaining": snap_val, "collectedAt": _now()})
        _save(d)
    return {"ok": True, "added": added, "total": len(inp.get("items", []))}

def month_cost(a):
    """统一成本口径: 成本 = Σ(用量 × 单价)。
    按量账号: 余额实扣额(平台已按各资源官方单价算好的真实扣费), 充值自动剔除, 即 monthlyUsed。
    订阅账号: 套餐折算单价 = 月费 ÷ 套餐总量, 消耗按已用配额比例摊销 → 月费 × (100 - 剩余配额%)/100。
    免费额度(配额包赠送/免费档) 单价记为 0, 故成本为 0, 仅记录等效价值。"""
    if a.get("apiType") == "sub":
        fee = float(a.get("monthlyFee") or 0)
        qp = a.get("quotaPercent")
        if not fee or qp is None:
            return 0.0
        used_pct = max(0.0, min(100.0, 100.0 - float(qp)))
        return round(fee * used_pct / 100.0, 2)
    return float(a.get("monthlyUsed") or 0)


def sub_used_pct(a):
    """订阅账号已用配额百分比(0-100); 无配额数据返回 None"""
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
    led = _ledger_data(d)
    cur_ym = datetime.now().strftime("%Y-%m")
    cur_month = next((m for m in led.get("months", []) if m["month"] == cur_ym), None)
    cur_plat = (cur_month or {}).get("byPlatform", {}) or {}
    month_total = float((cur_month or {}).get("total", 0.0))
    remaining_total = sum((0 if a.get("apiType") == "sub" else a.get("remaining", 0)) for a in lst)
    # 按量账号累计充值合计(总投入额度的参考值之一)
    recharge_total = sum(float(a.get("totalRecharge") or 0) for a in lst if a.get("apiType") != "sub")
    by_plat = {}
    for a in lst:
        cur = by_plat.setdefault(a["platform"], {
            "platform": a["platform"], "used": 0, "budget": 0, "remaining": 0,
            "subQuota": None, "subUsedPct": None, "subCount": 0, "subFee": 0,
            "paygUsed": 0, "subCost": 0,
        })
        cur["budget"] += a.get("budget", 0)
        if a.get("apiType") == "sub":
            cur["subCount"] += 1
            cur["subFee"] += float(a.get("monthlyFee") or 0)
            cur["subCost"] += month_cost(a)
            up = sub_used_pct(a)
            if up is not None:
                cur["_up"] = cur.get("_up", []) + [up]
        else:
            cur["remaining"] += a.get("remaining", 0)
            cur["paygUsed"] += float(a.get("monthlyUsed") or 0)
    for p, c in by_plat.items():
        ups = c.pop("_up", [])
        if ups:
            c["subUsedPct"] = round(sum(ups) / len(ups), 1)
            c["subQuota"] = round(100 - c["subUsedPct"], 1)
        # 本月消耗改用账目统计口径(与「账目统计」页完全一致)
        c["used"] = round(float(cur_plat.get(p, 0.0)), 2)
    alerts = [{"id": a["id"], "level": a["status"], "platform": a["platform"], "name": a["name"],
               "title": f"{a['name']} 额度预警", "desc": a["statusText"]}
              for a in lst if a.get("status") != "success"]
    # 订阅配额总览(用于占比区展示订阅口径)
    sub_list = []
    for a in lst:
        if a.get("apiType") == "sub":
            sub_list.append({"name": a["name"], "platform": a["platform"],
                             "usedPct": sub_used_pct(a), "fee": float(a.get("monthlyFee") or 0),
                             "cost": month_cost(a)})
    return {"monthTotal": round(month_total, 2), "remainingTotal": remaining_total,
            "rechargeTotal": round(recharge_total, 2),
            "month": cur_ym,
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
            # 订阅账号: 用「当日配额快照均值」画配额曲线(与消耗无关)
            for t, rem in arr:
                if t < since:
                    continue
                dq = daily_quota.setdefault(t.date().isoformat(), {})
                cur = dq.setdefault(a["platform"], [0.0, 0])
                cur[0] += float(rem or 0); cur[1] += 1
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
            du[a["platform"]] = du.get(a["platform"], 0.0) + (-delta)
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
            c = qd.get(p)
            qrow[p] = round(c[0] / c[1], 1) if c else None
        quota_out.append(qrow)
    return {"days": out, "platforms": payg_plats, "quotaDays": quota_out, "subPlatforms": sub_plats}

def _days_in_month(y, m):
    nxt = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    return (nxt - datetime(y, m, 1)).days


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

    # "YYYY-MM" -> {platform: amount}
    m_plat = {}
    m_plat_full = {}
    # "YYYY-MM" -> {accountId: amount}
    m_acct = {}
    m_acct_full = {}

    def _add(mkey, aid, plat, amount, full=None):
        if amount <= 0:
            return
        mp = m_plat.setdefault(mkey, {})
        mp[plat] = mp.get(plat, 0.0) + amount
        ma = m_acct.setdefault(mkey, {})
        ma[aid] = ma.get(aid, 0.0) + amount
        if full is not None:
            mpf = m_plat_full.setdefault(mkey, {})
            mpf[plat] = mpf.get(plat, 0.0) + full
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
            _add(mid.strftime("%Y-%m"), aid, plat, -delta)

    # ---- 2) 订阅账号: 月费（按天折算 + 全月口径） ----
    for aid, a in accs.items():
        if a.get("apiType") != "sub":
            continue
        fee = float(a.get("monthlyFee") or 0)
        if fee <= 0:
            continue
        plat = a.get("platform") or "custom"
        try:
            start = datetime.fromisoformat(str(a.get("createdAt") or ""))
        except ValueError:
            start = today
        start = datetime(start.year, start.month, start.day)
        cur = datetime(start.year, start.month, 1)
        guard = 0
        while cur <= today and guard < 600:
            guard += 1
            y, m = cur.year, cur.month
            n_days = _days_in_month(y, m)
            m_start = datetime(y, m, 1)
            m_last = m_start + timedelta(days=n_days - 1)
            lo = max(start, m_start)
            hi = min(datetime(today.year, today.month, today.day), m_last)
            active = (hi - lo).days + 1 if hi >= lo else 0
            if active > 0:
                _add(f"{y:04d}-{m:02d}", aid, plat,
                     round(fee * active / n_days, 6), full=fee)
            cur = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)

    keys = sorted(set(m_plat) | set(m_plat_full))
    if not keys:
        return {"months": [], "years": [], "platforms": [], "generatedAt": _now(),
                "hasData": False}
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
            out.append({"accountId": aid, "name": a.get("name") or "?",
                        "platform": a.get("platform") or "custom",
                        "apiType": a.get("apiType") or "payg",
                        "monthlyFee": round(float(a.get("monthlyFee") or 0), 2),
                        "amount": round(v, 2)})
        out.sort(key=lambda x: -x["amount"])
        return out

    months = []
    for mk in seq:
        mp = m_plat.get(mk, {})
        mpf = m_plat_full.get(mk, {})
        payg = sub = sub_full = 0.0
        for aid, v in (m_acct.get(mk) or {}).items():
            a = accs.get(aid) or {}
            if a.get("apiType") == "sub":
                sub += v
                sub_full += (m_acct_full.get(mk) or {}).get(aid, v)
            else:
                payg += v
        months.append({
            "month": mk,
            "byPlatform": {p: round(mp.get(p, 0.0), 2) for p in platforms},
            "byPlatformFull": {p: round(mpf.get(p, 0.0), 2) for p in platforms},
            "paygTotal": round(payg, 2),
            "subTotal": round(sub, 2),
            "subTotalFull": round(sub_full, 2),
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
            "total": round(sum(ym.values()), 2),
            "totalFull": round(sum(ymf.values()), 2),
            "monthCount": sum(1 for mk in seq if mk[:4] == y),
        })
    return {"months": months, "years": years, "platforms": platforms,
            "generatedAt": _now(), "hasData": True}


def proc_ledger(_inp):
    """账目统计接口: 读账本 → 交给 _ledger_data 计算(与消耗总览同口径)。"""
    return _ledger_data(_load())


def proc_get_total_quota(_inp):
    """总投入额度(用户手动填写): 所有账号的总投入, 含已消费部分。

    因为涉及订阅套餐费 + API 充值费两类支出, 系统无法自动算准, 故由用户手动填写;
    另返回系统可自动统计的参考值(rechargeTotal=按量累计充值, spentTotal=累计消耗)。
    """
    d = _load()
    s = d.get("settings", {})
    raw = s.get("totalQuota", "")
    try:
        total = float(raw) if raw not in ("", None) else 0.0
    except (TypeError, ValueError):
        total = 0.0
    led = _ledger_data(d)
    spent = round(sum(float(y.get("total", 0.0)) for y in led.get("years", [])), 2)
    recharge = sum(float(a.get("totalRecharge") or 0) for a in d.get("accounts", [])
                   if a.get("apiType") != "sub")
    return {"totalQuota": total, "hasValue": raw not in ("", None),
            "spentTotal": spent, "rechargeTotal": round(recharge, 2)}


def proc_set_total_quota(inp):
    """保存总投入额度(手动填写)。传空字符串表示清除。"""
    with _DATA_TX:
        d = _load()
        v = inp.get("totalQuota", "")
        try:
            fv = float(v)
        except (TypeError, ValueError):
            fv = 0.0
        if fv < 0:
            fv = 0.0
        d.setdefault("settings", {})["totalQuota"] = ("" if fv <= 0 else str(fv))
        _save(d)
    return {"ok": True, "totalQuota": fv}


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

PROCS = {
    "quota.list": proc_list, "quota.upsert": proc_upsert, "quota.remove": proc_remove,
    "quota.collect": proc_collect, "quota.importAll": proc_import_all,
    "quota.summary": proc_summary, "quota.trend": proc_trend,
    "quota.ledger": proc_ledger,
    "quota.getTotalQuota": proc_get_total_quota, "quota.setTotalQuota": proc_set_total_quota,
    "quota.getBudget": proc_get_budget, "quota.setBudget": proc_set_budget,
    "quota.setPlatformBudget": proc_set_platform_budget,
}

# ---------------------------------------------------------------- 代理采集(可选)
def proxy_accounts():
    """同目录存在 额度管家_本地代理.py 时, 调用它查询真实余额/配额"""
    proxy_file = BASE_DIR / "额度管家_本地代理.py"
    if not proxy_file.exists():
        return {"accounts": [], "hint": "未找到 额度管家_本地代理.py"}
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("local_proxy", proxy_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
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
    "使用说明.md",
}
# 上传接口额外禁止: 这些名字即使是 .zip 也一律拒绝(防 name 直写核心文件)
_UPLOAD_DENY = _PROTECTED_FILES | {"VERSION", "version.json", "启动额度管家.py", "额度管家_本地代理.py"}


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


def apply_update_pack(pack):
    """应用更新包: 校验版本 -> 备份旧文件 -> 解压替换 + 删除废弃文件到本目录。返回 (ok, detail)"""
    cur = _version()
    try:
        man = _read_manifest(pack)
    except KeyError:
        return False, "更新包里没有 version.json, 不是有效的更新包"
    except Exception as e:
        return False, f"读取更新包失败: {e}"
    latest = str(man.get("version") or "")
    if _ver_tuple(latest) <= _ver_tuple(cur):
        return False, f"当前已是最新版本 V{cur}(更新包为 V{latest}), 无需更新"
    files = man.get("files") or man.get("proxy_files") or []
    deletes = man.get("deletes") or []
    if not files and not deletes:
        return False, "更新清单里既没有要更新的文件, 也没有要删除的文件"
    allowed = {".py", ".md", ".json", ".txt", ".html", ".js", ".css", ".bat", ".command", ""}
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
    return True, ("V%s 更新完成: %s。旧文件(含被删文件)备份在 备份/V%s/。"
                  "前端文件刷新浏览器即生效; 若更新了 启动额度管家.py 请重启本程序"
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
    const d=await r.json();show((d.ok?'✅ ':'⚠️ ')+d.detail);ver();
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
  show((d.ok?'✅ ':'⚠️ ')+d.detail);ver();
}
ver();
</script></div></body></html>"""

# 系统更新入口已并入「关于系统 → 版本与更新」页(支持上传更新包), 不再在主页注入浮动按钮。


# ---------------------------------------------------------------- 价格政策数据
PRICING_PATH = BASE_DIR / "价格政策.json"

# 内置默认(文件缺失时兜底, 与最新整理版保持同步)
_PRICING_DEFAULT = {
    "updated": "2026-09-12",
    "source": "各平台官网公开价格(人工核对整理)",
    "payg": [], "sub": [],
    "note": "价格政策文件缺失, 请从更新包恢复 价格政策.json 或以官网实时页面为准。"
}

def _load_pricing():
    """读取本地价格政策数据(供帮助页动态渲染)"""
    try:
        data = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
        data["_local"] = True
        return data
    except Exception:
        d = dict(_PRICING_DEFAULT)
        d["_local"] = True
        return d

def refresh_pricing():
    """从配置的远程地址拉取最新价格政策(覆盖本地)。远程源在 accounts_config.json 的 pricing_url 配置。"""
    try:
        mod = _load_proxy_module()
        url = ""
        if mod:
            url = (mod.load_config().get("pricing_url") or "").strip()
        if not url:
            cur = _load_pricing().get("updated", "?")
            return {"ok": True, "local": True, "updated": cur,
                    "detail": f"政策数据随版本更新包同步更新(当前版本 {cur}), 无需联网检查。"}
        req = urllib.request.Request(url, headers={"User-Agent": "quota-guardian"})
        import ssl as _ssl
        with urllib.request.urlopen(req, timeout=15, context=_ssl.create_default_context()) as r:
            data = json.loads(r.read().decode("utf-8"))
        # 基本结构校验
        if not isinstance(data.get("payg"), list) or not isinstance(data.get("sub"), list):
            return {"ok": False, "detail": "远程数据格式不正确(缺少 payg/sub 列表)"}
        old_updated = _load_pricing().get("updated", "")
        PRICING_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        new_updated = data.get("updated", "?")
        changed = new_updated != old_updated
        return {"ok": True, "updated": new_updated, "changed": changed,
                "detail": f"政策数据已更新: {old_updated} → {new_updated}" if changed else f"已是最新政策数据({new_updated})"}
    except Exception as e:
        return {"ok": False, "detail": f"拉取失败: {e}"}


# ---------------------------------------------------------------- HTTP
# 安全加固(2026-10-16): 本机服务也必须防「任意网页跨域读写」与「DNS rebinding」
#   ★ 2026-10-16 二次收紧: 只认「回环主机 + 本服务端口」这一种源。
#     第一版只校验主机名, 于是 http://127.0.0.1:8899(本机另一个端口上的网页) 也被当成
#     "同源"放行 —— 而本地 dev server / 别人发来的 html 恰恰都跑在别的端口上, 等于防线旁路。
#     另外 urlsplit().hostname 会把 "127.0.0.1:8787.evil.com" 解析出 "127.0.0.1" 而误放行,
#     所以改为对 netloc(主机:端口) 做显式字符串比对。
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _self_netlocs():
    """本服务的合法 netloc 集合(主机:端口)。
    ★ 运行时按当前 PORT 计算, 不要固化成模块级常量 —— 否则端口一变防线就失效。
    """
    return {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}", f"::1:{PORT}"}


def _self_origins():
    """本服务的合法 Origin 集合(含无端口的理论写法, 便于手工调试)。"""
    outs = {"http://" + n for n in _self_netlocs()}
    outs |= {"http://" + h for h in ("127.0.0.1", "localhost", "[::1]", "::1")}
    return outs


def _origin_allowed(origin):
    """只放行「本机回环主机 + 本服务端口」的同源页面。空 Origin(同源导航/curl/本地程序)也放行。
    其余一律拒绝 —— 包括本机其它端口上的网页(本地 dev server、他人发来的 html)。
    """
    if not origin:
        return True
    o = origin.strip().rstrip("/")
    if not o:
        return True
    if o.lower() in {s.lower() for s in _self_origins()}:
        return True
    try:
        u = urllib.parse.urlsplit(o)
    except Exception:
        return False
    if (u.scheme or "").lower() != "http":
        return False            # https / file / null 等一律不放行
    netloc = (u.netloc or "").lower()
    if not netloc or "@" in netloc:
        return False            # 带 userinfo 的一律拒
    return netloc in _self_netlocs()


class Handler(BaseHTTPRequestHandler):
    def _host_ok(self):
        """校验 Host 头: 只接受本机回环地址(带端口时必须等于本服务端口)。
        挡掉 DNS rebinding(evil.com 解析到 127.0.0.1)。"""
        h = (self.headers.get("Host") or "").strip()
        if not h:
            return True
        if h.startswith("["):                       # IPv6: [::1]:8787
            host = h.split("]")[0] + "]"
            rest = h.split("]", 1)[1].lstrip(":")
        else:
            if ":" in h:
                host, rest = h.rsplit(":", 1)
            else:
                host, rest = h, ""
        port = rest.strip()
        if port and port != str(PORT):
            return False                            # 端口不符 -> 拒绝
        return host.lower() in _ALLOWED_HOSTS

    def _guard(self):
        """统一的来源防线。返回 True=放行; False=已回写 403。"""
        if not self._host_ok():
            self._json(403, {"error": "非法 Host", "detail": "只允许通过 127.0.0.1 / localhost 访问"})
            return False
        if not _origin_allowed(self.headers.get("Origin")):
            self._json(403, {"error": "跨域请求已拒绝",
                             "detail": "本服务只允许本机页面访问, 不接受来自其它网站的请求"})
            return False
        return True

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # 只对本机同源页面开放跨域; 不再使用 "*", 并移除 Allow-Private-Network
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if origin and _origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
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
        if path.startswith("/api/health"):
            self._json(200, {"ok": True, "ts": time.time()})
            return
        if path.startswith("/api/version"):
            self._json(200, {"version": _version()})
            return
        if path.startswith("/api/update/check"):
            self._json(200, update_check_local())
            return
        if path == "/api/pricing":
            self._json(200, _load_pricing())
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
        if path == "/api/pricing/refresh":
            self._json(200, refresh_pricing())
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

def _load_proxy_module():
    """加载同目录的本地代理模块(查询余额/预警推送引擎)"""
    proxy_file = BASE_DIR / "额度管家_本地代理.py"
    if not proxy_file.exists():
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("local_proxy", proxy_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
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
        n = cfg.get("notify") or {}
        if n.get("enabled"):
            threading.Thread(target=mod.snapshot_loop, daemon=True).start()
            print(f"  微信预警: 已启动(每 {mod.poll_seconds(cfg)} 秒采集+评估, 异常自动推送)")
        else:
            print("  微信预警: 未开启(在 accounts_config.json 的 notify 块配置后重启生效)")
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


def main():
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"端口 {PORT} 被占用——可能已经启动了一个额度管家, 直接打开 http://{HOST}:{PORT} 即可")
        webbrowser.open(f"http://{HOST}:{PORT}/")
        return
    print("=" * 46)
    print(f"  AI额度管家 V{_version()} 已启动")
    print(f"  地址: http://{HOST}:{PORT}")
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
    threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
