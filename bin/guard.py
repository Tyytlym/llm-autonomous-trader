#!/usr/bin/env python3
"""guard.py - 零号决策守门员：逐字段校验，越线拒单即终局"""
import json
import math
import time
# ===================== 风控参数（唯一配置点） =====================
WHITELIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
NOTIONAL_RATIO = 0.50       # 单笔名义上限 = 余额 x 50%（2026-10-25 用户拍板，无固定兜底）
MAX_LEVERAGE = 10
RISK_PCT = 0.30             # 单笔风险 <= 余额30%（按止损距离反算）
MAX_SNAPSHOT_AGE_MS = 120000  # 快照时效上限2分钟（快照timestamp为毫秒）
EXTRA_OPEN_WHEN_POSITION = "reject"  # reject=已有仓位拒单 / pyramiding=放行
MIN_BALANCE_USDT = 1.0
# ================================================================
VALID_ACTIONS = ["open_long", "open_short", "close", "hold", "invalid"]
def bad_num(v):
    """数值含NaN/Inf判非法（Python json.loads默认放行NaN/Infinity字面量，这里单独拦）"""
    return isinstance(v, float) and (math.isnan(v) or math.isinf(v))
def reject(d, reasons):
    return {"passed": False, "decision": d, "reasons": reasons, "position": None}
def check(text, snapshot):
    """text: 零号原始输出；snapshot: 快照dict"""
    # 支持两种形态：[{...}]（零号一次可出多条）或单个 {...}
    try:
        whole = json.loads(text.strip())
    except Exception:
        whole = None
    if isinstance(whole, list):
        if len(whole) != 1:
            return reject(None, ["决策条数为%d，单次只允许1条" % len(whole)])
        d = whole[0]
        if not isinstance(d, dict):
            return reject(None, ["决策不是对象"])
    else:
        s = text.find("{")
        e = text.rfind("}")
        if s == -1 or e == -1:
            return reject(None, ["输出不含JSON"])
        stray = text[:s].strip() + text[e + 1:].strip()
        if stray:
            return reject(None, ["JSON外有多余字符: " + stray[:50]])
        try:
            d = json.loads(text[s:e + 1])
        except Exception as ex:
            return reject(None, ["JSON解析失败: " + str(ex)[:50]])
    if any(bad_num(d.get(k)) for k in ("notional_usdt", "leverage", "stop_loss", "take_profit")):
        return reject(d, ["决策数值含NaN/Inf"])
    # 快照时效：缺/超龄一律拒（先于一切价格与风险判定）
    ts = snapshot.get("ts_ms")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts <= 0:
        ts = snapshot.get("timestamp")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts <= 0:
        return reject(d, ["快照缺有效timestamp"])
    age_ms = int(time.time() * 1000) - ts
    if age_ms > MAX_SNAPSHOT_AGE_MS:
        return reject(d, ["快照超龄: %ds前生成(上限%ds)" % (age_ms // 1000, MAX_SNAPSHOT_AGE_MS // 1000)])
    if age_ms < -5000:
        return reject(d, ["快照时间戳异常(来自未来%ds)" % (-age_ms // 1000)])
    action, symbol = d.get("action"), d.get("symbol")
    reasons = []
    if action not in VALID_ACTIONS:
        reasons.append("非法action: " + str(action))
    if symbol not in WHITELIST:
        reasons.append("symbol不在白名单: " + str(symbol))
    md = snapshot.get("market_data") or {}
    if not md.get("symbol"):
        reasons.append("快照缺market_data.symbol")
    elif symbol != md.get("symbol"):
        reasons.append("symbol(%s)与快照(%s)不一致" % (symbol, md.get("symbol")))
    if action in ("hold", "invalid"):
        if d.get("notional_usdt") not in (0, 0.0, None):
            reasons.append("hold/invalid但notional非0")
        if d.get("stop_loss") is not None or d.get("take_profit") is not None:
            reasons.append("hold/invalid但止损止盈非null")
        return reject(d, reasons) if reasons else {"passed": True, "decision": d, "reasons": [], "position": None}
    px = md.get("price")
    if not isinstance(px, (int, float)) or isinstance(px, bool) or bad_num(px) or px <= 0:
        return reject(d, ["快照无有效价格"])
    acc = snapshot.get("account") or {}
    mine = [p for p in (acc.get("positions") or []) if p.get("symbol") == symbol]
    if action == "close":
        if not mine:
            return reject(d, ["close被拒: 无%s持仓" % symbol])
        return {"passed": True, "decision": d, "reasons": [], "position": mine[0]}
    # open_long / open_short
    if mine and EXTRA_OPEN_WHEN_POSITION == "reject":
        return reject(d, ["已有%s持仓(%s), 拒绝加仓" % (symbol, mine[0].get("side"))])
    notional = d.get("notional_usdt")
    if not isinstance(notional, (int, float)) or isinstance(notional, bool) or notional <= 0:
        reasons.append("notional_usdt非法: " + str(notional))
    else:
        # 名义动态上限：余额 x NOTIONAL_RATIO（唯一限制，无固定兜底；余额缺失/非法时拒单）
        cap = None
        bal_for_cap = acc.get("balance_usdt")
        if isinstance(bal_for_cap, (int, float)) and not isinstance(bal_for_cap, bool) and not bad_num(bal_for_cap) and bal_for_cap > 0:
            cap = round(bal_for_cap * NOTIONAL_RATIO, 2)
        if cap is None:
            reasons.append("账户无有效余额，无法核定名义上限")
        elif notional > cap:
            reasons.append("名义%s超上限%s(余额%s x 50%%)" % (notional, cap, bal_for_cap))
    lev = d.get("leverage")
    if not isinstance(lev, (int, float)) or isinstance(lev, bool) or lev < 1 or lev > MAX_LEVERAGE:
        reasons.append("杠杆非法或超上限: " + str(lev))
    sl = d.get("stop_loss")
    if not isinstance(sl, (int, float)) or isinstance(sl, bool) or bad_num(sl) or sl <= 0:
        reasons.append("无有效止损: " + str(sl))  # 无止损不开仓，在此兜底
    else:
        if action == "open_long" and sl >= px:
            reasons.append("做多但止损%s>=现价%s" % (sl, px))
        if action == "open_short" and sl <= px:
            reasons.append("做空但止损%s<=现价%s" % (sl, px))
        # 按止损距离反算单笔风险
        dist = abs(px - sl) / px
        bal = acc.get("balance_usdt")
        if not isinstance(bal, (int, float)) or isinstance(bal, bool) or bad_num(bal) or bal < MIN_BALANCE_USDT:
            return reject(d, ["余额数据无效或过低(数据异常)"])
        risk = dist * (notional or 0)
        max_risk = bal * RISK_PCT
        if risk > max_risk:
            reasons.append(
                "单笔风险超限: 距离%.2f%% x %s = %.2fu > %.2fu(余额%s x %s%%)"
                % (dist * 100, notional, risk, max_risk, bal, RISK_PCT * 100)
            )
    tp = d.get("take_profit")
    if tp is not None and (not isinstance(tp, (int, float)) or bad_num(tp) or tp <= 0):
        reasons.append("take_profit非法: " + str(tp))
    elif tp is not None:
        if action == "open_long" and tp <= px:
            reasons.append("做多但止盈%s<=现价%s" % (tp, px))
        if action == "open_short" and tp >= px:
            reasons.append("做空但止盈%s>=现价%s" % (tp, px))
    er = d.get("entry_reason")
    if not isinstance(er, str) or len(er) < 4:
        reasons.append("entry_reason缺失或过短")
    return reject(d, reasons) if reasons else {"passed": True, "decision": d, "reasons": [], "position": None}
if __name__ == "__main__":
    snap = {"timestamp": int(time.time() * 1000),
            "market_data": {"symbol": "BTCUSDT", "price": 60000.0},
            "account": {"balance_usdt": 1000.0, "positions": []}}
    t1 = check('{"action":"open_long","symbol":"BTCUSDT","notional_usdt":500,"leverage":5,'
               '"entry_reason":"ema20上穿ema50回调企稳","stop_loss":59400,"take_profit":61200}', snap)
    t2 = check('{"action":"open_long","symbol":"BTCUSDT","notional_usdt":3500,"leverage":20,'
               '"entry_reason":"突破前高","stop_loss":59400,"take_profit":null}', snap)
    t3 = check('{"action":"hold","symbol":"BTCUSDT","notional_usdt":0,"leverage":1,'
               '"entry_reason":"趋势不明等待","stop_loss":null,"take_profit":null}', snap)
    t4 = check('{"action":"open_short","symbol":"BTCUSDT","notional_usdt":300,"leverage":3,'
               '"entry_reason":"ema死叉+rsi回落","stop_loss":59000,"take_profit":null}', snap)
    snap_no_px = {"timestamp": snap["timestamp"],
                  "market_data": {"symbol": "BTCUSDT"},
                  "account": {"balance_usdt": 1000.0, "positions": []}}
    t5 = check('{"action":"open_long","symbol":"BTCUSDT","notional_usdt":200,"leverage":2,'
               '"entry_reason":"测试快照缺价格用例","stop_loss":59400,"take_profit":null}', snap_no_px)
    t6 = check('{"action":"close","symbol":"BTCUSDT","notional_usdt":0,"leverage":1,'
               '"entry_reason":"无仓却要平仓测试","stop_loss":null,"take_profit":null}', snap)
    t7 = check('{"action":"open_long","symbol":"BTCUSDT","notional_usdt":NaN,"leverage":5,'
               '"entry_reason":"notional为NaN测试","stop_loss":59400,"take_profit":null}', snap)
    t8 = check('{"action":"open_long","symbol":"ETHUSDT","notional_usdt":200,"leverage":2,'
               '"entry_reason":"symbol与快照不一致","stop_loss":59000,"take_profit":null}', snap)
    stale = {"timestamp": snap["timestamp"] - 700000,
             "market_data": snap["market_data"], "account": snap["account"]}
    t9 = check('{"action":"open_long","symbol":"BTCUSDT","notional_usdt":200,"leverage":2,'
               '"entry_reason":"快照超龄校验测试","stop_loss":59400,"take_profit":null}', stale)
    t10 = check('{"action":"open_long","symbol":"BTCUSDT","notional_usdt":200,"leverage":2,'
                '"entry_reason":"做多止盈低于现价测试","stop_loss":59400,"take_profit":58000}', snap)
    t11 = check('{"action":"open_long","symbol":"BTCUSDT","notional_usdt":200,"leverage":2,'
                '"entry_reason":"做多止盈合法测试","stop_loss":59400,"take_profit":61500}', snap)
    print("用例1(正常开多):", "PASS" if t1["passed"] else t1["reasons"])
    print("用例2(超名义+超杠杆):", "PASS" if not t2["passed"] and any("超" in r for r in t2["reasons"]) else t2["reasons"])
    print("用例3(合法hold):", "PASS" if t3["passed"] else t3["reasons"])
    print("用例4(空单止损方向错误):", "PASS" if not t4["passed"] else "FAIL应拒")
    print("用例5(快照缺price):", "PASS" if not t5["passed"] and any("无有效价格" in r for r in t5["reasons"]) else t5["reasons"])
    print("用例6(close无仓):", "PASS" if not t6["passed"] and any("close被拒" in r for r in t6["reasons"]) else t6["reasons"])
    print("用例7(NaN数值):", "PASS" if not t7["passed"] and any("NaN" in r for r in t7["reasons"]) else t7["reasons"])
    print("用例8(symbol不一致):", "PASS" if not t8["passed"] and any("不一致" in r for r in t8["reasons"]) else t8["reasons"])
    print("用例9(快照超龄):", "PASS" if not t9["passed"] and any("超龄" in r for r in t9["reasons"]) else t9["reasons"])
    print("用例10(TP方向错误):", "PASS" if not t10["passed"] and any("止盈" in r for r in t10["reasons"]) else t10["reasons"])
    print("用例11(TP方向合法):", "PASS" if t11["passed"] else t11["reasons"])