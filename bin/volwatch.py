#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
volwatch.py - 常驻调度器（定时兜底 + 波动唤醒）

Termux 没有 crond，所以自己睡自己醒。每一轮做两件事：
  1) 定时兜底：每隔 HOURLY_SEC 秒，按三品种轮换顺序叫一次零号决策
  2) 波动唤醒：每 TICK_SEC 秒探三品种现价，与「上次决策基线」比，
     涨跌超 vol_threshold_pct 且过了冷却，就提前叫零号

唤醒之后的完整链路由 decide.py 内部串起来：
  decide.py -> guard.py -> executor.py
本脚本只管「什么时候叫」，不管「决策对不对」，也不判单。

用法:
  python3 volwatch.py                 # 前台常驻（先这么跑，看着它工作）
  python3 volwatch.py --once          # 只跑一轮就退出（调试用）
  python3 volwatch.py --once --no-vol # 只做定时兜底判断，不探价
  python3 volwatch.py --status        # 看基线与冷却状态

建议配合 termux-wake-lock 使用，否则息屏后可能被系统冻结：
  termux-wake-lock
  nohup python3 volwatch.py >> log/volwatch.out 2>&1 &
  termux-wake-unlock   # 要停的时候
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(BASE, "bin")
STATE = os.path.join(BASE, "state")
LOG = os.path.join(BASE, "log")
CFG_PATH = os.path.join(BASE, "cfg.json")
VOL_STATE_PATH = os.path.join(STATE, "vol_state.json")
CURSOR_PATH = os.path.join(STATE, "hourly_cursor.json")
OUT_LOG = os.path.join(LOG, "volwatch_%s.out" % time.strftime("%Y-%m-%d"))

TESTNET = "https://testnet.binancefuture.com"
TICK_SEC = 900           # 探价间隔
HOURLY_SEC = 600         # 定时轮换间隔：10 分钟换一个品种（名字沿用旧称 cycle）
DRY = os.environ.get("VOLWATCH_DRY") == "1"   # 1=只跑决策+守门员，不下单

os.makedirs(STATE, exist_ok=True)
os.makedirs(LOG, exist_ok=True)


def now_ms():
    return int(time.time() * 1000)


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def logline(s):
    line = "[%s] %s" % (now_str(), s)
    try:
        with open(OUT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def probe(sym):
    """探现价。不带签名，公开端点。返回 (px, err)。"""
    url = TESTNET + "/fapi/v1/ticker/price?symbol=" + sym
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "zero-volwatch/2"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return float(json.loads(r.read().decode())["price"]), None
    except urllib.error.HTTPError as e:
        return None, "HTTP%d" % e.code
    except Exception as e:
        return None, type(e).__name__


def refresh_snapshot(sym):
    """拉一次快照。失败不中断循环，只记日志。"""
    sp = os.path.join(BIN, "snapshot.py")
    if not os.path.exists(sp):
        logline("SNAPSHOT_MISSING %s" % sp)
        return False
    try:
        r = subprocess.run(
            [sys.executable, sp, sym],
            cwd=BIN, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            logline("SNAPSHOT_FAIL %s rc=%d %s" % (sym, r.returncode, (r.stderr or "")[-300:]))
            return False
        return True
    except Exception as e:
        logline("SNAPSHOT_EXC %s %s" % (sym, e))
        return False


def refresh_all(symbols):
    """每轮无条件给所有品种刷一次快照，失败只记日志不中断。"""
    for sym in symbols:
        ok = refresh_snapshot(sym)
        logline("SNAPSHOT_TICK %s %s" % (sym, "OK" if ok else "FAIL"))


def run_chain(sym, why, px=None):
    """
    跑一次完整决策链：decide -> guard -> executor
    每一步退出码不阻断后续（guard 拒绝是正常结果，不是故障）。
    用 && 串会在 guard 返回 2 时停住，这里显式分开跑，保证日志都拿到。
    """
    t0 = now_ms()
    logline("CHAIN_START %s why=%s px=%s" % (sym, why, px))

    # 1) 拉新快照
    if not refresh_snapshot(sym):
        logline("CHAIN_ABORT %s 快照拉取失败" % sym)
        return 1

    # 2) 决策
    dp = os.path.join(BIN, "decide.py")
    try:
        r = subprocess.run(
            [sys.executable, dp, sym, "--reason", why],
            cwd=BIN, capture_output=True, text=True, timeout=240)
        rc = r.returncode
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        logline("DECIDE %s rc=%d\n%s" % (sym, rc, out[-1200:]))
        if rc != 0:
            logline("CHAIN_ABORT %s 决策失败 rc=%d" % (sym, rc))
            return rc
    except Exception as e:
        logline("DECIDE_EXC %s %s" % (sym, e))
        return 1

    # 3) 守门员
    gp = os.path.join(BIN, "guard.py")
    if not os.path.exists(gp):
        logline("GUARD_MISSING %s" % gp)
        return 1
    decision_path = os.path.join(STATE, "decision_%s.json" % sym)
    try:
        r = subprocess.run(
            [sys.executable, gp, "--file", decision_path, "--sym", sym],
            cwd=BIN, capture_output=True, text=True, timeout=60)
        grc = r.returncode
        gout = ((r.stdout or "") + (r.stderr or "")).strip()
        logline("GUARD %s rc=%d\n%s" % (sym, grc, gout[-800:]))
    except Exception as e:
        logline("GUARD_EXC %s %s" % (sym, e))
        return 1

    if grc == 2:
        logline("CHAIN_STOP %s 守门员拒单（终局，不重试不改单）" % sym)
        return 0
    if grc != 0:
        logline("CHAIN_STOP %s 守门员异常 rc=%d" % (sym, grc))
        return grc

    # 4) 执行器
    ep = os.path.join(BIN, "executor.py")
    if not os.path.exists(ep):
        logline("EXEC_MISSING %s" % ep)
        return 1
    try:
        cmd = [sys.executable, ep, "--file", decision_path, sym]
        if DRY:
            cmd.append("--dry-run")
        r = subprocess.run(
            cmd,
            cwd=BIN, capture_output=True, text=True, timeout=180)
        erc = r.returncode
        eout = ((r.stdout or "") + (r.stderr or "")).strip()
        logline("EXEC %s rc=%d\n%s" % (sym, erc, eout[-1200:]))
        # 5) 出场巡检：独立子进程，只读+条件平仓，不决策
        try:
            mcmd = [sys.executable, ep, "--manage", sym]
            mr = subprocess.run(mcmd, cwd=BIN, capture_output=True, text=True, timeout=90)
            mout = ((mr.stdout or "") + (mr.stderr or "")).strip()
            logline("EXIT_MANAGE %s rc=%d\n%s" % (sym, mr.returncode, mout[-600:]))
        except Exception as me:
            logline("EXIT_MANAGE_EXC %s %s" % (sym, me))
        logline("CHAIN_DONE %s rc=%d 耗时%dms" % (sym, erc, now_ms() - t0))
        return erc
    except Exception as e:
        logline("EXEC_EXC %s %s" % (sym, e))
        return 1


def next_symbol(symbols):
    """三品种轮换游标。"""
    cur = load_json(CURSOR_PATH, {"idx": 0})
    idx = int(cur.get("idx", 0)) % len(symbols)
    sym = symbols[idx]
    save_json(CURSOR_PATH, {"idx": (idx + 1) % len(symbols), "last": sym, "ts": now_ms()})
    return sym


def do_hourly(cfg, symbols):
    """定时唤醒：所有品种都跑一次决策链。"""
    logline("HOURLY_TRIG 全品种 %s" % ",".join(symbols))
    rc = 0
    for sym in symbols:
        rc = run_chain(sym, "hourly")
    return rc


def do_vol(cfg, symbols, vol_state):
    """探价并判断波动。返回触发次数。"""
    thr_pct = float(cfg.get("vol_threshold_pct", 0.5))
    now = now_ms()
    fired = 0
    for sym in symbols:
        px, err = probe(sym)
        if err:
            logline("PROBE_FAIL %s %s" % (sym, err))
            continue

        base = vol_state["baseline"].get(sym)
        if not base:
            vol_state["baseline"][sym] = {"px": px, "ts": now}
            logline("BASE_INIT %s px=%s" % (sym, px))
            continue

        move = abs(px - base["px"]) / base["px"] * 100

        if move >= thr_pct:
            why = "vol%.2f%%" % move
            logline("VOL_TRIG %s %s px=%s base=%s" % (sym, why, px, base["px"]))
            run_chain(sym, why, px)
            vol_state["baseline"][sym] = {"px": px, "ts": now_ms()}
            vol_state["last_trig"][sym] = now_ms()
            fired += 1
            time.sleep(2)
        else:
            logline("QUIET %s px=%s move=%.3f%%" % (sym, px, move))
    return fired


def load_vol_state():
    st = load_json(VOL_STATE_PATH, {})
    st.setdefault("baseline", {})
    st.setdefault("last_trig", {})
    return st


def main():
    cfg = load_json(CFG_PATH)
    symbols = cfg.get("symbols") or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    args = sys.argv[1:]
    if "--status" in args:
        print(json.dumps({
            "vol_state": load_vol_state(),
            "cursor": load_json(CURSOR_PATH, {}),
        }, ensure_ascii=False, indent=1))
        return 0

    once = "--once" in args
    no_vol = "--no-vol" in args

    logline("=== volwatch 启动 品种=%s 波动阈值=%s%% 兜底间隔=%ds ==="
            % (",".join(symbols), cfg.get("vol_threshold_pct", 0.5), HOURLY_SEC))

    vol_state = load_vol_state()
    last_hourly = now_ms()
    # 启动时先初始化基线，避免第一轮就因「无基线」瞎触发
    if not no_vol:
        for sym in symbols:
            if not vol_state["baseline"].get(sym):
                px, err = probe(sym)
                if not err:
                    vol_state["baseline"][sym] = {"px": px, "ts": now_ms()}
                    logline("BASE_INIT %s px=%s" % (sym, px))
        save_json(VOL_STATE_PATH, vol_state)

    if once:
        do_hourly(cfg, symbols)
        if not no_vol:
            do_vol(cfg, symbols, vol_state)
            save_json(VOL_STATE_PATH, vol_state)
        return 0

    while True:
        try:
            # 每轮无条件刷全部品种快照（不论行情动不动）
            refresh_all(symbols)

            # 定时唤醒：每轮循环都跑一次全部品种决策
            do_hourly(cfg, symbols)
            last_hourly = now_ms()

            # 波动：每轮都探
            if not no_vol:
                vol_state = load_vol_state()
                do_vol(cfg, symbols, vol_state)
                save_json(VOL_STATE_PATH, vol_state)

            time.sleep(TICK_SEC)
        except KeyboardInterrupt:
            logline("收到 Ctrl-C，退出")
            return 0
        except Exception as e:
            logline("LOOP_EXC %s: %s（继续下一轮）" % (type(e).__name__, e))
            time.sleep(TICK_SEC)


if __name__ == "__main__":
    sys.exit(main())