# AI额度管家（ai-quota-ledger）

一个跑在**你自己电脑上**的 AI 平台余额与花费账本：把 DeepSeek、Kimi、智谱、MiniMax、硅基流动、火山方舟、阿里百炼、OpenAI、Claude、Gemini 等平台的余额和配额放进一屏，自动记账、趋势分析、预算预警。

> 定位是「判断账本」而不是行情终端：核心回答三个问题——**还有多少钱、这个月花了多少、什么时候快用完**。

## 特性

- **本机运行，数据不出电脑**：账本、API Key 全部存在本机 `data/` 目录，没有任何云端账号
- **自动采集**：支持余额接口的平台全自动查询，按「余额下降额」累计真实消耗
- **订阅摊销**：包月账号（ChatGPT Plus、Claude Pro 等）按 月费÷当月天数 计入每日成本
- **多币种**：存储永远原币（CNY/USD/EUR/JPY/HKD/GBP），换算只在展示层
- **预算与微信预警**：余额不足 / 消耗异常 / 每日日报，推送到企业微信或 Server酱
- **价格政策速览**：内置各平台价格政策页定期核查（ Robots 规则内、低频）
- **桌面体验**：pywebview 托盘壳，关窗收托盘、退出才真退；也支持纯浏览器模式
- **安全设计**：同源校验防 CSRF、更新包 Ed25519 签名校验、原子写盘 + 自动备份、可选 DPAPI 密钥加密

## 快速开始（Windows）

```
1. 安装 Python 3.10+（或使用内置便携 Python 的发行包）
2. 双击 启动AI额度管家.bat        （或: python 启动额度管家.py）
3. 浏览器自动打开 http://127.0.0.1:8788
4. 「模型API管理 → 添加API账号」填入平台的 API Key
```

Linux/macOS：`./启动额度管家.sh`（探测 python3、支持 --daemon 后台）。

### 从源码跑

```bash
# 前端（需要 Node 18+）
cd app && npm install && npm run build

# 后端（仅标准库，无 pip 依赖）
python 启动额度管家.py
```

开发模式（前端热重载）：`cd app && npm run dev`，后端会自动代理。

## 平台支持说明

| 类型 | 平台 | 采集方式 |
|---|---|---|
| 全自动 | DeepSeek、Kimi、硅基流动、OpenRouter、Mistral、阿里百炼(AK/SK)、火山方舟(Agent Plan/按量AK/SK)、MiniMax/智谱套餐 | 官方余额接口，定时查询 |
| 探活+手填 | OpenAI、Claude、Gemini、Grok、ChatAnywhere、智谱/MiniMax按量 | 官方无余额接口，验证 Key 有效后由用户手动录入余额，按两次差额计消耗 |
| 订阅制 | ChatGPT Plus、Claude Pro、Gemini Advanced 等 | 填月费，按天摊销 |

**为什么有些平台只能手填？** 这些平台的官方 API 就不提供余额查询，任何第三方软件都查不到——这不是本项目的缺陷，是平台的边界。admin Key（`sk-ant-admin` / `sk-admin-`）可额外自动读取成本报表。

## 数据与安全

- 所有数据在安装目录 `data/`：`data.json`（账本）、`accounts_config.json`（Key，可选 DPAPI 加密）、`balance_history.jsonl`（趋势数据）
- 备份 = 复制整个 `data/` 目录；每次写入自动留 `.bak`
- 本服务只监听 `127.0.0.1`，手机访问需自行配置内网穿透（frp/Tailscale 等），风险自担
- 远程访问可设口令；更新包必须通过 Ed25519 签名校验才会应用

## 目录结构

```
├── 启动额度管家.py        # 主程序（HTTP + tRPC 风格 API + 定时采集）
├── 桌面壳.pyw             # pywebview 托盘壳（可选）
├── 本地代理/              # 各平台余额适配器（余额接口封装）
├── plat/                  # 平台适配层（数据目录/自启动/开窗，Win/Linux/macOS）
├── app/                   # React + Vite + TypeScript 前端
├── tests/                 # 离线单元测试（无需联网）
└── tools/                 # ed25519 参考实现（更新包签名自测）
```

## 开发

```bash
python -m pytest tests/ -v     # 或直接逐个运行: python tests/test_adapters.py
cd app && npm run build        # 改前端后重新构建, 产物在 app/dist/public
```

测试不联网（适配器测试用本地桩），直接 import 代理模块的测试会自动隔离 `CONFIG_PATH`。

## 许可证

[MIT](LICENSE) © 2026

## 免责声明

本项目仅供个人记录与研究使用。各平台名称与接口归 respective owners 所有；使用本工具查询余额请遵守各平台的服务条款与 API 频率限制。
