import { useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts";
import { trpc } from "@/providers/trpc";
import { lineDistinct, platformInfo } from "@/lib/fmt";

function useChart(option: echarts.EChartsOption) {
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
  return ref;
}

export default function Trend() {
  const [days, setDays] = useState(30);
  const trend = trpc.quota.trend.useQuery({ days });
  const [selected, setSelected] = useState<string[] | null>(null);

  const platforms = trend.data?.platforms ?? [];
  const active = selected ?? platforms;

  // 按量付费: 每日消耗(元)
  const option = useMemo<echarts.EChartsOption>(
    () => ({
      tooltip: { trigger: "axis" },
      grid: { left: 50, right: 16, top: 20, bottom: 30 },
      xAxis: { type: "category", data: (trend.data?.days ?? []).map((r) => r.date.slice(5)) },
      yAxis: { type: "value", name: "元" },
      series: platforms
        .filter((p) => active.includes(p))
        .map((p, i) => ({
          name: platformInfo(p).name,
          type: "line" as const,
          smooth: true,
          ...lineDistinct(i),   // 形状+实虚线区分不同模型
          data: (trend.data?.days ?? []).map((r) => Number(r[p] ?? 0)),
          color: platformInfo(p).color,
        })),
    }),
    [trend.data, active, platforms]
  );

  const ref = useChart(option);

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-xl p-5 shadow-sm">
        <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
          <div className="font-semibold">按量 API 每日消耗(元) <span className="text-xs text-slate-400 font-normal">来自数据库快照首末差值, 非估算；不同模型用形状+实虚线区分</span></div>
          <div className="flex gap-2">
            {[7, 14, 30, 90].map((d) => (
              <button key={d} onClick={() => setDays(d)}
                className={`px-3 py-1.5 text-xs rounded-lg ${days === d ? "bg-[#1B6B7A] text-white" : "border text-slate-500"}`}>
                近{d}天
              </button>
            ))}
          </div>
        </div>
        <div className="flex flex-wrap gap-2 mb-3">
          {platforms.map((p) => {
            const on = active.includes(p);
            return (
              <button key={p}
                onClick={() => setSelected(on ? active.filter((x) => x !== p) : [...active, p])}
                className={`px-3 py-1 text-xs rounded-full border transition-colors ${on ? "text-white" : "text-slate-400 bg-white"}`}
                style={on ? { background: platformInfo(p).color, borderColor: platformInfo(p).color } : {}}>
                {platformInfo(p).name}
              </button>
            );
          })}
          {platforms.length === 0 && <span className="text-sm text-slate-400">暂无按量账号——先在「模型API管理」添加「按量付费 API」账号并做几次采集</span>}
        </div>
        <div ref={ref} style={{ height: 340 }} />
      </div>

      <div className="bg-white rounded-xl p-5 shadow-sm text-sm text-slate-500 leading-6">
        <div className="font-semibold text-[#1A2B40] mb-1">趋势是怎么算出来的</div>
        每次「采集」或「从本地代理同步」都会在数据库写入一条快照。按量账号快照记余额(元), 相邻两次快照的下降额即真实消耗(余额上升视为充值, 不计负消耗), 按区间中点归到某一天。
        <br />
        订阅制账号没有「每日消耗」曲线: 月费是固定的, 金额只按天摊销(月费÷当月天数), 每天的线是平的、没有信息量——所以订阅不再画趋势图。订阅的额度窗口(5小时/7天/月度)请看「当前总览 → 订阅额度窗口」面板。
      </div>
    </div>
  );
}
