import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts";
import { trpc } from "@/providers/trpc";
import { platformInfo, currencySymbol } from "@/lib/fmt";

/** 金额格式: 保留 2 位小数, 便于逐笔对账（符号跟随「显示货币」, V3.30.0 统一口径） */
const fmtAmt = (v: number) =>
  currencySymbol() + (Number(v) || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtAmt0 = (v: number) =>
  currencySymbol() + (Number(v) || 0).toLocaleString("zh-CN", { maximumFractionDigits: 0 });

function Chart({ option, height = 340 }: { option: echarts.EChartsOption; height?: number }) {
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

type Basis = "prorated" | "full";

export default function Ledger() {
  const utils = trpc.useUtils();
  const [tab, setTab] = useState<"month" | "year">("month");
  // 订阅账号口径: prorated=当月实际在订天数折算(默认) / full=整月月费
  const [basis, setBasis] = useState<Basis>("prorated");
  const [openMonth, setOpenMonth] = useState<string | null>(null);
  const ledger = trpc.quota.ledger.useQuery(undefined, { refetchInterval: 60000 });
  const [hidden, setHidden] = useState<string[]>([]);

  // 总投入额度(自动计算为主, 可手动覆盖)
  const tq = trpc.quota.getTotalQuota.useQuery();
  const [tqInput, setTqInput] = useState("");
  const [tqMsg, setTqMsg] = useState("");
  const [tqEditing, setTqEditing] = useState(false); // 是否正在手动编辑
  useEffect(() => {
    // 手动覆盖时回填手填值; 自动模式不占用输入框(显示自动值)
    if (tq.data && !tq.data.isAuto) setTqInput(String(tq.data.totalQuota));
  }, [tq.data]);
  const saveTq = trpc.quota.setTotalQuota.useMutation({
    onSuccess: () => { utils.quota.invalidate(); setTqEditing(false); setTqMsg("已保存"); setTimeout(() => setTqMsg(""), 2000); },
    onError: (e) => setTqMsg("保存失败: " + e.message),
  });

  const data = ledger.data;
  const platforms = useMemo(() => data?.platforms ?? [], [data?.platforms]);
  const shown = useMemo(() => platforms.filter((p) => !hidden.includes(p)), [platforms, hidden]);
  const months = data?.months ?? [];
  const years = data?.years ?? [];

  const nowYM = new Date().toISOString().slice(0, 7);
  const nowY = String(new Date().getFullYear());
  const thisMonth = months.find((m) => m.month === nowYM);
  const thisYear = years.find((y) => y.year === nowY);
  const totalOf = (row: { total: number; totalFull: number }) => (basis === "full" ? row.totalFull : row.total);
  const platOf = (row: { byPlatform: Record<string, number>; byPlatformFull: Record<string, number> }, p: string) =>
    basis === "full" ? (row.byPlatformFull?.[p] ?? 0) : (row.byPlatform?.[p] ?? 0);
  const subTotalOf = (row: { subTotal: number; subTotalFull: number }) => (basis === "full" ? row.subTotalFull : row.subTotal);
  const activeMonths = months.filter((m) => totalOf(m) > 0);
  const monthAvg = activeMonths.length ? activeMonths.reduce((s, m) => s + totalOf(m), 0) / activeMonths.length : 0;

  const cards = [
    { label: "当月花销", value: fmtAmt(thisMonth ? totalOf(thisMonth) : 0), sub: nowYM + " (截至今日)" },
    { label: "月均花销", value: fmtAmt0(monthAvg), sub: `按 ${activeMonths.length} 个有记录的月份平均` },
    { label: "涉及平台", value: `${platforms.length} 家`, sub: "按模型公司/平台分别统计" },
  ];

  // ---- 月度柱状图: 每个自然月一根, 按平台堆叠 ----
  const monthOption = useMemo<echarts.EChartsOption>(
    () => ({
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v) => fmtAmt(Number(v)) },
      // 图例顶部单行可滚动: 平台多时不换行挤压柱体区, 消除文字重叠
      legend: { top: 0, textStyle: { fontSize: 11 }, type: "scroll", pageIconSize: 10, pageTextStyle: { fontSize: 10 } },
      grid: { left: 66, right: 18, top: 38, bottom: 52 },
      xAxis: { type: "category", data: months.map((m) => m.month.slice(2)), axisLabel: { interval: 0, hideOverlap: true } },
      yAxis: { type: "value", name: "元", axisLabel: { formatter: (v: number) => (v >= 1000 ? (v / 1000) + "k" : String(v)) } },
      series: shown.map((p) => ({
        name: platformInfo(p).name,
        type: "bar" as const,
        stack: "total",
        barMaxWidth: months.length <= 4 ? 108 : 54,
        data: months.map((m) => platOf(m, p)),
        color: platformInfo(p).color,
        emphasis: { focus: "series" as const },
      })),
    }),
    [months, shown, basis]
  );

  // ---- 年度柱状图: 每年一根, 按平台堆叠 ----
  const yearOption = useMemo<echarts.EChartsOption>(
    () => ({
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v) => fmtAmt(Number(v)) },
      legend: { top: 0, textStyle: { fontSize: 11 }, type: "scroll", pageIconSize: 10, pageTextStyle: { fontSize: 10 } },
      grid: { left: 66, right: 18, top: 38, bottom: 52 },
      xAxis: { type: "category", data: years.map((y) => y.year + " 年") },
      yAxis: { type: "value", name: "元", axisLabel: { formatter: (v: number) => (v >= 1000 ? (v / 1000) + "k" : String(v)) } },
      series: shown.map((p) => ({
        name: platformInfo(p).name,
        type: "bar" as const,
        stack: "total",
        barMaxWidth: 90,
        data: years.map((y) => platOf(y, p)),
        color: platformInfo(p).color,
        emphasis: { focus: "series" as const },
      })),
    }),
    [years, shown, basis]
  );

  const grand = activeMonths.reduce((s, m) => s + totalOf(m), 0);

  // 账目总计(会计恒等式: 总投入 = 累计消耗 + 当前余额): 自动计算为主, 可手动覆盖
  const autoTotal = tq.data?.autoTotal ?? 0; // 自动计算值
  const isAuto = tq.data?.isAuto ?? true; // 当前是否自动模式
  // 生效总投入: 手动编辑中预览输入值, 否则用后端返回(自动或已存手填)
  const totalQuota = tqEditing ? (Number(tqInput) || 0) : (tq.data?.totalQuota ?? 0);
  // 联动指标(与「模型API管理」逐账号完全同口径, 后端 _account_view 聚合):
  //   spentTotal   = 累计消耗(Σ 各账号 已付−当前余额价值)
  //   remainingAll = 当前余额(按量余额 + 订阅已付未用, 年付含未来周期价值)
  const spentTotal = tq.data?.totalUsedAll ?? grand;
  const remainingAll = tq.data?.remainingAll ?? 0;
  const monthUsedAll = tq.data?.monthUsedAll ?? 0;
  const quotaPct = totalQuota > 0 ? Math.min(100, (spentTotal / totalQuota) * 100) : 0;

  if (ledger.isLoading) {
    return <div className="bg-white rounded-xl p-10 text-center text-slate-400 text-sm">正在统计账目…</div>;
  }
  if (ledger.isError) {
    return (
      <div className="bg-white rounded-xl p-10 text-center text-sm leading-7">
        <div className="text-amber-600 font-medium mb-1">统计接口暂不可用</div>
        <div className="text-slate-500">
          该功能需要后台服务重启后生效。请关闭启动窗口后重新双击「双击启动_Windows.bat」，
          再回到本页即可看到账目统计。
        </div>
      </div>
    );
  }
  if (!data?.hasData) {
    return (
      <div className="bg-white rounded-xl p-10 text-center text-slate-400 text-sm leading-7">
        还没有可统计的账目数据。<br />
        添加账号并保持自动采集后, 系统会按自然月/自然年自动累积花销, 这里就会出图表了。
      </div>
    );
  }

  const curRows = tab === "month" ? months : years;
  const curKey = (r: Record<string, unknown>) => (tab === "month" ? String(r.month) : String(r.year));

  return (
    <div className="space-y-4">
      {/* 账目总计: 总投入 = 累计消耗 + 当前余额(会计恒等式, 与「模型API管理」联动) */}
      <div className="bg-white rounded-xl p-5 shadow-sm">
        <div className="flex items-center justify-between flex-wrap gap-2 mb-3">
          <div className="font-semibold flex items-center gap-2 flex-wrap">
            账目总计
            {isAuto && !tqEditing && (
              <span className="text-[11px] px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-600 border border-emerald-200 font-normal">
                自动计算
              </span>
            )}
            {!isAuto && !tqEditing && (
              <span className="text-[11px] px-2 py-0.5 rounded-full bg-amber-50 text-amber-600 border border-amber-200 font-normal">
                手动覆盖
              </span>
            )}
            <span className="text-xs text-slate-400 font-normal">
              总投入 = 累计消耗 + 当前余额（与模型API管理逐账号联动）
            </span>
          </div>
          <div className="text-sm text-slate-500">
            参考：按量累计充值 <span className="font-medium text-[#1A2B40]">{fmtAmt0(tq.data?.rechargeTotal ?? 0)}</span>
          </div>
        </div>

        {!tqEditing ? (
          /* ---- 展示模式: 总投入 / 累计消耗 / 当前余额 恒等式三联动 ---- */
          <div className="flex items-end gap-6 flex-wrap">
            <div>
              <div className="text-xs text-slate-500 mb-0.5">总投入(元)</div>
              <div className="text-2xl font-semibold tabular-nums text-[#1A2B40]">{fmtAmt0(totalQuota)}</div>
            </div>
            <div>
              <div className="text-xs text-slate-500 mb-0.5">累计消耗(元)</div>
              <div className="text-2xl font-semibold tabular-nums text-[#B45309]">{fmtAmt(spentTotal)}</div>
            </div>
            <div>
              <div className="text-xs text-slate-500 mb-0.5">当前余额(元)</div>
              <div className="text-2xl font-semibold tabular-nums text-emerald-600">{fmtAmt(remainingAll)}</div>
            </div>
            <div className="text-xs text-slate-400">
              本月消耗 {fmtAmt(monthUsedAll)}<br />恒等式: 投入 = 消耗 + 余额
            </div>
            <div className="flex items-center gap-2">
              <button
                className="px-3 py-2 text-sm rounded-lg border border-slate-200 text-slate-600 hover:bg-slate-50"
                onClick={() => { setTqInput(isAuto ? "" : String(tq.data?.totalQuota ?? "")); setTqEditing(true); }}>
                手动调整
              </button>
              {!isAuto && (
                <button
                  className="px-3 py-2 text-sm rounded-lg border border-emerald-200 text-emerald-600 hover:bg-emerald-50"
                  disabled={saveTq.isPending}
                  onClick={() => saveTq.mutate({ totalQuota: 0 })}>
                  恢复自动（{fmtAmt0(autoTotal)}）
                </button>
              )}
              {tqMsg && <span className="text-sm text-slate-500">{tqMsg}</span>}
            </div>
          </div>
        ) : (
          /* ---- 编辑模式 ---- */
          <div className="flex items-end gap-3 flex-wrap">
            <label className="text-sm">
              总投入(元)
              <input type="number" placeholder={`自动值 ${fmtAmt0(autoTotal)}`}
                className="mt-1 block w-44 border rounded-lg px-2 py-1.5"
                value={tqInput} onChange={(e) => setTqInput(e.target.value)} />
            </label>
            <button
              className="px-4 py-2 text-sm rounded-lg bg-[#1B6B7A] text-white disabled:opacity-50"
              disabled={saveTq.isPending}
              onClick={() => saveTq.mutate({ totalQuota: Number(tqInput) || 0 })}>
              保存
            </button>
            <button
              className="px-3 py-2 text-sm rounded-lg border border-slate-200 text-slate-500 hover:bg-slate-50"
              onClick={() => { setTqEditing(false); setTqInput(""); }}>
              取消
            </button>
            {isAuto && Number(tqInput) <= 0 && (
              <span className="text-xs text-slate-400">留空或填 0 保存 = 恢复自动计算（{fmtAmt0(autoTotal)}）</span>
            )}
            {tqMsg && <span className="text-sm text-slate-500">{tqMsg}</span>}
          </div>
        )}

        {totalQuota > 0 ? (
          <div className="mt-4">
            <div className="flex items-center justify-between text-sm mb-1.5">
              <span className="text-slate-500">累计消耗 {fmtAmt(spentTotal)} / 总投入 {fmtAmt0(totalQuota)}</span>
              <span className={`font-semibold ${quotaPct >= 90 ? "text-red-500" : quotaPct >= 70 ? "text-amber-500" : "text-emerald-600"}`}>
                已用 {quotaPct.toFixed(1)}% · 当前余额 {fmtAmt0(remainingAll)}
              </span>
            </div>
            <div className="h-3 bg-slate-100 rounded-full overflow-hidden">
              <div className={`h-full rounded-full ${quotaPct >= 90 ? "bg-red-500" : quotaPct >= 70 ? "bg-amber-500" : "bg-emerald-500"}`}
                style={{ width: `${quotaPct}%` }} />
            </div>
            <div className="text-[11px] text-slate-400 mt-1.5">
              三个数字与「模型API管理」逐账号联动且恒等：总投入 = Σ已付(按量累计充值 + 订阅年付×12/月付按周期)；累计消耗 = Σ(已付 − 当前余额价值)；当前余额 = 按量余额 + 订阅已付未用(年付含未来周期价值)。与「当前总览-当前余额」同源一致。
            </div>
          </div>
        ) : (
          <div className="text-xs text-slate-400 mt-3">
            添加账号并填写「累计充值 / 订阅月费」后，账目总计会自动算出来。也可点「手动调整」覆盖为任意值。
          </div>
        )}
      </div>

      {/* 汇总卡片 */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {cards.map((c) => (
          <div key={c.label} className="bg-white rounded-xl p-4 shadow-sm">
            <div className="text-xs text-slate-500">{c.label}</div>
            <div className="text-2xl font-semibold mt-1 tabular-nums">{c.value}</div>
            <div className="text-[11px] text-slate-400 mt-1">{c.sub}</div>
          </div>
        ))}
      </div>

      {/* 控制条 */}
      <div className="bg-white rounded-xl p-4 shadow-sm flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          {([["month", "按自然月"], ["year", "按自然年"]] as const).map(([k, lb]) => (
            <button key={k} onClick={() => setTab(k)}
              className={`px-3.5 py-1.5 text-sm rounded-lg transition-colors ${tab === k ? "bg-[#1B6B7A] text-white" : "border border-slate-200 text-slate-600 hover:bg-slate-50"}`}>
              {lb}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <span>订阅费口径</span>
          {([["prorated", "按在订天数折算"], ["full", "整月计入"]] as const).map(([k, lb]) => (
            <button key={k} onClick={() => setBasis(k)}
              className={`px-2.5 py-1 rounded-md transition-colors ${basis === k ? "bg-[#1B6B7A] text-white" : "border border-slate-200 text-slate-500 hover:bg-slate-50"}`}>
              {lb}
            </button>
          ))}
        </div>
      </div>

      {/* 图例筛选 */}
      <div className="bg-white rounded-xl p-4 shadow-sm">
        <div className="flex items-center justify-between flex-wrap gap-2 mb-3">
          <div className="font-semibold">
            {tab === "month" ? "各自然月花销" : "各自然年花销"}
            <span className="text-xs text-slate-400 font-normal ml-2">按模型公司/平台堆叠, 点击图例可隐藏</span>
          </div>
          <div className="text-sm text-slate-500">
            {tab === "month" ? "累计" : `${years.length} 个年度`}：
            <span className="font-semibold text-[#1A2B40] ml-1">{fmtAmt0(grand)}</span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2 mb-3">
          {platforms.map((p) => {
            const on = !hidden.includes(p);
            return (
              <button key={p}
                onClick={() => setHidden(on ? [...hidden, p] : hidden.filter((x) => x !== p))}
                className={`px-3 py-1 text-xs rounded-full border transition-colors ${on ? "text-white" : "text-slate-400 bg-white"}`}
                style={on ? { background: platformInfo(p).color, borderColor: platformInfo(p).color } : {}}>
                {platformInfo(p).name}
              </button>
            );
          })}
        </div>
        {tab === "month" && <Chart option={monthOption} height={340} />}
        {tab === "year" && <Chart option={yearOption} height={340} />}
      </div>

      {/* 统计表 */}
      <div className="bg-white rounded-xl p-4 shadow-sm overflow-x-auto">
        <div className="font-semibold mb-3">
          {tab === "month" ? "月度花销统计表（元）" : "年度花销统计表（元）"}
          <span className="text-xs text-slate-400 font-normal ml-2">
            {tab === "month" ? "点击任意行展开该月账号明细" : "由各自然月自动汇总"}
          </span>
        </div>
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="text-slate-500 border-b border-slate-200">
              <th className="text-left py-2 pr-3 font-medium whitespace-nowrap">{tab === "month" ? "自然月" : "自然年"}</th>
              {platforms.map((p) => (
                <th key={p} className="text-right py-2 px-3 font-medium whitespace-nowrap">{platformInfo(p).name}</th>
              ))}
              <th className="text-right py-2 pl-3 font-medium whitespace-nowrap">{tab === "month" ? "本月合计" : "全年合计"}</th>
            </tr>
          </thead>
          <tbody>
            {tab === "month" &&
              months.map((m) => {
                const tot = totalOf(m);
                const open = openMonth === m.month;
                return (
                  <Fragment key={m.month}>
                    <tr
                      onClick={() => setOpenMonth(open ? null : m.month)}
                      className={`border-b border-slate-100 cursor-pointer transition-colors ${open ? "bg-[#F1F6F8]" : "hover:bg-slate-50"}`}>
                      <td className="py-2.5 pr-3 whitespace-nowrap font-medium">
                        <span className="text-slate-400 mr-1.5 text-xs">{open ? "▾" : "▸"}</span>{m.month}
                      </td>
                      {platforms.map((p) => {
                        const v = platOf(m, p);
                        return (
                          <td key={p} className="text-right py-2.5 px-3 tabular-nums text-slate-600">
                            {v > 0 ? fmtAmt(v) : <span className="text-slate-300">—</span>}
                          </td>
                        );
                      })}
                      <td className="text-right py-2.5 pl-3 tabular-nums font-semibold">{fmtAmt(tot)}</td>
                    </tr>
                    {open && (
                      <tr className="bg-[#F8FAFC] border-b border-slate-200">
                        <td colSpan={platforms.length + 2} className="py-3 px-4">
                          <div className="text-xs text-slate-500 mb-2">
                            {m.month} 账号明细
                            {subTotalOf(m) > 0 && (
                              <span className="ml-2 text-slate-400">
                                （其中订阅 {fmtAmt(subTotalOf(m))}
                                {basis === "prorated" ? ", 按当月实际在订天数折算" : ", 按整月月费计入"}）
                              </span>
                            )}
                          </div>
                          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                            {(basis === "full" ? m.detailFull : m.detail).map((dt) => (
                              <div key={dt.accountId + dt.platform} className="flex items-center justify-between bg-white border border-slate-200 rounded-lg px-3 py-2">
                                <div className="min-w-0">
                                  <div className="text-sm truncate">{dt.name}</div>
                                  <div className="text-[11px] text-slate-400">
                                    {platformInfo(dt.platform).name} · {dt.apiType === "sub" ? `订阅 ${currencySymbol()}${dt.monthlyFee}/月` : "按量付费"}
                                  </div>
                                </div>
                                <div className="text-sm font-medium tabular-nums ml-3 shrink-0">{fmtAmt(dt.amount)}</div>
                              </div>
                            ))}
                            {(basis === "full" ? m.detailFull : m.detail).length === 0 && (
                              <div className="text-xs text-slate-400">该月无消耗记录</div>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            {tab === "year" &&
              years.map((y) => (
                <tr key={y.year} className="border-b border-slate-100">
                  <td className="py-2.5 pr-3 whitespace-nowrap font-medium">{y.year} 年</td>
                  {platforms.map((p) => {
                    const v = platOf(y, p);
                    return (
                      <td key={p} className="text-right py-2.5 px-3 tabular-nums text-slate-600">
                        {v > 0 ? fmtAmt(v) : <span className="text-slate-300">—</span>}
                      </td>
                    );
                  })}
                  <td className="text-right py-2.5 pl-3 tabular-nums font-semibold">{fmtAmt(totalOf(y))}</td>
                </tr>
              ))}
          </tbody>
          <tfoot>
            <tr className="border-t-2 border-slate-200">
              <td className="py-2.5 pr-3 font-semibold whitespace-nowrap">总计</td>
              {platforms.map((p) => {
                const v = curRows.reduce((s, r) => s + platOf(r, p), 0);
                return (
                  <td key={p} className="text-right py-2.5 px-3 tabular-nums font-medium">
                    {v > 0 ? fmtAmt(v) : <span className="text-slate-300">—</span>}
                  </td>
                );
              })}
              <td className="text-right py-2.5 pl-3 tabular-nums font-bold text-[#1B6B7A]">{fmtAmt(grand)}</td>
            </tr>
          </tfoot>
        </table>
      </div>

      {/* 口径说明 */}
      <div className="bg-white rounded-xl p-5 shadow-sm text-sm text-slate-500 leading-6">
        <div className="font-semibold text-[#1A2B40] mb-1">账目是怎么算出来的</div>
        <ul className="list-disc pl-5 space-y-1">
          <li><b>按量付费账号</b>：消耗 = 相邻两次采集的「余额下降额」累加。充值（余额上升）自动剔除，绝不计成负消耗；跨天、跨月的消耗按发生时间归属到对应自然月，因此不会漏算。</li>
          <li><b>订阅账号</b>：消耗 = 月费。默认按「当月实际在订天数 ÷ 当月总天数」折算（月中订阅不会虚增整月）；切到「整月计入」则当月只要在订即记整月月费。</li>
          <li><b>免费/赠送额度</b>不产生金额（单价记 0），只体现在额度进度里，不进账目。</li>
          <li>统计依据是系统自动累积的余额快照，<b>不依赖任何平台接口</b>，所以全程自动、无需手工记账。系统运行越久，账目越完整。</li>
        </ul>
      </div>
    </div>
  );
}
