import {
  mysqlTable,
  serial,
  varchar,
  text,
  timestamp,
  bigint,
  double,
} from "drizzle-orm/mysql-core";

// 账号表: 只支持两类 API 账号
//  apiType="payg" 按量付费API(余额制, 人民币): remaining=余额, monthlyUsed=本月消耗, totalRecharge=累计充值
//  apiType="sub"  订阅制API(配额制, 如 Coding Plan): quotaPercent=剩余配额%, monthlyFee=月费(计入预算), validUntil=重置/到期日
export const accounts = mysqlTable("accounts", {
  id: serial("id").primaryKey(),
  platform: varchar("platform", { length: 64 }).notNull(),
  name: varchar("name", { length: 128 }).notNull(),
  groupName: varchar("group_name", { length: 64 }).notNull().default("默认"),
  apiType: varchar("api_type", { length: 8 }).notNull().default("payg"), // payg | sub
  remaining: double("remaining").notNull().default(0), // payg: 余额(元)
  quotaPercent: double("quota_percent"), // sub: 剩余配额 %
  monthlyUsed: double("monthly_used").notNull().default(0), // payg: 本月消耗(元)
  monthlyFee: double("monthly_fee").notNull().default(0), // sub: 月费(元, 计入消耗与预算)
  totalRecharge: double("total_recharge").notNull().default(0),
  budget: double("budget").notNull().default(0), // 该平台月预算(元)
  collectMethod: varchar("collect_method", { length: 16 }).notNull().default("手动录入"),
  status: varchar("status", { length: 16 }).notNull().default("success"),
  statusText: varchar("status_text", { length: 64 }).notNull().default("正常"),
  note: text("note"),
  validUntil: varchar("valid_until", { length: 32 }), // sub: 配额重置/到期日
  // sub 订阅配额重置周期: weekly=每周 / monthly=每月; resetDay: 周=0-6(周一0) 月=1-31
  resetCycle: varchar("reset_cycle", { length: 8 }), // weekly | monthly | null
  resetDay: bigint("reset_day", { mode: "number", unsigned: true }),
  subSince: varchar("sub_since", { length: 10 }), // sub 订阅开始日 YYYY-MM-DD(算已计费周期数用)
  lastCollectAt: timestamp("last_collect_at"),
  createdAt: timestamp("created_at").notNull().defaultNow(),
  updatedAt: timestamp("updated_at").notNull().defaultNow().onUpdateNow(),
});

// 快照: 每次采集追加一条。payg 记余额(元), sub 记剩余配额%(存 value 字段同一列)
export const snapshots = mysqlTable("snapshots", {
  id: serial("id").primaryKey(),
  accountId: bigint("account_id", { mode: "number", unsigned: true }).notNull(),
  remaining: double("remaining").notNull(), // payg=余额(元) / sub=剩余配额%
  collectedAt: timestamp("collected_at").notNull().defaultNow(),
});

// 全局设置: 月度总预算、预警阈值
export const settings = mysqlTable("settings", {
  id: serial("id").primaryKey(),
  skey: varchar("skey", { length: 64 }).notNull().unique(),
  svalue: varchar("svalue", { length: 512 }).notNull(),
});

export type Account = typeof accounts.$inferSelect;
export type InsertAccount = typeof accounts.$inferInsert;
export type Snapshot = typeof snapshots.$inferSelect;
