# -*- coding: utf-8 -*-
"""L1 Mock 测试: 不打真实网络, 用 fixtures 里的真实响应样本验证适配器解析。

方案 §8.3 要求: 采集器签名统一 query_xxx(acc) -> {remaining, live, note},
测试 harness 一个就够。这里直接打桩 _get_json, 覆盖海外平台的新适配器。

用法:
    python tests/test_adapters.py
退出码 0 = 全部通过。
"""
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"

spec = importlib.util.spec_from_file_location(
    "proxy", BASE / "本地代理" / "额度管家_本地代理.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

ok = 0
fail = 0


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


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def stub(routes):
    """routes: {url 路径后缀: data | Exception}; 记录请求过的 URL。
    匹配时忽略 ?query, 否则带参数的接口(成本报表/ Gemini key)匹配不上。"""
    calls = []

    def fake(url, key, bearer=True, headers=None):
        calls.append({"url": url, "key": key, "bearer": bearer, "headers": headers or {}})
        path = url.split("?", 1)[0]
        for suffix, val in routes.items():
            if path.endswith(suffix):
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError(f"unexpected url: {url}")

    m._get_json = fake
    return calls


def http_err(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


# ---------------------------------------------------------------- Mistral
@case("Mistral 余额接口正常(credit_balance)")
def _():
    stub({"/v1/credit_balance": fixture("mistral_credit_balance.json")})
    r = m.adapter_mistral({"platform": "mistral", "api_key": "k"})
    assert r["live"] is True, r
    assert abs(r["remaining"] - 42.5) < 1e-9, r
    assert r["currency"] == "USD", r


@case("Mistral 余额接口 404 → 探活 + 手填余额")
def _():
    stub({"/v1/credit_balance": http_err(404), "/v1/models": fixture("models.json")})
    r = m.adapter_mistral({"platform": "mistral", "api_key": "k", "remaining": 8.8})
    assert r["live"] is False and abs(r["remaining"] - 8.8) < 1e-9, r
    assert "Key 有效" in r["note"] and "上次记录" in r["note"], r


@case("Mistral Key 无效 → 明确报认证失败")
def _():
    stub({"/v1/credit_balance": http_err(401)})
    try:
        m.adapter_mistral({"platform": "mistral", "api_key": "bad"})
        raise AssertionError("should raise")
    except RuntimeError as e:
        assert "认证失败" in str(e), e


@case("Mistral 自定义 base_url 生效")
def _():
    calls = stub({"/v1/credit_balance": fixture("mistral_credit_balance.json")})
    m.adapter_mistral({"platform": "mistral", "api_key": "k",
                       "base_url": "https://my-gateway.example.com/"})
    assert calls[0]["url"] == "https://my-gateway.example.com/v1/credit_balance", calls[0]


# ---------------------------------------------------------------- Anthropic
@case("Anthropic Admin Key → 成本报表累加为本月消耗")
def _():
    stub({"/v1/organization/cost_report": fixture("anthropic_cost_report.json"),
          "/v1/models": fixture("models.json")})
    r = m.adapter_claude({"platform": "claude", "api_key": "sk-ant-admin01-xyz",
                          "remaining": 20})
    assert r["live"] is False and abs(r["remaining"] - 20) < 1e-9, r
    assert abs(r.get("monthlyUsed") - 3.75) < 1e-9, r
    assert r["currency"] == "USD", r


@case("Anthropic 普通 Key → 只探活, 不查报表")
def _():
    calls = stub({"/v1/models": fixture("models.json")})
    r = m.adapter_claude({"platform": "claude", "api_key": "sk-ant-api03-xyz"})
    assert r["live"] is False, r
    assert all("cost_report" not in c["url"] for c in calls), calls
    assert calls[0]["headers"].get("x-api-key"), calls[0]
    assert calls[0]["headers"].get("anthropic-version"), calls[0]


# ---------------------------------------------------------------- OpenAI
@case("OpenAI Admin Key → Costs API 累加")
def _():
    stub({"/v1/organization/costs": fixture("openai_costs.json"),
          "/v1/models": fixture("models.json")})
    r = m.adapter_openai({"platform": "openai", "api_key": "sk-admin-xyz", "remaining": 50})
    assert abs(r.get("monthlyUsed") - 1.35) < 1e-9, r
    assert "Costs API" in r["note"], r


@case("OpenAI 普通 Key → 探活 + 手填")
def _():
    calls = stub({"/v1/models": fixture("models.json")})
    r = m.adapter_openai({"platform": "openai", "api_key": "sk-proj-abc"})
    assert r["live"] is False, r
    assert all("organization/costs" not in c["url"] for c in calls), calls


# ---------------------------------------------------------------- Gemini / Groq
@case("Gemini 默认官方地址 → key 走 query 参数")
def _():
    calls = stub({"/v1beta/models": fixture("models.json")})
    r = m.adapter_gemini({"platform": "gemini", "api_key": "AIza-xyz"})
    assert "key=AIza-xyz" in calls[0]["url"], calls[0]
    assert r["currency"] == "USD", r


@case("Gemini 自定义中转 → Bearer + /v1/models")
def _():
    calls = stub({"/v1/models": fixture("models.json")})
    m.adapter_gemini({"platform": "gemini", "api_key": "k",
                      "base_url": "https://gw.example.com"})
    assert calls[0]["url"] == "https://gw.example.com/v1/models", calls[0]


@case("Groq 探活走 /openai/v1/models")
def _():
    calls = stub({"/openai/v1/models": fixture("models.json")})
    r = m.adapter_groq({"platform": "groq", "api_key": "gsk_xyz"})
    assert calls[0]["url"] == "https://api.groq.com/openai/v1/models", calls[0]
    assert r["live"] is False and r["currency"] == "USD", r


print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(0 if fail == 0 else 1)
