# Zero — Automated Binance Futures Trading Pipeline

A fully automated futures trading pipeline that runs on **Termux (Android)**:

**snapshot → LLM decision → guard validation → signed orders → full audit log**

Defaults to Binance **testnet**. Validate thoroughly before considering a real account.

> ⚠️ **DISCLAIMER** — This project is for educational and research purposes only. Cryptocurrency futures trading is extremely high risk and may result in total loss of capital. The author accepts no liability for any financial loss arising from use of this code. **Always validate on testnet first.**

[中文版见下方 ↓](#中文版)

---

## Why this exists

Most "AI trading bot" projects are a thin wrapper around an exchange SDK, and they break the moment the exchange changes an endpoint. This one is built around a different premise:

**An LLM proposes; a deterministic guard disposes.**

The model is never trusted with money directly. Every decision it produces is checked against hard constraints before a single byte hits the wire — and if the check fails, nothing happens. The whole chain is designed so that when something goes wrong, you can read a log and know exactly why.

## Architecture

| Layer | File | Responsibility |
|---|---|---|
| 1. Data | `snapshot.py` | Fetch market data + account state → `latest_*.json` |
| 2. Decision | `decide.py` | Feed snapshot to LLM, receive decision JSON |
| 3. Guard | `guard.py` | Hard constraints + format checks; rejects anything invalid |
| 4. Execution | `executor.py` | Hand-rolled HMAC signing, places orders incl. SL/TP + exit ladder |
| 5. Trigger | `volwatch.py` | Hourly fallback + volatility-threshold early wake + exit patrol |

### Exit management (v4)

After a position opens, the executor places a **4-order exit ladder** in one shot:

| # | Order | Trigger | Role | Qty |
|---|-------|---------|------|-----|
| 1 | Stop-loss (STOP_MARKET) | price breaks `stop_loss` | floor: max loss locked | full |
| 2 | TP tier 1 (TAKE_PROFIT_MARKET) | price reaches +1R | take first profit | 30% |
| 3 | TP tier 2 (TAKE_PROFIT_MARKET) | price reaches +2R | take second profit | 30% |
| 4 | Trailing stop (TRAILING_STOP_MARKET) | pullback after activation | runs the tail | 40% |

**R** = |actual fill price − stop_loss|. Computed once at fill from the real fill price, then locked.

**Trailing stop notes:**
- Placed at open, but carries an activation price (`entry + 2R`). Before 2R it is a "sleeping" order — no tracking, no trigger.
- Once activated, the exchange tracks the highest price and sells the tail when it pulls back more than `trailing_callback_pct` (default 1%).
- Runs entirely exchange-side: no local polling, no rate-limit risk.
- Callback is a *percentage*, not an ATR distance (API constraint) — wider instruments see wider absolute moves at the same percent.

**Breakeven raise:** after price passes 1R, the stop is raised to just above entry (`be_buffer_pct`), so the worst case is a scratch.

**Active close patrol:** each decision cycle ends with `executor.py --manage <symbol>`; if peak drawdown exceeds `pullback_close_pct` (default 60%), it market-closes. Drawdown management is automated — the LLM only handles entries and structure.

**Peak memory:** `state/position_peak.json` records entry/SL/peak per position. Written on open, cleared on close, updated each cycle. Don't delete it.

### The guard's three hard constraints

No matter what the LLM returns, these are never negotiable:

1. **No stop-loss, no position** — an open decision must carry `stop_loss`; missing → rejected.
2. **Symbol whitelist** — only `BTCUSDT` / `ETHUSDT` / `SOLUSDT`, and the decision's symbol must **exactly match** the snapshot's `market_data.symbol`. Mismatch → forced `hold`. Closing a symbol you don't hold is forbidden.
3. **No revenge trading** — consecutive losses past a threshold trigger a cooldown (`recover_loss_streak_max` / `cooldown_after_streak_min`).

Plus a **format gate** (format only, not strategy):

- `stop_loss` / `take_profit` must be valid numbers
- Directional sanity: long → `sl < entry < tp`, short → `sl > entry > tp`
- Snapshot freshness: older than `snapshot_max_age_ms` (default 2 min) → rejected

> Strategy is strategy; format is format. The guard does **not** dictate how you price things — it only guarantees that what comes out is executable and not directionally inverted.

### SL/TP pricing belongs to the model

A deliberate design choice: **the decision model sets its own stop-loss and take-profit prices and must justify them.** No preset template like "entry − 2×ATR".

Why: a hardcoded formula reduces the model to a formula executor and discards whatever it actually sees in the tape. The cost is that the model must produce credible reasoning every single time — which is what the logs are for.

---

## Quick start

### 1. Environment

```bash
pkg install python
pip install requests   # everything else is stdlib; no ccxt
```

> ccxt is **deliberately avoided**. Reasons below.

### 2. Configure

```bash
cp keys.example.json keys.json   # fill in your API Key / Secret
cp cfg.example.json cfg.json     # tune parameters
```

`keys.json` is gitignored and never committed.

**API permissions:** read + futures trading only. **Never enable withdrawals.**

### 3. Run

```bash
mkdir -p state log

python3 bin/snapshot.py                              # one snapshot
python3 bin/decide.py BTCUSDT                        # one decision
python3 bin/guard.py state/decision.json state/latest_BTCUSDT.json
python3 bin/executor.py --file state/decision.json   # actually place orders
python3 bin/volwatch.py                              # daemon: schedule + volatility
```

`executor.py` supports `--dry-run` (print, don't send). Try that first.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | OK |
| 2 | Rejected by guard |
| 3 | Account communication failure |
| 4 | Order failed |

---

## Field notes (the useful part)

Everything below was learned the hard way on testnet.

### Algo Order API (SL/TP)

Binance migrated conditional-order endpoints in 2025. Traps:

- **The trigger price field is `triggerPrice`, not `stopPrice`.** Passing the old name **does not error** — the order gets placed and **never triggers**. This is the nastiest one: everything looks fine while you are actually naked.
- `POST /fapi/v1/algoOrder` requires `algoType=CONDITIONAL`.
- The response returns `algoId`, not `orderId`. Don't grab the wrong field.
- Query separately: classic orders via `/fapi/v1/openOrders`, conditional via `/fapi/v1/openAlgoOrders`.
- `GET /fapi/v1/allOpenOrders` returns **404** on testnet (that path only works for DELETE). Don't use it to list.

### Don't use ccxt

ccxt lagged the Algo Order migration, which silently broke stop-loss/take-profit. Since v3 this project uses **pure stdlib + urllib + hand-rolled HMAC** — and it's more reliable, not less.

### testnet rate limits and 418

- testnet enforces IP rate limits; exceeding them returns **HTTP 418**, a temporary ban that can last 10+ minutes.
- Probe `/fapi/v1/time` before critical calls, and space out signed requests (this project uses `_gap = 0.15s`).
- The SL/TP flow makes many requests (probe + entry + SL + TP + cleanup) — spacing matters.

### Snapshot freshness

`MAX_SNAPSHOT_AGE_MS` defaults to **120000 (2 minutes)**.

It was 10 minutes at first — far too stale for actual order placement. Tightening to 2 minutes compresses the whole "snapshot → decision → validate → order" chain into a hard 2-minute realtime constraint. Stale snapshots are rejected outright.

**No exemptions.** Even manual debugging requires a fresh snapshot.

### Don't trust the market-order response

`executedQty=0` in a market-order response does **not** mean nothing filled. Poll to confirm the actual fill before attaching SL/TP — otherwise you'll re-enter and double your position.

### Clear stale orders

Before opening: `DELETE /fapi/v1/allOpenOrders` to drop leftover classic orders. After closing: **sweep both classic and algo orders** (list and delete each), or error `-4120` will keep coming back.

### Power loss is survivable

The pipeline runs locally; power loss stops everything. But any open position is protected by **exchange-side SL/TP** — which is precisely why stops must live on the exchange, not in local memory.

After power returns, the stale snapshot gets rejected by the guard; fetch a fresh one and the chain resumes.

---

## Repository layout

```
bin/
  snapshot.py     market + account → snapshot
  decide.py       LLM → decision JSON
  guard.py        guard validation
  executor.py     order executor
  volwatch.py     schedule + volatility trigger
zero_prompt.md    system prompt for the decision model
cfg.example.json  parameter template
keys.example.json secret template
state/            runtime state (not committed)
log/              logs (not committed)
```

## License

PolyForm Noncommercial License 1.0.0 — free for personal, research, educational and other noncommercial use. Commercial use requires separate written permission. See [LICENSE](LICENSE).

---
---

# 中文版

一套跑在 **Termux（Android 手机）** 上的全自动合约交易链路：

**拉快照 → LLM 决策 → 守门员校验 → 签名发单 → 全量落盘**

默认对接币安 **testnet（模拟盘）**，充分验证后再自行决定是否切真实账户。

> ⚠️ **免责声明** — 本项目仅供学习与研究。加密货币合约交易风险极高，可能导致本金全部损失。作者不对任何人使用本代码产生的资金损失承担责任。**请务必先在 testnet 充分验证。**

---

## 为什么会有这个项目

大部分「AI 交易机器人」不过是交易所 SDK 的薄壳，交易所一改接口就崩了。这套东西的前提不一样：

**模型负责提议，守门员负责否决。**

模型永远不直接被信任去动钱。它输出的每一个决策都要先过一遍硬约束，过不了就什么都不发生。整条链的设计目标是：出事的时候，翻日志就能知道为什么。

## 五层结构

| 层 | 文件 | 职责 |
|---|---|---|
| 1. 数据 | `snapshot.py` | 拉三品种行情 + 账户状态，落 `latest_*.json` |
| 2. 决策 | `decide.py` | 把快照喂给 LLM，拿回决策 JSON |
| 3. 校验 | `guard.py` | 守门员：硬约束 + 格式关，不合规一律拒 |
| 4. 执行 | `executor.py` | 手搓 HMAC 签名下单，含止损/止盈条件单 + 出场阶梯 |
| 5. 触发 | `volwatch.py` | 定时兜底 + 波动阈值提前唤醒 + 出场巡检 |

### 出场管理（v4）

开仓后，执行器一次性挂 **4 张单**组成出场阶梯：

| # | 单类型 | 触发条件 | 作用 | 数量 |
|---|--------|----------|------|------|
| 1 | 全平止损 STOP_MARKET | 价格跌破决策的 stop_loss | 兜底：最大亏损锁死 | 全仓 |
| 2 | 档1止盈 TAKE_PROFIT_MARKET | 价格到达 +1R | 分批落袋第一批 | 30% |
| 3 | 档2止盈 TAKE_PROFIT_MARKET | 价格到达 +2R | 分批落袋第二批 | 30% |
| 4 | 跟踪止损 TRAILING_STOP_MARKET | 激活后从峰值回撤 | 接管尾仓，锁住大肉 | 40% |

**R 的计算**：R = |真实成交价 − 止损价|。开仓成交瞬间用真实成交价（非快照价）算好 1R/2R 目标价，之后锁死不变。

**跟踪止损要点**：

- 开仓即挂单，但带激活价（入场价 + 2R）：价格未到 2R 前是"睡眠单"，不盯盘、不触发
- 价格触及激活价后，交易所开始记录持仓最高价，从最高价回撤超过 `trailing_callback_pct`（默认 1%）即市价卖出尾仓
- 全程在交易所内部运行，本地不需要轮询，也不受限频影响
- 回调率为百分比而非 ATR 距离（交易所 API 限制），波动大的品种同样百分比对应更大价格空间

**保本抬升**：价格走过 1R 后，把止损从原价抬到入场价上方一点（`be_buffer_pct`），最差白忙、不伤本金。

**主动平仓巡检**：每轮决策链末尾执行 `executor.py --manage <symbol>`，持仓峰值回撤超过 `pullback_close_pct`（默认 60%）即市价主动平仓。回吐管理交给自动化，决策模型只负责入场与结构判断。

**峰值记忆**：`state/position_peak.json` 记录每笔持仓的入场/止损/峰值，开仓写入、平仓清除、每轮更新。别删。

### 守门员的三根硬约束

不管 LLM 返回什么，这三条永远不可突破：

1. **无止损不开仓** —— 开仓决策必须带 `stop_loss`，缺失直接拒。
2. **品种白名单** —— 只允许 `BTCUSDT` / `ETHUSDT` / `SOLUSDT`，且决策里的 symbol 必须与快照 `market_data.symbol` **逐字符一致**，不一致一律 `hold`；未持仓品种禁止 `close`。
3. **不报复性交易** —— 连亏达阈值进冷却期（`recover_loss_streak_max` / `cooldown_after_streak_min`）。

外加一道**格式关**（只管格式，不管策略）：

- `stop_loss` / `take_profit` 必须是合法数字
- 方向逻辑校验：做多 `sl < entry < tp`，做空 `sl > entry > tp`
- 快照时效：超过 `snapshot_max_age_ms`（默认 2 分钟）的快照直接拒

> 策略归策略，格式归格式。守门员**不干预**你怎么定价，只保证「说出来的东西是能执行的、且方向不会写反」。

### 止盈止损定价权归决策方

本项目的设计选择：**止盈止损价格由决策模型自己填写并给出理由**，不预置「入场 − 2×ATR」之类的模板。

理由：预置模板会让模型退化成一个公式执行器，失去了对盘面的判断。代价是模型必须每次给出可信的定价逻辑，这靠日志复盘来监督。

---

## 快速开始

### 1. 环境

```bash
pkg install python
pip install requests   # 其余全部用标准库，无 ccxt 依赖
```

> 本项目**刻意不依赖 ccxt**。原因见下方「踩坑记录」。

### 2. 配置

```bash
cp keys.example.json keys.json   # 填入你的 API Key / Secret
cp cfg.example.json cfg.json     # 按需调整参数
```

`keys.json` 已在 `.gitignore` 中，**不会被提交**。

**API 权限**：只需要「读取」+「合约交易」。**绝对不要开启提现权限**。

### 3. 开跑

```bash
mkdir -p state log

python3 bin/snapshot.py                              # 拉一次快照
python3 bin/decide.py BTCUSDT                        # 让模型做一次决策
python3 bin/guard.py state/decision.json state/latest_BTCUSDT.json
python3 bin/executor.py --file state/decision.json   # 真正下单
python3 bin/volwatch.py                              # 常驻：定时兜底 + 波动唤醒
```

`executor.py` 支持 `--dry-run`，只打印不发单，建议先这么试。

### 退出码约定

| 码 | 含义 |
|---|---|
| 0 | 正常 |
| 2 | 守门员拒绝 |
| 3 | 账户通信失败 |
| 4 | 交易失败 |

---

## 踩坑记录

这部分是项目最有价值的内容，全是模拟盘打出来的。

### Algo Order API（止盈止损）

币安在 2025 年迁移了条件单接口，几个坑：

- **触发价参数名是 `triggerPrice`，不是 `stopPrice`**。传旧名**不会报错**，单子会挂上，但**永远不会触发**。这是最阴的一个坑——看起来一切正常，实际上裸奔。
- 新端点 `POST /fapi/v1/algoOrder` 必须带 `algoType=CONDITIONAL`。
- 响应里 `orderId` 变成了 `algoId`，别拿错字段。
- 查询要分开：经典单 `/fapi/v1/openOrders`，条件单 `/fapi/v1/openAlgoOrders`。
- `GET /fapi/v1/allOpenOrders` 在 testnet 返回 **404**（该路径只在 DELETE 时可用）。别用它查单。

### 不要用 ccxt

ccxt 对上述 Algo Order 迁移跟进不及时，导致止损止盈静默失效。本项目 v3 起彻底弃用，改为**纯标准库 + urllib + 手搓 HMAC**，反而更稳。

### testnet 的限频与 418

- testnet 有 IP 限频，触发后返回 **HTTP 418** 临时封禁，窗口可达十几分钟。
- 关键请求前先探 `/fapi/v1/time`，并在每次签名请求之间拉开间隔（本项目 `_gap = 0.15s`）。
- 止盈止损全链路请求次数较多（探针 + 开仓 + 止损 + 止盈 + 撤单），务必拉开。

### 快照新鲜线

`MAX_SNAPSHOT_AGE_MS` 默认 **120000（2 分钟）**。

一开始设的是 10 分钟，后来发现对做单来说太长了——盘面早变了。收紧到 2 分钟后，「快照 → 决策 → 校验 → 发单」整链被压进 2 分钟实时硬约束，超时的旧快照会被守门员直接拒绝。

**不设豁免口子**。手动调试时超时也得现刷现用。

### 成交确认不要信市价响应

市价单响应里的 `executedQty=0` 不代表没成交。必须**轮询确认**实际成交后再挂止损止盈，否则会出现「以为没成交又下一单」的重复开仓。

### 开仓前先清旧单

开仓前 `DELETE /fapi/v1/allOpenOrders` 撤掉残留经典单；平仓后**经典单 + algo 单双份清扫**（列表遍历逐个删），否则 `-4120` 错误会反复出现。

### 断电解耦

全链跑在本机，断电就全停。但在场仓位由**交易所级 SL/TP 独立保护**——这是为什么止损止盈必须真正挂到交易所，而不是本地内存里判断。

来电后过期快照会被守门员拒，重新拉快照即可接上。

---

## 目录结构

```
bin/
  snapshot.py     拉行情 + 账户，出快照
  decide.py       调 LLM 出决策 JSON
  guard.py        守门员校验
  executor.py     下单执行器
  volwatch.py     定时 + 波动触发
zero_prompt.md    决策模型的系统提示词
cfg.example.json  参数模板
keys.example.json 密钥模板
state/            运行状态（不入库）
log/              日志（不入库）
```

## License

PolyForm Noncommercial 1.0.0 许可 —— 个人、研究、教学等非商业用途免费使用；商业使用须另行取得作者书面许可。见 [LICENSE](LICENSE)。
