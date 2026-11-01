import { useState, useEffect } from "react";
import { trpc } from "@/providers/trpc";
import { setDisplayCurrency, fmtMoney } from "@/lib/fmt";

type Pricing = {
  updated: string; source: string;
  payg: string[][]; sub: string[][]; note: string; urls_note?: string;
  checkedAt?: string;   // 上次联网核查时间(后端从 _pricing_check.json 读出)
};
type RefreshResult = {
  ok?: boolean; local?: boolean; updated?: string; detail?: string;
  changed?: number; data?: Pricing; checkedAt?: string;
  checks?: { platform: string; url: string; status: "same" | "new" | "changed" | "shell" | "fail"; detail?: string }[];
};

// 兜底默认(接口不可用时, 与 价格政策.json / 主程序内置数据同步维护)
const DEFAULT_PRICING: Pricing = {
  updated: "2026-10-17",
  source: "各平台官网公开价格(人工核对整理)",
  payg: [
    ["DeepSeek", "platform.deepseek.com", "V4-Flash 输入1/输出2;V4-Pro 3.1/6.2(元/百万token)", "预充值余额扣减, 欠费即停;缓存命中输入低至0.02元", "https://api-docs.deepseek.com/quick_start/pricing"],
    ["Kimi 开放平台", "platform.kimi.com", "K2.6/K2.7 Code 输入6.5/输出27;K3 20/100;联网搜索0.03元/次", "充值500元+送20-30%代金券;Batch 6折;缓存命中1.1-2元", "https://platform.kimi.com/docs/pricing"],
    ["智谱 GLM", "bigmodel.cn", "GLM-5 输入7.2/输出23;GLM-4.7 4.3/15.8(元/百万token)", "GLM-4.7-Flash、4.5-Flash 免费", "https://open.bigmodel.cn/pricing"],
    ["MiniMax", "platform.minimaxi.com", "M2 输入2.1/输出8.4;高速版4.2/16.8(元/百万token);图片0.025元/张", "付费默认500 RPM;缓存读取0.21元", "https://platform.minimaxi.com/document/price"],
    ["火山方舟(豆包)", "volcengine.com", "Seed 2.0 Mini 0.2/2 至 2.1 Pro 6/30(元/百万token)", "注册送50万token;Batch约5折", "https://www.volcengine.com/docs/82379/1099320"],
    ["ChatAnywhere(转发)", "api.chatanywhere.tech", "GPT/Claude/Gemini/DeepSeek/Qwen/Kimi 聚合, 各模型约为官方价 6-9 折", "满100元95折/满1000元9折;CA系列(Azure)更低", "https://chatanywhere.apifox.cn/"],
    ["SiliconFlow", "siliconflow.cn", "多模型聚合, 按各模型标价按量计费", "部分小模型免费;注册有体验金", "https://siliconflow.cn/pricing"],
  ],
  sub: [
    ["智谱 Coding Plan", "49 / 149 / 469", "5小时窗口+周窗口双限流, 到期自动重置", "包年7折/包季8折(限时)", "https://open.bigmodel.cn/finance/codingplan"],
    ["Kimi Coding Plan", "49 / 99 / 199 / 699", "7天周限 + 5小时频限双窗口", "年付约8折: 39/79/159/559", "https://platform.kimi.com/docs/pricing"],
    ["MiniMax Token Plan", "29 / 49 / 119", "5小时窗口+周窗口双限流", "首月折扣(如 Starter 26.1元)", "https://platform.minimax.cn/docs/pricing/overview"],
    ["百炼 Token Plan(阿里)", "个人版/团队版多档", "Credits 计量, 5小时+周双窗口重置", "sk-sp- 专用 Key; 用量官方无 API, 需控制台查看", "https://help.aliyun.com/zh/model-studio/coding-plan"],
    ["火山方舟 Agent Plan", "Small 9.9(首两月)起 / Medium / Large / Max", "5小时+日+周+月四窗口 AFP 额度", "连续包季9.7折/包年9.5折", "https://www.volcengine.com/product/ark"],
    ["Work Buddy", "99 / 199 / 999", "积分每月刷新", "连续包月7折: 70/140/700", "https://www.workbuddy.cn/pricing"],
    ["豆包会员", "68 / 200 / 500", "会员周期内额度刷新", "连续包年688/2048/5088", "https://www.doubao.com/chat/subscribe"],
  ],
  note: "智谱、Kimi、MiniMax、火山方舟同时有按量 API 与订阅套餐两种形态, 可分别添加两个不同类型的账号跟踪。Kimi 订阅 Key(sk-kimi-xxx, kimi.com/code/console 创建)与开放平台按量 Key 是两套; 火山方舟 Agent Plan 查用量需火山引擎 Access Key(AKLT 开头, 控制台 → API 访问密钥), 推理 Key 查不了。阿里百炼按量余额可用阿里云 AccessKey 采集, Token Plan 用量官方仅控制台可见。近期价格变动频繁, 录入预算前请以官网实时页面为准。",
  urls_note: "各平台政策页地址收录在「政策地址」列, 点击直达官网核对最新价格。",
};

function Table({ heads, rows, links }: { heads: string[]; rows: string[][]; links?: boolean }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="text-left border-b-2 border-slate-200">
            {heads.map((h) => <th key={h} className="py-2 px-2.5 whitespace-nowrap text-[#1A2B40]">{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className={i % 2 ? "bg-slate-50" : ""}>
              {r.map((c, j) => (
                <td key={j} className="py-2 px-2.5 border-b border-slate-100 text-slate-600 leading-relaxed align-top">
                  {links && j === r.length - 1 && /^https?:\/\//.test(c) ? (
                    <a href={c} target="_blank" rel="noreferrer" className="text-[#1B6B7A] hover:underline break-all">官网政策 ↗</a>
                  ) : c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const FAQ: [string, string][] = [
  ["快速开始 · 三步上手", "① 添加账号：在「模型API管理」手动录入, 或启动本地代理后点「从本地代理同步」一键导入。每个账号选类型：按量付费 API(余额制) 或 订阅制 API(配额制)。\n② 定期采集：按量账号自动/手动采集最新余额(元), 订阅账号采集剩余配额(%)供总览窗口展示; 每次采集都写入快照, 趋势图随之生长。\n③ 配置预算：在「当前总览 → 预算预警」设月度总预算与各平台预算——按量看实扣、订阅按月费按天摊销, 超预警线自动标黄/标红。"],
  ["两类 API 账号有什么区别?", "「按量付费 API」是充值余额制: 充多少用多少, 按 token 用量扣费, 跟踪余额(元)与本月消耗(元), 如 DeepSeek、Kimi 开放平台、SiliconFlow。「订阅制 API」是包月配额制: 月费固定, 在 5小时/周/月窗口内有限额, 如智谱 Coding Plan、Kimi Coding Plan、MiniMax Token Plan、火山方舟 Agent Plan。订阅的配额%只在「当前总览 → 订阅额度窗口」面板展示(选模型用), 不进金额计算。注意 Kimi 订阅 Key(sk-kimi-xxx, kimi.com/code/console 创建)与开放平台按量 Key 是两套; 火山方舟 Agent Plan 查用量要用火山引擎 AK/SK(控制台 → API 访问密钥), 推理 Key 查不了。"],
  ["数据存在哪里?", "本机 data.json(单机版)。账号、每次采集的快照、预算设置都会持久化保存; 网站更新版本不会清空数据。"],
  ["API Key 会传到云端吗?", "不会。系统只保存余额/配额数值与配置, 不保存任何 Key/Cookie。真实的余额查询由你电脑上的本地代理(额度管家_本地代理.py)完成, Key 混淆后仅存在本机 accounts_config.json。"],
  ["趋势图的数据哪来的?", "每次采集/同步都会写一条快照。按量账号记余额(元), 相邻两次采集的余额下降额累加=真实消耗(余额上升视为充值, 不计), 按区间中点归到某一天。订阅账号没有「每日消耗」曲线: 月费是固定的, 金额只按天摊销(月费÷当月天数), 每天的线是平的、没有信息量, 所以趋势页只画按量 API。订阅的额度窗口(5小时/7天/月度)请到「当前总览 → 订阅额度窗口」面板看。不是估算。"],
  ["账目统计(自然月/自然年)是怎么算的?", "在「账目统计」页: 按量付费账号的消耗 = 相邻两次采集的「余额下降额」累加, 充值(余额上升)自动剔除; 每笔消耗按发生时间归属到对应自然月, 跨天、跨月的消耗都能正确归月, 不会漏算。订阅账号的消耗 = 月费按天摊销(月费 × 当月实际在订天数 ÷ 当月总天数), 月中订阅不虚增整月。免费/赠送额度不计入金额。统计完全基于系统自动累积的余额快照, 不依赖任何平台接口, 全程自动、无需手工记账; 系统运行越久, 账目越完整。\n\n订阅金额只跟时间走, 与配额窗口(5小时/7天/月)完全解绑——这是 V3.17.37 定稿口径: 无论你本周用了多少额度, 本月摊销的钱都一样多。配额窗口只是「限流提醒」, 不影响账单。\n\n删除账号是「软删除」: 账号从列表移除, 但它的历史余额快照会保留并继续参与统计, 所以你看到的历史花销不会因为删掉一个账号而凭空减少。"],
  ["总投入额度是怎么算的? 累计消耗是什么?", "「账目统计」页顶部的总投入默认自动计算 = 各账号「累计充值」之和(按量=历史充值总额; 订阅=实际已付: 年付(截止日距今超3个月)按 月费×12 一次性计入, 月付滚动按 已计费月数×月费)。也可以点「手动调整」覆盖成任意值, 再点「恢复自动」即可回到自动口径。\n\n总投入旁边还有两个联动数字: 累计消耗 = 各账号累计消耗之和; 本月消耗 = 各账号自然月1号以来消耗之和。三个数字与「模型API管理」逐账号完全同口径 —— 只要账号列表里的数是对的, 总账目就不会错。\n\n「模型API管理」里每个账号的三个金额口径: 本月消耗 = 自然月 1 号以来的真实消耗(按量=余额下降累加; 订阅=月费×本月在订天数÷当月天数); 累计充值 = 实际已付的钱(按量=历史充值; 订阅: 年付=月费×12, 月付=已计费月数×月费); 累计消耗 = 实际用掉的钱(按量=累计充值−剩余余额; 订阅=月费按天摊销累计, 从「订阅开始日」起算)。编辑账号里填上真实订阅日开始算得准, 留空则按最早采集记录估算。例: Kimi Coding Plan 月费159, 2026-09-04 起订, 到 10-26 已摊 52+31=83 天 → 累计消耗 ≈ 159×(27/30) + 159 + 159×(26/31) = 143.10+159+133.35 = 435.45。"],
  ["订阅制账号的消耗怎么显示?", "订阅制月费固定, 金额只按天摊销(V3.17.37 定稿): 「本月消耗」列显示 本月 ¥X(按天摊) = 月费×本月在订天数÷当月天数; 「剩余额度」列显示 ¥剩余价值 = 已付−已摊。配额%(5小时/7天/月度)只在「当前总览 → 订阅额度窗口」面板展示, 不进账号列表、不进金额计算、不进趋势图。免费档(月费 0)显示「免费额度(单价 0)」。"],
  ["数据安全吗? 会被别的网站读到吗?", "不会。本服务已加了四道防线: ① 只接受「与本次访问地址同源」的页面请求, 其它网站跨域访问一律拒绝(挡 CSRF 与本地恶意网页); ② 手机/外网访问必须输入「访问口令」(可在本页查看和修改), 在电脑本机打开则免口令; ③ 口令用 URL 传一次后立即写入 Cookie 并重定向到无口令地址, 不会残留在地址栏和历史里; ④ 更新包与上传接口都不允许触碰核心文件(账本 data.json、主程序等), 更新清单里的 data.json 会被直接拒绝。另外每次写入都是原子操作并自动留一份 data.json.bak, 坏文件会被自动识别并从备份恢复, 不会再出现「一次意外全丢」。"],
  ["更新包会覆盖我的数据吗?", "不会。更新包里的核心文件(账本 data.json、账号配置、主程序等)一律受保护, 即使清单里写了也会被跳过; 只更新 dist 前端产物、版本号等程序文件。旧文件(含被删文件)会先备份到 备份/V{旧版本}/ 再替换。"],
  ["手机能看吗? 怎么弄?", "默认不能——软件只允许本机访问, 手机直接打开连不上, 这是刻意的安全设计(数据只留在你这台电脑, 不暴露到网络上)。\n\n确实想在手机上看, 需要自己配一个内网穿透工具(把本机 8788 端口映射出去, 如 frp / Tailscale / ZeroTier), 属进阶操作。\n\n⚠️ 只要对公网开放了端口, 知道地址的人就能看到你的余额数据, 请只在自己可控的网络里用。"],
  ["为什么 GPT/Claude/Gemini 加进来查不到余额?", "因为这些平台的服务器在境外，能否直连取决于你本地的网络环境——软件本身不提供、也不代理任何网络服务。\n\n两种解决办法：① 如果你有第三方 API 中转服务，添加账号时把地址填进「API 地址」栏；② 如果电脑本来就能访问，直接添加即可，连不上再填中转地址。\n\n注意：DeepSeek、Kimi、智谱、MiniMax、阿里百炼、火山方舟这些国内平台不需要任何特殊网络。另外海外平台余额多是美元计价，币种记得选 USD（添加时已自动默认）。"],
  ["微信预警怎么配? 收不到怎么办?", "步骤: 「当前总览 → 预算预警 → 微信预警」打开开关 → 选推送方式 → 填地址 → 点「发一条测试消息」。\n\n地址怎么拿（注意新版改名）: 手机或电脑企业微信里建一个**内部群**（只有自己也行, 但群里至少要有 1 个真人）→ 进群 → 右上角「···」→ **「消息推送」**（v4.1.41 前叫「群机器人」）→ 添加 → 新建 → 复制 Webhook 地址, 粘进软件。\n\n找不到入口? ① 新版叫「消息推送」不是「群机器人」; ② 必须是内部群（外部群/上下游群/家校群不支持）; ③ 你必须是群主或群管理员; ④ 自建团队可去 work.weixin.qq.com 管理后台 → 应用管理 → 消息推送, 检查是否停用。\n\n收不到的常见原因: ① 地址粘错或过期（重新复制一次）; ② 企业微信机器人被移出群; ③ 软件没在运行（预警靠软件后台跑，关掉软件就不监控了）; ④ 阈值设太高，实际还没触发。"],

  ["Windows 安全中心 / Defender 把我的程序拦了怎么办?", "新发布且未购买代码签名证书的程序可能触发 SmartScreen 或 Defender 提示。先确认安装包来自本人发送，并核对 SHA256；来源不明或校验不一致时不要允许运行，也不要关闭全部安全防护。确认无误后按提示类型处理：\n\n**① 文件属性里有「解除锁定」**\n右键安装包 → 属性 → 勾选「解除锁定」→ 确定 → 再运行。\n\n**② 蓝框提示「Windows 已保护你的电脑」**\n点「更多信息」→ 核对发布者和文件来源 → 点「仍要运行」。\n\n**③ 文件被隔离或删除**\nWindows 安全中心 → 病毒和威胁防护 → 保护历史记录 → 找到对应记录，核对路径后选择「允许在设备上」，再重新安装。若记录显示为实际恶意软件，或无法确认是否为本程序，不要放行。\n\n**④ 同一程序反复被误拦**\nWindows 安全中心 → 病毒和威胁防护 → 管理设置 → 排除项 → 添加或删除排除项 → 添加「文件夹」→ 只选择本软件的安装目录（例如 D:\\AI额度管家）。排除项会降低该目录的防护范围，不要选择整个磁盘、桌面或下载目录。\n\n**⑤ 提示「受控文件夹访问」阻止写入**\nWindows 安全中心 → 病毒和威胁防护 → 勒索软件防护 → 管理勒索软件防护 → 通过受控文件夹访问允许应用 → 添加本软件安装目录下的 python\\pythonw.exe。\n\n**⑥ 使用 360、火绒等第三方安全软件**\n在对应软件中核对拦截记录，仅把确认无误的本程序或安装目录加入信任区。\n\n**核对文件完整性**：如果发送包附有「文件校验.txt」，可在命令行运行 certutil -hashfile 安装包文件名 SHA256，与文件中的值逐字比较；一致说明传输后文件未发生变化。"],
];

type UpdateInfo = {
  current?: string; latest?: string; has_update?: boolean;
  date?: string; notes?: string[]; detail?: string; package_zip?: string;
};


/** 把日志里的 markdown 标记转成界面元素: **加粗** → <b>, `代码` → 等宽 */
function renderNote(text: string) {
  const parts: (string | { b: string } | { c: string })[] = [];
  const re = /\*\*(.+?)\*\*|`([^`]+)`/g;
  let last = 0, m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    if (m[1] != null) parts.push({ b: m[1] });
    else parts.push({ c: m[2] });
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts.map((p, i) => {
    if (typeof p === "string") return <span key={i}>{p}</span>;
    if ("b" in p) return <b key={i} className="text-slate-700">{p.b}</b>;
    return <code key={i} className="text-[11px] bg-slate-100 px-1 rounded">{p.c}</code>;
  });
}

function UpdateCard() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [curVer, setCurVer] = useState("");
  const [curNotes, setCurNotes] = useState<string[]>([]);
  const [curDate, setCurDate] = useState("");
  // 版本历史(近期更新, 只展示最近几条)
  const [hist, setHist] = useState<{ version: string; date: string; title: string; notes: string[] }[]>([]);

  useEffect(() => {
    fetch("/api/version?t=" + Date.now()).then((r) => r.json())
      .then((d) => {
        setCurVer(d.version || "");
        setCurDate(d.date || "");
        setCurNotes(Array.isArray(d.notes) ? d.notes : []);
      }).catch(() => {});
    fetch("/api/version/history?t=" + Date.now()).then((r) => r.json())
      .then((d) => setHist(Array.isArray(d.items) ? d.items : []))
      .catch(() => {});
    check(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const check = async (silent = false) => {
    setBusy(true); if (!silent) setMsg("正在检查更新...");
    try {
      const r = await fetch("/api/update/check?t=" + Date.now());
      const d = await r.json();
      setInfo(d);
      if (!silent) setMsg(d.detail ? String(d.detail) : (d.has_update ? `发现新版本 V${d.latest}` : `当前已是最新版本 V${d.current}`));
    } catch {
      if (!silent) setMsg("检查更新请求失败");
      setInfo(null);
    }
    setBusy(false);
  };

  const apply = async () => {
    setBusy(true); setMsg("正在应用更新...");
    try {
      const r = await fetch("/api/update/apply", { method: "POST" });
      const d = await r.json();
      if (d.ok) {
        // V3.17.41 起主程序更新后会自动重启, 页面 8 秒后自动刷新, 全程无需手动操作
        setMsg("✅ " + String(d.detail ?? "") + "　页面将在 8 秒后自动刷新…");
        setTimeout(() => window.location.reload(), 8000);
        return;
      }
      setMsg(String(d.detail ?? ""));
    } catch {
      setMsg("更新请求失败");
    }
    setBusy(false);
  };

  const upload = async (file: File) => {
    setBusy(true); setMsg("正在上传并应用更新...");
    try {
      const r = await fetch("/api/update/upload?name=" + encodeURIComponent(file.name), { method: "POST", body: file });
      const d = await r.json();
      if (d.ok) {
        setMsg("✅ " + String(d.detail ?? "") + "　页面将在 8 秒后自动刷新…");
        setTimeout(() => window.location.reload(), 8000);
        return;
      }
      setMsg("⚠️ " + String(d.detail ?? ""));
    } catch {
      setMsg("上传失败");
    }
    setBusy(false);
  };
  return (
    <div className="bg-white rounded-xl p-6 shadow-sm">
      <div className="flex items-center justify-between mb-3">
        <div className="font-semibold">版本与更新</div>
        <div className="flex gap-2 items-center">
          <span className="text-xs text-slate-400">当前 V{curVer}</span>
          <button onClick={() => check()} disabled={busy}
            className="px-3 py-1.5 text-sm rounded-lg border border-[#1B6B7A] text-[#1B6B7A] hover:bg-[#1B6B7A]/5 disabled:opacity-50">
            检查目录更新包
          </button>
          {info?.has_update && (
            <button onClick={apply} disabled={busy}
              className="px-3 py-1.5 text-sm rounded-lg bg-[#1B6B7A] text-white hover:bg-[#155a66] disabled:opacity-50">
              ⬇ 一键更新到 V{info.latest}
            </button>
          )}
        </div>
      </div>

      {/* 系统更新: 直接上传更新包 */}
      <div className="flex gap-3 items-center flex-wrap border-t border-slate-100 pt-3">
        <label className="text-sm">
          系统更新——上传更新包:
          <input type="file" accept=".zip" className="ml-2 text-[13px]"
            disabled={busy}
            onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.currentTarget.value = ""; }} />
        </label>
        <span className="text-xs text-slate-400">支持增量更新包与完整安装包, 旧文件自动备份</span>
      </div>

      {msg && <div className="mt-3 text-sm px-3 py-2 rounded-lg bg-slate-50 text-slate-600">{msg}</div>}

      {/* 本版本更新内容: 无论有没有新版本都显示, 方便回看改了什么 */}
      <div className="mt-3 rounded-lg bg-slate-50 px-4 py-3">
        <div className="flex items-baseline justify-between flex-wrap gap-2">
          <div className="text-[13px] font-medium text-slate-700">
            {info?.has_update
              ? <>发现新版本：V{info.current} → <b className="text-[#1B6B7A]">V{info.latest}</b>{info.date ? `（${info.date}）` : ""}</>
              : <>本版本更新内容：<b>V{curVer}</b>{curDate ? `（${curDate}）` : ""}</>}
          </div>
          <span className="text-[11px] text-slate-400">更新包来源：程序目录 / 本页上传</span>
        </div>
        {(() => {
          // V3.33.6: 只有「确实有新版本」时才显示更新包里的 notes;
          // 否则一律用当前版本自己的说明(否则会显示目录里历史更新包的旧内容)
          const list = (info?.has_update ? info.notes : (curNotes.length ? curNotes : (info?.notes || []))) || [];
          return list.length > 0 ? (
            <ul className="list-disc pl-5 mt-2 space-y-1 text-[13px] leading-relaxed text-slate-600">
              {list.map((n: string, i: number) => <li key={i}>{n}</li>)}
            </ul>
          ) : (
            <div className="text-[13px] text-slate-400 mt-2">把「更新包_Vx.x.zip」放进程序目录后点「检查目录更新包」, 或直接在上方上传, 即可一键升级。</div>
          );
        })()}
        <div className="text-[11px] text-slate-400 mt-3 border-t border-slate-200 pt-2 leading-relaxed">
          更新只覆盖程序文件与前端页面，账号配置和你录入的账本数据不受影响；旧文件自动备份到「备份/」。更新后主程序会自动重启、页面自动刷新，全程无需手动操作。
        </div>
      </div>

      {/* 近期版本: 只留最近 3 条(完整开发史不给客户看, 需要时看 version_history.json) */}
      {hist.length > 0 && (
        <div className="mt-3 rounded-lg border border-slate-200 bg-white px-4 py-3">
          <div className="text-[13px] font-medium text-slate-700 mb-2">近期更新</div>
          {hist.slice(0, 3).map((it) => (
            <div key={it.version} className="py-2 border-t border-slate-100 first:border-t-0">
              <div className="flex items-baseline gap-2 flex-wrap">
                <span className="text-[13px] font-semibold text-[#1B6B7A]">V{it.version}</span>
                {it.date && <span className="text-[11px] text-slate-400">{it.date}</span>}
                {it.title && <span className="text-[12px] text-slate-600">{it.title}</span>}
              </div>
              {it.notes && it.notes.length > 0 && (
                <ul className="list-disc pl-5 mt-1 space-y-0.5 text-[12px] leading-relaxed text-slate-500">
                  {it.notes.slice(0, 4).map((n, i) => <li key={i}>{renderNote(n)}</li>)}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Help() {
  return (
    <div className="space-y-4">
      <LicenseCard />

      {/* 数据与安全: 密钥保护(开关) + 数据位置 合一, 一屏说清 */}
      <div className="bg-white rounded-xl p-6 shadow-sm space-y-5">
        <div className="font-semibold">数据与安全</div>
        <SecurityCard />
        <div className="border-t border-slate-100 pt-4">
          <StorageCard />
        </div>
      </div>

      {/* 微信预警已迁到「当前总览 → 预算预警」页(V3.32.4) */}
      <OverseasCard />

      <UpdateCard />

      <CurrencyCard />

      <div className="bg-white rounded-xl p-6 shadow-sm">
        <div className="font-semibold mb-3">常见问题</div>
        {FAQ.map((x) => (
          <div key={x[0]} className="py-3 border-t border-slate-100 first:border-0">
            <div className="font-medium mb-1">{x[0]}</div>
            <div className="text-[13px] text-slate-500 leading-relaxed whitespace-pre-line">{x[1]}</div>
          </div>
        ))}
      </div>

      <div className="bg-white rounded-xl p-6 shadow-sm">
        <PricingCard />
      </div>

      <div className="bg-white rounded-xl p-6 shadow-sm">
        <QuitCard />
      </div>
    </div>
  );
}

function CurrencyCard() {
  const utils = trpc.useUtils();
  const q = trpc.quota.getCurrency.useQuery();
  const setCur = trpc.quota.setCurrency.useMutation({
    onSuccess: (d) => {
      setDisplayCurrency(d.displayCurrency);
      utils.quota.getCurrency.invalidate();
      utils.quota.summary.invalidate();
      utils.quota.ledger.invalidate();
      utils.quota.getTotalQuota.invalidate();
    },
  });
  const cur = q.data?.displayCurrency || "CNY";
  const rates = q.data?.rates || {};
  const symbols = q.data?.symbols || {};
  const sample = (Object.keys(rates) as string[]).filter((c) => c !== cur).slice(0, 5);
  return (
    <div className="bg-white rounded-xl p-6 shadow-sm">
      <div className="font-semibold mb-1">显示设置</div>
      <div className="text-[13px] text-slate-500 mb-4">
        账号金额永远按各自币种保存（原币），这里只决定「汇总数字」用哪种货币显示。
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <label className="text-sm">显示货币
          <select className="ml-2 border rounded-lg px-2 py-1.5 text-sm" value={cur}
            disabled={setCur.isPending}
            onChange={(e) => setCur.mutate({ displayCurrency: e.target.value })}>
            {Object.keys(rates).length === 0 && <option value={cur}>{cur}</option>}
            {(Object.keys(rates) as string[]).map((c) => (
              <option key={c} value={c}>{c} {symbols[c] || ""}</option>
            ))}
          </select>
        </label>
        <span className="text-[12px] text-slate-400">
          汇率表更新于 {q.data?.updated || "—"}（内置静态表，量级参考足够）
        </span>
      </div>
      <div className="mt-3 text-[12px] text-slate-400 leading-relaxed">
        示例：1 美元 ≈ {fmtMoney(Number(rates["USD"] || 7.1) / Number(rates[cur] || 1), cur)}
        {sample.length > 0 && <> ；其他：{sample.map((c) => `${c} ${fmtMoney(Number(rates[c]) / Number(rates[cur] || 1), cur)}`).join("、")}</>}
      </div>
    </div>
  );
}

type StorageInfo = {
  dataHome: string; programDir: string;
  migratedThisRun?: string[]; legacyLeft?: string[];
  files?: { name: string; size: number }[];
  note?: string;
};

type Sec = {
  ok?: boolean; encryptAccounts: boolean; dpapiAvailable: boolean;
  path: string; size: number; decryptError?: string; decryptFailed?: boolean;
  error?: string;
};

function SecurityCard() {
  const [sec, setSec] = useState<Sec | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const load = () => fetch("/api/security", { cache: "no-store" })
    .then((r) => r.json()).then(setSec).catch(() => {});
  useEffect(() => { load(); }, []);

  const toggle = async () => {
    if (!sec) return;
    const on = !sec.encryptAccounts;            // 开→关 / 关→开, 一个按钮双向
    setBusy(true); setMsg("");
    try {
      const r = await fetch("/api/security", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ encryptAccounts: on }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || "操作失败");
      setSec(d);
      setMsg(on ? "已开启加密 ✓" : "已切回明文（原密文自动留备份）");
    } catch (e) {
      setMsg("失败：" + (e instanceof Error ? e.message : String(e)));
    }
    setBusy(false);
  };

  if (!sec) return null;
  const enc = sec.encryptAccounts;
  return (
    <div className="flex items-center justify-between gap-3">
      <div className="min-w-0">
        <div className="text-sm font-medium">密钥保护</div>
        <div className="text-[12px] mt-0.5 text-slate-500">
          {enc
            ? <span className="text-emerald-600">已加密 —— Key 文件只有本机能解开</span>
            : <span className="text-slate-400">未加密（明文存放）</span>}
        </div>
        {msg && <div className="text-[12px] mt-0.5 text-slate-500">{msg}</div>}
        {enc && (
          <div className="text-[11px] mt-1 text-amber-600">
            换电脑前先在这里关闭加密再迁移数据
          </div>
        )}
      </div>
      <button onClick={toggle} disabled={busy || !sec.dpapiAvailable || sec.decryptFailed}
        className={`relative shrink-0 w-12 h-7 rounded-full transition-colors disabled:opacity-40 ${enc ? "bg-emerald-500" : "bg-slate-300"}`}>
        <span className={`absolute top-0.5 left-0.5 w-6 h-6 rounded-full bg-white shadow transition-transform ${enc ? "translate-x-5" : ""}`} />
      </button>
    </div>
  );
}

type Rule = {
  id: string; type: string; scope: string[]; threshold: number; enabled: boolean; note?: string;
};
type RuleType = {
  id: string; label: string; unit: string; default: number; scopes: string[];
};
type Notify = {
  enabled: boolean; channel: string;
  hasWebhook: boolean; hasSendKey: boolean;
  burnThresholdPerHour: number; lowBalanceThreshold: number;
  lowQuotaPercent: number; cooldown: number; pollSeconds: number;
  rules?: Rule[]; ruleTypes?: RuleType[];
};

/** 退出软件(V3.32.2): 明确的退出口, 不依赖右下角托盘图标是否正常 */
function QuitCard() {
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const quit = async () => {
    if (!confirm("确定要完全退出 AI额度管家 吗？\n\n退出后：\n· 软件停止运行，不再自动采集余额\n· 手机/远程访问会断开\n\n下次双击桌面图标即可重新启动")) return;
    setBusy(true);
    setMsg("正在退出…");
    try {
      await fetch("/api/app/quit", { method: "POST" });
      setMsg("已退出。可以关闭这个窗口了。");
    } catch {
      setMsg("已退出（连接已断开）。");
    }
    setBusy(false);
  };

  return (
    <div>
      <div className="font-semibold mb-1">退出软件</div>
      <div className="text-[12px] text-slate-500 leading-relaxed mb-2">
        关闭窗口只是收进右下角托盘（软件继续在后台采集）。要彻底停止，请点下面的按钮，
        或者右键任务栏右下角的软件图标 → 退出。
      </div>
      <button onClick={quit} disabled={busy}
        className="px-3 py-1.5 text-[12px] rounded-lg border border-red-300 text-red-600 hover:bg-red-50 disabled:opacity-50">
        {busy ? "正在退出…" : "完全退出软件"}
      </button>
      {msg && <span className="text-[12px] text-slate-500 ml-2">{msg}</span>}
    </div>
  );
}

function OverseasCard() {
  return (
    <div className="bg-white rounded-xl p-6 shadow-sm">
      <div className="font-semibold mb-2">海外平台（GPT / Claude / Gemini 等）需要你自备网络</div>
      <div className="text-[13px] text-slate-600 leading-relaxed space-y-2">
        <p>
          <b>OpenAI（GPT）、Claude、Gemini、Grok、Mistral</b> 这些平台的服务器在境外，
          能否直连取决于你本地的网络环境。软件本身不提供、也不代理任何网络服务，
          需要你自己准备能访问它们的网络环境。
        </p>
        <div className="bg-slate-50 rounded-lg px-3 py-2.5 space-y-1.5">
          <div className="font-medium text-slate-700 text-[12px]">两种解决办法（任选其一）：</div>
          <div className="text-[12.5px]">
            <b>① 填中转地址（最简单）</b>：如果你买了第三方 API 中转/代理服务，
            在添加账号时把地址填进<b>「API 地址」</b>那一栏（如 <code className="text-[11px] bg-white px-1 rounded">https://你的中转站.com</code>），
            软件就会走这个地址查余额。
          </div>
          <div className="text-[12.5px]">
            <b>② 本机已有代理</b>：如果你电脑上本来就能访问（比如开了某个代理软件），
            一般不用改任何设置，直接添加账号即可；连不上再填中转地址。
          </div>
        </div>
        <p className="text-[12px] text-slate-500">
          说明：<b>国内平台</b>（DeepSeek、Kimi、智谱、MiniMax、阿里百炼、火山方舟、硅基流动等）
          一般直接填 Key 就能用，「API 地址」那一栏留空即可。
        </p>
        <p className="text-[12px] text-slate-500">
          另外：海外平台的余额一般用<b>美元</b>计价，添加账号时币种选 USD 即可（默认已自动切好），
          总览里会按「显示货币」统一换算展示。
        </p>
      </div>
    </div>
  );
}

function StorageCard() {
  const [info, setInfo] = useState<StorageInfo | null>(null);
  const [openMsg, setOpenMsg] = useState("");
  useEffect(() => {
    fetch("/api/storage", { cache: "no-store" })
      .then((r) => r.json()).then(setInfo).catch(() => {});
  }, []);
  if (!info) return null;
  const migrated = (info.migratedThisRun ?? []).length > 0;
  const openDir = async () => {
    setOpenMsg("");
    try {
      const r = await fetch("/api/storage/open", { method: "POST" });
      const j = await r.json();
      setOpenMsg(j.ok ? "已打开" : "打开失败，请手动前往上方路径");
    } catch {
      setOpenMsg("打开失败，请手动前往上方路径");
    }
    window.setTimeout(() => setOpenMsg(""), 3000);
  };
  return (
    <div>
      <div className="text-sm font-medium mb-1">数据位置</div>
      <div className="flex items-center gap-2">
        <div className="text-[12px] font-mono bg-slate-50 rounded-lg px-3 py-2 break-all flex-1">{info.dataHome}</div>
        <button onClick={openDir}
          className="shrink-0 px-3 py-2 text-[12px] rounded-lg bg-slate-800 text-white hover:bg-slate-700 transition-colors">
          打开目录
        </button>
      </div>
      {openMsg && <div className="text-[12px] text-emerald-600 mt-1">{openMsg}</div>}
      {migrated && (
        <div className="text-[12px] text-emerald-600 mt-2">
          本次启动已自动迁移旧数据 ✓（{(info.migratedThisRun ?? []).join("、")}）
        </div>
      )}
      <div className="text-[12px] text-slate-400 mt-2">
        备份软件 = 备份这个文件夹（Key 和账本都在里面）；更新程序不会动它。
      </div>
    </div>
  );
}

type Lic = {
  state: string; source?: string; exp?: string | null; daysLeft: number;
  name?: string; plan?: string; trialEnd?: string | null;
  readonly: boolean; graceDays: number; trialDays: number; invalidCode?: boolean;
  stacked?: boolean;
};

const LIC_LABEL: Record<string, { text: string; cls: string }> = {
  licensed: { text: "已授权", cls: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  open: { text: "开源版 · 全功能", cls: "bg-emerald-50 text-emerald-700 border-emerald-200" },
};

function LicenseCard() {
  // 开源版(V3.34.0): 无授权门禁, 本卡片仅展示开源协议信息
  return (
    <div className="bg-white rounded-xl p-6 shadow-sm">
      <div className="flex items-center gap-3 mb-2">
        <div className="font-semibold">授权</div>
        <span className={`text-[12px] px-2 py-0.5 rounded-full border ${LIC_LABEL.open.cls}`}>
          {LIC_LABEL.open.text}
        </span>
      </div>
      <div className="text-[13px] text-slate-600 leading-relaxed">
        本软件以 MIT 协议开源，所有功能永久开放，没有试用期与激活码。
      </div>
      <div className="text-[11px] text-slate-400 mt-3 leading-relaxed">
        数据全部保存在本机；更新包经 Ed25519 签名校验，防止传输中被篡改。
      </div>
    </div>
  );
}

function PricingCard() {
  const [data, setData] = useState<Pricing>(DEFAULT_PRICING);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [checks, setChecks] = useState<NonNullable<RefreshResult["checks"]>>([]);
  const [checkedAt, setCheckedAt] = useState<string>("");   // 上次联网核查时间

  const load = async () => {
    try {
      const r = await fetch("/api/pricing?t=" + Date.now());
      if (r.ok) {
        const d = await r.json();
        if (Array.isArray(d.payg) && d.payg.length) setData(d);
        if (d.checkedAt) setCheckedAt(d.checkedAt);
      }
    } catch {
      /* 保持默认 */
    }
  };
  useEffect(() => { load(); }, []);

  const refresh = async () => {
    setBusy(true); setMsg("正在联网核查各平台官网政策页，约需 5-10 秒...");
    try {
      const r = await fetch("/api/pricing/refresh", { method: "POST" });
      const d: RefreshResult = await r.json();
      // 无论本地/远程, 都把接口返回的最新完整数据立刻重绘(修复"点了没反应")
      if (d.data && Array.isArray(d.data.payg) && d.data.payg.length) setData(d.data);
      else await load();
      setChecks(d.checks ?? []);
      // 「上次核查时间」优先用 refresh 返回的; 没有就用本地当前时间兜底
      setCheckedAt(d.checkedAt || new Date().toLocaleString("zh-CN", { hour12: false }));
      setMsg((d.ok ? "" : "⚠️ ") + String(d.detail || ""));
    } catch {
      setMsg("请求失败：连不上本机服务");
    }
    setBusy(false);
  };

  const CHECK_STYLE: Record<string, { icon: string; cls: string; text: string }> = {
    same: { icon: "✓", cls: "bg-emerald-50 text-emerald-700 border-emerald-200", text: "无变化" },
    changed: { icon: "⚠", cls: "bg-amber-50 text-amber-700 border-amber-300", text: "有变动" },
    new: { icon: "·", cls: "bg-slate-50 text-slate-600 border-slate-200", text: "首次核查" },
    shell: { icon: "·", cls: "bg-slate-50 text-slate-400 border-slate-200", text: "可访问" },
    fail: { icon: "✗", cls: "bg-red-50 text-red-600 border-red-200", text: "打不开" },
  };

  return (
    <>
      <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
        <div className="font-semibold">API 渠道收费政策速览</div>
        <div className="flex items-center gap-3 text-xs">
          {/* V3.32.8: 只显示一个时间 —— 检查更新后同步为本次核查时间, 避免两个时间不同步造成困惑 */}
          <span className="text-slate-400">数据更新于 {checkedAt ? checkedAt.replace("T", " ").slice(0, 16) : data.updated}</span>
          <button onClick={refresh} disabled={busy}
            className="px-2.5 py-1 rounded-lg border border-[#1B6B7A] text-[#1B6B7A] hover:bg-[#1B6B7A]/5 disabled:opacity-50">
            {busy ? "核查中..." : "检查政策更新"}
          </button>
        </div>
      </div>
      {msg && <div className="mb-3 text-xs px-3 py-2 rounded-lg bg-slate-50 text-slate-600 leading-relaxed">{msg}</div>}
      {checks.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          {checks.map((c) => {
            const st = CHECK_STYLE[c.status] ?? CHECK_STYLE.same;
            return (
              <a key={c.platform + c.url} href={c.url} target="_blank" rel="noreferrer"
                title={`${c.platform}：${st.text}${c.detail ? "（" + c.detail + "）" : ""}　点击打开官网政策页`}
                className={`text-[11px] px-2 py-0.5 rounded border hover:opacity-80 ${st.cls}`}>
                {st.icon} {c.platform}
              </a>
            );
          })}
        </div>
      )}
      <div className="font-medium text-sm mb-2">一、按量付费 API(余额制, 录入「按量付费 API」类型)</div>
      <Table heads={["平台", "API入口", "代表价格", "免费额度与优惠", "政策地址"]} rows={data.payg} links />
      <div className="font-medium text-sm mt-5 mb-2">二、订阅制 API(配额制 / Coding Plan 类, 录入「订阅制 API」类型)</div>
      <Table heads={["平台", "套餐档位(元/月)", "配额窗口与重置", "续费优惠", "政策地址"]} rows={data.sub} links />
      <div className="text-xs text-slate-400 mt-3 leading-relaxed">{data.note}{data.urls_note ? " " + data.urls_note : ""} 点「检查政策更新」会重新读取政策数据并联网核查各平台官网政策页是否变动; 也可配置远程政策源自动更新。</div>
    </>
  );
}
