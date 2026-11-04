import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { trpc } from "@/providers/trpc";
import {
  API_TYPES, METHOD_COLORS, PLATFORMS, STATUS_STYLE,
  platformInfo, remainingText, usedText, usedPercent, usedPercentText,
  totalRechargeText, totalUsedText, fmtMoney, currencySymbol,
} from "@/lib/fmt";
import type { Account } from "@db/schema";
import type { AccountView } from "@/lib/fmt";

// 本地代理接口地址: 用相对路径, 服务端把 /api/accounts 指向同一进程的代理,
// 这样本机访问、局域网访问、内网穿透(手机)访问都能通 —— 不能写死 127.0.0.1,
// 否则手机会去请求手机自己。
const apiBase = "";

const EMPTY = {
  platform: "deepseek", name: "", groupName: "默认",
  apiType: "payg" as "payg" | "sub",
  remaining: 0, quotaPercent: null as number | null,
  monthlyUsed: 0, monthlyFee: 0, totalRecharge: 0, budget: 0,
  collectMethod: "手动录入", note: "", validUntil: "",
  resetCycle: "" as "" | "weekly" | "monthly", resetDay: null as number | null,
  subSince: null as string | null,
  currency: "CNY",
  customName: "", phone: "",   // 同号去重(V3.34.1): 选填, 两者都填且与另一账号完全一致才生效
  win5h: null as number | null, win7d: null as number | null, winUpdatedAt: null as string | null,
};

// 平台按「计费类型」分三类(V3.30.1): 只按钱怎么算分, 不掺地域维度
// ①按量计费(充值余额) ②API订阅套餐(包月配额, 有 Key) ③在线会员(官网买的, 没有 Key)
// 海外平台也是按量充值, 归①; 它们的"需要自备网络"只是使用提示, 不是分类依据
export const PLATFORM_GROUPS: {
  id: string; label: string; hint: string; items: [string, string][];
}[] = [
  {
    id: "payg",
    label: "① 按量计费 · 充多少用多少",
    hint: "填 Key 自动查余额, 用多少扣多少",
    items: [
      ["deepseek", "DeepSeek"],
      ["kimi", "Kimi 开放平台"],
      ["glm", "智谱 GLM"],
      ["minimax", "MiniMax"],
      ["siliconflow", "SiliconFlow 硅基流动"],
      ["qwen", "阿里百炼(通义)"],
      ["stepfun", "阶跃星辰 StepFun"],
      ["kling", "可灵 Kling"],
      ["wenxin", "文心一言(千帆)"],
      ["xunfei", "讯飞星火"],
      ["hunyuan", "腾讯混元"],
      ["arkpayg", "火山方舟·按量(Doubao-Seed)"],
      ["lingyi", "零一万物"],
      ["baichuan", "百川智能"],
      ["openai", "OpenAI(海外)"],
      ["claude", "Claude(海外)"],
      ["gemini", "Gemini(海外)"],
      ["grok", "Grok(海外)"],
      ["openrouter", "OpenRouter(海外)"],
      ["mistral", "Mistral AI(海外)"],
      ["chatanywhere", "ChatAnywhere(转发站)"],
    ],
  },
  {
    id: "sub",
    label: "② API 订阅套餐 · 包月配额",
    hint: "按月付费, 5小时/周/月 窗口限额",
    items: [
      ["kimicode", "Kimi Coding Plan"],
      ["qwenplan", "百炼 Token Plan"],

      ["doubao", "火山方舟 Agent Plan"],
      ["glm@sub", "智谱 Coding Plan"],
      ["minimax@sub", "MiniMax Token Plan"],
    ],
  },
  {
    id: "online",
    label: "③ 在线会员 · 官网买的, 没有 Key",
    hint: "只需填月费, 按天摊进成本",
    items: [
      ["chatgpt", "ChatGPT Plus/Pro"],
      ["claude_sub", "Claude Pro/Max"],
      ["gemini_sub", "Gemini 会员"],
    ],
  },
];

// 扁平列表(兼容旧逻辑: 校验/查找用)
const PROXY_PLATFORMS: [string, string][] = PLATFORM_GROUPS.flatMap((g) => g.items);

/** 分组值 → 真实平台 id（"glm@sub" → "glm"）；后端适配器只认基础 id */
const basePlatform = (v: string) => v.split("@")[0];

// 有余额接口、能自动采集的平台(其余靠探活+手填)
const AUTO_COLLECT = new Set([
  "deepseek", "kimi", "glm", "minimax", "siliconflow", "qwen", "stepfun", "kling",
  "kimicode", "qwenplan", "doubao", "openrouter", "mistral",
]);
// qwen: 推荐填阿里云 AccessKey(AK空格SK)自动查账户余额; 百炼免费额度官方无 API 可查, 手动填
const SUB_ONLY = new Set(["kimicode", "qwenplan", "doubao"]);  // 这几个平台只有订阅配额形态(智谱/MiniMax 两种都有)
const AKSK_PLATFORMS = new Set(["doubao", "qwen", "arkpayg"]);
/** V3.33.5: 火山引擎系平台(Agent Plan / 按量)共用同一套 AK/SK 填写说明 */
const VOLC_PLATFORMS = new Set(["doubao", "arkpayg"]);

/** V3.33.1: 官方确实没有余额查询接口的平台 —— 在添加账号时明确告诉用户"只能手填", 并说清怎么做 */
const MANUAL_BALANCE_HINT: Record<string, string> = {
  chatanywhere: "ChatAnywhere 官方未开放余额查询 API(Key 只能验证有效性)。请到官网「余额查询」页看到数额后, "
    + "在账号列表点该账号的「填余额」录进去; 之后系统按你两次录入的差额自动算消耗。",
  claude: "Claude 官方没有余额接口。普通 Key 只能验证有效; 若填组织 Admin Key(sk-ant-admin 开头)"
    + "还能自动读本月成本报表。余额请在账号列表手动填, 系统按两次填写差额算消耗。",
  openai: "OpenAI 已关闭公开余额接口。Key 只做有效性验证; 余额请在账号列表手动填"
    + "(若填 sk-admin- 组织 Key 可自动读本月成本)。",
  gemini: "Google AI Studio 没有余额接口, Key 只做有效性验证。余额请手动填; "
    + "若走 OpenAI 兼容中转, 在「API 地址」填中转地址后可能可查。",
  grok: "xAI 官方没有余额/用量接口。余额请手动填, 系统按两次填写差额算消耗。",
  qwenplan: "百炼 Token Plan 用量未开放 API Key 查询。请在控制台「套餐用量」页看后手动填; "
    + "或用「阿里百炼(通义)」填阿里云 AccessKey 查账户余额。",
  glm: "智谱按量没有余额接口(只能按 token 用量折算)。请务必填「累计充值」, 系统据此推算余额。",
};  // 两段式 AccessKey(AK/SK)平台: 分两个框填写, 提交时自动拼成 "AK SK"

const API_FORM_EMPTY = {
  platform: "deepseek", name: "", apiKey: "", apiKeyAk: "", apiKeySk: "",
  apiType: "payg" as "payg" | "sub", monthlyFee: 0,
  currency: "CNY",   // 多币种(V3.19.0): 账号原币, 海外平台自动切 USD
  baseUrl: "",       // 自定义接入点(V3.20.0): 海外平台/中转站可填
  totalRecharge: "", // V3.33.1: 按量账号累计充值(必填, 否则累计消耗/总投入算不出来)
  customName: "", phone: "",  // V3.34.1: 同号去重身份字段(选填)
};

// 海外平台默认美元(只是默认值, 用户可在表单里改)
const USD_PLATFORMS = new Set(["openai", "claude", "gemini", "openrouter", "grok", "mistral"]);

// 在线订阅(V3.21.0): 官网买的消费级会员 —— 没有 API Key, 手动录入, 只有订阅形态
const ONLINE_SUBS = new Set(["chatgpt", "claude_sub", "gemini_sub"]);
const ONLINE_FEE_PRESETS: Record<string, number[]> = {
  chatgpt: [20, 200],
  claude_sub: [20, 100, 200],
  gemini_sub: [19.99],
};

/** 账号归属类别（V3.30.5: 只按计费方式分三类, 列表用分组条区分, 不再每行挂标签）
 *  归类优先级: 平台固有形态 > apiType —— 保证新增/历史账号都能自动落到正确组里 */
const accountCategory = (a: { platform: string; apiType: string }) => {
  const onlineSub = ONLINE_SUBS.has(a.platform);
  const subOnly = SUB_ONLY.has(a.platform);          // kimicode/qwenplan/doubao 只有订阅形态
  const isSub = subOnly || a.apiType === "sub";
  if (onlineSub) {
    return {
      label: "在线订阅", order: 2,
      desc: "官网买的会员, 只有月费没有 Key",
      section: "bg-amber-50/70 border-amber-200 text-amber-900",
      bar: "bg-amber-500",
    };
  }
  if (isSub) {
    return {
      label: "套餐订阅", order: 1,
      desc: "包月配额, 5小时/周/月 窗口限额",
      section: "bg-purple-50/70 border-purple-200 text-purple-900",
      bar: "bg-purple-500",
    };
  }
  return {
    label: "按量计费", order: 0,
    desc: "充值余额, 用多少扣多少",
    section: "bg-emerald-50/70 border-emerald-200 text-emerald-900",
    bar: "bg-emerald-500",
  };
};

/** 平台显示名（V3.30.3）: 同一平台两种形态时带上形态后缀。
 *  智谱订阅特例(V3.30.7): 与录入下拉一致, 显示「智谱 Coding Plan」而不是「智谱 GLM(订阅)」。 */
const platformLabel = (a: { platform: string; apiType: string }) => {
  if (a.platform === "glm" && a.apiType === "sub") return "智谱 Coding Plan";
  if (a.platform === "minimax" && a.apiType === "sub") return "MiniMax Token Plan";
  const base = platformInfo(a.platform).name;
  if (ONLINE_SUBS.has(a.platform)) return base;
  return `${base}（${a.apiType === "sub" ? "订阅" : "按量"}）`;
};

export default function Accounts({ focusAccount, onFocusHandled }: {
  /** V3.32.1: 预警联动目标 —— 带了账号名就精确定位到那一个, 否则按平台定位 */
  focusAccount?: { platform: string; name?: string } | null;
  onFocusHandled?: () => void;
}) {
  const utils = trpc.useUtils();
  const list = trpc.quota.list.useQuery(undefined, { refetchInterval: 30000 });
  const [editing, setEditing] = useState<(typeof EMPTY & { id?: number }) | null>(null);
  const [apiForm, setApiForm] = useState<typeof API_FORM_EMPTY | null>(null);
  const [apiBusy, setApiBusy] = useState(false);
  const [collecting, setCollecting] = useState<AccountView | null>(null);
  const [collectVal, setCollectVal] = useState("");
  const [msg, setMsg] = useState("");
  const [highlight, setHighlight] = useState<string | null>(null);
  const tableRef = useRef<HTMLTableElement>(null);

  // 预警联动(V3.32.1): 优先按账号名精确定位 —— 同平台多个账号时也能跳到正确那一个
  useEffect(() => {
    if (!focusAccount) return;
    const target = focusAccount.name || focusAccount.platform;
    setHighlight(target);
    const t = setTimeout(() => {
      const root = tableRef.current;
      const el = (focusAccount.name
                    ? root?.querySelector(`[data-account="${CSS.escape(focusAccount.name)}"]`)
                    : null)
                 ?? root?.querySelector(`[data-platform="${focusAccount.platform}"]`);
      el?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 250);
    const clear = setTimeout(() => { setHighlight(null); onFocusHandled?.(); }, 6000);
    return () => { clearTimeout(t); clearTimeout(clear); };
  }, [focusAccount, onFocusHandled]);

  const upsert = trpc.quota.upsert.useMutation({
    onSuccess: () => { utils.quota.invalidate(); setEditing(null); setMsg("已保存到数据库"); },
    onError: (e) => setMsg("保存失败: " + e.message),
  });
  const remove = trpc.quota.remove.useMutation({ onSuccess: () => utils.quota.invalidate() });
  const collect = trpc.quota.collect.useMutation({
    onSuccess: () => { utils.quota.invalidate(); setCollecting(null); setCollectVal(""); setMsg("采集成功, 已记录快照"); },
    onError: (e) => setMsg("采集失败: " + e.message),
  });
  const importAll = trpc.quota.importAll.useMutation({
    onSuccess: (r) => { utils.quota.invalidate(); setMsg(`同步完成: 新增 ${r.added} 个, 共处理 ${r.total} 个账号`); },
    onError: (e) => setMsg("导入失败: " + e.message),
  });

  // TOKEN用量(V3.34.1): 页内「账号列表 / TOKEN用量」两个视图
  const [view, setView] = useState<"list" | "tokens">("list");

  // 从本地代理同步(代理只在用户自己电脑上运行, Key 不出本机)
  const syncFromProxy = async () => {
    setMsg("正在连接本地代理 ...");
    try {
      const r = await fetch(apiBase + "/api/accounts?t=" + Date.now());
      const d = await r.json();
      const items = (d.accounts ?? []).map((a: Record<string, unknown>) => {
        const isSub = a.apiType === "sub" || a.unit === "%";
        return {
          platform: String(a.platformId ?? a.platform ?? "custom"),
          name: String(a.name ?? "未命名"),
          groupName: String(a.group ?? "默认"),
          apiType: (isSub ? "sub" : "payg") as "payg" | "sub",
          remaining: isSub ? 0 : Number(a.remaining ?? 0),
          quotaPercent: isSub ? Number(a.quotaPercent ?? a.remaining ?? 100) : null,
          monthlyUsed: isSub ? 0 : Number(a.monthlyUsed ?? 0),
          monthlyFee: isSub ? Number(a.monthlyFee ?? 0) : 0,
          totalRecharge: Number(a.totalRecharge ?? 0),
          budget: 0,
          collectMethod: String(a.collectMethod ?? "API直连"),
          note: String(a.note ?? ""),
          validUntil: a.validUntil ? String(a.validUntil) : null,
          customName: String(a.customName ?? ""),   // V3.34.1: 同号去重身份字段
          phone: String(a.phone ?? ""),
        };
      });
      if (!items.length) { setMsg("本地代理里没有账号"); return; }
      importAll.mutate({ items });
    } catch {
      setMsg("连不上本地代理——请在电脑上启动: python3 额度管家_本地代理.py(Key 仍只保存在你本机)");
    }
  };

  // 添加 API 账号: Key 直接写入本地代理(混淆存储), 然后自动同步回网页
  const addApiAccount = async () => {
    if (!apiForm) return;
    setApiBusy(true);
    setMsg("正在写入本地代理...");
    try {
      const r = await fetch(apiBase + "/api/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "add",
          platform: basePlatform(apiForm.platform),   // @sub 后缀只用于分组, 提交真实 id
          name: apiForm.name,
          api_key: AKSK_PLATFORMS.has(basePlatform(apiForm.platform))
            ? `${apiForm.apiKeyAk.trim()} ${apiForm.apiKeySk.trim()}`
            : apiForm.apiKey,
          currency: apiForm.currency,
          totalRecharge: Number(apiForm.totalRecharge) || 0,
          customName: apiForm.customName.trim(),   // V3.34.1: 同号去重身份字段
          phone: apiForm.phone.trim(),
          ...(apiForm.baseUrl.trim() ? { base_url: apiForm.baseUrl.trim() } : {}),
          ...(apiForm.apiType === "sub"
            ? { query: "quota", monthlyFee: apiForm.monthlyFee }
            : {}),
        }),
      });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || "写入失败");
      setApiForm(null);
      setMsg("已写入本地代理(Key 已混淆存储, 界面不显示), 正在同步余额...");
      await syncFromProxy();
    } catch (e) {
      setMsg("写入代理失败: " + (e instanceof Error ? e.message : String(e)) + " — 请确认本地代理已启动(双击 启动AI额度管家.bat)");
    }
    setApiBusy(false);
  };

  const allRows = list.data ?? [];
  // V3.30.3: 按类别分组排序(按量 → 订阅 → 在线), 同类别内按平台名+账号名。
  // 注意: rows 里放的是**裸账号**(不带包装), 类别在渲染处用 accountCategory(a) 现算 ——
  // 之前包装成 {a, cat, kind} 导致手机端/桌面端把包装对象当账号用而白屏(V3.30.8 修)。
  const [catFilter, setCatFilter] = useState<"all" | "payg" | "sub" | "online">("all");
  const rows = useMemo(() => {
    const kindOf = (a: Account) =>
      ONLINE_SUBS.has(a.platform) ? "online" : (a.apiType === "sub" ? "sub" : "payg");
    const filtered = catFilter === "all" ? [...allRows] : allRows.filter((a) => kindOf(a) === catFilter);
    filtered.sort((x, y) => {
      const cx = accountCategory(x), cy = accountCategory(y);
      if (cx.order !== cy.order) return cx.order - cy.order;
      const p = platformInfo(x.platform).name.localeCompare(platformInfo(y.platform).name, "zh");
      return p !== 0 ? p : String(x.name).localeCompare(String(y.name), "zh");
    });
    return filtered;
  }, [allRows, catFilter]);
  const counts = useMemo(() => {
    const c = { payg: 0, sub: 0, online: 0 };
    for (const a of allRows) {
      if (ONLINE_SUBS.has(a.platform)) c.online++;
      else if (a.apiType === "sub") c.sub++;
      else c.payg++;
    }
    return c;
  }, [allRows]);

  // V3.33.1: 必填项校验 —— 缺哪项就在弹窗底部直接说清, 不再"按钮灰着但不知道为什么"
  const apiFormMissing = (() => {
    if (!apiForm) return [] as string[];
    const miss: string[] = [];
    if (!apiForm.name.trim()) miss.push("账号名称");
    if (AKSK_PLATFORMS.has(basePlatform(apiForm.platform))) {
      if (!apiForm.apiKeyAk.trim()) miss.push("AccessKey");
      if (!apiForm.apiKeySk.trim()) miss.push("SecretKey");
    } else if (!apiForm.apiKey.trim()) miss.push("API Key");
    if (apiForm.apiType === "payg") {
      if (String(apiForm.totalRecharge).trim() === "" || Number(apiForm.totalRecharge) <= 0) {
        miss.push("累计充值");
      }
    } else if (!(Number(apiForm.monthlyFee) > 0)) {
      miss.push("月费");
    }
    return miss;
  })();

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-xl p-5 shadow-sm">
        <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
          {/* 视图切换(V3.34.1): 账号列表 / TOKEN用量 */}
          <div className="flex items-center gap-2 flex-wrap">
            {([["list", "账号列表"], ["tokens", "TOKEN 用量"]] as const).map(([id, label]) => (
              <button key={id} onClick={() => setView(id)}
                className={`px-3.5 py-1.5 text-sm rounded-lg font-medium transition-colors ${view === id ? "bg-[#1B6B7A] text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}>
                {label}
              </button>
            ))}
            <span className="text-xs text-slate-400 font-normal hidden sm:inline">共 {allRows.length} 个账号</span>
          </div>
          <div className="flex gap-2 flex-wrap">
            <button onClick={() => setApiForm({ ...API_FORM_EMPTY })} className="px-3 py-2 text-sm rounded-lg bg-[#1B6B7A] text-white hover:bg-[#155a66]">
              🔑 添加API账号
            </button>
            <button onClick={syncFromProxy} className="px-3 py-2 text-sm rounded-lg border border-[#1B6B7A] text-[#1B6B7A] hover:bg-[#1B6B7A]/5">
              ⬇ 从本地代理同步
            </button>
            <button onClick={() => setEditing({ ...EMPTY })} className="px-3 py-2 text-sm rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50">
              + 手动录入
            </button>
          </div>
        </div>
        {/* 类别筛选(V3.30.3): 一眼看清各类有几个, 点一下只看该类(仅列表视图显示) */}
        {view === "list" && (
        <div className="flex gap-1.5 flex-wrap mb-3">
          {([
            ["all", "全部", allRows.length, "bg-slate-800 text-white", "bg-slate-100 text-slate-600"],
            ["payg", "按量计费", counts.payg, "bg-green-600 text-white", "bg-green-50 text-green-700"],
            ["sub", "订阅套餐", counts.sub, "bg-purple-600 text-white", "bg-purple-50 text-purple-700"],
            ["online", "在线订阅", counts.online, "bg-amber-600 text-white", "bg-amber-50 text-amber-700"],
          ] as const).map(([id, label, n, onCls, offCls]) => (
            <button key={id} onClick={() => setCatFilter(id as typeof catFilter)}
              className={`px-2.5 py-1 text-[12px] rounded-full transition-colors ${catFilter === id ? onCls : offCls + " hover:opacity-80"}`}>
              {label} {n}
            </button>
          ))}
        </div>
        )}
        {view === "tokens" ? (
          <TokenUsagePanel />
        ) : (<>
        {msg && <div className="mb-3 text-sm px-3 py-2 rounded-lg bg-slate-50 text-slate-600">{msg}</div>}
        {/* 桌面端: 精简表格(9 列, 数字单行不换行; 订阅重置详情不放在列表里) */}
        <div className="overflow-x-auto hidden md:block">
          <table ref={tableRef} className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-slate-400 border-b">
                <th className="py-2.5 pr-3 font-normal">平台</th>
                <th className="pr-3 font-normal">账号</th>
                <th className="pr-3 font-normal" title="按量=账户余额；订阅=平台配额剩余，(周)=7天周额度、(月)=官方月度额度">剩余额度</th>
                <th className="pr-3 font-normal" title="自然月 1 号起算">本月消耗</th>
                <th className="pr-3 font-normal text-right" title="上：累计充值　下：累计消耗">充值/消耗</th>
                <th className="pr-3 font-normal">状态</th>
                <th className="font-normal text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a, idx) => {
                const pct = usedPercent(a);
                const st = STATUS_STYLE[a.status] ?? STATUS_STYLE.success;
                const pi = platformInfo(a.platform);
                const cat = accountCategory(a);
                const prevCat = idx > 0 ? accountCategory(rows[idx - 1]) : null;
                const showHeader = !prevCat || prevCat.label !== cat.label;
                return (
                  <Fragment key={a.id}>
                  {showHeader && (
                    <tr>
                      <td colSpan={7} className="pt-5 pb-0">
                        <div className={`flex items-center gap-2.5 px-3 py-2 rounded-lg border ${cat.section}`}>
                          <span className={`w-1.5 h-5 rounded-full ${cat.bar}`} />
                          <span className="font-semibold text-[13px]">{cat.label}</span>
                          <span className="text-[11px] opacity-70">
                            {rows.filter((r) => accountCategory(r).label === cat.label).length} 个账号 · {cat.desc}
                          </span>
                        </div>
                      </td>
                    </tr>
                  )}
                  <tr data-platform={a.platform} data-account={a.name}
                    className={`border-b last:border-0 hover:bg-slate-50/60 ${(highlight === a.name || highlight === a.platform) ? "bg-amber-50 outline outline-2 outline-amber-300" : ""}`}>
                    <td className="py-3 pr-3">
                      <span className="flex items-center gap-2 min-w-0">
                        <span className="w-7 h-7 shrink-0 rounded-lg text-white text-[11px] font-bold flex items-center justify-center" style={{ background: pi.color }}>{pi.abbr}</span>
                        <span className="truncate">{platformLabel(a)}</span>
                      </span>
                    </td>
                    <td className="pr-3 max-w-[230px]">
                      <div className="flex items-center gap-1.5 min-w-0">
                        <span className="font-medium truncate" title={a.name}>{a.name}</span>
                        {a.dupIdentity && (
                          <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-sky-50 text-sky-700 border border-sky-200"
                            title="与另一账号的「账户名称+电话」完全一致，判定为同一真实账户：累计充值/总投入只计一次">同号</span>
                        )}
                      </div>
                      {a.customName && (
                        <div className="text-[10px] text-slate-400 truncate" title={`账户: ${a.customName}${a.phone ? " / " + a.phone : ""}`}>
                          {a.customName}{a.phone ? ` · ${a.phone}` : ""}
                        </div>
                      )}
                      {a.groupName && a.groupName !== "默认" && (
                        <div className="text-[10px] text-slate-400 truncate">{a.groupName}</div>
                      )}
                    </td>
                    <td className="pr-3 font-semibold whitespace-nowrap tabular-nums">{remainingText(a)}</td>
                    <td className="pr-3 whitespace-nowrap tabular-nums text-slate-600">{usedText(a)}</td>
                    <td className="pr-4 whitespace-nowrap">
                      <div className="flex flex-col items-end leading-tight tabular-nums">
                        <div className="text-[12px] text-slate-400">充 {totalRechargeText(a)}</div>
                        <div className="text-[13px] font-medium">耗 {totalUsedText(a)}</div>
                      </div>
                    </td>
                    <td className="pr-3 whitespace-nowrap">
                      <span className={`inline-flex items-center gap-1.5 text-xs ${st.text}`} title={a.statusText}>
                        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${st.dot}`} />
                        <span className="truncate">{a.statusText}</span>
                      </span>
                      <div className="text-[10px] text-slate-400 mt-0.5">{a.collectMethod}</div>
                    </td>
                    <td className="whitespace-nowrap text-right">
                      <button className="text-xs text-[#1B6B7A] hover:underline mr-2" onClick={() => { setCollecting(a); setCollectVal(String(a.apiType === "sub" ? (a.quotaPercent ?? 100) : a.remaining)); }}>采集</button>
                      <button className="text-xs text-slate-500 hover:underline mr-2" onClick={() => setEditing({ ...a, apiType: a.apiType === "sub" ? "sub" : "payg", note: a.note ?? "", validUntil: a.validUntil ?? "", resetCycle: (a.resetCycle ?? "") as "" | "weekly" | "monthly", resetDay: a.resetDay ?? null, subSince: a.subSince ?? null })}>编辑</button>
                      <button className="text-xs text-red-500 hover:underline" onClick={() => { if (confirm(`删除账号「${a.name}」?`)) remove.mutate({ id: a.id }); }}>删除</button>
                    </td>
                  </tr>
                  </Fragment>
                );
              })}
              {rows.length === 0 && <tr><td colSpan={7} className="py-10 text-center text-slate-400">暂无账号。点「🔑 添加API账号」输 Key 自动采集, 或「+ 手动录入」自己填余额</td></tr>}
            </tbody>
          </table>
        </div>

        {/* 手机端: 卡片列表(每账号一张卡, 不用横滑) */}
        <div className="md:hidden space-y-3">
          {rows.map((a) => {
            const pct = usedPercent(a);
            const st = STATUS_STYLE[a.status] ?? STATUS_STYLE.success;
            const pi = platformInfo(a.platform);
            return (
              <div key={a.id} data-platform={a.platform} data-account={a.name}
                className={`border border-slate-200 rounded-xl p-3.5 ${(highlight === a.name || highlight === a.platform) ? "bg-amber-50 outline outline-2 outline-amber-300" : "bg-white"}`}>
                {/* 卡头: 平台 + 类型 + 状态 */}
                <div className="flex items-center justify-between gap-2">
                  <span className="inline-flex items-center gap-2 min-w-0">
                    <span className="w-7 h-7 shrink-0 rounded-lg text-white text-[11px] font-bold flex items-center justify-center" style={{ background: pi.color }}>{pi.abbr}</span>
                    <span className="font-medium truncate">{a.name || pi.name}</span>
                    {a.dupIdentity && (
                      <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-sky-50 text-sky-700 border border-sky-200"
                        title="与另一账号身份一致(账户名称+电话), 累计充值只计一次">同号</span>
                    )}
                  </span>
                  <span className="flex items-center gap-1.5 shrink-0">
                    <span className={`inline-flex items-center gap-1 text-[11px] ${st.text}`}><span className={`w-1.5 h-1.5 rounded-full ${st.dot}`} />{a.statusText}</span>
                  </span>
                </div>
                {/* 平台名带形态(V3.30.3): 智谱 GLM 按量 vs 订阅一眼分清 */}
                <div className="text-[11px] text-slate-400 mt-0.5 truncate">{platformLabel(a)}</div>
                {/* 订阅账号: 重置详情已移出列表(到编辑弹窗/额度窗口看), 这里只留分组 */}
                {a.groupName && a.groupName !== "默认" && (
                  <div className="text-[10px] text-slate-400 mt-1">{a.groupName}</div>
                )}
                {/* 核心数字 2×2 (用内联网格: 手机上确保两列, 不依赖工具类级联) */}
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", columnGap: 12, rowGap: 8 }} className="mt-3">
                  <div><div className="text-[10px] text-slate-400">{a.apiType === "sub" ? "剩余价值(按天摊)" : "剩余额度"}</div><div className="text-sm font-semibold tabular-nums">{remainingText(a)}</div></div>
                  <div><div className="text-[10px] text-slate-400">本月消耗(自然月1号起)</div><div className="text-sm tabular-nums">{usedText(a)}</div></div>
                  <div><div className="text-[10px] text-slate-400">累计充值</div><div className="text-sm tabular-nums text-slate-600">{totalRechargeText(a)}</div></div>
                  <div><div className="text-[10px] text-slate-400">累计消耗</div><div className="text-sm font-medium tabular-nums">{totalUsedText(a)}</div></div>
                </div>
                {/* 用量进度 */}
                <div className="mt-2.5">
                  <div className="flex justify-between text-[11px] text-slate-500 mb-1">
                    <span>累计用量</span>
                    <span>{pct == null ? usedPercentText(a) : `${pct.toFixed(0)}% (${usedPercentText(a)})`}</span>
                  </div>
                  <div className="h-1.5 bg-slate-100 rounded-full overflow-hidden">
                    <div className={`h-full ${(pct ?? 0) >= 90 ? "bg-red-500" : (pct ?? 0) >= 70 ? "bg-amber-500" : "bg-emerald-500"}`}
                      style={{ width: `${Math.min(100, pct ?? 0)}%` }} />
                  </div>
                </div>
                {/* 卡尾: 操作 + 时间 */}
                <div className="flex items-center justify-between mt-3 pt-2.5 border-t border-slate-100">
                  <div className="flex gap-3">
                    <button className="text-xs text-[#1B6B7A]" onClick={() => { setCollecting(a); setCollectVal(String(a.apiType === "sub" ? (a.quotaPercent ?? 100) : a.remaining)); }}>采集</button>
                    <button className="text-xs text-slate-500" onClick={() => setEditing({ ...a, apiType: a.apiType === "sub" ? "sub" : "payg", note: a.note ?? "", validUntil: a.validUntil ?? "", resetCycle: (a.resetCycle ?? "") as "" | "weekly" | "monthly", resetDay: a.resetDay ?? null, subSince: a.subSince ?? null })}>编辑</button>
                    <button className="text-xs text-red-500" onClick={() => { if (confirm(`删除账号「${a.name}」?`)) remove.mutate({ id: a.id }); }}>删除</button>
                  </div>
                  <span className="text-[10px] text-slate-400">{a.lastCollectAt ? new Date(a.lastCollectAt).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "未采集"}</span>
                </div>
              </div>
            );
          })}
          {rows.length === 0 && <div className="py-10 text-center text-slate-400 text-sm">暂无账号。点上方「添加API账号」或「手动录入」</div>}
        </div>
        </>)}
      </div>

      {/* 添加 API 账号弹窗(Key 写入本地代理, 自动采集) */}
      {apiForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setApiForm(null)}>
          <div className="bg-white rounded-xl p-5 w-[480px] max-w-[calc(100vw-1.5rem)] max-h-[88vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            <div className="font-semibold mb-1">添加 API 账号</div>
            <div className="text-xs text-slate-400 mb-4">Key 只存你本机(混淆存储), 代理每 5 分钟自动查余额, 界面永不显示 Key</div>
            <div className="space-y-3 text-sm">
              <label className="block">平台
                <select className="mt-1 w-full border rounded-lg px-2 py-1.5" value={apiForm.platform}
                  onChange={(e) => {
                    const raw = e.target.value;           // 可能带 @sub 后缀(同平台两种形态)
                    const p = basePlatform(raw);
                    setApiForm({
                      ...apiForm, platform: raw,
                      apiType: (raw.endsWith("@sub") || SUB_ONLY.has(p)) ? "sub" : "payg",
                      // 换平台时把币种带回该平台默认值(用户仍可手动覆盖)
                      currency: USD_PLATFORMS.has(p) ? "USD" : "CNY",
                    });
                  }}>
                  {PLATFORM_GROUPS.map((g) => (
                    <optgroup key={g.id} label={`${g.label}  —  ${g.hint}`}>
                      {g.items.map(([id, n]) => <option key={id} value={id}>{n}</option>)}
                    </optgroup>
                  ))}
                </select>
                <span className="block text-[11px] text-slate-400 mt-1">
                  {PLATFORM_GROUPS.find((g) => g.items.some(([id]) => id === apiForm.platform))?.hint}
                  {AUTO_COLLECT.has(basePlatform(apiForm.platform))
                    ? " · 该平台填 Key 可自动查"
                    : " · 该平台官方无接口, 填 Key 后需手填一次余额"}
                </span>
                {MANUAL_BALANCE_HINT[basePlatform(apiForm.platform)] && (
                  <span className="block text-[11.5px] text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-2 mt-1.5 leading-relaxed">
                    <b>这个平台余额只能手动录：</b>{MANUAL_BALANCE_HINT[basePlatform(apiForm.platform)]}
                  </span>
                )}
                {USD_PLATFORMS.has(basePlatform(apiForm.platform)) && (
                  <span className="block text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2 py-1.5 mt-1.5 leading-relaxed">
                    ⚠️ 海外平台（OpenAI/Claude/Gemini 等）服务器在境外，能否直连取决于你的网络环境。
                    软件不提供网络代理——如访问不畅，请在下方「API 地址」填你的中转地址；能直接访问就留空。
                  </span>
                )}
              </label>
              <label className="block">账号名称 <span className="text-red-500">*</span>
                <input className="mt-1 w-full border rounded-lg px-2 py-1.5" value={apiForm.name}
                  onChange={(e) => setApiForm({ ...apiForm, name: e.target.value })}
                  placeholder="如: DeepSeek-写作系统" />
              </label>
              {/* 同号去重(V3.34.1): 两个选填字段都填且与另一账号完全一致时, 累计充值/总投入只计一次 */}
              <div className="grid grid-cols-2 gap-3">
                <label className="block">账户名称(选填)
                  <input className="mt-1 w-full border rounded-lg px-2 py-1.5" value={apiForm.customName}
                    onChange={(e) => setApiForm({ ...apiForm, customName: e.target.value })}
                    placeholder="你在该平台的自定义名称" />
                </label>
                <label className="block">电话(选填)
                  <input className="mt-1 w-full border rounded-lg px-2 py-1.5" value={apiForm.phone}
                    onChange={(e) => setApiForm({ ...apiForm, phone: e.target.value })}
                    placeholder="用于识别同一真实账户" />
                </label>
              </div>
              <div className="text-[11px] text-slate-400 leading-relaxed -mt-1">
                同平台填多个 Key 其实是同一个账户时：把这两栏填成一样的，「累计充值 / 总投入」就不会重复累加（只计一次）。
              </div>
              <label className="block">结算币种
                <select className="mt-1 w-full border rounded-lg px-2 py-1.5" value={apiForm.currency}
                  onChange={(e) => setApiForm({ ...apiForm, currency: e.target.value })}>
                  <option value="CNY">人民币 CNY</option>
                  <option value="USD">美元 USD</option>
                  <option value="EUR">欧元 EUR</option>
                  <option value="JPY">日元 JPY</option>
                  <option value="HKD">港币 HKD</option>
                  <option value="GBP">英镑 GBP</option>
                </select>
                <span className="block text-[11px] text-slate-400 mt-1">账号余额按这个币种存; 总览汇总按「显示货币」换算(关于系统里可切)</span>
              </label>
              <label className="block">API 地址(可选)
                <input className="mt-1 w-full border rounded-lg px-2 py-1.5" value={apiForm.baseUrl}
                  onChange={(e) => setApiForm({ ...apiForm, baseUrl: e.target.value })}
                  placeholder={USD_PLATFORMS.has(basePlatform(apiForm.platform)) ? "如 https://你的中转地址.com(留空用官方)" : "留空用平台默认地址"} />
                <span className="block text-[11px] text-slate-400 mt-1">
                  海外平台如访问不畅可填自己的中转/网关地址; 国内平台留空。ChatAnywhere 海外网络可填 https://api.chatanywhere.org
                </span>
              </label>
              {AKSK_PLATFORMS.has(basePlatform(apiForm.platform)) ? (
                <div className="block">
                  <span className="block mb-1">密钥(AccessKey 两段式, 分框填写不易错)</span>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    <label className="block">① Access Key ID (AK) <span className="text-red-500">*</span>
                      <input className="mt-1 w-full border rounded-lg px-2 py-1.5" value={apiForm.apiKeyAk}
                        onChange={(e) => setApiForm({ ...apiForm, apiKeyAk: e.target.value })}
                        placeholder={VOLC_PLATFORMS.has(basePlatform(apiForm.platform)) ? "AKLT 开头" : "LTAI 开头"} />
                    </label>
                    <label className="block">② Secret Access Key (SK) <span className="text-red-500">*</span>
                      <input type="password" className="mt-1 w-full border rounded-lg px-2 py-1.5" value={apiForm.apiKeySk}
                        onChange={(e) => setApiForm({ ...apiForm, apiKeySk: e.target.value })}
                        placeholder="对应的 Secret 密钥" />
                    </label>
                  </div>
                  <span className="block text-[11px] text-slate-400 mt-1">
                    {basePlatform(apiForm.platform) === "doubao"
                      ? "火山方舟 Agent Plan(订阅)必须用火山引擎 Access Key, 不是 ark- 开头的推理 Key。获取: 火山引擎控制台 → 右上角头像 → API 访问密钥 → 新建密钥; 左框填 AKLT 开头的 Access Key ID、右框填 Secret Access Key"
                      : basePlatform(apiForm.platform) === "arkpayg"
                      ? "火山方舟·按量(豆包 Doubao-Seed)查余额同样用火山引擎 Access Key, 不是 ark- 开头的推理 Key。获取: 火山引擎控制台 → 右上角头像 → API 访问密钥 → 新建密钥; 左框填 AKLT 开头的 Access Key ID、右框填 Secret Access Key。⚠️ 该密钥需在 IAM 授予「BillingCenterReadOnlyAccess(费用中心只读)」权限, 否则验签通过但查不到余额"
                      : "推荐填阿里云 AccessKey 自动查按量扣费的账户余额, 入口: 阿里云控制台 → 右上角头像 → AccessKey 管理; 左框填 AccessKey ID(LTAI 开头)、右框填 AccessKey Secret; 百炼免费额度官方无 API 可查"}
                  </span>
                </div>
              ) : (
                <label className="block">API Key <span className="text-red-500">*</span>
                  <input type="password" className="mt-1 w-full border rounded-lg px-2 py-1.5" value={apiForm.apiKey}
                    onChange={(e) => setApiForm({ ...apiForm, apiKey: e.target.value })}
                    placeholder="粘贴你的 Key, 保存后不再显示" />
                  <span className="block text-[11px] text-slate-400 mt-1">
                    {basePlatform(apiForm.platform) === "kimicode"
                      ? "Coding Plan Key 在 www.kimi.com/code/console 创建(sk-kimi-xxx), 与开放平台按量 Key 不通用"
                      : basePlatform(apiForm.platform) === "kimi"
                      ? "按量付费 Key 在 platform.kimi.com 创建; 若要监控 Coding Plan 订阅, 请选「Kimi Coding Plan(订阅)」入口"
                      : basePlatform(apiForm.platform) === "chatanywhere"
                      ? "ChatAnywhere 转发站 Key(sk-xxx): 官网控制台复制。系统自动验证 Key 有效性; 余额官方未开放 API 查询, 请到官网「余额查询」页看数额后在本页手动录入, 系统按两次录入差额自动算消耗"
                      : basePlatform(apiForm.platform) === "mistral"
                      ? "Mistral 控制台 → API Keys 创建。有官方 /v1/credit_balance 余额接口, 填 Key 即自动查余额(美元); 若该接入点/中转站没开这个接口, 系统自动退回「Key 已验证 + 手填余额」"
                      : basePlatform(apiForm.platform) === "claude"
                      ? "Claude 用 x-api-key 认证。普通 API Key 只能验证有效性(官方无余额接口, 余额请手填); 若填组织 Admin Key(sk-ant-admin 开头), 还能自动读本月成本报表"
                      : basePlatform(apiForm.platform) === "gemini"
                      ? "Google AI Studio Key(AIza 开头): 官方无余额接口, 系统只验证 Key 有效, 余额请手填; 若走 OpenAI 兼容中转, 在「API 地址」填中转地址即可"
                      : basePlatform(apiForm.platform) === "grok"
                      ? "xAI Grok: 官方无余额/用量接口, 系统探活验证 Key 后余额请手填, 按快照差值算消耗"
                      : basePlatform(apiForm.platform) === "openai"
                      ? "OpenAI: 官方已关闭公开余额接口, 系统探活验证 Key 后余额请手填; 若填 sk-admin- 开头的组织 Admin Key, 还能自动读本月成本(Costs API)"
                      : basePlatform(apiForm.platform) === "openrouter"
                      ? "OpenRouter: 填普通 Key 即可, 系统用 /api/v1/credits 自动查账户余额(美元); 给 Key 设了 credit limit 的账号也有兜底(读 key 限额)"
                      : basePlatform(apiForm.platform) === "glm"
                      ? "按量付费: 直接填 open.bigmodel.cn 的 API Key(系统按本月 token 用量折算费用; 智谱官方无余额接口, 请同时填累计充值以便推算余额)。若监控 Coding Plan 订阅, 请把上方类型切成「订阅配额」"
                      : basePlatform(apiForm.platform) === "minimax"
                      ? "按量付费: 填「sk-xxx GroupId」两者用空格分开(GroupId 在 MiniMax 控制台 → 账户管理 → 账户信息, 19 位数字)。若监控 Token Plan 订阅, 请把类型切成「订阅配额」"
                      : basePlatform(apiForm.platform) === "qwenplan"
                      ? "百炼 Token Plan 用量官方未开放 API Key 直连查询: 请在控制台「套餐用量」页查看后手动填写, 或改用「阿里百炼(通义)」填阿里云 AccessKey 查账户余额"
                      : "Key 只存本机, 界面不显示"}
                  </span>
                </label>
              )}
              <label className="block">账号类型
                <div className="mt-1 grid grid-cols-2 gap-2">
                  <button type="button" disabled={SUB_ONLY.has(basePlatform(apiForm.platform))}
                    onClick={() => setApiForm({ ...apiForm, apiType: "payg" })}
                    className={`border rounded-lg px-3 py-2 text-left ${apiForm.apiType === "payg" ? "border-[#1B6B7A] bg-[#1B6B7A]/5" : "hover:bg-slate-50"} disabled:opacity-40`}>
                    <div className="font-medium">按量付费</div>
                    <div className="text-[11px] text-slate-400 mt-0.5">跟踪余额(账号原币)</div>
                  </button>
                  <button type="button"
                    onClick={() => setApiForm({ ...apiForm, apiType: "sub" })}
                    className={`border rounded-lg px-3 py-2 text-left ${apiForm.apiType === "sub" ? "border-[#1B6B7A] bg-[#1B6B7A]/5" : "hover:bg-slate-50"}`}>
                    <div className="font-medium">订阅配额</div>
                    <div className="text-[11px] text-slate-400 mt-0.5">跟踪剩余配额(%)</div>
                  </button>
                </div>
              </label>
              {apiForm.apiType === "payg" && (
                <label className="block">累计充值(累计充进去多少钱) <span className="text-red-500">*</span>
                  <input type="number" className="mt-1 w-full border rounded-lg px-2 py-1.5"
                    value={apiForm.totalRecharge}
                    onChange={(e) => setApiForm({ ...apiForm, totalRecharge: e.target.value })}
                    placeholder={`这个账号历史上总共充值了多少 ${apiForm.currency}`} />
                  <span className="block text-[11px] text-amber-700 mt-1 leading-relaxed">
                    必填。累计消耗 = 累计充值 − 当前余额，总投入和「账目统计」都要靠它；
                    填 0 或留空会导致这个账号的消耗算不出来。忘了具体数额可去平台账单页查。
                  </span>
                </label>
              )}
              {apiForm.apiType === "sub" && (
                <label className="block">月费(元/月, 计入预算) <span className="text-red-500">*</span>
                  <input type="number" className="mt-1 w-full border rounded-lg px-2 py-1.5" value={apiForm.monthlyFee}
                    onChange={(e) => setApiForm({ ...apiForm, monthlyFee: Number(e.target.value) })} />
                  <span className="block text-[11px] text-slate-400 mt-1">订阅按「月费÷当月天数」每天摊销，填真实月费即可</span>
                </label>
              )}
              {(SUB_ONLY.has(basePlatform(apiForm.platform)) || apiForm.platform.endsWith("@sub")) && (
                <div className="text-xs text-slate-400">该平台只有订阅配额形态(如 Coding Plan), 已自动切换</div>
              )}
            </div>
            <div className="flex items-center justify-between gap-2 mt-5 flex-wrap">
              <div className="text-[12px]">
                {apiFormMissing.length > 0
                  ? <span className="text-red-600">还差：{apiFormMissing.join("、")}</span>
                  : <span className="text-emerald-600">必填项已齐全 ✓</span>}
              </div>
              <div className="flex gap-2">
                <button className="px-4 py-2 text-sm rounded-lg border" onClick={() => setApiForm(null)}>取消</button>
                <button className="px-4 py-2 text-sm rounded-lg bg-[#1B6B7A] text-white disabled:opacity-50"
                  disabled={apiBusy || apiFormMissing.length > 0}
                  onClick={addApiAccount}>
                  {apiBusy ? "写入中..." : "保存并开始监测"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 编辑弹窗 */}
      {editing && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setEditing(null)}>
          <div className="bg-white rounded-xl p-5 w-[520px] max-w-[calc(100vw-1.5rem)] max-h-[88vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            <div className="font-semibold mb-4">{editing.id ? "编辑账号" : "添加账号"}</div>
            <div className="grid grid-cols-2 gap-3 text-sm">
              <label className="col-span-1">平台
                <select className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.platform}
                  onChange={(e) => {
                    const p = e.target.value;
                    setEditing(ONLINE_SUBS.has(p)
                      ? { ...editing, platform: p, apiType: "sub", currency: "USD" }
                      : { ...editing, platform: p });
                  }}>
                  {Object.entries(PLATFORMS).map(([id, p]) => <option key={id} value={id}>{p.name}</option>)}
                </select>
              </label>
              <label>名称<input className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} placeholder="如: DeepSeek 主账号" /></label>
              {/* 同号去重(V3.34.1): 选填; 两个字段都填且与另一账号完全一致时, 累计充值/总投入只计一次 */}
              <label>账户名称(选填)
                <input className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.customName ?? ""} onChange={(e) => setEditing({ ...editing, customName: e.target.value })} placeholder="你在该平台的自定义名称" />
              </label>
              <label>电话(选填)
                <input className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.phone ?? ""} onChange={(e) => setEditing({ ...editing, phone: e.target.value })} placeholder="用于识别同一真实账户" />
              </label>
              <div className="col-span-2 text-[11px] text-slate-400 leading-relaxed">
                同平台多个 Key 属于同一真实账户时，两栏填成一致即自动去重：「累计充值 / 总投入」只计一次，列表会显示「同号」标记。
              </div>
              <label>币种
                <select className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.currency || "CNY"}
                  onChange={(e) => setEditing({ ...editing, currency: e.target.value })}>
                  <option value="CNY">人民币 CNY</option>
                  <option value="USD">美元 USD</option>
                  <option value="EUR">欧元 EUR</option>
                  <option value="JPY">日元 JPY</option>
                  <option value="HKD">港币 HKD</option>
                  <option value="GBP">英镑 GBP</option>
                </select>
              </label>
              <label className="col-span-2">API 类型
                <div className="mt-1 grid grid-cols-2 gap-2">
                  {(Object.entries(API_TYPES) as [("payg" | "sub"), (typeof API_TYPES)["payg"]][]).map(([id, t]) => (
                    <button key={id} type="button"
                      disabled={ONLINE_SUBS.has(editing.platform) && id === "payg"}
                      onClick={() => setEditing({ ...editing, apiType: id })}
                      className={`border rounded-lg px-3 py-2 text-left ${editing.apiType === id ? "border-[#1B6B7A] bg-[#1B6B7A]/5" : "hover:bg-slate-50"} disabled:opacity-40 disabled:cursor-not-allowed`}>
                      <div className="font-medium">{t.label}</div>
                      <div className="text-[11px] text-slate-400 mt-0.5">
                        {id === "payg" ? "充值余额, 按 token 用量扣费 (DeepSeek / Kimi 开放平台等)" : "包月配额, 按 5h/周/月窗口重置 (Coding Plan / Kimi Code 等)"}
                      </div>
                    </button>
                  ))}
                </div>
              </label>
              {editing.apiType === "payg" ? (
                <>
                  <label>剩余余额(账号原币)<input type="number" className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.remaining} onChange={(e) => setEditing({ ...editing, remaining: Number(e.target.value) })} /></label>
                  <label>本月已消耗(账号原币)<input type="number" className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.monthlyUsed} onChange={(e) => setEditing({ ...editing, monthlyUsed: Number(e.target.value) })} /></label>
                  <label>累计充值(账号原币)<input type="number" className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.totalRecharge} onChange={(e) => setEditing({ ...editing, totalRecharge: Number(e.target.value) })} /></label>
                </>
              ) : (
                <>
                  <label>剩余配额(%)<input type="number" min={0} max={100} className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.quotaPercent ?? ""} onChange={(e) => setEditing({ ...editing, quotaPercent: e.target.value === "" ? null : Number(e.target.value) })} placeholder="如: 62" /></label>
                  <label>套餐月费(账号原币/月)
                    <input type="number" className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.monthlyFee} onChange={(e) => setEditing({ ...editing, monthlyFee: Number(e.target.value) })} placeholder="如: 149" />
                    <span className="block text-[10px] text-slate-400 mt-0.5">月费固定, 按天摊进总成本与账目(与配额窗口无关); 免费档填 0</span>
                    {ONLINE_SUBS.has(editing.platform) && (
                      <span className="mt-1 flex flex-wrap gap-1">
                        {(ONLINE_FEE_PRESETS[editing.platform] || []).map((fee) => (
                          <button key={fee} type="button"
                            onClick={() => setEditing({ ...editing, monthlyFee: fee, currency: "USD" })}
                            className={`text-[11px] px-2 py-0.5 rounded border ${editing.monthlyFee === fee ? "border-[#1B6B7A] text-[#1B6B7A] bg-[#1B6B7A]/5" : "border-slate-300 text-slate-500"}`}>
                            ${fee}/月
                          </button>
                        ))}
                      </span>
                    )}
                  </label>
                  {ONLINE_SUBS.has(editing.platform) && (
                    <>
                      <div className="col-span-2 text-[11px] text-slate-500 bg-slate-50 rounded-lg px-3 py-2 leading-relaxed">
                        在线订阅没有官方额度接口（额度是前端动态流控）。系统按月费按天摊销算成本；
                        下面的 5 小时 / 7 天剩余额度是「手动录入」的参考值，只进「订阅额度窗口」面板，不参与金额计算。
                      </div>
                      <label>5 小时窗口剩余(%)
                        <input type="number" min={0} max={100} className="mt-1 w-full border rounded-lg px-2 py-1.5"
                          value={editing.win5h ?? ""} placeholder="留空=不显示"
                          onChange={(e) => setEditing({ ...editing, win5h: e.target.value === "" ? null : Number(e.target.value) })} />
                      </label>
                      <label>7 天窗口剩余(%)
                        <input type="number" min={0} max={100} className="mt-1 w-full border rounded-lg px-2 py-1.5"
                          value={editing.win7d ?? ""} placeholder="留空=不显示"
                          onChange={(e) => setEditing({ ...editing, win7d: e.target.value === "" ? null : Number(e.target.value) })} />
                      </label>
                    </>
                  )}
                  <label>配额重置/到期日<input type="date" className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.validUntil} onChange={(e) => setEditing({ ...editing, validUntil: e.target.value })} /></label>
                  <label>订阅开始日
                    <input type="date" className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.subSince ?? ""} onChange={(e) => setEditing({ ...editing, subSince: e.target.value || null })} />
                    <span className="block text-[10px] text-slate-400 mt-0.5">用于算「累计消耗」的已计费月数; 留空则按最早采集记录估算</span>
                  </label>
                  <div className="col-span-2">
                    <label className="block">配额重置周期
                      <select className="mt-1 w-full border rounded-lg px-2 py-1.5"
                        value={editing.resetCycle || ""}
                        onChange={(e) => setEditing({ ...editing, resetCycle: e.target.value as "" | "weekly" | "monthly", resetDay: e.target.value ? (editing.resetDay ?? (e.target.value === "weekly" ? 1 : 1)) : null })}>
                        <option value="">自动识别(看快照里配额何时回升)</option>
                        <option value="weekly">每周重置</option>
                        <option value="monthly">每月重置</option>
                      </select>
                    </label>
                    {editing.resetCycle === "weekly" && (
                      <label className="block mt-2">每周几重置
                        <select className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.resetDay ?? 1}
                          onChange={(e) => setEditing({ ...editing, resetDay: Number(e.target.value) })}>
                          {["一", "二", "三", "四", "五", "六", "日"].map((w, i) => <option key={i} value={i}>每周{w}</option>)}
                        </select>
                      </label>
                    )}
                    {editing.resetCycle === "monthly" && (
                      <label className="block mt-2">每月几号重置
                        <input type="number" min={1} max={31} className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.resetDay ?? 1}
                          onChange={(e) => setEditing({ ...editing, resetDay: Number(e.target.value) })} />
                      </label>
                    )}
                    <div className="text-[10px] text-slate-400 mt-1">
                      用于账目统计的摊销口径: 每周重置→每周摊销 月费/4; 每月重置→按当月实际在订天数折算。留空则系统根据配额快照自动识别。
                    </div>
                  </div>
                  <div className="col-span-2 text-[11px] text-slate-500 bg-slate-50 rounded-lg px-3 py-2 leading-relaxed">
                    当前配额 {editing.quotaPercent == null ? "未录入" : `剩 ${editing.quotaPercent}%`}
                    {editing.quotaPercent != null && editing.monthlyFee > 0 && (
                      <> · 已用 {100 - editing.quotaPercent}% → 本月成本约 <b>{fmtMoney(editing.monthlyFee * (100 - editing.quotaPercent) / 100)}</b></>
                    )}
                    {editing.quotaPercent != null && !editing.monthlyFee && <> · 未填月费按免费额度计, 成本 0(仅看配额进度)</>}
                  </div>
                </>
              )}
              <label>月预算(账号原币)<input type="number" className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.budget} onChange={(e) => setEditing({ ...editing, budget: Number(e.target.value) })} /></label>
              <label>采集方式
                <select className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.collectMethod} onChange={(e) => setEditing({ ...editing, collectMethod: e.target.value })}>
                  {Object.keys(METHOD_COLORS).map((m) => <option key={m}>{m}</option>)}
                </select>
              </label>
              <label>分组<input className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.groupName} onChange={(e) => setEditing({ ...editing, groupName: e.target.value })} /></label>
              <label className="col-span-2">备注<input className="mt-1 w-full border rounded-lg px-2 py-1.5" value={editing.note ?? ""} onChange={(e) => setEditing({ ...editing, note: e.target.value })} placeholder={editing.apiType === "sub" ? "如: GLM Coding Plan Pro, 每 5 小时重置" : "如: 按 token 计费, 欠费即停"} /></label>
            </div>
            <div className="flex justify-end gap-2 mt-5">
              <button className="px-4 py-2 text-sm rounded-lg border" onClick={() => setEditing(null)}>取消</button>
              <button className="px-4 py-2 text-sm rounded-lg bg-[#1B6B7A] text-white disabled:opacity-50" disabled={!editing.name || upsert.isPending}
                onClick={() => upsert.mutate({
                  ...editing,
                  note: editing.note || null,
                  validUntil: editing.validUntil || null,
                  quotaPercent: editing.apiType === "sub" ? editing.quotaPercent : null,
                  resetCycle: editing.apiType === "sub" && editing.resetCycle ? editing.resetCycle : null,
                  resetDay: editing.apiType === "sub" && editing.resetCycle ? editing.resetDay : null,
                  subSince: editing.apiType === "sub" && editing.subSince ? editing.subSince : null,
                  // 手动额度窗口只对在线订阅有意义, 其它平台清空避免脏数据
                  win5h: ONLINE_SUBS.has(editing.platform) ? editing.win5h : null,
                  win7d: ONLINE_SUBS.has(editing.platform) ? editing.win7d : null,
                })}>
                {upsert.isPending ? "保存中..." : "保存到数据库"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 采集弹窗 */}
      {collecting && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setCollecting(null)}>
          <div className="bg-white rounded-xl p-6 w-[400px]" onClick={(e) => e.stopPropagation()}>
            <div className="font-semibold mb-1">采集「{collecting.name}」</div>
            <div className="text-xs text-slate-400 mb-4">
              {collecting.apiType === "sub"
                ? "录入当前剩余配额百分比(0-100), 系统会记录一条快照用于趋势计算"
                : "录入平台当前实时余额(账号原币), 系统会记录一条快照用于趋势计算"}
            </div>
            {/* 手记账号: 提示去哪里看余额 + 上次记录值, 方便对照填写 */}
            {collecting.apiType !== "sub" && !collecting.live && (
              <div className="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-2 mb-3 leading-relaxed">
                该平台官方未开放自动余额接口, 请到控制台「余额」页查看后填入。
                {collecting.note && <><br />{collecting.note.split("；")[0]}</>}
              </div>
            )}
            {collecting.apiType !== "sub" && (
              <div className="text-[11px] text-slate-400 mb-2">
                上次记录: {collecting.remaining ? `${currencySymbol()}${collecting.remaining.toFixed(2)}` : "尚无记录"}
                {collecting.lastCollectAt ? `（${new Date(collecting.lastCollectAt).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}）` : ""}
                {collecting.remaining ? "，两次之差即这段时间的消耗" : ""}
              </div>
            )}
            <input type="number" className="w-full border rounded-lg px-3 py-2" value={collectVal} onChange={(e) => setCollectVal(e.target.value)} />
            <div className="flex justify-end gap-2 mt-4">
              <button className="px-4 py-2 text-sm rounded-lg border" onClick={() => setCollecting(null)}>取消</button>
              <button className="px-4 py-2 text-sm rounded-lg bg-[#1B6B7A] text-white disabled:opacity-50" disabled={collect.isPending}
                onClick={() => collect.mutate({ id: collecting.id, value: Number(collectVal) })}>
                确认采集
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ============================== TOKEN 用量(V3.34.1) ==============================
type TokenUsageItem = {
  id: number; platform: string; name: string; customName?: string;
  hasApi: boolean; tokens?: number | null; windowLabel: string;
  models: { name: string; tokens: number }[];
  points: { ts: number; tokens: number }[];
  updatedAt: string;
};

function fmtTokens(v: number): string {
  if (v >= 1e8) return (v / 1e8).toFixed(2) + " 亿";
  if (v >= 1e4) return (v / 1e4).toFixed(1) + " 万";
  return String(Math.round(v));
}

function TokenUsagePanel() {
  const q = trpc.quota.tokenUsage.useQuery(undefined, { refetchInterval: 30000 });
  const items = (q.data?.items ?? []) as TokenUsageItem[];
  if (q.isLoading) return <div className="py-10 text-center text-slate-400 text-sm">正在读取 TOKEN 用量…</div>;
  if (q.isError) return <div className="py-10 text-center text-red-500 text-sm">读取失败：{q.error.message}</div>;
  return (
    <div className="space-y-3">
      <div className="text-[12px] text-slate-400 leading-relaxed">
        只展示「官方开放了用量接口」的平台自动采集的真实 token 用量（随余额同步自动刷新，无需手填）；
        官方没开放接口的平台如实标注，不做估算、不编数字。
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {items.map((a) => <TokenUsageCard key={a.id} a={a} />)}
        {!items.length && <div className="col-span-full py-10 text-center text-slate-400 text-sm">暂无账号</div>}
      </div>
    </div>
  );
}

function TokenUsageCard({ a }: { a: TokenUsageItem }) {
  const pi = platformInfo(a.platform);
  const models = a.models ?? [];
  const modelMax = Math.max(1, ...models.map((m) => m.tokens));
  const pts = a.points ?? [];
  const ptMax = Math.max(1, ...pts.map((p) => p.tokens));
  return (
    <div className="border border-slate-200 rounded-xl p-4 bg-white">
      <div className="flex items-center justify-between gap-2 mb-2">
        <span className="inline-flex items-center gap-2 min-w-0">
          <span className="w-7 h-7 shrink-0 rounded-lg text-white text-[11px] font-bold flex items-center justify-center" style={{ background: pi.color }}>{pi.abbr}</span>
          <span className="min-w-0">
            <span className="font-medium truncate block">{a.name}</span>
            <span className="text-[11px] text-slate-400 truncate block">
              {pi.name}{a.customName ? ` · ${a.customName}` : ""}
            </span>
          </span>
        </span>
        {a.hasApi
          ? <span className="shrink-0 text-[10px] px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200">官方接口自动采集</span>
          : <span className="shrink-0 text-[10px] px-2 py-0.5 rounded-full bg-slate-100 text-slate-500 border border-slate-200">官方未开放接口</span>}
      </div>

      {!a.hasApi && (
        <div className="text-[12px] text-slate-400 leading-relaxed bg-slate-50 rounded-lg px-3 py-2">
          该平台官方没有开放 token 用量查询接口，任何软件都拿不到。
          可以跟踪的是余额变化：请看「账号列表」里的消耗与趋势。
        </div>
      )}

      {a.hasApi && (a.tokens == null) && (
        <div className="text-[12px] text-slate-400 bg-slate-50 rounded-lg px-3 py-2">
          支持自动采集，等第一次同步完成即出现数据（通常与余额刷新同周期）。
        </div>
      )}

      {a.hasApi && a.tokens != null && (
        <>
          <div className="flex items-baseline gap-2">
            <span className="text-2xl font-bold tabular-nums">{fmtTokens(a.tokens)}</span>
            <span className="text-[12px] text-slate-400">tokens · {a.windowLabel}
              {a.updatedAt ? ` · 更新于 ${a.updatedAt}` : ""}</span>
          </div>

          {models.length > 0 && (
            <div className="mt-2.5 space-y-1">
              {models.slice(0, 6).map((m) => (
                <div key={m.name} className="flex items-center gap-2 text-[12px]">
                  <span className="w-28 shrink-0 truncate text-slate-500" title={m.name}>{m.name}</span>
                  <span className="flex-1 h-2 rounded bg-slate-100 overflow-hidden">
                    <span className="block h-full rounded" style={{ width: `${Math.max(2, (m.tokens / modelMax) * 100)}%`, background: pi.color }} />
                  </span>
                  <span className="shrink-0 tabular-nums text-slate-500">{fmtTokens(m.tokens)}</span>
                </div>
              ))}
              {models.length > 6 && <div className="text-[11px] text-slate-400">… 共 {models.length} 个模型</div>}
            </div>
          )}

          {pts.length >= 2 && (
            <div className="mt-3">
              <div className="text-[11px] text-slate-400 mb-1">采集走势（{a.windowLabel}，近 {pts.length} 次）</div>
              <div className="flex items-end gap-[2px] h-8">
                {pts.map((p, i) => (
                  <span key={i} className="flex-1 rounded-t" title={fmtTokens(p.tokens)}
                    style={{ height: `${Math.max(6, (p.tokens / ptMax) * 100)}%`, background: pi.color, opacity: 0.35 + 0.65 * (i / Math.max(1, pts.length - 1)) }} />
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

