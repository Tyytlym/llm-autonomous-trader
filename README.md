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
| 4. Execution | `executor.py` | Hand-rolled HMAC signing, orders incl. SL/TP + exit ladder + trade ledger |
| 5. Trigger | `volwatch.py` | Hourly fallback + volatility-threshold early wake + log housekeeping |
| 6. UI | `webui/` | Local glassmorphism dashboard: live state, params, command runner |

### Exit management (v4+)

After a position opens, the executor places a **4-order exit ladder** in one shot:

| # | Order | Trigger | Role | Qty |
|---|-------|---------|------|-----|
| 1 | Stop-loss (STOP_MARKET) | price breaks `stop_loss` | floor: max loss locked | full |
| 2 | TP tier 1 (TAKE_PROFIT_MARKET) | price reaches +1R | take first profit | 30% |
| 3 | TP tier 2 (TAKE_PROFIT_MARKET) | price reaches +2R | take second profit | 30% |
| 4 | Trailing stop (TRAILING_STOP_MARKET) | pullback after activation | runs the tail | 40% |

**R** = |actual fill price − stop_loss|. Computed once at fill from the real fill price, then locked.

**Exit protections (v5 hardened):**
- **Verify & repair** — after opening, `verify_exit()` reads back the exchange's algo orders and re-places anything missing (up to 3 attempts). No silent naked positions.
- **`ensure_stop_loss()`** — emergency backstop: if no protective SL is on the books, place a full-close STOP_MARKET. Never auto-closes.
- **Alert ledger** — if repair fails, an alert lands in `state/alerts.jsonl` for the dashboard to show; **never** auto-closes a position over a missing order.
- **Trade ledger** — every close appends to `state/trades.jsonl` (entry_ts idempotent key; pulls real `income`, net of fees). Re-runs skip duplicates.
- **Trailing stop notes:** placed at open with activation price (entry + 2R); before that it's a "sleeping" order. Once activated the exchange tracks peak and exits on `trailing_callback_pct` (default 1%) pullback. Percent-based, not ATR (API constraint).

**Active close patrol:** each cycle may end with `executor.py --manage <symbol>`; if peak drawdown exceeds `pullback_close_pct`, it market-closes. In v5 this is **off by default** (`exit_manage:false`) — the exchange-side ladder does the work; flip it on if you want local drawdown patrol too.

### The guard's three hard constraints

No matter what the LLM returns, these are never negotiable:

1. **No stop-loss, no position** — an open decision must carry `stop_loss`; missing → rejected.
2. **Symbol whitelist** — only `BTCUSDT` / `ETHUSDT` / `SOLUSDT`, exact match with snapshot `market_data.symbol`. Closing a symbol you don't hold is forbidden.
3. **No revenge trading** — consecutive losses past a threshold trigger a cooldown.

Plus a **format gate** (format only, not strategy):

- `stop_loss` / `take_profit` valid numbers
- Directional sanity: long `sl < entry < tp`, short `sl > entry > tp`
- Snapshot freshness: older than `snapshot_max_age_ms` (default 2 min) → rejected

> Strategy is strategy; format is format. The guard does **not** dictate pricing — it only guarantees what comes out is executable and not inverted.

### Notional sizing (v5)

The old fixed `MAX_NOTIONAL_USDT = 3000` is gone. The guard now computes **notional ≤ balance × 50%** (`NOTIONAL_RATIO`) — dynamic, no fixed floor; missing/invalid balance → reject. `zero_prompt.md` and the guard agree at 50%.

### SL/TP pricing belongs to the model

The decision model sets its own stop-loss and take-profit prices and must justify them. No preset "entry − 2×ATR" template. The cost: it must produce credible reasoning every time — logs are for auditing that.

---

## Web dashboard (v5 new)

`webui/` is a local read-only+params glassmorphism dashboard. **No manual trading buttons** — the account belongs to Zero.

- `index.html` — landing page
- `app.html` — live dashboard: wallet, positions, snapshot freshness, recent decisions, trade ledger, alerts; refreshes every 15s
- `help.html` — command quick reference
- `webui.py` — stdlib-only HTTP server (port 8123 by default)

**Only real, effective params are editable**: `vol_threshold_pct`, `min_sec_between_orders`, `exit.tiers`, `exit.tier1_r`, `exit.tier2_r`, `exit.trailing_atr_k`. Saved atomically to `cfg.json`; live params (vol threshold / order interval) take effect next volwatch round; exit tier params apply to the **next opened position**, never retroactively.

It also exposes a **whitelisted command runner** (`/api/cmd`) — the same `snap/dec/decs/wl/ex/bal/pos/tr/vwps/vwres/res` shortcuts from the app, read-only except start/restart buttons. No arbitrary shell.

Run: `cd ~/binance/webui && python3 webui.py 8123` → open `http://localhost:8123`. Local only; no port forwarding.

> **Note:** the dashboard's `--manage`/trailing activation internals differ slightly from the README above — `trailing_atr_k` drives the tail stop at ATR multiples (see cfg), and `be_buffer_pct` / `pullback_close_pct` are no longer exposed. When in doubt, trust the code + the alert ledger.

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

### Terminal shortcuts (v5 new)

`bashrc_shortcuts.txt` contains the user's everyday commands: `snap` (snapshot), `vwps` (volwatch process), `vwres` (restart volwatch), `dec`/`decs` (decisions), `wl` (live log), `ex` (exec ledger), `bal` (wallet), `pos` (positions), `tr` (trade ledger), `res` (restart all). Source it in `~/.bashrc` or paste into Termux. All read-only except restarts.

---

## Field notes (the useful part)

Everything below was learned the hard way on testnet.

### Algo Order API (SL/TP)

Binance migrated conditional-order endpoints in 2025. Traps:

- **The trigger price field is `triggerPrice`, not `stopPrice`.** Passing the old name **does not error** — the order is placed and **never triggers**. The nastiest one.
- `POST /fapi/v1/algoOrder` requires `algoType=CONDITIONAL`.
- The response returns `algoId`, not `orderId`. Don't grab the wrong field.
- Query separately: classic via `/fapi/v1/openOrders`, conditional via `/fapi/v1/openAlgoOrders`.
- `GET /fapi/v1/allOpenOrders` returns **404** on testnet (DELETE-only path). Don't use it to list.

### Don't use ccxt

ccxt lagged the Algo Order migration, silently breaking SL/TP. Since v3: **pure stdlib + urllib + hand-rolled HMAC** — more reliable, not less.

### testnet rate limits and 418

- testnet enforces IP rate limits; exceeding returns **HTTP 418**, a temporary ban that can last 10+ minutes.
- Probe `/fapi/v1/time` before critical calls, space out signed requests (`_gap = 0.15s`).
- The SL/TP flow makes many requests — spacing matters.

### Snapshot freshness

`MAX_SNAPSHOT_AGE_MS` defaults to **120000 (2 minutes)** — was 10 min at first, far too stale. Tightened to compress the whole chain into a 2-minute hard window. Stale snapshots rejected. **No exemptions.**

### Don't trust the market-order response

`executedQty=0` ≠ nothing filled. Poll to confirm the actual fill before attaching SL/TP — otherwise you'll re-enter and double your position.

### Clear stale orders

Before opening: `DELETE /fapi/v1/allOpenOrders` for leftover classic orders. After closing: **sweep both classic and algo orders**, or error `-4120` returns.

### Power loss is survivable

Everything local; power loss stops the pipeline, but open positions are protected by **exchange-side SL/TP** — exactly why stops live on the exchange, not in local memory. On restart, stale snapshot rejected; fetch fresh and the chain resumes.

### Log housekeeping (v5)

`volwatch.py` trims `volwatch_live.out` when it exceeds 5 MB (keeps last 500 lines) and deletes other logs older than 15 days. **Decision and execution logs are permanent — never cleaned.**

### Price rounding (v5)

`fmt_price`/`fmt_qty` floor to exchange `tickSize`/`stepSize` — kills `-1111` precision errors like `103.38000000000001`.

---

## Repository layout

```
bin/
  snapshot.py     market + account → snapshot
  decide.py       LLM → decision JSON
  guard.py        guard validation (notional = balance × 50%)
  executor.py     order executor + exit ladder + trade ledger + verify/repair
  volwatch.py     schedule + volatility trigger + log housekeeping
  exp_trailing.py exchange capability probe (trailing stop / partial TP)
  t_selfcheck.py  post-upgrade self-test (syntax + tier-split logic)
webui/
  index.html      landing page
  app.html        live dashboard
  help.html       command quick reference
  webui.py        stdlib HTTP server (port 8123)
bashrc_shortcuts.txt  everyday terminal commands
快捷指令速查.html      Chinese command cheat-sheet (for browsing on phone)
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

## 五层结构 + 看板

| 层 | 文件 | 职责 |
|---|---|---|
| 1. 数据 | `snapshot.py` | 拉三品种行情 + 账户状态，落 `latest_*.json` |
| 2. 决策 | `decide.py` | 把快照喂给 LLM，拿回决策 JSON |
| 3. 校验 | `guard.py` | 守门员：硬约束 + 格式关，不合规一律拒 |
| 4. 执行 | `executor.py` | 手搓 HMAC 签名下单，止损/止盈条件单 + 出场阶梯 + 交易账本 |
| 5. 触发 | `volwatch.py` | 定时兜底 + 波动阈值提前唤醒 + 日志卫生 |
| 6. 看板 | `webui/` | 手机本地玻璃拟态看板：实时状态 / 参数 / 指令执行 |

### 出场管理（v4 起）

开仓后，执行器一次性挂 **4 张单**组成出场阶梯：

| # | 单类型 | 触发条件 | 作用 | 数量 |
|---|--------|----------|------|------|
| 1 | 全平止损 STOP_MARKET | 价格跌破决策的 stop_loss | 兜底：最大亏损锁死 | 全仓 |
| 2 | 档1止盈 TAKE_PROFIT_MARKET | 价格到达 +1R | 分批落袋第一批 | 30% |
| 3 | 档2止盈 TAKE_PROFIT_MARKET | 价格到达 +2R | 分批落袋第二批 | 30% |
| 4 | 跟踪止损 TRAILING_STOP_MARKET | 激活后从峰值回撤 | 接管尾仓，锁住大肉 | 40% |

**R 的计算**：R = |真实成交价 − 止损价|。开仓成交瞬间用真实成交价（非快照价）算好 1R/2R 目标价，之后锁死不变。

**v5 出场加固：**
- **核验补挂 `verify_exit()`** — 开仓后回读交易所 algo 挂单，缺哪张补哪张（最多 3 次），不留「裸奔仓位」；
- **`ensure_stop_loss()`** — 异常兜底：账上没有保护性止损就补挂一张全平止损，绝不主动平仓；
- **警示账本** — 补挂失败落 `state/alerts.jsonl` 给看板弹条，绝不因缺单而擅自平仓；
- **交易账本** — 每次平仓追加 `state/trades.jsonl`（以开仓时刻为幂等键，拉真实 income、已扣手续费），重复触发不重复记账。

**主动平仓巡检**：每轮决策链末尾可执行 `executor.py --manage <symbol>`，峰值回撤超阈值即市价平仓。v5 起**默认关闭**（`exit_manage:false`）——浮盈保护由交易所侧条件单负责，想加本地巡检再打开。

**跟踪止损要点**：开仓即挂，带激活价（入场价 + 2R）；未到激活价前是"睡眠单"；激活后由交易所记录峰值、回撤超 `trailing_callback_pct`（默认1%）即卖出尾仓。回调率为百分比而非 ATR 距离（API 限制）。

### 守门员的三根硬约束

不管 LLM 返回什么，这三条永远不可突破：

1. **无止损不开仓** — 开仓决策必须带 `stop_loss`，缺失直接拒。
2. **品种白名单** — 只允许 `BTCUSDT` / `ETHUSDT` / `SOLUSDT`，且与快照 `market_data.symbol` **逐字符一致**；未持仓品种禁止 `close`。
3. **不报复性交易** — 连亏达阈值进冷却期。

外加一道**格式关**（只管格式，不管策略）：

- `stop_loss` / `take_profit` 必须是合法数字
- 方向逻辑校验：做多 `sl < entry < tp`，做空 `sl > entry > tp`
- 快照时效：超过 `snapshot_max_age_ms`（默认 2 分钟）直接拒

> 策略归策略，格式归格式。守门员**不干预**你怎么定价，只保证「说出来的东西是能执行的、且方向不会写反」。

### 名义上限（v5 改版）

旧版固定 `MAX_NOTIONAL_USDT = 3000` 已删除。守门员改为 **名义 ≤ 余额 × 50%**（`NOTIONAL_RATIO`）——动态上限、无固定兜底；余额缺失/非法则直接拒单。`zero_prompt.md` 与守门员一致（50%）。

### 止盈止损定价权归决策方

止盈止损价格由决策模型自己填写并给出理由，不预置「入场 − 2×ATR」模板。代价是模型必须每次给出可信的定价逻辑，这靠日志复盘来监督。

---

## 网页看板（v5 新增）

`webui/` 是手机本地「只读展示 + 参数修改」玻璃拟态看板。**不加入手动下单/平仓**——账号全程由零号负责。

- `index.html` — 营销落地页
- `app.html` — 实时看板：钱包、持仓、三品种快照新鲜度、最近决策、平仓账本、警示条；每 15 秒自动刷新
- `help.html` — 指令速查（与手机端一致）
- `webui.py` — 纯标准库 HTTP 服务（默认 8123 端口）

**只开放真生效的参数**：`vol_threshold_pct`、`min_sec_between_orders`、`exit.tiers`、`exit.tier1_r`、`exit.tier2_r`、`exit.trailing_atr_k`。保存用原子写（不写坏文件）；波动阈值/下单间隔下次 volwatch 轮次即时生效，出场档位下次新开仓才生效、已持仓旧单不追改。

另带**白名单指令面板**（`/api/cmd`）：看板里可直接点 `snap/dec/decs/wl/ex/bal/pos/tr/vwps/vwres/res`，除启动/重启按钮外全部只读，不开放任意命令。

启动：`cd ~/binance/webui && python3 webui.py 8123` → 浏览器开 `http://localhost:8123`。仅本机可访问，不做外网穿透。

---

## 快速开始

### 1. 环境

```bash
pkg install python
pip install requests   # 其余全部用标准库，无 ccxt 依赖
```

> 本项目**刻意不依赖 ccxt**。原因见「踩坑记录」。

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

### 终端快捷指令（v5 新增）

`bashrc_shortcuts.txt` 收录日常命令：`snap`（快照）、`vwps`（查 volwatch 进程）、`vwres`（重启 volwatch）、`dec`/`decs`（决策原文/精简）、`wl`（实时日志）、`ex`（执行记录）、`bal`（钱包）、`pos`（持仓）、`tr`（平仓账本）、`res`（全套重启）。除重启类外全部只读，拷进 `~/.bashrc` 即用。

---

## 踩坑记录

这部分是项目最有价值的内容，全是模拟盘打出来的。

### Algo Order API（止盈止损）

币安在 2025 年迁移了条件单接口，几个坑：

- **触发价参数名是 `triggerPrice`，不是 `stopPrice`**。传旧名**不会报错**，单子会挂上，但**永远不会触发**。最阴的一个坑。
- 新端点 `POST /fapi/v1/algoOrder` 必须带 `algoType=CONDITIONAL`。
- 响应里 `orderId` 变成了 `algoId`，别拿错字段。
- 查询要分开：经典单 `/fapi/v1/openOrders`，条件单 `/fapi/v1/openAlgoOrders`。
- `GET /fapi/v1/allOpenOrders` 在 testnet 返回 **404**（该路径只在 DELETE 时可用）。别用它查单。

### 不要用 ccxt

ccxt 对上述 Algo Order 迁移跟进不及时，导致止损止盈静默失效。本项目 v3 起彻底弃用，改为**纯标准库 + urllib + 手搓 HMAC**，反而更稳。

### testnet 的限频与 418

- testnet 有 IP 限频，触发后返回 **HTTP 418** 临时封禁，窗口可达十几分钟。
- 关键请求前先探 `/fapi/v1/time`，并在每次签名请求之间拉开间隔（本项目 `_gap = 0.15s`）。
- 止盈止损全链路请求次数较多，务必拉开。

### 快照新鲜线

`MAX_SNAPSHOT_AGE_MS` 默认 **120000（2 分钟）**。一开始设 10 分钟，对做单来说太长了。收紧后整链被压进 2 分钟实时硬约束，超时旧快照直接拒。**不设豁免口子**。

### 成交确认不要信市价响应

市价单响应里的 `executedQty=0` 不代表没成交。必须**轮询确认**实际成交后再挂止损止盈，否则会出现「以为没成交又下一单」的重复开仓。

### 开仓前先清旧单

开仓前 `DELETE /fapi/v1/allOpenOrders` 撤掉残留经典单；平仓后**经典单 + algo 单双份清扫**，否则 `-4120` 错误会反复出现。

### 断电解耦

全链跑在本机，断电就全停。但在场仓位由**交易所级 SL/TP 独立保护**——这是为什么止损止盈必须真正挂到交易所。来电后过期快照会被拒，重新拉快照即可接上。

### 日志自动清理（v5）

`volwatch.py` 会在 `volwatch_live.out` 超过 5MB 时裁剪（保留末尾 500 行），并清理超过 15 天的一般日志。**决策与执行日志永久保留、绝不清理**（审计用）。

### 价格取整（v5）

`fmt_price`/`fmt_qty` 按交易所 `tickSize`/`stepSize` 向下取整，杜绝 `-1111` 精度超限（如 `103.38000000000001` 尾数）。

---

## 目录结构

```
bin/
  snapshot.py     拉行情 + 账户，出快照
  decide.py       调 LLM 出决策 JSON
  guard.py        守门员校验（名义 = 余额×50%）
  executor.py     下单执行器 + 出场阶梯 + 交易账本 + 核验补挂
  volwatch.py     定时 + 波动触发 + 日志清理
  exp_trailing.py 交易所能力探路（跟踪止损/部分止盈）
  t_selfcheck.py  升级后置自检（语法 + 分档逻辑）
webui/
  index.html      落地页
  app.html        实时看板
  help.html       指令速查
  webui.py        纯标准库 HTTP 服务（8123）
bashrc_shortcuts.txt  日常终端命令
快捷指令速查.html     中文指令速查手册（手机直接打开浏览）
zero_prompt.md    决策模型的系统提示词
cfg.example.json  参数模板
keys.example.json 密钥模板
state/            运行状态（不入库）
log/              日志（不入库）
```

## License

PolyForm Noncommercial 1.0.0 许可 —— 个人、研究、教学等非商业用途免费使用；商业使用须另行取得作者书面许可。见 [LICENSE](LICENSE)。