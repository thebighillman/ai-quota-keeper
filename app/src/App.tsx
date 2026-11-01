import { useCallback, useEffect, useState } from "react";
import Dashboard from "./pages/Dashboard";
import Accounts from "./pages/Accounts";
import Help from "./pages/Help";
import ErrorBoundary from "./components/ErrorBoundary";
import { trpc } from "@/providers/trpc";
import { setDisplayCurrency } from "@/lib/fmt";

/* 系统 Logo: 圆环+余额刻度(主题色) */
function Logo({ size = 36 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" fill="none" aria-label="AI额度管家">
      <defs>
        <linearGradient id="lg" x1="0" y1="0" x2="48" y2="48">
          <stop offset="0" stopColor="#37DCF2" />
          <stop offset="1" stopColor="#1B6B7A" />
        </linearGradient>
      </defs>
      {/* 外环: 余额刻度盘 */}
      <circle cx="24" cy="24" r="21" stroke="url(#lg)" strokeWidth="4" strokeDasharray="99 33" strokeLinecap="round" transform="rotate(-90 24 24)" />
      {/* 内部: 上升柱状(消耗趋势) */}
      <rect x="14" y="24" width="5" height="10" rx="1.5" fill="#1B6B7A" />
      <rect x="21.5" y="19" width="5" height="15" rx="1.5" fill="#1B6B7A" />
      <rect x="29" y="14" width="5" height="20" rx="1.5" fill="#37DCF2" />
    </svg>
  );
}

const NAV = [
  { key: "dashboard", label: "当前总览", icon: "◉" },
  { key: "accounts", label: "模型API管理", icon: "▦" },
  { key: "about", label: "关于系统", icon: "ⓘ" },
] as const;

type PageKey = (typeof NAV)[number]["key"];
const TITLES: Record<PageKey, string> = {
  dashboard: "当前总览",
  accounts: "模型API管理",
  about: "关于系统",
};

export default function App() {
  const [page, setPage] = useState<PageKey>(() => {
    try {
      const saved = localStorage.getItem("lq_page");
      if (saved === "dashboard" || saved === "accounts" || saved === "about") return saved;
      // 旧页面(ledger 已并入当前总览的子标签页; trend/budget/help 为更早结构)统一迁移
      return "dashboard";
    } catch {
      return "dashboard";
    }
  });
  const [ver, setVer] = useState("");
  const [newVer, setNewVer] = useState<string | null>(null);   // V3.30.4: 磁盘已升级但页面还是旧界面
  // 预警联动: 平台名 + 账号名(V3.32.1: 同平台多账号要能定位到具体那一个)
  const [focusAccount, setFocusAccount] = useState<{ platform: string; name?: string } | null>(null);

  // 版本号要跟着程序文件走(V3.30.4): 升级后旧窗口会一直显示老版本号,
  // 所以定期 + 窗口重新获得焦点时重取; 发现磁盘版本比页面新就提示刷新。
  const fetchVer = useCallback(() => {
    fetch("/api/version?t=" + Date.now())
      .then((r) => r.json())
      .then((d) => {
        const v = String(d.version || "");
        if (!v) return;
        setVer((prev) => {
          if (prev && v !== prev) setNewVer(v);   // 文件已升级, 页面仍是旧界面
          return v;
        });
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetchVer();
    const timer = window.setInterval(fetchVer, 60000);          // 每分钟探一次
    const onVisible = () => { if (document.visibilityState === "visible") fetchVer(); };
    const onFocus = () => fetchVer();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onFocus);
    };
  }, [fetchVer]);

  // 多币种(V3.19.0): 启动时拉「显示货币」, 汇总金额按它渲染(账号行仍显示各自原币)
  const currencyQ = trpc.quota.getCurrency.useQuery(undefined, { refetchInterval: 120000 });
  const [, setCurrencyTick] = useState(0);
  useEffect(() => {
    if (currencyQ.data?.displayCurrency) {
      setDisplayCurrency(currencyQ.data.displayCurrency);
      setCurrencyTick((t) => t + 1);   // 触发一次重渲染, 让汇总金额立刻换符号
    }
  }, [currencyQ.data?.displayCurrency]);


  const nav = (k: PageKey | string) => {
    try {
      localStorage.setItem("lq_page", k);
    } catch {
      /* ignore */
    }
    setPage(k as PageKey);
  };

  /** 预警点击 → 跳模型API管理并定位该账号（V3.32.1: 带账号名可精确定位） */
  const gotoAccount = (platform: string, name?: string) => {
    setFocusAccount({ platform, name });
    nav("accounts");
  };

  return (
    <div className="flex min-h-screen bg-[#F3F5F9] text-[#1A2B40]">
      {/* 侧边栏 */}
      <aside className="w-52 shrink-0 bg-[#12233A] text-white flex flex-col fixed h-full">
        <div className="flex items-center gap-3 px-5 py-5">
          <Logo size={38} />
          <div className="font-semibold leading-tight text-base">AI额度管家</div>
        </div>
        <div className="px-5 pt-4 pb-1 text-[11px] text-slate-500">功能</div>
        <nav className="flex-1 px-3 space-y-1">
          {NAV.map((n) => (
            <button
              key={n.key}
              onClick={() => nav(n.key)}
              className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors ${
                page === n.key ? "bg-[#1B6B7A] text-white" : "text-slate-300 hover:bg-white/10"
              }`}
            >
              <span className="w-4 text-center">{n.icon}</span>
              {n.label}
            </button>
          ))}
        </nav>
        <div className="px-5 py-4 text-[11px] text-slate-400 border-t border-white/10">
          {ver ? `V${ver}` : ""}
        </div>
      </aside>

      {/* 主区域 */}
      <main className="flex-1 ml-52">
        <header className="h-14 bg-white border-b border-slate-200 flex items-center px-6 text-sm text-slate-500 sticky top-0 z-10">
          AI额度管家 / <span className="ml-2 text-[#1A2B40] font-semibold text-base">{TITLES[page]}</span>
        </header>
        <div className="p-6 max-w-[1400px]">
          {/* 已升级但页面还是旧界面(V3.30.4): 点一下刷新到新版 */}
          {newVer && (
            <div className="mb-4 px-4 py-3 rounded-lg bg-emerald-50 border border-emerald-200 text-sm text-emerald-800 flex items-center justify-between gap-3 flex-wrap">
              <span>🔄 程序已更新到 <b>V{newVer}</b>，当前窗口还是旧界面</span>
              <button onClick={() => window.location.reload()}
                className="px-3 py-1.5 text-[13px] rounded-lg bg-emerald-600 text-white hover:bg-emerald-700">
                刷新到新版
              </button>
            </div>
          )}
          {/* V3.30.9: 错误边界按页面隔离 —— 单页崩只显示错误块, 导航仍在, 可切走 */}
          <ErrorBoundary key={page} pageKey={page}>
            {page === "dashboard" && <Dashboard onNavigate={(k) => nav(k)} onAlertClick={gotoAccount} />}
            {page === "accounts" && <Accounts focusAccount={focusAccount} onFocusHandled={() => setFocusAccount(null)} />}
            {page === "about" && <Help />}
          </ErrorBoundary>
        </div>
      </main>
    </div>
  );
}
