import { useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts";
import { trpc } from "@/providers/trpc";
import { fmtMoney, lineDistinct, platformInfo } from "@/lib/fmt";
import Trend from "./Trend";
import Budget from "./Budget";
import Ledger from "./Ledger";
import QuotaWindows from "@/components/QuotaWindows";

function Chart({ option, height = 280 }: { option: echarts.EChartsOption; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const c = echarts.init(ref.current);
    c.setOption(option);
    const onResize = () => c.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      c.dispose();
    };
  }, [option]);
  return <div ref={ref} style={{ height }} />;
}

type Tab = "overview" | "ledger" | "trend" | "budget";

export default function Dashboard({
  onNavigate,
  onAlertClick,
}: {
  onNavigate: (k: string) => void;
  onAlertClick: (platform: string, accountName?: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("overview");
  const summary = trpc.quota.summary.useQuery(undefined, { refetchInterval: 30000 });
  const trend = trpc.quota.trend.useQuery({ days: 14 });
  const s = summary.data;

  const cards = [
    { label: "本月总消耗", value: fmtMoney(s?.monthTotal ?? 0), sub: "与「账目统计」同口径：按量实扣 + 订阅折算" },
    { label: "当前余额", value: fmtMoney(s?.remainingTotal ?? 0), sub: "按量余额 + 订阅已付未用(与账目总计同源)" },
    { label: "账号数量", value: `${s?.accountCount ?? 0} 个`, sub: "数据库实时统计" },
    { label: "预警账号", value: `${s?.alertCount ?? 0} 个`, sub: "点击下方预警可跳转对应账号" },
  ];

  const lineOption = useMemo<echarts.EChartsOption>(() => {
    const d = trend.data;
    return {
      tooltip: { trigger: "axis" },
      legend: { bottom: 0, textStyle: { fontSize: 11 } },
      grid: { left: 50, right: 16, top: 20, bottom: 46 },
      xAxis: { type: "category", data: (d?.days ?? []).map((r) => r.date.slice(5)) },
      yAxis: { type: "value", name: "元" },
      series: (d?.platforms ?? []).map((p, i) => ({
        name: platformInfo(p).name,
        type: "line" as const,
        smooth: true,
        // 不堆叠: 堆叠会把各条线压在一起看不出单个模型; 用形状+实虚线区分
        ...lineDistinct(i),
        data: (d?.days ?? []).map((r) => Number(r[p] ?? 0)),
        color: platformInfo(p).color,
      })),
    };
  }, [trend.data]);

  // 占比图: 金额口径的平台消耗 + 订阅平台(用摊销成本, 为0时退化为配额进度)
  const pieData = useMemo(() => {
    const arr = (s?.byPlatform ?? [])
      .filter((p) => p.used > 0)
      .map((p) => ({
        name: platformInfo(p.platform).name,
        value: Math.round(p.used * 100) / 100,
        itemStyle: { color: platformInfo(p.platform).color },
      }));
    return arr;
  }, [s]);

  const pieTotal = useMemo(() => pieData.reduce((a, b) => a + b.value, 0), [pieData]);

  // V3.32.1 美化: 名称/金额/占比全部交给右侧图例(可滚动), 扇区只在大扇区内部显示百分比,
  // 不用外置引导线标签 —— 平台再多也不会出现文字重叠或引导线打结。
  const pieOption = useMemo<echarts.EChartsOption>(
    () => ({
      tooltip: {
        trigger: "item",
        backgroundColor: "rgba(23,42,58,.92)",
        borderWidth: 0,
        textStyle: { color: "#fff", fontSize: 12 },
        formatter: (pp: { name: string; value: number; percent: number; marker: string }) =>
          `${pp.marker} ${pp.name}<br/>${fmtMoney(pp.value)}　<b>${pp.percent}%</b>`,
      },
      // V3.32.1: 不用 echarts legend(列宽有限会截断名称/挤掉百分比),
      // 右侧改成 HTML 列表渲染, 名称完整、金额与占比右对齐、不滚动。
      series: [
        {
          type: "pie",
          radius: ["54%", "80%"],
          center: ["50%", "50%"],
          data: pieData,
          minAngle: 6,                 // 极小占比也留出可见扇区
          avoidLabelOverlap: true,
          itemStyle: { borderColor: "#fff", borderWidth: 2, borderRadius: 3 },
          label: {
            // 只在足够大的扇区内部标百分比(白字), 小扇区不标 → 绝不重叠
            position: "inside",
            formatter: (pp: { percent: number }) => (pp.percent >= 15 ? `${pp.percent.toFixed(0)}%` : ""),
            fontSize: 11,
            fontWeight: 600,
            color: "#fff",
            textShadowColor: "rgba(0,0,0,.35)",
            textShadowBlur: 2,
          },
          labelLine: { show: false },
          emphasis: {
            scale: true,
            scaleSize: 5,
            itemStyle: { shadowBlur: 12, shadowColor: "rgba(0,0,0,.18)" },
          },
          // 圆环中心显示总消耗
          ...(pieTotal > 0 ? {
            markPoint: undefined,
          } : {}),
        },
      ],
      // 中心文字: 总消耗
      graphic: pieTotal > 0 ? [
        { type: "text", left: "center", top: "44%", style: {
            text: "本月合计", textAlign: "center", fill: "#94a3b8", fontSize: 11 } },
        { type: "text", left: "center", top: "48.5%", style: {
            text: fmtMoney(pieTotal), textAlign: "center", fill: "#0f172a",
            fontSize: 16, fontWeight: "bold" } },
      ] : [],
    }),
    [pieData, pieTotal]
  );

  // 订阅账号: 金额为 0 时用配额进度单列展示, 保证「占比」区不漏掉订阅平台
  const subRows = (s?.byPlatform ?? []).filter((p) => p.subCount > 0);

  return (
    <div className="space-y-4">
      {/* 子板块切换 */}
      <div className="flex gap-1 bg-white rounded-xl p-1 shadow-sm w-fit">
        {([["overview", "总览"], ["ledger", "账目统计"], ["trend", "趋势分析"], ["budget", "预算预警"]] as [Tab, string][]).map(([k, label]) => (
          <button key={k} onClick={() => setTab(k)}
            className={`px-5 py-2 text-sm rounded-lg transition-colors ${tab === k ? "bg-[#1B6B7A] text-white" : "text-slate-500 hover:bg-slate-50"}`}>
            {label}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <>
          <div className="grid grid-cols-4 gap-4">
            {cards.map((c) => (
              <div key={c.label} className="bg-white rounded-xl px-4 py-3 shadow-sm">
                <div className="text-[11px] text-slate-500">{c.label}</div>
                <div className="text-xl font-bold mt-0.5 tabular-nums">{c.value}</div>
                <div className="text-[10px] text-slate-400 mt-0.5 truncate">{c.sub}</div>
              </div>
            ))}
          </div>

          {/* 成本结构(V3.21.0): 按量 / 订阅 API / 在线订阅 三视图, 一个总成本 */}
          {(() => {
            const bc = s?.byCategory ?? { payg: 0, sub: 0, online: 0 };
            const total = (bc.payg || 0) + (bc.sub || 0) + (bc.online || 0);
            if (total <= 0) return null;
            const rows = [
              { key: "payg", label: "按量 API", v: bc.payg || 0, color: "#1B6B7A" },
              { key: "sub", label: "订阅 API", v: bc.sub || 0, color: "#8B5CF6" },
              { key: "online", label: "在线订阅", v: bc.online || 0, color: "#10A37F" },
            ];
            return (
              <div className="bg-white rounded-xl p-5 shadow-sm">
                <div className="flex items-center justify-between mb-3">
                  <div className="font-semibold">
                    本月成本结构
                    <span className="text-xs text-slate-400 font-normal ml-2">按量 / 订阅 API / 在线订阅，一个总成本</span>
                  </div>
                  <div className="text-sm text-slate-500">合计 {fmtMoney(total)}</div>
                </div>
                <div className="flex h-3 rounded-full overflow-hidden bg-slate-100">
                  {rows.map((r) => r.v > 0
                    ? <div key={r.key} style={{ width: `${(r.v / total) * 100}%`, background: r.color }} />
                    : null)}
                </div>
                <div className="grid grid-cols-3 gap-3 mt-3">
                  {rows.map((r) => (
                    <div key={r.key}>
                      <div className="text-[11px] text-slate-500 flex items-center gap-1">
                        <span className="inline-block w-2 h-2 rounded-full" style={{ background: r.color }} />
                        {r.label}
                      </div>
                      <div className="text-lg font-semibold">
                        {fmtMoney(r.v)}
                        <span className="text-[11px] text-slate-400 font-normal ml-1">
                          {((r.v / total) * 100).toFixed(0)}%
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            );
          })()}

          {/* 订阅额度窗口: 5小时 / 7天 并列展示, 只读不计费 */}
          <QuotaWindows />

          <div className="grid grid-cols-5 gap-4">
            <div className="col-span-3 bg-white rounded-xl p-5 shadow-sm">
              <div className="font-semibold mb-2">
                近 14 天按量消耗趋势（元，真实快照差值）
                <button onClick={() => setTab("trend")} className="text-xs text-[#1B6B7A] hover:underline ml-2 font-normal">详细趋势 →</button>
              </div>
              <Chart option={lineOption} />
            </div>
            <div className="col-span-2 bg-white rounded-xl px-4 py-3 shadow-sm flex flex-col">
              <div className="font-semibold mb-1 shrink-0">本月平台消耗占比</div>
              {pieData.length > 0 ? (
                <div className="flex-1 flex items-center gap-1.5 min-h-0">
                  <div className="shrink-0" style={{ width: 196 }}>
                    <Chart option={pieOption} height={300} />
                  </div>
                  {/* 图例列表：名称完整显示, 金额/占比右对齐, 超长可滚动 */}
                  <div className="flex-1 min-w-0 max-h-[320px] overflow-y-auto pr-1">
                    {[...pieData]
                      .sort((x, y) => y.value - x.value)
                      .map((it) => {
                        const pct = pieTotal > 0 ? (it.value / pieTotal) * 100 : 0;
                        return (
                          <div key={it.name}
                            className="flex items-center gap-1.5 py-[4px] border-b border-slate-50 last:border-0">
                            <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: it.itemStyle.color }} />
                            <span className="text-[11.5px] text-slate-700 flex-1 truncate whitespace-nowrap" title={it.name}>{it.name}</span>
                            <span className="text-[11.5px] font-semibold text-slate-800 tabular-nums shrink-0 text-right w-[54px]">{fmtMoney(it.value)}</span>
                            <span className="text-[10.5px] text-slate-400 tabular-nums w-8 text-right shrink-0">{pct.toFixed(0)}%</span>
                          </div>
                        );
                      })}
                  </div>
                </div>
              ) : (
                <div className="text-sm text-slate-400 py-8 text-center">本月暂无金额消耗</div>
              )}
            </div>
          </div>

          {/* 预警卡片: 点击联动跳转模型API管理 */}
          <div className="bg-white rounded-xl px-4 py-3 shadow-sm">
            <div className="flex items-center justify-between mb-2">
              <div className="font-semibold text-[14px]">当前预警 <span className="text-[11px] text-slate-400 font-normal">点击预警条目可跳到「模型API管理」定位该账号</span></div>
              <button onClick={() => setTab("budget")} className="text-xs text-[#1B6B7A] hover:underline">预算设置 →</button>
            </div>
            {(s?.alerts ?? []).length === 0 && <div className="text-sm text-slate-400">暂无预警, 所有账号状态正常 ✓</div>}
            <div className="space-y-2">
              {(s?.alerts ?? []).map((a) => (
                <button key={a.id}
                  onClick={() => onAlertClick(a.platform, a.name)}
                  className={`w-full text-left flex items-center gap-2.5 px-3 py-2 rounded-lg text-[13px] transition-transform hover:scale-[1.005] ${a.level === "danger" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-700"}`}>
                  <span>{a.level === "danger" ? "🔴" : "🟡"}</span>
                  <span className="font-medium">{a.title}</span>
                  <span className="text-xs opacity-80">{a.desc}</span>
                  <span className="ml-auto text-xs opacity-60">查看账号 →</span>
                </button>
              ))}
            </div>
          </div>

        </>
      )}

      {tab === "ledger" && <Ledger />}
      {tab === "trend" && <Trend />}
      {tab === "budget" && <Budget onAlertClick={onAlertClick} />}
    </div>
  );
}
