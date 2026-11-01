import mysql from "mysql2/promise";
import { env } from "./lib/env";

// 幂等建表: 服务启动时确保表存在 (与 db/schema.ts 保持一致)
// 账号模型: apiType="payg" 按量付费API(余额制,元) / apiType="sub" 订阅制API(配额制,%)
export async function ensureSchema() {
  const conn = await mysql.createConnection(env.databaseUrl);
  try {
    await conn.execute(`CREATE TABLE IF NOT EXISTS accounts (
      id bigint unsigned NOT NULL AUTO_INCREMENT PRIMARY KEY,
      platform varchar(64) NOT NULL,
      name varchar(128) NOT NULL,
      group_name varchar(64) NOT NULL DEFAULT '默认',
      api_type varchar(8) NOT NULL DEFAULT 'payg',
      remaining double NOT NULL DEFAULT 0,
      quota_percent double NULL,
      monthly_used double NOT NULL DEFAULT 0,
      monthly_fee double NOT NULL DEFAULT 0,
      total_recharge double NOT NULL DEFAULT 0,
      budget double NOT NULL DEFAULT 0,
      collect_method varchar(16) NOT NULL DEFAULT '手动录入',
      status varchar(16) NOT NULL DEFAULT 'success',
      status_text varchar(64) NOT NULL DEFAULT '正常',
      note text,
      valid_until varchar(32),
      last_collect_at timestamp NULL,
      created_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    )`);
    // 兼容旧库: 补充新增列 (已存在则忽略)
    const alters = [
      "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS api_type varchar(8) NOT NULL DEFAULT 'payg'",
      "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS quota_percent double NULL",
      "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS monthly_fee double NOT NULL DEFAULT 0",
    ];
    for (const sql of alters) {
      try {
        await conn.execute(sql);
      } catch {
        // 列已存在或语法不支持时忽略
      }
    }
    await conn.execute(`CREATE TABLE IF NOT EXISTS snapshots (
      id bigint unsigned NOT NULL AUTO_INCREMENT PRIMARY KEY,
      account_id bigint unsigned NOT NULL,
      remaining double NOT NULL,
      collected_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
      INDEX idx_snap_acc_time (account_id, collected_at)
    )`);
    await conn.execute(`CREATE TABLE IF NOT EXISTS settings (
      id bigint unsigned NOT NULL AUTO_INCREMENT PRIMARY KEY,
      skey varchar(64) NOT NULL UNIQUE,
      svalue varchar(512) NOT NULL
    )`);
    console.log("[db] schema ensured");
  } finally {
    await conn.end();
  }
}
