#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
snapshot.py - 拉三品种合约快照并落盘。纯标准库，不下单、不决策。

用法:
    python3 snapshot.py              # 拉 cfg 里全部品种
    python3 snapshot.py BTCUSDT      # 只拉一个

输出:
    state/latest_<SYMBOL>.json      每个品种一个
    log/snapshot_<YYYYMMDD>.jsonl   每次运行的记录

快照结构（与旧 Operit 版保持一致，勿改字段名）:
{
  "ts_ms": 毫秒时间戳,
  "iso": "UTC 时间字符串",
  "market_data": {
    "symbol": "BTCUSDT",
    "price": 110234.5,
    "klines": [[openTime, o, h, l, c, vol], ...],   # 最后 N 根
    "atr": 353.2,                                    # 基于收盘价简单振幅均值
    "high_24h": ..., "low_24h": ...
  },
  "account": {
    "totalWalletBalance": ..., "availableBalance": ...,
    "positions": [ {"symbol","positionAmt","entryPrice","markPrice","unRealizedProfit"}, ... ]
  }
}
"""

import os
import sys
import json
import time
import hmac
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone

BASE = "https://testnet.binancefuture.com"

# 项目根目录 = 本脚本所在目录的上一级
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
LOG_DIR = os.path.join(ROOT, "log")
KEYS_PATH = os.path.join(ROOT, "keys.json")
CFG_PATH = os.path.join(ROOT, "cfg.json")

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
KLINE_LIMIT = 60          # 拿 60 根 5m K 线，够算 ATR
KLINE_INTERVAL = "5m"
SLEEP_BETWEEN = 1.5       # 品种之间间隔，避开限频


def now_ms():
    return int(time.time() * 1000)


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, label):
    if not os.path.exists(path):
        die("%s 不存在: %s" % (label, path))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        die("%s 读取失败 (%s): %s" % (label, path, e))


def die(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def http_get(path, params=None, signed=False, api_key=None, secret=None, timeout=15):
    """统一 GET。signed=True 时追加 timestamp + HMAC-SHA256 签名。"""
    if params is None:
        params = {}
    params = dict(params)

    if signed:
        if not api_key or not secret:
            raise RuntimeError("签名请求缺少 apiKey/secret")
        params["timestamp"] = now_ms()
        params["recvWindow"] = 5000
        query = "&".join("%s=%s" % (k, v) for k, v in params.items())
        sig = hmac.new(secret.encode("utf-8"), query.encode("utf-8"),
                       hashlib.sha256).hexdigest()
        query = query + "&signature=" + sig
    else:
        query = "&".join("%s=%s" % (k, v) for k, v in params.items())

    url = BASE + path + ("?" + query if query else "")
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "snapshot.py/1.0")
    if signed:
        req.add_header("X-MBX-APIKEY", api_key)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError("HTTP %s %s -> %s" % (e.code, path, detail))
    except Exception as e:
        raise RuntimeError("%s 请求失败: %s" % (path, e))


def calc_atr(klines, period=14):
    """用收盘价与最高最低的简单真实振幅均值，够用即可，不做 Wilder 平滑。"""
    if len(klines) < 2:
        return 0.0
    trs = []
    for i in range(1, len(klines)):
        h = float(klines[i][2])
        l = float(klines[i][3])
        prev_c = float(klines[i - 1][4])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    tail = trs[-period:] if len(trs) >= period else trs
    return round(sum(tail) / len(tail), 8)


def fetch_symbol(symbol, api_key, secret):
    """公开段：K 线 + 24h ticker。签名段：账户 + 持仓（只取本品种）。"""
    klines = http_get("/fapi/v1/klines",
                      {"symbol": symbol, "interval": KLINE_INTERVAL,
                       "limit": KLINE_LIMIT})
    klines_slim = []
    for k in klines:
        klines_slim.append([
            int(k[0]), float(k[1]), float(k[2]), float(k[3]),
            float(k[4]), float(k[5])
        ])

    ticker = http_get("/fapi/v1/ticker/24hr", {"symbol": symbol})
    price = float(ticker["lastPrice"])

    atr = calc_atr(klines_slim)

    return {
        "symbol": symbol,
        "price": price,
        "klines": klines_slim,
        "atr": atr,
        "high_24h": float(ticker["highPrice"]),
        "low_24h": float(ticker["lowPrice"]),
        "quoteVolume_24h": float(ticker["quoteVolume"]),
    }


def fetch_account(api_key, secret, symbols):
    acct = http_get("/fapi/v2/account", signed=True,
                    api_key=api_key, secret=secret)
    pos_raw = http_get("/fapi/v2/positionRisk", signed=True,
                       api_key=api_key, secret=secret)

    positions = []
    for p in pos_raw:
        if p.get("symbol") not in symbols:
            continue
        amt = float(p.get("positionAmt", 0) or 0)
        if amt == 0:
            continue
        positions.append({
            "symbol": p["symbol"],
            "positionAmt": amt,
            "entryPrice": float(p.get("entryPrice", 0) or 0),
            "markPrice": float(p.get("markPrice", 0) or 0),
            "unRealizedProfit": float(p.get("unRealizedProfit", 0) or 0),
            "leverage": p.get("leverage"),
        })

    return {
        "totalWalletBalance": float(acct.get("totalWalletBalance", 0) or 0),
        "availableBalance": float(acct.get("availableBalance", 0) or 0),
        "totalUnrealizedProfit": float(acct.get("totalUnrealizedProfit", 0) or 0),
        "positions": positions,
    }


def main():
    cfg = load_json(CFG_PATH, "cfg.json")
    keys = load_json(KEYS_PATH, "keys.json")

    api_key = keys.get("apiKey")
    secret = keys.get("secret")
    if not api_key or not secret:
        die("keys.json 里 apiKey/secret 缺失")

    symbols = cfg.get("symbols") or DEFAULT_SYMBOLS
    args = sys.argv[1:]
    if args:
        symbols = [s.upper() for s in args]

    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    log_path = os.path.join(
        LOG_DIR, "snapshot_%s.jsonl" % datetime.now().strftime("%Y%m%d"))

    # 账户一次拉齐，省请求
    account = fetch_account(api_key, secret, symbols)

    results = []
    for i, sym in enumerate(symbols):
        if i > 0:
            time.sleep(SLEEP_BETWEEN)
        try:
            md = fetch_symbol(sym, api_key, secret)
        except Exception as e:
            print("WARN: %s 拉取失败: %s" % (sym, e), file=sys.stderr)
            results.append({"symbol": sym, "ok": False, "err": str(e)})
            continue

        snap = {
            "ts_ms": now_ms(),
            "iso": iso_now(),
            "market_data": md,
            "account": account,
        }

        out = os.path.join(STATE_DIR, "latest_%s.json" % sym)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)

        results.append({"symbol": sym, "ok": True,
                        "price": md["price"], "atr": md["atr"]})
        print("%-9s price=%-12s atr=%s" % (sym, md["price"], md["atr"]))

    rec = {"ts_ms": now_ms(), "iso": iso_now(),
           "symbols": symbols, "results": results,
           "wallet": account.get("totalWalletBalance")}
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    ok_count = sum(1 for r in results if r["ok"])
    print("DONE %d/%d  wallet=%.4f" % (
        ok_count, len(results), account.get("totalWalletBalance", 0)))
    return 0 if ok_count else 2


if __name__ == "__main__":
    sys.exit(main())
