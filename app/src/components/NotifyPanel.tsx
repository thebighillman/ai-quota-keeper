import { useCallback, useEffect, useState } from "react";
import { platformInfo } from "@/lib/fmt";

/** 一条预警规则 */
export type Rule = {
  id: string;
  type: string;
  scope: string[];          // payg / sub / online
  accounts?: string[];      // V3.32.4: 指定账号（空=该类别的全部账号）
  threshold: number;
  enabled: boolean;
  note?: string;
};
type RuleType = { id: string; label: string; unit: string; default: number; scopes: string[] };
type Notify = {
  enabled: boolean;
  hasWebhook: boolean;
  cooldown: number;
  pollSeconds: number;
  rules?: Rule[];
  ruleTypes?: RuleType[];
};

const SCOPE_LABEL: Record<string, string> = { payg: "按量计费", sub: "套餐订阅", online: "在线订阅" };

/**
 * 微信预警面板（V3.32.4：从「关于系统」搬到「预算预警」，规则可指定到具体账号）
 *  - 顶部：开关 + 机器人地址 + 测试
 *  - 中部：规则列表（类型 / 阈值 / 适用类别 / 指定账号）
 *  - 底部：拿地址说明
 */
export default function NotifyPanel({ accounts }: {
  /** 可选账号列表（用于规则里指定具体账号） */
  accounts: { name: string; platform: string; apiType: string }[];
}) {
  const [n, setN] = useState<Notify | null>(null);
  const [webhook, setWebhook] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [pickFor, setPickFor] = useState<string | null>(null);  // 正在选账号的规则 id

  const load = useCallback(() => {
    fetch("/api/notify", { cache: "no-store" })
      .then((r) => r.json()).then(setN).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async (patch: Record<string, unknown>) => {
    setBusy(true); setMsg("");
    try {
      const r = await fetch("/api/notify", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...patch, webhook }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || "保存失败");
      setMsg(d.detail || "已保存");
      setWebhook("");
      load();
    } catch (e) {
      setMsg("失败：" + (e instanceof Error ? e.message : String(e)));
    }
    setBusy(false);
    window.setTimeout(() => setMsg(""), 4000);
  };

  const test = async () => {
    setTesting(true); setMsg("");
    try {
      const r = await fetch("/api/notify/test", { cache: "no-store" });
      const d = await r.json();
      setMsg(d.ok ? "✅ 已发送，去微信看看收到没有" : "⚠️ " + (d.detail || "发送失败"));
    } catch {
      setMsg("测试请求失败");
    }
    setTesting(false);
  };

  if (!n) return null;
  const rules = n.rules ?? [];
  const types = n.ruleTypes ?? [];
  const typeOf = (id: string) => types.find((t) => t.id === id);

  const commitRules = (next: Rule[]) =>
    save({ rules: next.map((r, i) => ({ ...r, id: r.id || `r${i + 1}` })) });

  const addRule = () => {
    const t = types[0];
    if (!t) return;
    commitRules([...rules, {
      id: `r${Date.now().toString(36)}`, type: t.id, scope: [...t.scopes],
      accounts: [], threshold: t.default, enabled: true,
    }]);
  };

  return (
    <div className="bg-white rounded-xl p-5 shadow-sm space-y-3">
      {/* 头部: 开关 + 状态 */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-semibold">微信预警</div>
          <div className="text-[12px] mt-0.5 text-slate-500">
            {n.enabled
              ? <span className="text-emerald-600">监控中 —— 触发下面任意一条规则就推送到微信</span>
              : <span className="text-slate-400">未开启</span>}
          </div>
          {msg && <div className="text-[12px] mt-1 text-slate-600">{msg}</div>}
        </div>
        <button onClick={() => save({ enabled: !n.enabled })} disabled={busy}
          className={`relative shrink-0 w-12 h-7 rounded-full transition-colors disabled:opacity-40 ${n.enabled ? "bg-emerald-500" : "bg-slate-300"}`}
          title={n.enabled ? "点击关闭" : "点击开启"}>
          <span className={`absolute top-0.5 left-0.5 w-6 h-6 rounded-full bg-white shadow transition-transform ${n.enabled ? "translate-x-5" : ""}`} />
        </button>
      </div>

      {/* 机器人地址 */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[12px] text-slate-500 w-16 shrink-0">机器人地址</span>
        <input value={webhook} onChange={(e) => setWebhook(e.target.value)}
          placeholder={n.hasWebhook ? "已配置（留空=不修改）" : "粘贴企业微信群机器人 webhook 地址"}
          className="border rounded-lg px-2 py-1 text-[12px] flex-1 min-w-[260px]" />
        <button onClick={() => save({})} disabled={busy}
          className="px-3 py-1.5 text-[12px] rounded-lg bg-[#1B6B7A] text-white hover:bg-[#155a66] disabled:opacity-50">保存</button>
        <button onClick={test} disabled={testing || !n.hasWebhook}
          className="px-3 py-1.5 text-[12px] rounded-lg border border-[#1B6B7A] text-[#1B6B7A] hover:bg-[#1B6B7A]/5 disabled:opacity-40">
          {testing ? "发送中..." : "测试消息"}
        </button>
      </div>

      {/* 规则列表 */}
      <div className="pt-1">
        <div className="flex items-center justify-between gap-2 mb-2">
          <span className="text-[12px] text-slate-600 font-medium">预警规则（可自定义多条，可指定到具体账号）</span>
          <button onClick={addRule} disabled={busy || !types.length}
            className="px-2 py-1 text-[12px] rounded-lg border border-[#1B6B7A] text-[#1B6B7A] hover:bg-[#1B6B7A]/5 disabled:opacity-40">
            + 添加规则
          </button>
        </div>

        {rules.length === 0 && (
          <div className="text-[12px] text-slate-400 bg-slate-50 rounded-lg px-3 py-2">
            还没规则。点「+ 添加规则」——例如「本月消耗达预算 90% 提醒」「订阅套餐到期前 5 天提醒」。
          </div>
        )}

        <div className="space-y-2">
          {rules.map((r, idx) => {
            const t = typeOf(r.type);
            const patch = (p2: Partial<Rule>) =>
              commitRules(rules.map((x, i) => (i === idx ? { ...x, ...p2 } : x)));
            const picked = r.accounts ?? [];
            // 该规则口径下可选的账号（按 rule 的 scope 过滤）
            const candidates = accounts.filter((a) => {
              const kind = a.apiType === "sub" ? "sub" : "payg";
              return r.scope.includes(kind) || r.scope.includes("online");
            });
            return (
              <div key={r.id} className="border border-slate-200 rounded-lg px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <input type="checkbox" checked={r.enabled} disabled={busy}
                    onChange={(e) => patch({ enabled: e.target.checked })}
                    className="w-4 h-4 accent-[#1B6B7A] shrink-0" title="启用/停用" />
                  <select className="border rounded-lg px-2 py-1 text-[12px] max-w-[210px]" value={r.type}
                    disabled={busy}
                    onChange={(e) => {
                      const nt = typeOf(e.target.value);
                      patch({ type: e.target.value, scope: nt ? [...nt.scopes] : r.scope,
                              threshold: nt ? nt.default : r.threshold, accounts: [] });
                    }}>
                    {types.map((t2) => <option key={t2.id} value={t2.id}>{t2.label}</option>)}
                  </select>
                  <input type="number" value={r.threshold} disabled={busy}
                    onChange={(e) => patch({ threshold: Number(e.target.value) })}
                    className="border rounded-lg px-2 py-1 text-[12px] w-20" />
                  <span className="text-[12px] text-slate-400">{t?.unit ?? ""}</span>

                  <span className="text-[12px] text-slate-400 ml-1">适用：</span>
                  {["payg", "sub", "online"].map((sc) => (
                    <label key={sc} className="inline-flex items-center gap-1 text-[12px] text-slate-600">
                      <input type="checkbox" checked={r.scope.includes(sc)} disabled={busy}
                        onChange={(e) => {
                          const next = e.target.checked
                            ? [...new Set([...r.scope, sc])]
                            : r.scope.filter((x) => x !== sc);
                          patch({ scope: next.length ? next : r.scope });
                        }}
                        className="w-3.5 h-3.5 accent-[#1B6B7A]" />
                      {SCOPE_LABEL[sc]}
                    </label>
                  ))}

                  <button onClick={() => commitRules(rules.filter((_, i) => i !== idx))} disabled={busy}
                    className="ml-auto text-[12px] text-red-500 hover:underline">删除</button>
                </div>

                {/* 指定账号 */}
                <div className="flex flex-wrap items-center gap-2 mt-2 pl-6">
                  <span className="text-[12px] text-slate-400">账号：</span>
                  <span className="text-[12px] text-slate-600">
                    {picked.length === 0 ? `全部（${candidates.length} 个）` : `已选 ${picked.length} 个`}
                  </span>
                  <button onClick={() => setPickFor(pickFor === r.id ? null : r.id)} disabled={busy}
                    className="text-[12px] text-[#1B6B7A] hover:underline">
                    {pickFor === r.id ? "收起" : "选择账号"}
                  </button>
                  {picked.length > 0 && (
                    <button onClick={() => patch({ accounts: [] })} disabled={busy}
                      className="text-[12px] text-slate-400 hover:underline">清空(改为全部)</button>
                  )}
                </div>

                {pickFor === r.id && (
                  <div className="mt-1.5 ml-6 border border-slate-100 rounded-lg p-2 max-h-44 overflow-y-auto bg-slate-50/60">
                    {candidates.length === 0 && (
                      <div className="text-[12px] text-slate-400">该口径下暂无账号</div>
                    )}
                    {candidates.map((a) => {
                      const on = picked.includes(a.name);
                      const pi = platformInfo(a.platform);
                      // 同平台多形态时带形态后缀(智谱订阅=Coding Plan), 与账号列表口径一致
                      let platName = pi.name;
                      if (a.platform === "glm" && a.apiType === "sub") platName = "智谱 Coding Plan";
                      else if (a.platform === "minimax" && a.apiType === "sub") platName = "MiniMax Token Plan";
                      return (
                        <label key={a.name + a.platform}
                          className="flex items-center gap-2 py-1 text-[12px] text-slate-700 cursor-pointer hover:bg-white rounded px-1 -mx-1">
                          <input type="checkbox" checked={on} disabled={busy}
                            onChange={(e) => {
                              const next = e.target.checked
                                ? [...picked, a.name]
                                : picked.filter((x) => x !== a.name);
                              patch({ accounts: next });
                            }}
                            className="w-3.5 h-3.5 accent-[#1B6B7A] shrink-0" />
                          <span className="w-4 h-4 rounded text-white text-[8px] font-bold flex items-center justify-center shrink-0"
                            style={{ background: pi.color }}>{pi.abbr}</span>
                          <span className="text-[11px] text-slate-400 shrink-0">{platName}</span>
                          <span className="text-slate-800 truncate">· {a.name}</span>
                          <span className="text-[11px] text-slate-400 ml-auto shrink-0">
                            {SCOPE_LABEL[a.apiType === "sub" ? "sub" : "payg"]}
                          </span>
                        </label>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {rules.length > 0 && (
          <div className="text-[11px] text-slate-400 leading-relaxed mt-2 space-y-0.5">
            <div>· 不选账号 = 该类别下所有账号都监控；选了账号就只管这几个。列表里「平台 · 账号名」对照着选。</div>
            <div>· 同一账号同一条规则触发后 2 小时内不重复推送（防轰炸）。改完点上面的「保存」。</div>
          </div>
        )}
      </div>

      <div className="text-[11px] text-slate-400 leading-relaxed pt-1 border-t border-slate-100">
        怎么拿地址？手机企业微信里建一个群（只有自己也行）→ 群设置 → 群机器人 → 添加 → 复制 Webhook 地址。
      </div>
    </div>
  );
}
