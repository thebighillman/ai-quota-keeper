import type { Account } from "@db/schema";

/** 单机版 proc_list 在账号上附加的计算口径字段(不写入数据库) */
export type AccountView = Account & {
  monthUsedReal?: number;      // 本月真实消耗(元): 按量=自然月1号起快照下降累加; 订阅=本月摊销
  totalUsed?: number;          // 累计消耗(元): 按量=累计充值-余额; 订阅=已计费周期数×周期费−剩余%×周期费
  totalRechargeShow?: number;  // 累计充值展示(元): 按量=手填值; 订阅=月费*12
  resetCycle?: "weekly" | "monthly" | null;  // 订阅配额重置周期
  resetDay?: number | null;    // 周=星期几(周一0) / 月=几号(1-31)
  resetAuto?: boolean;         // 重置周期是自动识别(true)还是手动设置(false)
  resetLabel?: string;         // 重置周期可读文案
  nextReset?: string;          // 下次重置日期(MM-DD)
  billedPeriods?: number;      // 订阅已计费周期数(含当前周期)
  subSince?: string | null;    // 订阅开始日(YYYY-MM-DD), 空=按最早快照估算
  monthUsedFee?: number;       // 本月消耗金额(元)
  remainingMoney?: number;     // 当前余额(元): 按量=余额; 订阅=已付−按天已摊
  subCycleKind?: "weekly" | "monthly" | "flat" | "interval5h";  // 配额重置口径(仅展示标注): weekly=周额度 monthly=月度额度 interval5h=月付5小时窗(MiniMax)
  subQuotaTag?: string;             // 成本栏标注: 周 / 月
  subCycleLabel?: string;      // 口径说明(展示用)
  currency?: string;           // 账号原币(V3.19.0): CNY/USD/EUR... 金额字段都是原币
  fxRate?: number;             // 原币→显示货币 的换算系数(后端按当前显示币算好)
  customName?: string;         // 同号去重(V3.34.1): 用户在该平台的自定义名称(选填)
  phone?: string;              // 同号去重(V3.34.1): 联系电话(选填, 与账户名称一起判定)
  dupIdentity?: boolean;       // 同号去重(V3.34.1): 存在另一账号身份完全相同(累计充值只计一次)
};

export const PLATFORMS: Record<string, { name: string; color: string; abbr: string }> = {
  deepseek: { name: "DeepSeek", color: "#6B5CE7", abbr: "DS" },
  kimi: { name: "Kimi 开放平台", color: "#1B6B7A", abbr: "KM" },
  kimicode: { name: "Kimi Coding Plan", color: "#0F766E", abbr: "KC" },
  glm: { name: "智谱 GLM", color: "#2F6BFF", abbr: "GLM" },
  minimax: { name: "MiniMax", color: "#8B5CF6", abbr: "MM" },
  doubao: { name: "火山方舟 Agent Plan", color: "#3B82F6", abbr: "DB" },
  siliconflow: { name: "SiliconFlow", color: "#059669", abbr: "SF" },
  openai: { name: "OpenAI", color: "#10A37F", abbr: "OA" },
  stepfun: { name: "阶跃星辰 StepFun", color: "#0EA5E9", abbr: "ST" },
  openrouter: { name: "OpenRouter", color: "#6366F1", abbr: "OR" },
  mistral: { name: "Mistral AI", color: "#FF7000", abbr: "MS" },
  qwen: { name: "阿里百炼(通义)", color: "#FF6A00", abbr: "QW" },
  qwenplan: { name: "百炼 Token Plan", color: "#C2410C", abbr: "TP" },
  chatanywhere: { name: "ChatAnywhere(转发)", color: "#2563EB", abbr: "CA" },
  chatgpt: { name: "ChatGPT 在线订阅", color: "#10A37F", abbr: "GP" },
  claude_sub: { name: "Claude 在线订阅", color: "#D97757", abbr: "CP" },
  gemini_sub: { name: "Gemini 在线订阅", color: "#4285F4", abbr: "GS" },
  wenxin: { name: "文心一言(千帆)", color: "#2932E1", abbr: "WX" },
  xunfei: { name: "讯飞星火", color: "#E60012", abbr: "XF" },
  hunyuan: { name: "腾讯混元", color: "#00C8BE", abbr: "HY" },
  claude: { name: "Claude(Anthropic)", color: "#D97757", abbr: "CL" },
  gemini: { name: "Gemini(Google)", color: "#4285F4", abbr: "GM" },
  grok: { name: "Grok(xAI)", color: "#000000", abbr: "GK" },
  lingyi: { name: "零一万物", color: "#7C3AED", abbr: "01" },
  baichuan: { name: "百川智能", color: "#F59E0B", abbr: "BC" },
  kling: { name: "可灵 Kling", color: "#E8792D", abbr: "KL" },
  jimeng: { name: "即梦 Dreamina", color: "#0B8A6E", abbr: "JM" },
  custom: { name: "自定义平台", color: "#64748B", abbr: "CU" },
};

export const platformInfo = (id: string) =>
  PLATFORMS[id] ?? { name: id, color: "#64748B", abbr: id.slice(0, 2).toUpperCase() };

export const CURRENCY_SYMBOLS: Record<string, string> = {
  CNY: "¥", USD: "$", EUR: "€", JPY: "JP¥", HKD: "HK$", GBP: "£",
};

/** 当前「显示货币」(App 启动时从后端拉取并写入) —— 只影响汇总类金额,
 *  账号行有自己的原币(a.currency), 二者互不干扰。 */
let DISPLAY_CURRENCY = "CNY";
export const setDisplayCurrency = (c?: string) => {
  DISPLAY_CURRENCY = c && CURRENCY_SYMBOLS[c] ? c : "CNY";
};
export const getDisplayCurrency = () => DISPLAY_CURRENCY;

/** 当前显示货币的符号（V3.30.0）: 所有需要手拼金额的地方都走这个, 保证符号跟随「显示货币」统一 */
export const currencySymbol = (cur: string = DISPLAY_CURRENCY) =>
  CURRENCY_SYMBOLS[cur] ?? `${cur} `;

/** 金额格式化(V3.19.0 多币种): 不传币种时用「显示货币」。
 *  原则: 存储永远原币, 换算只在展示层 —— 这里不做换算, 只换符号, 避免双重折算。 */
export const fmtMoney = (v: number, cur: string = DISPLAY_CURRENCY) =>
  (CURRENCY_SYMBOLS[cur] ?? `${cur} `) +
  Number(v || 0).toLocaleString("zh-CN", { maximumFractionDigits: Math.abs(v) >= 100 ? 0 : 2 });

/** 账号原币金额 */
export const fmtMoneyIn = (v: number | null | undefined, cur?: string) =>
  fmtMoney(Number(v || 0), cur || "CNY");

/** 原币 + 换算小字: 「$12.34(≈¥87.61)」; 同币种或没有汇率时只显示原币 */
export const fmtMoneyBoth = (v: number | null | undefined, cur?: string, fx?: number) => {
  const c = cur || "CNY";
  const base = fmtMoneyIn(v, c);
  if (c === "CNY" || !fx || Math.abs(fx - 1) < 1e-9) return base;
  return `${base}(≈${fmtMoney(Number(v || 0) * fx)})`;
};

/** API 类型: payg=按量付费API(余额制) / sub=订阅制API(配额制) */
export const API_TYPES: Record<string, { label: string; short: string; color: string }> = {
  payg: { label: "按量付费 API", short: "按量", color: "bg-green-100 text-green-700" },
  sub: { label: "订阅制 API", short: "订阅", color: "bg-purple-100 text-purple-700" },
};

type QuotaView = Pick<Account, "apiType" | "remaining" | "quotaPercent" | "monthlyUsed" | "monthlyFee" | "totalRecharge">;

/** 带计算字段的视图(单机版附加) */
type AnyView = QuotaView & Partial<AccountView>;

/** 剩余额度展示: 按量=¥余额 / 订阅=按天摊销后的剩余价值(元)
 *  V3.17.37: 订阅金额只跟时间走(月费固定按天摊), 不再绑定配额%;
 *  配额%(5小时/7天/月度)只在「当前总览 → 订阅额度窗口」面板展示, 不进账号列表。 */
export const remainingText = (a: AnyView) => {
  if (a.apiType === "sub") {
    return fmtMoneyIn(a.remainingMoney ?? 0, a.currency);
  }
  // 官方无余额接口的平台(如智谱按量)如实显示「—」, 不用 ¥0 骗人
  if ((a.note || "").includes("余额未知")) return "—";
  return fmtMoneyIn(a.remaining, a.currency);
};

/** 累计充值展示: 订阅=年付月费×12 / 月付已计费月数×月费; 按量=手填累计充值 */
export const totalRechargeText = (a: AnyView) =>
  fmtMoneyIn(a.totalRechargeShow ?? a.totalRecharge, a.currency);

/** 累计消耗展示: 按量=累计充值−余额; 订阅=月费按天摊销累计 */
export const totalUsedText = (a: AnyView) => fmtMoneyIn(a.totalUsed ?? 0, a.currency);

/**
 * 订阅账号本月成本(元) —— 按天摊销: 本月已摊金额(月费×本月在订天数/当月天数)。
 * V3.17.37 起不再用配额%折算。
 */
export const subMonthCost = (a: AnyView): number => {
  if (a.monthUsedFee != null) return a.monthUsedFee;
  if (a.monthUsedReal != null) return a.monthUsedReal;
  return 0;
};

/**
 * 本月消耗展示(自然月1号以来)。
 * 订阅 = 本月已摊金额(月费按天摊销, 与账目统计同源); 不再展示配额%。
 */
export const usedText = (a: AnyView) => {
  if (a.apiType === "sub") {
    const cost = a.monthUsedFee ?? a.monthUsedReal ?? subMonthCost(a);
    if (!(a.monthlyFee > 0)) return "免费额度";
    return `本月 ${fmtMoneyIn(cost, a.currency)}(按天摊)`;
  }
  return fmtMoneyIn(a.monthUsedReal != null ? a.monthUsedReal : a.monthlyUsed, a.currency);
};

/** 用量进度(已用百分比 0-100): 按量=累计消耗/累计充值; 订阅=已付时间占比(累计消耗/累计充值) */
export const usedPercent = (a: AnyView): number | null => {
  const tot = a.totalRechargeShow ?? a.totalRecharge;
  const used = a.totalUsed != null
    ? a.totalUsed
    : Math.max(0, (a.totalRecharge ?? 0) - (a.remaining ?? 0));
  return tot > 0 ? Math.min(100, (used / tot) * 100) : null;
};

/** 用量进度说明文字: 按量=累计消耗/累计充值; 订阅=已摊金额/已付金额 */
export const usedPercentText = (a: AnyView): string => {
  const tot = a.totalRechargeShow ?? a.totalRecharge;
  const used = a.totalUsed != null
    ? a.totalUsed
    : Math.max(0, (a.totalRecharge ?? 0) - (a.remaining ?? 0));
  return tot > 0
    ? `累计 ${fmtMoneyIn(used, a.currency)} / ${fmtMoneyIn(tot, a.currency)}`
    : "未设累计充值";
};

/** 订阅账号的成本展示(用于表格/图表 tooltip) */
export const subCostText = (a: AnyView): string => {
  const cost = subMonthCost(a);
  return a.monthlyFee > 0
    ? `本月已摊 ${fmtMoneyIn(cost, a.currency)} / 月费 ${fmtMoneyIn(a.monthlyFee, a.currency)}`
    : "免费额度(单价 0)";
};

export const METHOD_COLORS: Record<string, string> = {
  API直连: "bg-green-100 text-green-700",
  手动录入: "bg-gray-100 text-gray-600",
  官方未开放: "bg-amber-100 text-amber-700",
};

export const STATUS_STYLE: Record<string, { dot: string; text: string }> = {
  success: { dot: "bg-emerald-500", text: "text-emerald-600" },
  warning: { dot: "bg-amber-500", text: "text-amber-600" },
  danger: { dot: "bg-red-500", text: "text-red-600" },
};


/** 折线图「多线区分」样式: 每条线用不同形状标记 + 实线/虚线交替 + 不同粗细,
 *  避免同色系或堆叠时看不出是哪条模型线。i = 该平台在列表中的序号。 */
const LINE_SYMBOLS = ["circle", "rect", "triangle", "diamond", "roundRect", "pin"] as const;
export const lineDistinct = (i: number) => ({
  symbol: LINE_SYMBOLS[i % LINE_SYMBOLS.length],
  symbolSize: 7,
  showSymbol: true,
  lineStyle: { width: 2, type: (i % 2 === 0 ? "solid" : "dashed") as "solid" | "dashed" },
  emphasis: { focus: "series" as const },
});
