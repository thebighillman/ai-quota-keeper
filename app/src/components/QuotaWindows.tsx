import { useCallback, useEffect, useState } from "react";
import { platformInfo } from "@/lib/fmt";

type Win = {
  key: string;             // 5h / 7d / 1d / 30d
  label: string;           // 5 小时 / 7 天 ...
  usedPct: number;
  remainingPct: number;
  resetAt?: number;        // 毫秒时间戳
  used?: number;
  total?: number;
  unit?: string;
};
type Item = {
  platform: string; name: string; group?: string; plan?: string;
  windows: Win[]; live: boolean; ok: boolean; note?: string;
};

/** V3.30.7: 展示「5 小时」「7 天」「月度(30d/月)」窗口; 没有命中时退回该平台全部窗口 */
function pickWindows(ws: Win[]) {
  const hit = ws.filter((w) => w.key === "5h" || w.key === "7d" || w.key === "30d" || w.key === "monthly");
  return hit.length ? hit : ws;
}

function fmtReset(ms?: number) {
  if (!ms) return "重置时间未知";
  const d = new Date(ms);
  const now = Date.now();
  const hhmm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const sameDay = d.toDateString() === new Date().toDateString();
  const day = sameDay ? "今天" : `${d.getMonth() + 1}-${d.getDate()}`;
  const diff = ms - now;
  let rel = "";
  if (diff > 0) {
    const h = Math.floor(diff / 3600000);
    const m = Math.round((diff % 3600000) / 60000);
    rel = h >= 24 ? `（${Math.floor(h / 24)} 天 ${h % 24} 小时后）`
      : h > 0 ? `（${h} 小时 ${m} 分后）` : `（${m} 分钟后）`;
  }
  return `${day} ${hhmm} 重置${rel}`;
}

function barColor(used: number) {
  return used >= 90 ? "bg-red-500" : used >= 70 ? "bg-amber-500" : "bg-emerald-500";
}
function textColor(used: number) {
  return used >= 90 ? "text-red-600" : used >= 70 ? "text-amber-600" : "text-emerald-600";
}

export default function QuotaWindows() {
  const [items, setItems] = useState<Item[]>([]);
  const [ts, setTs] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(async (force = false) => {
    if (force) setBusy(true);
    try {
      const r = await fetch("/api/quota/windows" + (force ? "?force=1" : ""), { cache: "no-store" });
      const d = await r.json();
      if (d.ok) { setItems(d.items ?? []); setTs(d.fetchedAt ?? Date.now()); setErr(""); }
      else setErr(String(d.detail || "读取失败"));
    } catch {
      setErr("连不上本地服务");
    }
    if (force) setBusy(false);
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(() => load(), 120000);
    return () => clearInterval(t);
  }, [load]);

  const timeText = ts
    ? new Date(ts).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" })
    : "—";

  // 没有订阅制账号时不占位(新用户界面保持干净)
  if (items.length === 0 && !err) return null;

  return (
    <div className="bg-white rounded-xl p-5 shadow-sm">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <div className="font-semibold">
          订阅额度窗口
          <span className="text-xs text-slate-400 font-normal ml-2">
            5 小时 / 7 天窗口进度与重置时间 · 只展示额度，不计入账目与消耗
          </span>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <span className="text-slate-400">读取于 {timeText}</span>
          <button onClick={() => load(true)} disabled={busy}
            className="px-2.5 py-1 rounded-lg border border-[#1B6B7A] text-[#1B6B7A] hover:bg-[#1B6B7A]/5 disabled:opacity-50">
            {busy ? "刷新中..." : "刷新"}
          </button>
        </div>
      </div>

      {err && <div className="text-xs px-3 py-2 rounded-lg bg-amber-50 text-amber-700 mb-3">{err}</div>}

      <div className="grid gap-3 md:grid-cols-3">
        {items.map((it) => {
          const pi = platformInfo(it.platform);
          const ws = pickWindows(it.windows ?? []);
          return (
            <div key={it.platform + it.name} className="border border-slate-200 rounded-xl p-4">
              <div className="flex items-center gap-2 mb-3">
                <span className="w-6 h-6 rounded-md text-white text-[10px] font-bold flex items-center justify-center shrink-0"
                  style={{ background: pi.color }}>{pi.abbr}</span>
                <span className="font-medium truncate">{pi.name}</span>
                {it.plan && (
                  <span className="ml-auto text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 shrink-0">{it.plan}</span>
                )}
              </div>

              {ws.length === 0 && (
                <div className="text-xs text-slate-400 leading-relaxed">
                  {it.note || "该账号暂未返回窗口额度"}
                </div>
              )}

              <div className="space-y-3">
                {ws.map((w) => (
                  <div key={w.key}>
                    <div className="flex items-baseline justify-between text-xs mb-1">
                      <span className="text-slate-500">{w.label}窗口</span>
                      <span className={`font-semibold tabular-nums ${textColor(w.usedPct)}`}>
                        已用 {w.usedPct.toFixed(0)}%
                        <span className="text-slate-400 font-normal ml-1">剩 {w.remainingPct.toFixed(0)}%</span>
                      </span>
                    </div>
                    <div className="h-2 bg-slate-100 rounded-full overflow-hidden">
                      <div className={`h-full rounded-full ${barColor(w.usedPct)}`}
                        style={{ width: `${Math.min(100, w.usedPct)}%` }} />
                    </div>
                    <div className="text-[11px] text-slate-400 mt-1">{fmtReset(w.resetAt)}</div>
                  </div>
                ))}
              </div>

              {ws.length > 0 && ws.every((w) => w.usedPct >= 90) && (
                <div className="mt-3 text-[11px] px-2 py-1.5 rounded bg-red-50 text-red-600">
                  窗口额度已接近用尽，建议先安排其它平台的开发
                </div>
              )}
            </div>
          );
        })}
      </div>

    </div>
  );
}
