#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
exp_trailing.py - 交易所能力探路脚本（不碰真实持仓，只验证挂/查/删）

目的：零号要上「分批止盈(30/30/40) + 交易所跟踪止损」，动手改代码前先确认
      binance testnet 的 algo 条件单端点到底支不支持：
        1) TRAILING_STOP_MARKET（跟踪止损：从最高点回调 X% 触发）
        2) TAKE_PROFIT_MARKET 部分数量版（不带 closePosition，只平指定数量）
      以及创建 -> 查询 -> 删除整条链路是否可用。

安全设计：
  - 只挂"远触发价"的单，几秒内不会触发，验证完立即删除，不留残单；
  - 只删本脚本自己创建的 algoId，不碰账户里已有单；
  - 每次请求间隔 0.3s，防 testnet 限频 418。

用法（Termux 侧，真身目录）：
  cd ~/binance && python3 bin/exp_trailing.py [--symbol BTCUSDT] [--callback 0.1]
输出贴给艾艾酱即可。
"""
import sys, os, json, time, hmac, hashlib, argparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTNET = "https://testnet.binancefuture.com"
_key = json.load(open(os.path.join(BASE, "keys.json")))
API_KEY, API_SECRET = _key["apiKey"], _key["secret"]

_gap = 0.3
_last = 0.0

def call(method, path, params):
    global _last
    w = _gap - (time.time() - _last)
    if w > 0:
        time.sleep(w)
    _last = time.time()
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
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

def show(tag, code, body, maxlen=300):
    print("--- %s -> HTTP %d" % (tag, code))
    print((body or "")[:maxlen].replace("\n", " "))
    print()
    return code == 200

def min_qty(sym):
    """从 exchangeInfo 拿 LOT_SIZE 最小数量，拿不到就兜底尝试 0.001"""
    try:
        data = json.loads(urlopen(TESTNET + "/fapi/v1/exchangeInfo?symbol=" + sym, timeout=20)
                          .read().decode("utf-8", "replace"))
        for s in data.get("symbols", []):
            if s["symbol"] != sym:
                continue
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    return f.get("stepSize", "0.001")
    except Exception:
        pass
    return "0.001"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--callback", default="0.1", help="跟踪止损回调率(百分数)，默认0.1，远触发不真碰")
    args = ap.parse_args()
    sym = args.symbol.upper()
    qty = min_qty(sym)

    print("==== 交易所能力探路: %s (最小数量 %s) ====" % (sym, qty))
    print()

    # 0) 探活
    if not show("1. 时间探活 /fapi/v1/time", *call("GET", "/fapi/v1/time", {})):
        print("账户/网络不通，先查 keys.json 是否已填真 key")
        sys.exit(1)

    # 1) exchangeInfo: orderTypes 里有没有这两个类型
    code, body = call("GET", "/fapi/v1/exchangeInfo", {"symbol": sym})
    info_ok = show("2. exchangeInfo", code, body, 400)
    order_types = []
    if info_ok:
        try:
            for s in json.loads(body).get("symbols", []):
                if s["symbol"] == sym:
                    order_types = s.get("orderTypes", [])
        except Exception:
            pass
    print("该品种支持的单类型:", order_types)
    print("  TRAILING_STOP_MARKET 支持:", "TRAILING_STOP_MARKET" in order_types)
    print("  TAKE_PROFIT_MARKET    支持:", "TAKE_PROFIT_MARKET" in order_types)
    print()

    # 2) 挂一张跟踪止损单（远触发价，验证创建+查询+删除）
    code, body = call("GET", "/fapi/v1/ticker/price", {"symbol": sym})
    px = float(json.loads(body)["price"])
    # 多头侧激活价放现价+10%，回调率给很小，几秒内不会真触发
    activation = format(px * 1.10, "f")
    c, b = call("POST", "/fapi/v1/algoOrder", {
        "symbol": sym, "side": "SELL", "type": "TRAILING_STOP_MARKET",
        "algoType": "CONDITIONAL", "quantity": qty,
        "callbackRate": args.callback, "activationPrice": activation,
        "reduceOnly": "true", "workingType": "CONTRACT_PRICE"})
    ok = show("3. 创建 TRAILING_STOP_MARKET", c, b)
    algo_id = ""
    try:
        algo_id = str(json.loads(b).get("algoId", ""))
    except Exception:
        pass
    if not ok:
        print(">>> 创建跟踪止损失败，testnet 可能不支持或参数不对，把上面 HTTP 信息贴给艾艾酱")
        sys.exit(2)
    print(">>> 创建的 algoId:", algo_id)

    # 3) 查询：openAlgoOrders 列表里应能见到
    c, b = call("GET", "/fapi/v1/openAlgoOrders", {"symbol": sym})
    show("4. 查询 openAlgoOrders", c, b)
    found = algo_id in b if algo_id else False
    print(">>> 列表里能找到刚创建的单:", found)

    # 4) 删除刚创建的单
    c, b = call("DELETE", "/fapi/v1/algoOrder", {"symbol": sym, "algoId": algo_id})
    show("5. 删除刚创建的单", c, b, 200)
    # 复核列表已清
    c, b = call("GET", "/fapi/v1/openAlgoOrders", {"symbol": sym})
    show("6. 复核 openAlgoOrders(应回到原状态)", c, b)
    print()

    # 5) 部分数量止盈单：挂一张远触发 TAKE_PROFIT_MARKET（不带 closePosition）
    tp_price = format(px * 1.20, "f")
    c, b = call("POST", "/fapi/v1/algoOrder", {
        "symbol": sym, "side": "SELL", "type": "TAKE_PROFIT_MARKET",
        "algoType": "CONDITIONAL", "quantity": qty,
        "triggerPrice": tp_price, "reduceOnly": "true", "workingType": "CONTRACT_PRICE"})
    ok2 = show("7. 创建部分量 TAKE_PROFIT_MARKET", c, b)
    algo_id2 = ""
    try:
        algo_id2 = str(json.loads(b).get("algoId", ""))
    except Exception:
        pass
    if ok2 and algo_id2:
        c, b = call("DELETE", "/fapi/v1/algoOrder", {"symbol": sym, "algoId": algo_id2})
        show("8. 删除部分量止盈单", c, b, 200)

    print()
    print("==== 结论 ====")
    print("跟踪止损创建+查询+删除:", "OK" if ok and found else "有问题(见上)")
    print("部分数量止盈创建+删除:", "OK" if ok2 and algo_id2 else "有问题(见上)")
    print("若 OK，就可以放心按 30/30/40 分批 + 跟踪止损尾仓的方案改代码；")
    print("若某一步失败，把上面对应 HTTP 报错原样贴给艾艾酱，我按真实报错调整参数。")

if __name__ == "__main__":
    main()