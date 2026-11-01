import { Fragment, useEffect, useState } from "react";
import { trpc } from "@/providers/trpc";
import { fmtMoney, platformInfo } from "@/lib/fmt";

/** V3.32.5: 与「模型API管理」同口径的三分类 + 平台名带形态(订阅显示 Coding Plan 等) */
const ONLINE_SUBS = new Set(["chatgpt", "claude_sub", "gemini_sub"]);
const SUB_ONLY = new Set(["kimicode", "qwenplan", "doubao"]);
const kindOf = (platform: string, apiType: string) =>
  ONLINE_SUBS.has(platform) ? "online" : ((apiType === "sub" || SUB_ONLY.has(platform)) ? "sub" : "payg");
const KIND_META: Record<string, { label: string; desc: string; section: string; bar: string }> = {
  payg: { label: "按量计费", desc: "充值余额 · 在这里设金额预算", section: "bg-emerald-50/70 border-emerald-200 text-emerald-900", bar: "bg-emerald-500" },
  sub: { label: "套餐订阅", desc: "包月配额 · 月费固定按天摊销, 不设金额预算", section: "bg-purple-50/70 border-purple-200 text-purple-900", bar: "bg-purple-500" },
  online: { label: "在线订阅", desc: "官网会员 · 只记月费, 不设金额预算", section: "bg-amber-50/70 border-amber-200 text-amber-900", bar: "bg-amber-500" },
};
const platLabel = (platform: string, apiType: string) => {
  if (platform === "glm" && apiType === "sub") return "智谱 Coding Plan";
  if (platform === "minimax" && apiType === "sub") return "MiniMax Token Plan";
  return platformInfo(platform).name;
};
import NotifyPanel from "@/components/NotifyPanel";

export default function Budget({ onAlertClick }: {
  onAlertClick?: (platform: string, accountName?: string) => void;
}) {
  const utils = trpc.useUtils();
  const summary = trpc.quota.summary.useQuery();
  const list = trpc.quota.list.useQuery(undefined, { refetchInterval: 60000 });
  const budget = trpc.quota.getBudget.useQuery();
  const [total, setTotal] = useState("1800");
  const [warn, setWarn] = useState("80");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (budget.data) {
      setTotal(String(budget.data.monthlyTotal));
      setWarn(String(budget.data.warnPercent));
    }
  }, [budget.data]);

  const setBudget = trpc.quota.setBudget.useMutation({
    onSuccess: () => { utils.quota.invalidate(); setMsg("预算设置已保存"); },
    onError: (e) => setMsg("保存失败: " + e.message),
  });
  const setPlat = trpc.quota.setPlatformBudget.useMutation({ onSuccess: () => utils.quota.invalidate() });

  const s = summary.data;
  const monthTotal = s?.monthTotal ?? 0;
  const monthlyTotal = Number(total) || 0;
  const pct = monthlyTotal > 0 ? (monthTotal / monthlyTotal) * 100 : 0;
  const warnPct = Number(warn) || 80;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-4">
        <div className="bg-white rounded-xl p-5 shadow-sm">
          <div className="text-xs text-slate-500">月度总预算</div>
          <div className="text-2xl font-bold mt-1">{fmtMoney(monthlyTotal)}</div>
        </div>
        <div className="bg-white rounded-xl p-5 shadow-sm">
          <div className="text-xs text-slate-500">本月已消耗</div>
          <div className="text-2xl font-bold mt-1">{fmtMoney(monthTotal)}</div>
          <div className="text-[11px] text-slate-400 mt-1">按量实扣 + 订阅按月费÷当月天数摊销（月费按你录入的算，固定不随用量变）</div>
        </div>
        <div className="bg-white rounded-xl p-5 shadow-sm">
          <div className="text-xs text-slate-500">预算使用率(预警线 {warnPct}%)</div>
          <div className={`text-2xl font-bold mt-1 ${pct >= 90 ? "text-red-500" : pct >= warnPct ? "text-amber-500" : "text-emerald-600"}`}>{pct.toFixed(1)}%</div>
          <div className="h-2 bg-slate-100 rounded-full overflow-hidden mt-2">
            <div className={`h-full rounded-full ${pct >= 90 ? "bg-red-500" : pct >= warnPct ? "bg-amber-500" : "bg-emerald-500"}`} style={{ width: `${Math.min(100, pct)}%` }} />
          </div>
        </div>
      </div>

      {/* V3.30.7: 订阅制消耗板块已移除 —— 订阅额度统一到「当前总览 → 订阅额度窗口」看,
          预算页只管按量金额预算; 订阅月费固定, 按"月费÷当月天数"摊销进总成本, 不需要按配额比例折算 */}

      <div className="bg-white rounded-xl p-5 shadow-sm">
        <div className="font-semibold mb-4">预算设置(存入数据库, 多设备共享)</div>
        <div className="flex gap-3 items-end flex-wrap">
          <label className="text-sm">月度总预算(元)
            <input type="number" className="mt-1 block w-40 border rounded-lg px-2 py-1.5" value={total} onChange={(e) => setTotal(e.target.value)} />
          </label>
          <label className="text-sm">预警阈值(%)
            <input type="number" className="mt-1 block w-28 border rounded-lg px-2 py-1.5" value={warn} onChange={(e) => setWarn(e.target.value)} />
          </label>
          <button className="px-4 py-2 text-sm rounded-lg bg-[#1B6B7A] text-white disabled:opacity-50" disabled={setBudget.isPending}
            onClick={() => setBudget.mutate({ monthlyTotal: Number(total) || 0, warnPercent: Number(warn) || 80 })}>
            保存
          </button>
          {msg && <span className="text-sm text-slate-500">{msg}</span>}
        </div>
      </div>

      <div className="bg-white rounded-xl p-5 shadow-sm">
        <div className="font-semibold mb-1">各平台预算使用(与账号列表联动)
          <span className="text-xs text-slate-400 font-normal ml-2">与「模型API管理」完全同口径：按账号分三类；按量设金额预算，订阅看月费摊销</span>
        </div>

        {/* V3.32.5: 账号级三分类(与模型API管理一致)。
            byPlatform 是按平台聚合的(智谱按量+订阅会并一行), 这里直接用账号列表逐行展示:
            行 = 账号(平台名带形态), 消耗 = 该账号本月真实消耗, 预算 = 平台预算(同平台账号共享)。 */}
        {(() => {
          type Acc = { id: number; name: string; platform: string; apiType: string;
                       budget?: number; monthUsedReal?: number; monthUsedFee?: number; monthlyFee?: number };
          const accs = ((list.data ?? []) as Acc[]);
          const budByPlat = new Map<string, number>();
          for (const p of (s?.byPlatform ?? [])) budByPlat.set(p.platform, p.budget || 0);
          const groups: [string, Acc[]][] = [
            ["payg", accs.filter((a) => kindOf(a.platform, a.apiType) === "payg")],
            ["sub", accs.filter((a) => kindOf(a.platform, a.apiType) === "sub")],
            ["online", accs.filter((a) => kindOf(a.platform, a.apiType) === "online")],
          ];
          return (
            <table className="w-full text-sm mt-2">
              <thead>
                <tr className="text-left text-xs text-slate-400 border-b">
                  <th className="py-2">平台 / 账号</th><th>本月消耗</th><th>预算</th><th className="w-56">使用率</th><th>设置预算</th>
                </tr>
              </thead>
              <tbody>
                {groups.map(([kind, list2]) => {
                  if (!list2.length) return null;
                  const m = KIND_META[kind];
                  return (
                    <Fragment key={kind}>
                      <tr>
                        <td colSpan={5} className="pt-4 pb-0">
                          <div className={`flex items-center gap-2.5 px-3 py-1.5 rounded-lg border ${m.section}`}>
                            <span className={`w-1.5 h-4 rounded-full ${m.bar}`} />
                            <span className="font-semibold text-[13px]">{m.label}</span>
                            <span className="text-[11px] opacity-70">{list2.length} 个账号 · {m.desc}</span>
                          </div>
                        </td>
                      </tr>
                      {list2.map((a) => {
                        const used = a.monthUsedFee ?? a.monthUsedReal ?? 0;
                        const budget = budByPlat.get(a.platform) || 0;
                        const pctv = budget > 0 ? (used / budget) * 100 : 0;
                        const isSubRow = kind !== "payg";
                        return (
                          <tr key={a.id} className="border-b last:border-0">
                            <td className="py-2.5">
                              <div className="font-medium">{platLabel(a.platform, a.apiType)}</div>
                              <div className="text-[11px] text-slate-400">{a.name}</div>
                            </td>
                            <td className="tabular-nums">{fmtMoney(used)}</td>
                            <td>{!isSubRow && budget > 0 ? fmtMoney(budget) : "—"}</td>
                            <td>
                              {!isSubRow && budget > 0 ? (
                                <div className="flex items-center gap-2">
                                  <div className="h-2 flex-1 bg-slate-100 rounded-full overflow-hidden">
                                    <div className={`h-full rounded-full ${pctv >= 90 ? "bg-red-500" : pctv >= warnPct ? "bg-amber-500" : "bg-emerald-500"}`} style={{ width: `${Math.min(100, pctv)}%` }} />
                                  </div>
                                  <span className="text-xs text-slate-500 w-10">{pctv.toFixed(0)}%</span>
                                </div>
                              ) : <span className="text-xs text-slate-400">—</span>}
                            </td>
                            <td>
                              {!isSubRow ? (
                                <input type="number" defaultValue={budget || ""} placeholder="元/月"
                                  className="w-28 border rounded-lg px-2 py-1 text-sm"
                                  onBlur={(e) => setPlat.mutate({ platform: a.platform, budget: Number(e.target.value) || 0 })} />
                              ) : <span className="text-xs text-slate-400">订阅不设预算</span>}
                            </td>
                          </tr>
                        );
                      })}
                    </Fragment>
                  );
                })}
                {accs.length === 0 && <tr><td colSpan={5} className="py-8 text-center text-slate-400">暂无账号数据</td></tr>}
              </tbody>
            </table>
          );
        })()}
      </div>

      {/* V3.32.4: 微信预警(从「关于系统」搬来) + 规则可指定账号 */}
      <NotifyPanel accounts={(list.data ?? []).map((a) => ({
        name: String(a.name ?? ""), platform: String(a.platform ?? ""), apiType: String(a.apiType ?? "payg"),
      }))} />

      <div className="bg-white rounded-xl p-5 shadow-sm">
        <div className="font-semibold mb-3">当前预警</div>
        {(s?.alerts ?? []).length === 0 && <div className="text-sm text-slate-400">暂无预警, 所有账号状态正常</div>}
        <div className="space-y-2">
          {(s?.alerts ?? []).map((a) => (
            <button key={a.id}
              onClick={() => onAlertClick?.(a.platform, a.name)}
              className={`w-full text-left flex items-center gap-3 px-4 py-3 rounded-lg text-sm transition-transform hover:scale-[1.005] ${a.level === "danger" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-700"}`}>
              <span>{a.level === "danger" ? "🔴" : "🟡"}</span>
              <span className="font-medium">{a.title}</span>
              <span className="text-xs opacity-80">{a.desc}</span>
              {onAlertClick && <span className="ml-auto text-xs opacity-60">查看账号 →</span>}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
