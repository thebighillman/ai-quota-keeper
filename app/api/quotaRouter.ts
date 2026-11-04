import { z } from "zod";
import { createRouter, publicQuery } from "./middleware";
import { getDb } from "./queries/connection";
import { accounts, snapshots, settings } from "@db/schema";
import { eq, and, gte } from "drizzle-orm";

// 账号模型:
//  apiType="payg" 按量付费API: remaining=余额(元), monthlyUsed=本月消耗(元), totalRecharge=累计充值(元)
//  apiType="sub"  订阅制API:   quotaPercent=剩余配额%, monthlyFee=月费(元,计入本月消耗与预算), validUntil=重置/到期日
// 快照 snapshots.remaining 双义: payg=余额(元) / sub=剩余配额%

type StatusInput = {
  apiType: string;
  remaining: number;
  quotaPercent: number | null;
  monthlyUsed: number;
  monthlyFee: number;
  totalRecharge: number;
  validUntil?: string | null;
};

function deriveStatus(a: StatusInput) {
  if (a.apiType === "sub") {
    // 订阅制: 看剩余配额 % 与到期日
    if (a.validUntil) {
      const exp = new Date(a.validUntil);
      if (!Number.isNaN(exp.getTime()) && exp.getTime() < Date.now()) {
        return { status: "danger", statusText: "订阅已到期" };
      }
    }
    const qp = a.quotaPercent;
    if (qp == null) return { status: "success", statusText: "正常" };
    if (qp <= 10) return { status: "danger", statusText: "配额即将用尽" };
    if (qp <= 30) return { status: "warning", statusText: "配额消耗偏快" };
    return { status: "success", statusText: "正常" };
  }
  // 按量付费: 看余额占累计充值比例与本月消耗
  const pct = a.totalRecharge > 0 ? (a.monthlyUsed / a.totalRecharge) * 100 : 0;
  const low = a.totalRecharge > 0 && a.remaining <= a.totalRecharge * 0.1;
  if (a.remaining <= 0 && a.totalRecharge > 0) return { status: "danger", statusText: "余额已用尽" };
  if (low || pct >= 90) return { status: "danger", statusText: "余额不足" };
  if (pct >= 70) return { status: "warning", statusText: "消耗偏快" };
  return { status: "success", statusText: "正常" };
}

const accountInput = z.object({
  id: z.number().optional(),
  platform: z.string().min(1),
  name: z.string().min(1),
  groupName: z.string().default("默认"),
  apiType: z.enum(["payg", "sub"]).default("payg"),
  remaining: z.number().default(0),
  quotaPercent: z.number().min(0).max(100).nullish(),
  monthlyUsed: z.number().default(0),
  monthlyFee: z.number().default(0),
  totalRecharge: z.number().default(0),
  budget: z.number().default(0),
  collectMethod: z.string().default("手动录入"),
  note: z.string().nullish(),
  validUntil: z.string().nullish(),
  // 订阅配额重置周期(仅 sub): weekly=每周 / monthly=每月; resetDay: 周=0-6(周一0) 月=1-31
  resetCycle: z.enum(["weekly", "monthly"]).nullish(),
  resetDay: z.number().int().nullish(),
  // 订阅开始日(仅 sub, YYYY-MM-DD): 算「已计费周期数」用; 空=按最早快照估算
  subSince: z.string().nullish(),
});

// 统一成本口径(元): 成本 = Σ(用量 × 单价)
//  payg: 平台已按各资源官方单价算好的真实扣费(monthlyUsed), 充值自动剔除
//  sub : 套餐折算单价 = 月费 ÷ 套餐总量, 消耗按已用配额比例摊销 → 月费 × 已用配额%
//        未录入月费(免费档/赠送额度)单价记为 0, 成本为 0, 仅展示配额进度
function monthCost(a: {
  apiType: string; monthlyUsed: number; monthlyFee: number; quotaPercent: number | null;
}): number {
  if (a.apiType === "sub") {
    if (!a.monthlyFee || a.quotaPercent == null) return 0;
    const usedPct = Math.max(0, Math.min(100, 100 - a.quotaPercent));
    return Math.round(a.monthlyFee * usedPct) / 100;
  }
  return a.monthlyUsed;
}

function subUsedPct(a: { quotaPercent: number | null }): number | null {
  if (a.quotaPercent == null) return null;
  return Math.max(0, Math.min(100, 100 - a.quotaPercent));
}

// 归一化: 按类型整理字段 (payg 清空配额字段, sub 清空余额字段)
function normalize(input: Omit<z.infer<typeof accountInput>, "id">) {
  if (input.apiType === "sub") {
    return {
      ...input,
      remaining: 0,
      monthlyUsed: 0,
      totalRecharge: 0,
      quotaPercent: input.quotaPercent ?? null,
      resetCycle: input.resetCycle ?? null,
      resetDay: input.resetDay ?? null,
      subSince: input.subSince ?? null,
    };
  }
  // 按量账号没有重置周期概念
  return { ...input, quotaPercent: null, monthlyFee: 0, validUntil: input.validUntil ?? null, resetCycle: null, resetDay: null, subSince: null };
}

// 已计费周期数(含当前周期): 月度=跨自然月数(含当月); 周度=整周数+1。与单机版 _sub_billed_periods 对齐
function billedPeriods(sinceIso: string | null, cycle: string | null): number {
  const since = sinceIso ? new Date(sinceIso) : new Date();
  const today = new Date();
  if (isNaN(since.getTime()) || since > today) return 1;
  if (cycle === "weekly") return Math.floor((today.getTime() - since.getTime()) / 86400000 / 7) + 1;
  return (today.getFullYear() - since.getFullYear()) * 12 + (today.getMonth() - since.getMonth()) + 1;
}

// 订阅累计充值(实际已付): 年付(截止日距今>90天)=月费×12; 月付=已计费周期数×周期费。与单机版 _sub_total_recharge 对齐
function subTotalRecharge(a: { monthlyFee: number; validUntil?: string | null; resetCycle?: string | null }, periods: number): number {
  const fee = Number(a.monthlyFee) || 0;
  const periodFee = a.resetCycle === "weekly" ? fee / 4 : fee;
  if (a.validUntil) {
    const vu = new Date(a.validUntil);
    if (!isNaN(vu.getTime()) && (vu.getTime() - Date.now()) / 86400000 > 90) return Math.round(fee * 12 * 100) / 100;
  }
  return Math.round(periods * periodFee * 100) / 100;
}

export const quotaRouter = createRouter({
  // ---------- 账号 ----------
  list: publicQuery.query(async () => {
    const db = getDb();
    const rows = await db.select().from(accounts).orderBy(accounts.id);
    // 附加计算口径字段(与单机版 _account_view 对齐, 供前端展示; 单机版在 Python 端实算)
    return rows.map((a) => {
      const isSub = a.apiType === "sub";
      const fee = Number(a.monthlyFee) || 0;
      const periodFee = a.resetCycle === "weekly" ? fee / 4 : fee;
      const periods = isSub ? billedPeriods(a.subSince ?? a.createdAt?.toISOString?.() ?? null, a.resetCycle ?? null) : 0;
      // 累计充值: 订阅=实际已付(年付×12/月付=周期数×周期费); 按量=手填累计充值
      const totalRechargeShow = isSub ? subTotalRecharge(a, periods) : Number(a.totalRecharge) || 0;
      // 累计消耗: 订阅=已计费周期数×周期费 − 剩余%×周期费; 按量=累计充值−余额
      const remainMoney = isSub ? periodFee * (Number(a.quotaPercent) || 0) / 100 : Number(a.remaining) || 0;
      return {
        ...a,
        monthUsedReal: isSub ? 0 : Number(a.monthlyUsed) || 0, // 云端参考版简化; 单机版实算
        totalRechargeShow,
        totalUsed: Math.round((isSub ? periods * periodFee - remainMoney : totalRechargeShow - remainMoney) * 100) / 100,
        billedPeriods: isSub ? periods : 0,
        resetCycle: null as "weekly" | "monthly" | null,
        resetDay: null as number | null,
        resetAuto: true,
        resetLabel: "",
        nextReset: "",
      };
    });
  }),

  upsert: publicQuery.input(accountInput).mutation(async ({ input }) => {
    const db = getDb();
    const { id, ...rest } = input;
    const data = normalize(rest);
    const st = deriveStatus(data);
    if (id) {
      await db
        .update(accounts)
        .set({ ...data, ...st, lastCollectAt: new Date() })
        .where(eq(accounts.id, id));
      return { id };
    }
    const [{ id: newId }] = await db
      .insert(accounts)
      .values({ ...data, ...st, lastCollectAt: new Date() })
      .$returningId();
    // 快照值: payg 记余额(元), sub 记剩余配额%
    const snapVal = data.apiType === "sub" ? (data.quotaPercent ?? 100) : data.remaining;
    await db.insert(snapshots).values({ accountId: newId, remaining: snapVal });
    return { id: newId };
  }),

  // 删除账号(云端 MySQL 参考版)。
  // 注意: 单机版(Python + data.json)已改为「软删除」——账号移除但历史快照保留,
  //       否则该账号的历史花销会从账目统计里凭空消失。云端版若要一致, 需给 accounts
  //       加 deletedAt 软删除列并在统计时一并纳入(避免物理删除快照)。
  remove: publicQuery
    .input(z.object({ id: z.number() }))
    .mutation(async ({ input }) => {
      const db = getDb();
      await db.delete(snapshots).where(eq(snapshots.accountId, input.id));
      await db.delete(accounts).where(eq(accounts.id, input.id));
      return { ok: true };
    }),

  // 采集: 录入最新数值 (payg=余额元 / sub=剩余配额%), 追加快照
  collect: publicQuery
    .input(z.object({ id: z.number(), value: z.number(), note: z.string().optional() }))
    .mutation(async ({ input }) => {
      const db = getDb();
      const acc = await db.query.accounts.findFirst({ where: eq(accounts.id, input.id) });
      if (!acc) throw new Error("账号不存在");
      const isSub = acc.apiType === "sub";
      const patch = isSub
        ? { quotaPercent: Math.max(0, Math.min(100, input.value)) }
        : { remaining: input.value };
      const st = deriveStatus({ ...acc, ...patch });
      await db
        .update(accounts)
        .set({ ...patch, ...st, note: input.note ?? acc.note, lastCollectAt: new Date() })
        .where(eq(accounts.id, input.id));
      await db.insert(snapshots).values({ accountId: input.id, remaining: input.value });
      return { ok: true, ...st };
    }),

  // 从本地代理(127.0.0.1:8787)一键导入账号 (兼容旧代理: unit="%" 视为订阅制)
  importAll: publicQuery
    .input(z.object({ items: z.array(accountInput.omit({ id: true })) }))
    .mutation(async ({ input }) => {
      const db = getDb();
      let added = 0;
      for (const raw of input.items) {
        const data = normalize(raw);
        const exist = await db.query.accounts.findFirst({
          where: and(eq(accounts.platform, data.platform), eq(accounts.name, data.name)),
        });
        const st = deriveStatus(data);
        const snapVal = data.apiType === "sub" ? (data.quotaPercent ?? 100) : data.remaining;
        if (exist) {
          await db.update(accounts).set({ ...data, ...st, lastCollectAt: new Date() }).where(eq(accounts.id, exist.id));
          await db.insert(snapshots).values({ accountId: exist.id, remaining: snapVal });
        } else {
          const [{ id: newId }] = await db.insert(accounts).values({ ...data, ...st, lastCollectAt: new Date() }).$returningId();
          await db.insert(snapshots).values({ accountId: newId, remaining: snapVal });
          added++;
        }
      }
      return { ok: true, added, total: input.items.length };
    }),

  // ---------- 统计 ----------
  // TOKEN 用量(V3.34.1): 生产由 Python 主程序 proc_token_usage 提供;
  // 本 TS 后端只用于开发模式, 返回空结构保持类型一致
  tokenUsage: publicQuery.query(async () => {
    return { items: [] as Array<Record<string, unknown>>, generatedAt: "" };
  }),

  summary: publicQuery.query(async () => {
    const db = getDb();
    const list = await db.select().from(accounts);
    // 本月总消耗(元) = Σ 统一成本口径(按量真实扣费 + 订阅按已用配额摊销)
    const monthTotal = Math.round(list.reduce((s, a) => s + monthCost(a), 0) * 100) / 100;
    // 当前余额(V3.17.10): 按量余额 + 订阅已付未用(年付含未来周期价值, 恒等式口径)
    const remainingTotal = Math.round(list.reduce((s, a) => {
      if (a.apiType !== "sub") return s + a.remaining;
      const fee = a.monthlyFee || 0;
      const periodFee = a.resetCycle === "weekly" ? fee / 4 : fee;
      const periods = billedPeriods(a.subSince ?? a.createdAt?.toISOString?.() ?? null, a.resetCycle ?? null);
      const paid = subTotalRecharge(a, periods);
      const remain = a.quotaPercent == null ? 0 : (periodFee * a.quotaPercent) / 100;
      return s + Math.max(0, paid - (periods * periodFee - remain));
    }, 0) * 100) / 100;
    const byPlatformMap = new Map<
      string,
      {
        platform: string; used: number; budget: number; remaining: number;
        subQuota: number | null; subUsedPct: number | null; subCount: number;
        subFee: number; paygUsed: number; subCost: number; subCycle: string | null;
        subQuotaSource?: string;
      }
    >();
    const subAcc = new Map<string, number[]>();
    for (const a of list) {
      const cur = byPlatformMap.get(a.platform) ?? {
        platform: a.platform, used: 0, budget: 0, remaining: 0,
        subQuota: null, subUsedPct: null, subCount: 0, subFee: 0, paygUsed: 0, subCost: 0, subCycle: null,
        subQuotaSource: "quota",
      };
      cur.used += monthCost(a);
      cur.budget += a.budget;
      if (a.apiType !== "sub") {
        cur.remaining += a.remaining;
        cur.paygUsed += a.monthlyUsed;
      } else {
        cur.subCount += 1;
        cur.subFee += a.monthlyFee;
        cur.subCost += monthCost(a);
        // 周期标注(V3.17.10): 订阅配额标明 7天/30天 口径
        if (!cur.subCycle) cur.subCycle = a.resetCycle === "weekly" ? "7天" : a.resetCycle === "monthly" ? "30天" : null;
        const up = subUsedPct(a);
        if (up != null) {
          const arr = subAcc.get(a.platform) ?? [];
          arr.push(up);
          subAcc.set(a.platform, arr);
        }
      }
      byPlatformMap.set(a.platform, cur);
    }
    for (const [p, cur] of byPlatformMap) {
      const arr = subAcc.get(p);
      if (arr && arr.length) {
        cur.subUsedPct = Math.round((arr.reduce((s, v) => s + v, 0) / arr.length) * 10) / 10;
        cur.subQuota = Math.round((100 - cur.subUsedPct) * 10) / 10;
      }
      cur.used = Math.round(cur.used * 100) / 100;
      cur.subCost = Math.round(cur.subCost * 100) / 100;
    }
    const alerts = list
      .filter((a) => a.status !== "success")
      .map((a) => ({
        id: a.id, level: a.status, platform: a.platform, name: a.name,
        title: `${a.name} 额度预警`, desc: a.statusText,
      }));
    const subAccounts = list
      .filter((a) => a.apiType === "sub")
      .map((a) => ({
        name: a.name, platform: a.platform,
        usedPct: subUsedPct(a), fee: a.monthlyFee, cost: monthCost(a),
      }));
    // 在线订阅分类(V3.21.0): 按量 / 订阅API / 在线会员 三视图(Python 后端同口径)
    const ONLINE_SUBS = new Set(["chatgpt", "claude_sub", "gemini_sub"]);
    const kindOf = (a: (typeof list)[number]) =>
      a.apiType !== "sub" ? "payg" : (ONLINE_SUBS.has(a.platform) ? "online" : "sub");
    const byCategory = { payg: 0, sub: 0, online: 0 };
    for (const a of list) byCategory[kindOf(a)] += monthCost(a);
    byCategory.payg = Math.round(byCategory.payg * 100) / 100;
    byCategory.sub = Math.round(byCategory.sub * 100) / 100;
    byCategory.online = Math.round(byCategory.online * 100) / 100;
    const onlineAccounts = list.filter((a) => kindOf(a) === "online").map((a) => ({
      id: a.id, name: a.name, platform: a.platform,
      currency: (a as unknown as { currency?: string }).currency ?? "USD",
      monthlyFee: a.monthlyFee, monthCost: monthCost(a),
      win5h: (a as unknown as { win5h?: number | null }).win5h ?? null,
      win7d: (a as unknown as { win7d?: number | null }).win7d ?? null,
      winUpdatedAt: (a as unknown as { winUpdatedAt?: string | null }).winUpdatedAt ?? null,
    }));
    return {
      monthTotal, remainingTotal,
      accountCount: list.length,
      alertCount: alerts.length,
      byCategory, onlineAccounts,
      byPlatform: [...byPlatformMap.values()],
      alerts, subAccounts,
    };
  }),

  // 近 N 天趋势:
  //  payg 系列: 相邻两次快照「余额下降额」累加=消耗(元), 余额上升视为充值不计;
  //             每笔按区间「中点」归入某一天 —— 与 账目统计(ledger) 口径完全一致。
  //             (旧版用「同账号同日首末快照差值」, 会漏算充值当天的消耗、跨天断档算错, 已修正)
  //  sub  系列: 当日剩余配额%均值
  trend: publicQuery
    .input(z.object({ days: z.number().min(1).max(90).default(30) }).optional())
    .query(async ({ input }) => {
      const db = getDb();
      const days = input?.days ?? 30;
      const since = new Date(Date.now() - days * 86400_000);
      const accs = await db.select().from(accounts);
      const snaps = await db
        .select()
        .from(snapshots)
        .orderBy(snapshots.collectedAt);
      const accMap = new Map(accs.map((a) => [a.id, a]));
      // accountId -> 按时间升序的快照列表(全量, 用于相邻差分)
      const byAcc = new Map<number, { t: Date; r: number }[]>();
      for (const s of snaps) {
        const arr = byAcc.get(s.accountId) ?? [];
        arr.push({ t: s.collectedAt, r: s.remaining });
        byAcc.set(s.accountId, arr);
      }
      const dayKey = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
      // 按量: date -> platform -> 消耗(元)
      const dailyUsed = new Map<string, Map<string, number>>();
      // 订阅: date -> platform -> {sum, n} 剩余配额%
      const dailyQuota = new Map<string, Map<string, { sum: number; n: number }>>();
      for (const [accId, arr] of byAcc) {
        const a = accMap.get(accId);
        if (!a) continue;
        arr.sort((x, y) => x.t.getTime() - y.t.getTime());
        if (a.apiType === "sub") {
          for (const s of arr) {
            if (s.t < since) continue;
            const d = dayKey(s.t);
            const pd = dailyQuota.get(d) ?? new Map<string, { sum: number; n: number }>();
            const cur = pd.get(a.platform) ?? { sum: 0, n: 0 };
            cur.sum += s.r; cur.n += 1;
            pd.set(a.platform, cur);
            dailyQuota.set(d, pd);
          }
          continue;
        }
        for (let i = 1; i < arr.length; i++) {
          const delta = arr[i].r - arr[i - 1].r;
          if (delta >= 0) continue;                 // 充值/不变 → 不计消耗
          const mid = new Date((arr[i - 1].t.getTime() + arr[i].t.getTime()) / 2);
          if (mid < since) continue;
          const d = dayKey(mid);
          const pd = dailyUsed.get(d) ?? new Map<string, number>();
          pd.set(a.platform, (pd.get(a.platform) ?? 0) + -delta);
          dailyUsed.set(d, pd);
        }
      }
      const paygPlatforms = [...new Set(accs.filter((a) => a.apiType !== "sub").map((a) => a.platform))];
      const subPlatforms = [...new Set(accs.filter((a) => a.apiType === "sub").map((a) => a.platform))];
      const out: { date: string; [k: string]: number | string }[] = [];
      const quotaOut: { date: string; [k: string]: number | string }[] = [];
      for (let i = days - 1; i >= 0; i--) {
        const d = new Date(Date.now() - i * 86400_000).toISOString().slice(0, 10);
        const row: { date: string; [k: string]: number | string } = { date: d };
        const pd = dailyUsed.get(d);
        for (const p of paygPlatforms) row[p] = Math.round(((pd?.get(p) ?? 0) + Number.EPSILON) * 100) / 100;
        out.push(row);
        const qrow: { date: string; [k: string]: number | string } = { date: d };
        const qd = dailyQuota.get(d);
        for (const p of subPlatforms) {
          const c = qd?.get(p);
          qrow[p] = c ? Math.round((c.sum / c.n) * 10) / 10 : null as unknown as number;
        }
        quotaOut.push(qrow);
      }
      return { days: out, platforms: paygPlatforms, quotaDays: quotaOut, subPlatforms };
    }),

  // ---------- 自然月 / 自然年 账目统计 ----------
  //  按量: 相邻快照「余额下降额」累加, 上升=充值不计; 消耗归属到区间中点的自然月
  //  订阅: 月费(默认按当月实际在订天数折算, 另有整月口径)
  //  免费/赠送额度单价 0, 不进账目
  ledger: publicQuery.query(async () => {
    const db = getDb();
    const list = await db.select().from(accounts);
    const snapsAll = await db.select().from(snapshots).orderBy(snapshots.collectedAt);
    const accMap = new Map(list.map((a) => [a.id, a]));
    const today = new Date();
    const yarn = today.getFullYear();
    const ymon = today.getMonth();
    const daysInMonth = (y: number, m0: number) => new Date(y, m0 + 1, 0).getDate();
    const mkey = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;

    const mPlat = new Map<string, Map<string, number>>();
    const mPlatFull = new Map<string, Map<string, number>>();
    const mAcct = new Map<string, Map<number, number>>();
    const mAcctFull = new Map<string, Map<number, number>>();
    const add = (k: string, aid: number, plat: string, amount: number, full?: number) => {
      if (amount <= 0) return;
      let mp = mPlat.get(k); if (!mp) { mp = new Map(); mPlat.set(k, mp); }
      mp.set(plat, (mp.get(plat) ?? 0) + amount);
      let ma = mAcct.get(k); if (!ma) { ma = new Map(); mAcct.set(k, ma); }
      ma.set(aid, (ma.get(aid) ?? 0) + amount);
      if (full !== undefined) {
        let mpf = mPlatFull.get(k); if (!mpf) { mpf = new Map(); mPlatFull.set(k, mpf); }
        mpf.set(plat, (mpf.get(plat) ?? 0) + full);
        let maf = mAcctFull.get(k); if (!maf) { maf = new Map(); mAcctFull.set(k, maf); }
        maf.set(aid, (maf.get(aid) ?? 0) + full);
      }
    };

    // 按量: 逐账号相邻快照累加下降量
    const byAcc = new Map<number, typeof snapsAll>();
    for (const s of snapsAll) {
      const arr = byAcc.get(s.accountId) ?? [];
      arr.push(s);
      byAcc.set(s.accountId, arr);
    }
    for (const [aid, arr] of byAcc) {
      const a = accMap.get(aid);
      if (!a || a.apiType === "sub") continue;
      const plat = a.platform || "custom";
      const sorted = [...arr].sort((x, y) => x.collectedAt.getTime() - y.collectedAt.getTime());
      for (let i = 1; i < sorted.length; i++) {
        const t0 = sorted[i - 1].collectedAt.getTime();
        const t1 = sorted[i].collectedAt.getTime();
        const delta = sorted[i].remaining - sorted[i - 1].remaining;
        if (delta >= 0) continue;
        add(mkey(new Date((t0 + t1) / 2)), aid, plat, -delta);
      }
    }

    // 订阅: 月费
    for (const a of list) {
      if (a.apiType !== "sub") continue;
      const fee = a.monthlyFee || 0;
      if (fee <= 0) continue;
      const plat = a.platform || "custom";
      const created = a.createdAt ? new Date(a.createdAt) : today;
      const start = new Date(created.getFullYear(), created.getMonth(), created.getDate());
      let cur = new Date(start.getFullYear(), start.getMonth(), 1);
      let guard = 0;
      while (cur.getTime() <= today.getTime() && guard < 600) {
        guard++;
        const y = cur.getFullYear();
        const m0 = cur.getMonth();
        const nDays = daysInMonth(y, m0);
        const mStart = new Date(y, m0, 1);
        const mLast = new Date(y, m0, nDays);
        const today0 = new Date(today.getFullYear(), today.getMonth(), today.getDate());
        const lo = start.getTime() > mStart.getTime() ? start : mStart;
        const hi = today0.getTime() < mLast.getTime() ? today0 : mLast;
        const active = hi.getTime() >= lo.getTime() ? Math.round((hi.getTime() - lo.getTime()) / 86400_000) + 1 : 0;
        if (active > 0) add(mkey(cur), a.id, plat, Math.round((fee * active) / nDays * 1e6) / 1e6, fee);
        cur = new Date(y, m0 + 1, 1);
      }
    }

    const keys = [...new Set([...mPlat.keys(), ...mPlatFull.keys()])].sort();
    if (!keys.length) return { months: [], years: [], platforms: [], generatedAt: new Date().toISOString(), hasData: false };
    const [y0, m0s] = keys[0].split("-").map(Number);
    const [y1, m1s] = keys[keys.length - 1].split("-").map(Number);
    const seq: string[] = [];
    let cy = y0, cm = m0s;
    while (cy < y1 || (cy === y1 && cm <= m1s)) {
      seq.push(`${cy}-${String(cm).padStart(2, "0")}`);
      cm++; if (cm > 12) { cm = 1; cy++; }
    }
    const platforms = [...new Set([...mPlat.values(), ...mPlatFull.values()].flatMap((m) => [...m.keys()]))].sort();
    const detailOf = (k: string, map: Map<string, Map<number, number>>) =>
      [...(map.get(k) ?? new Map<number, number>())].map(([aid, v]) => {
        const a = accMap.get(aid);
        return {
          accountId: aid, name: a?.name ?? "?", platform: a?.platform ?? "custom",
          apiType: a?.apiType ?? "payg", monthlyFee: Math.round((a?.monthlyFee ?? 0) * 100) / 100,
          amount: Math.round(v * 100) / 100,
        };
      }).sort((x, y) => y.amount - x.amount);

    const months = seq.map((mk) => {
      const mp = mPlat.get(mk) ?? new Map<string, number>();
      const mpf = mPlatFull.get(mk) ?? new Map<string, number>();
      let payg = 0, sub = 0, subFull = 0;
      for (const [aid, v] of mAcct.get(mk) ?? new Map<number, number>()) {
        const a = accMap.get(aid);
        if (a?.apiType === "sub") { sub += v; subFull += mAcctFull.get(mk)?.get(aid) ?? v; }
        else payg += v;
      }
      const bp: Record<string, number> = {}; const bpf: Record<string, number> = {};
      for (const p of platforms) { bp[p] = Math.round((mp.get(p) ?? 0) * 100) / 100; bpf[p] = Math.round((mpf.get(p) ?? 0) * 100) / 100; }
      const total = Math.round([...mp.values()].reduce((s, v) => s + v, 0) * 100) / 100;
      const totalFull = Math.round([...mpf.values()].reduce((s, v) => s + v, 0) * 100) / 100;
      return {
        month: mk, byPlatform: bp, byPlatformFull: bpf,
        paygTotal: Math.round(payg * 100) / 100, subTotal: Math.round(sub * 100) / 100,
        subTotalFull: Math.round(subFull * 100) / 100, total, totalFull,
        detail: detailOf(mk, mAcct), detailFull: detailOf(mk, mAcctFull),
      };
    });

    const ymap = new Map<string, Map<string, number>>();
    const ymapFull = new Map<string, Map<string, number>>();
    for (const mk of seq) {
      const y = mk.slice(0, 4);
      let ym = ymap.get(y); if (!ym) { ym = new Map(); ymap.set(y, ym); }
      for (const [p, v] of mPlat.get(mk) ?? new Map<string, number>()) ym.set(p, (ym.get(p) ?? 0) + v);
      let ymf = ymapFull.get(y); if (!ymf) { ymf = new Map(); ymapFull.set(y, ymf); }
      for (const [p, v] of mPlatFull.get(mk) ?? new Map<string, number>()) ymf.set(p, (ymf.get(p) ?? 0) + v);
    }
    const years = [...new Set([...ymap.keys(), ...ymapFull.keys()])].sort().map((y) => {
      const ym = ymap.get(y) ?? new Map<string, number>();
      const ymf = ymapFull.get(y) ?? new Map<string, number>();
      const bp: Record<string, number> = {}; const bpf: Record<string, number> = {};
      for (const p of platforms) { bp[p] = Math.round((ym.get(p) ?? 0) * 100) / 100; bpf[p] = Math.round((ymf.get(p) ?? 0) * 100) / 100; }
      return {
        year: y, byPlatform: bp, byPlatformFull: bpf,
        total: Math.round([...ym.values()].reduce((s, v) => s + v, 0) * 100) / 100,
        totalFull: Math.round([...ymf.values()].reduce((s, v) => s + v, 0) * 100) / 100,
        monthCount: seq.filter((mk) => mk.startsWith(y)).length,
      };
    });
    return { months, years, platforms, generatedAt: new Date().toISOString(), hasData: true };
  }),

  // ---------- 总投入额度(自动计算为主, 可手动覆盖) ----------
  //  自动总投入 = Σ按量累计充值 + Σ订阅实际已付(年付×12/月付=周期数×周期费); 手填优先, 清空恢复自动。
  //  联动指标(与模型API管理同口径): totalUsedAll=Σ累计消耗, monthUsedAll=Σ本月消耗。
  getTotalQuota: publicQuery.query(async () => {
    const db = getDb();
    const rows = await db.select().from(settings);
    const m = new Map(rows.map((r) => [r.skey, r.svalue]));
    const raw = m.get("totalQuota");
    const list = await db.select().from(accounts);
    const rechargeTotal = list
      .filter((a) => a.apiType !== "sub")
      .reduce((s, a) => s + a.totalRecharge, 0);
    // 自动总投入: 与账号列表「累计充值」列同口径(逐账号聚合)
    const autoTotal = Math.round((list.reduce((s, a) => {
      if (a.apiType === "sub") {
        const periods = billedPeriods(a.subSince ?? a.createdAt?.toISOString?.() ?? null, a.resetCycle ?? null);
        return s + subTotalRecharge(a, periods);
      }
      return s + (a.totalRecharge || 0);
    }, 0)) * 100) / 100;
    // 联动聚合(云端简化实算: 订阅累计消耗=已计费周期数×周期费−剩余%×周期费)
    const totalUsedAll = Math.round(list.reduce((s, a) => {
      if (a.apiType === "sub") {
        const fee = a.monthlyFee || 0;
        const periodFee = a.resetCycle === "weekly" ? fee / 4 : fee;
        const remain = a.quotaPercent == null ? 0 : (periodFee * a.quotaPercent) / 100;
        const periods = billedPeriods(a.subSince ?? a.createdAt?.toISOString?.() ?? null, a.resetCycle ?? null);
        return s + (periods * periodFee - remain);
      }
      return s + (a.totalRecharge - a.remaining);
    }, 0) * 100) / 100;
    // 当前余额(V3.17.10 恒等式): Σ(已付 − 已用); 订阅年付含未来周期价值
    const remainingAll = Math.round((autoTotal - totalUsedAll) * 100) / 100;
    const monthUsedAll = Math.round(list.reduce((s, a) =>
      s + (a.apiType === "sub"
        ? (a.monthlyFee || 0) * (a.quotaPercent == null ? 0 : (100 - a.quotaPercent) / 100)
        : (a.monthlyUsed || 0)), 0) * 100) / 100;
    const isAuto = raw == null || raw === "";
    return {
      totalQuota: isAuto ? autoTotal : Number(raw),
      hasValue: isAuto ? autoTotal > 0 : true,
      isAuto,
      autoTotal,
      spentTotal: 0,
      rechargeTotal: Math.round(rechargeTotal * 100) / 100,
      totalUsedAll,
      remainingAll,
      monthUsedAll,
    };
  }),

  setTotalQuota: publicQuery
    .input(z.object({ totalQuota: z.number().min(0) }))
    .mutation(async ({ input }) => {
      const db = getDb();
      const v = input.totalQuota <= 0 ? "" : String(input.totalQuota);
      await db
        .insert(settings)
        .values({ skey: "totalQuota", svalue: v })
        .onDuplicateKeyUpdate({ set: { svalue: v } });
      return { ok: true, totalQuota: input.totalQuota };
    }),

  // ---------- 手机/远程访问(单机版由本地程序实现, 此处仅为类型对齐) ----------
  getAccess: publicQuery.query(async () => {
    const db = getDb();
    const rows = await db.select().from(settings);
    const m = new Map(rows.map((r) => [r.skey, r.svalue]));
    const pw = m.get("remotePassword") ?? "";
    return {
      password: pw,
      enabled: pw !== "",
      port: 8787,
      loopbackUrl: "http://127.0.0.1:8787",
      lanUrl: "",
      lanIp: "",
      remoteUrl: m.get("remoteUrl") ?? "",
      autoDetected: !(m.get("remoteUrl") ?? "").trim(),
    };
  }),

  setRemotePassword: publicQuery
    .input(z.object({ password: z.string() }))
    .mutation(async ({ input }) => {
      const db = getDb();
      const pw = input.password.replace(/\s+/g, "").slice(0, 32);
      await db
        .insert(settings)
        .values({ skey: "remotePassword", svalue: pw })
        .onDuplicateKeyUpdate({ set: { svalue: pw } });
      return { ok: true, password: pw, enabled: pw !== "" };
    }),

  genRemotePassword: publicQuery.mutation(async () => {
    const db = getDb();
    const ab = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
    let pw = "";
    for (let i = 0; i < 6; i++) pw += ab[Math.floor(Math.random() * ab.length)];
    await db
      .insert(settings)
      .values({ skey: "remotePassword", svalue: pw })
      .onDuplicateKeyUpdate({ set: { svalue: pw } });
    return { ok: true, password: pw, enabled: true };
  }),

  setRemoteUrl: publicQuery
    .input(z.object({ remoteUrl: z.string() }))
    .mutation(async ({ input }) => {
      const db = getDb();
      const url = input.remoteUrl.trim();
      await db
        .insert(settings)
        .values({ skey: "remoteUrl", svalue: url })
        .onDuplicateKeyUpdate({ set: { svalue: url } });
      return { ok: true, remoteUrl: url };
    }),

  // ---------- 预算 ----------
  getBudget: publicQuery.query(async () => {
    const db = getDb();
    const rows = await db.select().from(settings);
    const m = new Map(rows.map((r) => [r.skey, r.svalue]));
    return { monthlyTotal: Number(m.get("monthlyTotal") ?? 1800), warnPercent: Number(m.get("warnPercent") ?? 80) };
  }),

  setBudget: publicQuery
    .input(z.object({ monthlyTotal: z.number().min(0), warnPercent: z.number().min(1).max(100) }))
    .mutation(async ({ input }) => {
      const db = getDb();
      for (const [k, v] of [
        ["monthlyTotal", String(input.monthlyTotal)],
        ["warnPercent", String(input.warnPercent)],
      ] as const) {
        await db.insert(settings).values({ skey: k, svalue: v }).onDuplicateKeyUpdate({ set: { svalue: v } });
      }
      return { ok: true };
    }),

  // 更新某平台预算
  setPlatformBudget: publicQuery
    .input(z.object({ platform: z.string(), budget: z.number().min(0) }))
    .mutation(async ({ input }) => {
      const db = getDb();
      await db.update(accounts).set({ budget: input.budget }).where(eq(accounts.platform, input.platform));
      return { ok: true };
    }),
});
