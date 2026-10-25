#!/usr/bin/env python3
# executor.py - 执行器 v3（成交确认轮询 + closePosition 条件单 + 撤旧单生命周期）
# 链路: 读零号决策 -> 快照并入实时账户 -> guard 校验 -> testnet 下单 -> 全量落盘
# v3 变更:
#   1. 开仓/平仓后轮询查单确认真实成交量（不复信下单响应 executedQty）
#   2. 止损/止盈改 closePosition=true 全仓条件单（免算平仓量, 触发即全平）
#   3. 开仓前/平仓后/炸仓后 DELETE allOpenOrders 撤该 symbol 旧条件单
#   4. call() 每次请求前间隔 _gap 秒, 降低 testnet 限频/418 风险
# 用法:
#   python3 executor.py [SYMBOL] --file decision.json
#   python3 executor.py [SYMBOL] --text '{"action":...}'
#   加 --dry-run 只跑账户合并+guard校验，不碰交易所下单
# 依赖: keys.json(apiKey/secret)、guard.py
# 退出码: 0正常 2guard拒 3账户/通信失败 4交易失败
import sys, os, json, time, hmac, hashlib, argparse, traceback
from decimal import Decimal, ROUND_DOWN
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BIN_DIR)
import guard  # noqa: E402

LOG_DIR = os.path.join(BASE, "log")
STATE_DIR = os.path.join(BASE, "state")
TESTNET = "https://testnet.binancefuture.com"
_key = json.load(open(os.path.join(BASE, "keys.json")))
API_KEY, API_SECRET = _key["apiKey"], _key["secret"]
_offset = 0
_info = {"stepSize": {}, "minNotional": {}}  # symbol -> 值
PEAK_PATH = os.path.join(STATE_DIR, "position_peak.json")

def pub(path):
    return json.loads(urlopen(TESTNET + path, timeout=20).read().decode("utf-8", "replace"))

_gap = 0.15        # 每次签名请求前最小间隔(秒)，防 testnet 限频/418 封禁
_last_call = 0.0

def call(method, path, params):
    global _last_call
    gap_wait = _gap - (time.time() - _last_call)
    if gap_wait > 0:
        time.sleep(gap_wait)
    _last_call = time.time()
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000) + _offset
    p["recvWindow"] = 20000
    q = "&".join("%s=%s" % (a, b) for a, b in p.items())
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    req = Request(TESTNET + path + "?" + q + "&signature=" + sig, method=method,
                  headers={"X-MBX-APIKEY": API_KEY})
    if method == "POST":
        req.data = b""
    try:
        r = urlopen(req, timeout=30)
        return r.status, r.read().decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def must(code, body, tag):
    if code != 200:
        raise RuntimeError("%s HTTP %d: %s" % (tag, code, body[:200]))
    return json.loads(body)

def load_filters():
    try:
        data = pub("/fapi/v1/exchangeInfo?symbol=BTCUSDT")
        for s in data.get("symbols", []):
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    _info["stepSize"][s["symbol"]] = f.get("stepSize", "0.001")
                if f.get("filterType") == "MIN_NOTIONAL":
                    _info["minNotional"][s["symbol"]] = f.get("notional", "0")
    except Exception:
        pass  # 拿不到就用兜底精度

def q_step(sym):
    return Decimal(_info["stepSize"].get(sym, "0.001"))

def fmt_qty(sym, qty):
    # 尺寸精度兜底：无过滤表时按价格粗决定位（>10000的币按0.001步长）
    step = q_step(sym)
    v = (Decimal(str(qty)) // step) * step
    if v <= 0:
        v = step
    return format(v.normalize(), "f")

def real_account():
    """拉实时账户，替代快照里可能为 null 的 account 段"""
    acc = must(*call("GET", "/fapi/v2/account", {}), "ACCOUNT")
    positions = []
    code, body = call("GET", "/fapi/v2/positionRisk", {})
    for e in must(code, body, "POSITION"):
        amt = float(e.get("positionAmt") or 0)
        if amt != 0.0:
            positions.append({
                "symbol": e.get("symbol"),
                "side": "long" if amt > 0 else "short",
                "amount": amt,
                "entryPrice": float(e.get("entryPrice") or 0),
                "unrealizedPnl": e.get("unRealizedProfit"),
            })
    return {"balance_usdt": float(acc["totalWalletBalance"]), "positions": positions}

def load_snapshot(symbol):
    p = os.path.join(BASE, "state", "latest_%s.json" % symbol)
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}

def journal(record):
    line = json.dumps(record, ensure_ascii=False)
    day = time.strftime("%Y%m%d", time.localtime())
    with open(os.path.join(LOG_DIR, "exec_%s.jsonl" % day), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)

def cancel_stale(sym, strict=False):
    """撤该 symbol 全部挂单（旧 SL/TP 条件单清零）；strict=True 时失败抛异常阻断主流程"""
    code, body = call("DELETE", "/fapi/v1/allOpenOrders", {"symbol": sym})
    if strict:
        must(code, body, "CANCEL_STALE")
    out = {"code": code, "body": (body or "")[:160]}
    # -4120 迁移侧：algo 条件单在 /fapi/v1/openAlgoOrders，经典 allOpenOrders 撤不到，单独清
    lcode, lbody = call("GET", "/fapi/v1/openAlgoOrders", {"symbol": sym})
    if lcode != 200:
        if strict:
            raise RuntimeError("OPEN_ALGO_LIST HTTP %d: %s" % (lcode, (lbody or "")[:120]))
        out["algo_list_error"] = (lbody or "")[:120]
        return out
    r = json.loads(lbody)
    items = r.get("orders", []) if isinstance(r, dict) else (r or [])
    swept = 0
    for it in items:
        aid = it.get("algoId")
        c2, b2 = call("DELETE", "/fapi/v1/algoOrder", {"symbol": sym, "algoId": aid})
        swept += 1
        if strict and c2 != 200:
            raise RuntimeError("CANCEL_ALGO HTTP %d: %s" % (c2, (b2 or "")[:120]))
    out["algo_swept"] = swept
    return out

def wait_fill(sym, order_id, tries=6, pause=0.5):
    """轮询查单确认真实成交量。返回 (filled, order_detail)；超时未确认 filled 为 None"""
    for _ in range(tries):
        code, body = call("GET", "/fapi/v1/order", {"symbol": sym, "orderId": order_id})
        if code == 200:
            o = json.loads(body)
            if o.get("status") in ("FILLED", "CANCELED", "EXPIRED", "REJECTED"):
                return float(o.get("executedQty") or 0), o
        time.sleep(pause)
    return None, None

def load_cfg():
    """读 cfg.json；失败返回空 dict（用默认值兜底）"""
    try:
        with open(os.path.join(BASE, "cfg.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def load_peak():
    try:
        with open(PEAK_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_peak(d):
    tmp = PEAK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PEAK_PATH)

def set_peak(sym, side, entry_px, sl):
    d = load_peak()
    d[sym] = {"side": side, "entry": entry_px, "sl": sl,
              "peak": entry_px, "ts": int(time.time() * 1000)}
    save_peak(d)

def clear_peak(sym):
    d = load_peak()
    if sym in d:
        del d[sym]
        save_peak(d)

def split_tiers(qty, tiers, sym):
    """按比例拆分数量到分批档，逐档向下取整到 step；不够拆的档跳过（并入尾仓）。
    tiers 末尾一项视为尾仓比例，不拆成分批单；其余为分批止盈档。
    返回 (list[(ratio, qty_str)], tail_str)；tail 为尾仓（跟踪止损用），不足 step 时为 0。"""
    step = q_step(sym)
    qty_dec = Decimal(str(qty))
    batch_tiers = list(tiers[:-1]) if len(tiers) > 1 else []
    used = Decimal("0")
    out = []
    for r in batch_tiers:
        raw = (qty_dec * Decimal(str(r)) // step) * step
        if raw <= 0:
            continue
        out.append((r, format(raw.normalize(), "f")))
        used += raw
    tail = (qty_dec - used) // step * step
    if tail < step:
        tail = Decimal("0")
    return out, format(tail.normalize(), "f")

def place_exit(dec, xside, entry_px, filled_qty, cfg):
    """开仓后挂全套出场：常规全平止损 + 分批止盈（30/30/40 按 R 分档） + 尾仓跟踪止损。
    xside=SELL 表示多单的平仓方向，BUY 表示空单平仓方向。
    数量拆不开的档自动跳过并入尾仓，宁可少分档不硬挂 0 数量。"""
    sym = dec["symbol"]
    out = {}
    sl = float(dec["stop_loss"])
    dist = abs(entry_px - sl)          # 1R 的价格距离
    if dist <= 0:
        raise RuntimeError("EXIT_DIST_ZERO")
    ex = cfg.get("exit") or {}
    tiers = ex.get("tiers") or [0.30, 0.30, 0.40]
    tier1_r = float(ex.get("tier1_r", 1.0))
    tier2_r = float(ex.get("tier2_r", 2.0))
    callback = str(ex.get("trailing_callback_pct", 1.0))
    act_r = float(ex.get("trailing_activation_r", 2.0))
    sign = 1 if xside == "SELL" else -1   # 多单盈利向上 +1，空单向下 -1
    t1 = entry_px + sign * dist * tier1_r
    t2 = entry_px + sign * dist * tier2_r
    # 1) 常规全平止损（兜底，触发即清仓）
    code, body = call("POST", "/fapi/v1/algoOrder", {
        "symbol": sym, "side": xside, "type": "STOP_MARKET", "algoType": "CONDITIONAL",
        "triggerPrice": format(Decimal(str(sl)), "f"),
        "closePosition": "true", "workingType": "CONTRACT_PRICE"})
    out["stop_loss"] = must(code, body, "STOP_LOSS")
    # 2) 分批止盈：档1(1R)/档2(2R) 各挂一张部分量 TAKE_PROFIT_MARKET
    tier_qty, tail_str = split_tiers(filled_qty, tiers, sym)
    trigs = [t1, t2]
    out["tiers"] = []
    for i, (ratio, qstr) in enumerate(tier_qty):
        if i >= len(trigs):
            break
        code, body = call("POST", "/fapi/v1/algoOrder", {
            "symbol": sym, "side": xside, "type": "TAKE_PROFIT_MARKET", "algoType": "CONDITIONAL",
            "triggerPrice": format(trigs[i], "f"), "quantity": qstr,
            "reduceOnly": "true", "workingType": "CONTRACT_PRICE"})
        out["tiers"].append({"ratio": ratio, "qty": qstr,
                             "trigger": trigs[i], "resp": must(code, body, "TP_TIER%d" % (i + 1))})
    # 3) 尾仓跟踪止损（从峰值回调 callback% 触发，reduceOnly 只平不减）
    if tail_str != "0":
        activation = format(entry_px + sign * dist * act_r, "f")
        code, body = call("POST", "/fapi/v1/algoOrder", {
            "symbol": sym, "side": xside, "type": "TRAILING_STOP_MARKET", "algoType": "CONDITIONAL",
            "quantity": tail_str, "callbackRate": callback, "activationPrice": activation,
            "reduceOnly": "true", "workingType": "CONTRACT_PRICE"})
        out["trailing"] = {"qty": tail_str, "activation": activation,
                           "resp": must(code, body, "TRAILING")}
    else:
        out["trailing"] = None
    return out

def get_open_sl(sym):
    """查当前挂着的常规止损单（STOP_MARKET algo 单）"""
    code, body = call("GET", "/fapi/v1/openAlgoOrders", {"symbol": sym})
    if code != 200:
        return None
    try:
        for it in json.loads(body):
            if it.get("orderType") == "STOP_MARKET":
                return it
    except Exception:
        return None
    return None

def raise_breakeven(sym, rec, cfg):
    """价格已走过 1R 且止损还在原价时，把止损抬到入场价上方 buffer 处（保本线）"""
    side = rec.get("side", "long")
    entry = float(rec["entry"])
    sl = float(rec.get("sl") or 0)
    if sl <= 0:
        return {"action": "no_sl_record"}
    dist = abs(entry - sl)
    ex = cfg.get("exit") or {}
    tier1_r = float(ex.get("tier1_r", 1.0))
    buffer_pct = float(ex.get("be_buffer_pct", 0.002))
    sign = 1 if side == "long" else -1
    be_price = entry + sign * entry * buffer_pct   # 入场价上方一点点（含手续费余量）
    target1 = entry + sign * dist * tier1_r
    peak = float(rec["peak"])
    reached = (peak >= target1) if side == "long" else (peak <= target1)
    if not reached:
        return {"action": "wait"}                  # 还没走过 1R，不抬
    cur = get_open_sl(sym)
    if not cur:
        return {"action": "no_sl_open"}            # 止损单不见了（可能已触发/被撤）
    cur_px = float(cur.get("triggerPrice") or 0)
    need_raise = (cur_px < be_price) if side == "long" else (cur_px > be_price)
    if not need_raise:
        return {"action": "already"}               # 已经在保本线上方/下方
    aid = cur.get("algoId")
    c, b = call("DELETE", "/fapi/v1/algoOrder", {"symbol": sym, "algoId": aid})
    must(c, b, "BE_CANCEL")
    code, body = call("POST", "/fapi/v1/algoOrder", {
        "symbol": sym, "side": "SELL" if side == "long" else "BUY",
        "type": "STOP_MARKET", "algoType": "CONDITIONAL",
        "triggerPrice": format(be_price, "f"),
        "closePosition": "true", "workingType": "CONTRACT_PRICE"})
    out = must(code, body, "BE_RAISE")
    return {"action": "raised", "be_price": be_price, "algo": out}

def manage_exit(symbol, cfg):
    """出场巡检（volwatch 每轮调用）：
       1) 有持仓则更新峰值；2) 峰值回撤超阈值 -> 主动平仓；3) 走过 1R -> 抬保本止损。"""
    sym = symbol
    rec = load_peak().get(sym)
    if not rec:
        return {"action": "no_peak"}
    code, body = call("GET", "/fapi/v2/positionRisk", {"symbol": sym})
    rows = [e for e in must(code, body, "MANAGE_POS") if float(e.get("positionAmt") or 0) != 0.0]
    if not rows:
        clear_peak(sym)
        return {"action": "no_position_cleared"}
    px = float(pub("/fapi/v1/ticker/price?symbol=%s" % sym)["price"])
    side = rec.get("side", "long")
    peak = float(rec["peak"])
    if (side == "long" and px > peak) or (side == "short" and px < peak):
        rec["peak"] = px
        rec["ts"] = int(time.time() * 1000)
        d = load_peak(); d[sym] = rec; save_peak(d)
    ex = cfg.get("exit") or {}
    pull = float(ex.get("pullback_close_pct", 0.6))
    dd = ((peak - px) / peak) if side == "long" else ((px - peak) / peak) if peak > 0 else 0
    if dd >= pull:
        outcome = market_close({"symbol": sym})   # 主动平仓（回吐超阈值）
        return {"action": "close_drawdown", "dd": round(dd, 4), "outcome": outcome}
    be = raise_breakeven(sym, rec, cfg)
    return {"action": "manage", "dd": round(dd, 4), "be": be}

def market_open(dec, px):
    sym, action = dec["symbol"], dec["action"]
    side = "BUY" if action == "open_long" else "SELL"
    xside = "SELL" if action == "open_long" else "BUY"
    cancel_stale(sym, strict=True)  # 开仓前撤旧条件单：残留单会误伤新仓
    amount = float(dec["notional_usdt"]) / px
    code, body = call("POST", "/fapi/v1/order", {
        "symbol": sym, "side": side, "type": "MARKET", "quantity": fmt_qty(sym, amount)})
    o = must(code, body, "OPEN")
    filled, cur = wait_fill(sym, o.get("orderId"))  # 不复信响应 executedQty，轮询确认
    if not filled or filled <= 0:
        raise RuntimeError("OPEN_FILL_UNCONFIRMED status=%s filled=%s" %
                           ((cur or {}).get("status"), filled))
    out = {"entry": {"orderId": o.get("orderId"), "status": (cur or {}).get("status"),
                     "executedQty": filled, "avgPrice": (cur or {}).get("avgPrice")}}
    cfg = load_cfg()
    entry_px = float((cur or {}).get("avgPrice") or px)
    out["exit"] = place_exit(dec, xside, entry_px, filled, cfg)
    set_peak(sym, "long" if action == "open_long" else "short", entry_px, float(dec["stop_loss"]))
    return out

def market_close(dec):
    sym = dec["symbol"]
    cancelled = cancel_stale(sym)  # 平仓前先撤 SL/TP，防触发竞争
    code, body = call("GET", "/fapi/v2/positionRisk", {"symbol": sym})
    rows = [e for e in must(code, body, "POSITION") if float(e.get("positionAmt") or 0) != 0.0]
    if not rows:
        return {"result": "no_position", "cancelled": cancelled}
    out = []
    for e in rows:
        amt = abs(float(e["positionAmt"]))
        side = "SELL" if float(e["positionAmt"]) > 0 else "BUY"
        code, body = call("POST", "/fapi/v1/order", {
            "symbol": sym, "side": side, "type": "MARKET",
            "quantity": fmt_qty(sym, amt), "reduceOnly": "true"})
        o = must(code, body, "CLOSE")
        filled, cur = wait_fill(sym, o.get("orderId"))  # 不复信响应，轮询确认平仓量
        if not filled or filled <= 0:
            raise RuntimeError("CLOSE_FILL_UNCONFIRMED status=%s filled=%s" %
                               ((cur or {}).get("status"), filled))
        out.append({"orderId": o.get("orderId"), "status": (cur or {}).get("status"),
                    "executedQty": filled, "avgPrice": (cur or {}).get("avgPrice")})
    cleanup = cancel_stale(sym)  # 平仓后兜底再撤一遍，条件单零残留
    clear_peak(sym)
    return {"orders": out, "cancelled": cancelled, "cleanup": cleanup}

def emergency_flatten(sym):
    try:
        cancel_stale(sym)  # 先撤条件单，防平仓过程中 SL/TP 触发打架
        code, body = call("GET", "/fapi/v2/positionRisk", {"symbol": sym})
        for e in must(code, body, "FLATTEN_PROBE"):
            amt = float(e.get("positionAmt") or 0)
            if amt != 0.0:
                code2, body2 = call("POST", "/fapi/v1/order", {
                    "symbol": sym, "side": "SELL" if amt > 0 else "BUY",
                    "type": "MARKET", "quantity": fmt_qty(sym, abs(amt)),
                    "reduceOnly": "true"})
                must(code2, body2, "FLATTEN")
        cancel_stale(sym)  # 收尾再清一遍残留挂单
        clear_peak(sym)
        return "FLATTENED"
    except Exception as ex2:
        try:
            cancel_stale(sym)
        except Exception:
            pass
        return "FLATTEN_FAILED: " + repr(ex2)[:120]

def run(raw, symbol, dry, source="text"):
    global _offset
    t0 = must(*call("GET", "/fapi/v1/time", {}), "TIME")["serverTime"] - int(time.time() * 1000)
    _offset = t0
    load_filters()
    snap = load_snapshot(symbol)
    snap["account"] = real_account()
    res = guard.check(raw, snap)
    dec = res.get("decision") or {}
    rec = {"ts": int(time.time() * 1000), "symbol": symbol, "dry_run": dry,
           "source": source, "decision": dec,
           "guard": {"passed": res["passed"], "reasons": res["reasons"]}}
    if not res["passed"]:
        journal({**rec, "stage": "guard", "result": "REJECTED"})
        return {"guard_passed": False, "action": dec.get("action"), "result": "REJECTED",
                "reasons": res["reasons"]}, 2
    act = dec.get("action")
    if act in ("hold", "invalid"):
        journal({**rec, "stage": "final", "result": act})
        return {"guard_passed": True, "action": act, "result": act}, 0
    if dry:
        journal({**rec, "stage": "dry_run", "result": "GUARD_PASS_NOT_SENT"})
        return {"guard_passed": True, "action": act, "result": "GUARD_PASS_NOT_SENT"}, 0
    try:
        px = float(snap.get("market_data", {}).get("price") or 0)
        if px <= 0:
            px = float(pub("/fapi/v1/ticker/price?symbol=%s" % symbol)["price"])
        outcome = market_close(dec) if act == "close" else market_open(dec, px)
        journal({**rec, "stage": "final", "result": act, "outcome": outcome})
        return {"guard_passed": True, "action": act, "result": "SENT", "outcome": outcome}, 0
    except Exception as e:
        flat = emergency_flatten(symbol)
        journal({**rec, "stage": "final", "result": "ERROR", "safety": flat,
                 "error": repr(e)[:300], "tail": traceback.format_exc()[-600:]})
        return {"guard_passed": True, "action": act, "result": "ERROR",
                "error": repr(e)[:300], "safety": flat}, 4

def run_manage(symbol):
    """独立出场巡检：被 volwatch 每轮调用。只读+条件平仓，不决策。"""
    try:
        t0 = must(*call("GET", "/fapi/v1/time", {}), "TIME")["serverTime"]
        _offset = t0 - int(time.time() * 1000)
        load_filters()
        cfg = load_cfg()
        res = manage_exit(symbol, cfg)
        print(json.dumps(res, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"action": "error", "error": repr(e)[:300]}, ensure_ascii=False))
        return 1

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", nargs="?", default="BTCUSDT")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--file")
    g.add_argument("--text")
    ap.add_argument("--manage", action="store_true", help="出场巡检模式（volwatch 每轮调用）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    os.makedirs(LOG_DIR, exist_ok=True)
    if args.manage:
        sys.exit(run_manage(args.symbol))
    raw = open(args.file, encoding="utf-8").read() if args.file else args.text
    summary, code = run(raw, args.symbol, args.dry_run,
                        "file" if args.file else "text")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(code)

if __name__ == "__main__":
    main()