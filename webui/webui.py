#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webui.py - 零号交易链手机本地看板
只读展示 + 参数修改，不做任何手动下单。
用法: python3 webui.py [端口]   默认 8123
浏览器打开 http://localhost:8123
"""
import json, os, time, glob, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # ../ 即 ~/binance
STATE = os.path.join(BASE, "state")
LOG = os.path.join(BASE, "log")
CFG_PATH = os.path.join(BASE, "cfg.json")
WEBUI = os.path.dirname(os.path.abspath(__file__))

# 页面允许修改的配置键（只放真生效的；risk_pct 等 guard 硬编码的摆设不放）
# 移动止损已改按 ATR 自动计算（trailing_atr_k）；抬保本动作随本地巡检一并停用，
# 回调/激活/回撤/保本余量 均不再开放
EDITABLE = {
    "vol_threshold_pct": "波动阈值(%)",
    "min_sec_between_orders": "下单最小间隔(秒)",
    "exit.tiers": "分批止盈比例",
    "exit.tier1_r": "第1档止盈(R)",
    "exit.tier2_r": "第2档止盈(R)",
    "exit.trailing_atr_k": "尾仓移动止损(ATR倍数)",
}


def load_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(p, d):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def latest(path_pat):
    fs = sorted(glob.glob(path_pat))
    return fs[-1] if fs else None


def read_lines(path, tail=50):
    if not path or not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        chunk = 256 * 1024
        with open(path, "rb") as f:
            if size > chunk:
                f.seek(size - chunk)
                f.readline()
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-tail:]
    except Exception:
        return []


def get_snapshots():
    """三品种快照：价格、新鲜度、钱包、持仓"""
    out = {}
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        p = os.path.join(STATE, "latest_%s.json" % sym)
        if not os.path.exists(p):
            out[sym] = {"ok": False}
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
            md = d.get("market_data", {})
            age = (time.time() * 1000 - d.get("ts_ms", 0)) / 1000 / 60
            out[sym] = {"ok": True, "price": md.get("price"), "atr": md.get("atr"),
                        "age_min": round(age, 1), "ts": d.get("ts_ms")}
        except Exception as e:
            out[sym] = {"ok": False, "err": repr(e)[:80]}
    return out


def get_account():
    """合并所有快照的账户与持仓（只读展示）。

    每个快照文件的 account.positions 只含该品种自己的持仓（snapshot.py 按 symbol 过滤），
    必须把三份 latest_*.json 的持仓合并、按 symbol 去重，否则只取一份会漏仓位
    （如 BTC/ETH 有仓、SOL 无仓时，取 SOL 快照会显示「无持仓」）。
    钱包字段三份一致，取第一份即可。
    """
    acc = {"totalWalletBalance": None, "availableBalance": None,
           "totalUnrealizedProfit": None, "positions": []}
    seen = set()
    for p in sorted(glob.glob(os.path.join(STATE, "latest_*.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        a = d.get("account") or {}
        if acc["totalWalletBalance"] is None:
            acc["totalWalletBalance"] = a.get("totalWalletBalance")
        if acc["availableBalance"] is None:
            acc["availableBalance"] = a.get("availableBalance")
        if acc["totalUnrealizedProfit"] is None:
            acc["totalUnrealizedProfit"] = a.get("totalUnrealizedProfit")
        for pos in (a.get("positions") or []):
            sym = pos.get("symbol")
            if sym and sym not in seen:
                seen.add(sym)
                acc["positions"].append(pos)
    if acc["totalWalletBalance"] is None:
        return {}
    return acc


def get_trades():
    p = os.path.join(STATE, "trades.jsonl")
    if not os.path.exists(p):
        return []
    lines = open(p, encoding="utf-8").read().splitlines()
    trades = [json.loads(l) for l in lines if l.strip()]
    trades.reverse()  # 最新在前
    return trades[:15]


def get_decisions():
    p = latest(os.path.join(LOG, "decide_*.jsonl"))
    if not p:
        return []
    lines = open(p, encoding="utf-8").read().splitlines()
    decs = []
    for l in lines[-8:]:
        try:
            d = json.loads(l)
            acts = [x.get("action") for x in d.get("decisions", [])]
            decs.append({
                "ts": d.get("logged_at", d.get("ts", 0)),
                "model": d.get("model"),
                "wake": d.get("wake_reason"),
                "acts": acts,
            })
        except Exception:
            continue
    return decs


def get_vol_state():
    st = load_json(os.path.join(STATE, "vol_state.json"))
    return st.get("baseline") or {}


def get_cfg():
    return load_json(CFG_PATH)


# ===== 快捷指令（搬自 Termux bashrc，白名单只读查询，不开放任意命令）=====
def tail_file(path, n=50):
    """只读文件尾部 N 行：超大文件也只 seek 到末尾一小段读，避免整读卡死。

    之前的实现先整读再 splitlines，volwatch_live.out 这类长期累积的日志
    会越来越大，整读会阻塞请求 → 网页按钮看起来「卡死」。
    """
    if not path or not os.path.exists(path):
        return "(无文件)"
    try:
        size = os.path.getsize(path)
        chunk = 256 * 1024  # 最多只读尾部 256KB
        with open(path, "rb") as f:
            if size > chunk:
                f.seek(size - chunk)
                f.readline()  # 跳到整行开头，避免截断一半
            data = f.read().decode("utf-8", "replace")
        lines = data.splitlines()
        return "\n".join(lines[-n:]) if lines else ""
    except Exception as e:
        return "(读文件失败: %r)" % e


def cmd_snap():
    out = []
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        p = os.path.join(STATE, "latest_%s.json" % sym)
        try:
            d = json.load(open(p, encoding="utf-8"))
            age = (time.time() * 1000 - d.get("ts_ms", 0)) / 1000 / 60
            price = d.get("market_data", {}).get("price")
            out.append("%-8s price=%-12s age=%.1fmin" % (sym, price, age))
        except Exception as e:
            out.append("%-8s 读不到: %s" % (sym, e))
    return "\n".join(out) if out else "无快照状态"


def find_volwatch():
    """直接遍历 /proc 找 volwatch 进程，返回 [(pid, cmdline), ...]。

    不用 pgrep/pkill：Android toybox 对相对路径启动的进程匹配失灵，
    会误报「没在跑」。
    且只认「python <volwatch.py>」这种脚本当文件参数跑的进程，
    排除 bash -c / python -c 等命令行里恰好含 volwatch.py 字样的瞬态进程（自匹配）。
    """
    me = os.getpid()
    found = []
    try:
        pids = [int(x) for x in os.listdir("/proc") if x.isdigit()]
    except Exception:
        return found
    for pid in pids:
        if pid == me:
            continue
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                raw = f.read().replace(b"\0", b" ")
            cmd = raw.decode("utf-8", "replace").strip()
        except Exception:
            continue
        parts = cmd.split()
        if (len(parts) >= 2 and "python" in parts[0]
                and parts[1].endswith(".py") and "volwatch.py" in parts[1]):
            found.append((pid, cmd))
    return found


def cmd_vwps():
    found = find_volwatch()
    if not found:
        return "volwatch 没在跑"
    return "\n".join("%d  %s" % (pid, cmd) for pid, cmd in found)


def cmd_dec():
    return tail_file(latest(os.path.join(LOG, "decide_*.jsonl")), 3)


def cmd_decs():
    p = latest(os.path.join(LOG, "decide_*.jsonl"))
    if not p:
        return "暂无决策日志"
    lines = tail_file(p, 5).splitlines()
    out = []
    for line in lines:
        try:
            d = json.loads(line)
            t = datetime.fromtimestamp(d["logged_at"] / 1000).strftime("%m-%d %H:%M:%S")
            acts = [x.get("action") for x in d.get("decisions", [])]
            out.append("%s | %s | %s | %s" % (t, d.get("model"), d.get("wake_reason"), acts))
        except Exception:
            continue
    return "\n".join(out) if out else "(无可解析决策)"


def cmd_wl():
    return tail_file(os.path.join(LOG, "volwatch_live.out"), 50)


def cmd_ex():
    return tail_file(latest(os.path.join(LOG, "exec_*.jsonl")), 30)


def cmd_bal():
    acc = get_account()
    if not acc:
        return "读不到账户"
    return "总余额=%.4f  可用=%.4f  浮盈=%.4f" % (
        float(acc.get("totalWalletBalance") or 0),
        float(acc.get("availableBalance") or 0),
        float(acc.get("totalUnrealizedProfit") or 0))


def cmd_pos():
    acc = get_account()
    ps = acc.get("positions") or []
    if not ps:
        return "当前无持仓"
    out = []
    for p in ps:
        side = "多" if float(p.get("positionAmt") or 0) > 0 else "空"
        out.append("%-9s %s %.4f  入场=%.4f 现价=%.4f 浮盈=%.4f" % (
            p.get("symbol"), side, abs(float(p.get("positionAmt") or 0)),
            float(p.get("entryPrice") or 0), float(p.get("markPrice") or 0),
            float(p.get("unRealizedProfit") or 0)))
    return "\n".join(out)


def cmd_tr():
    p = os.path.join(STATE, "trades.jsonl")
    if not os.path.exists(p):
        return "账本还没有记录"
    lines = tail_file(p, 80).splitlines()
    trades = [json.loads(l) for l in lines if l.strip()]
    out = []
    total = 0.0
    for t in trades[-10:]:
        ts = datetime.fromtimestamp(t["ts"] / 1000).strftime("%m-%d %H:%M")
        side = "多" if t.get("side") == "long" else "空"
        total += t.get("net_pnl", 0)
        out.append("%s %-9s %s qty=%-6s 入=%-10s 出=%-10s 净盈亏=%+.4f (%s)" % (
            ts, t.get("symbol"), side, t.get("qty"),
            round(t.get("entry") or 0, 4), round(t.get("exit") or 0, 4),
            t.get("net_pnl", 0), t.get("reason")))
    out.append("---- 全部 %d 笔 累计净盈亏 %+.4f ----" % (len(trades), total))
    return "\n".join(out)


def cmd_vwres():
    """启动/重启 volwatch（单实例铁律：先杀干净再单起一个）。
    返回：进程核验 + 最新日志尾巴（volwatch 秒崩时能看到死因）。
    """
    try:
        import sys as _sys
        import signal
        py = _sys.executable or "python"
        # 1) 杀干净所有 volwatch 实例（直接 kill PID，不依赖 pkill）
        for pid, _cmd in find_volwatch():
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        time.sleep(1.2)
        # 2) 单起一个（nohup 脱离终端，日志追到 volwatch_live.out）
        #    Popen + DEVNULL 立即返回，不等子进程输出管道，避免超时
        subprocess.Popen(
            ["bash", "-c",
             "cd %s && nohup %s bin/volwatch.py >> log/volwatch_live.out 2>&1 &"
             % (BASE, py)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        time.sleep(2.5)
        # 3) 核验进程（/proc 直读，可靠）
        found = find_volwatch()
        proc = "\n".join("%d  %s" % (pid, cmd) for pid, cmd in found) or "volwatch 没在跑"
        # 4) 带出最新日志尾巴（webui 与 volwatch 同属 Termux 用户，能读 log/）
        tail = tail_file(os.path.join(BASE, "log", "volwatch_live.out"), 15)
        return proc + "\n--- 最新日志 ---\n" + tail
    except Exception as e:
        return "重启失败: %r" % e


def cmd_res():
    """全套重启：volwatch + webui。

    webui 自重启用延迟 detached 脚本：先把本次响应发给浏览器，
    2 秒后杀旧 webui 并起新的接管 8123（页面会短暂刷新，属正常）。
    """
    import signal
    import sys as _sys
    py = _sys.executable or "python"
    # 1) 重启 volwatch（先杀干净再单起）
    for pid, _cmd in find_volwatch():
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    time.sleep(1.2)
    # 拉起 volwatch：Popen + DEVNULL 立即返回，不等子进程输出管道
    subprocess.Popen(
        ["bash", "-c",
         "cd %s && nohup %s bin/volwatch.py >> log/volwatch_live.out 2>&1 &"
         % (BASE, py)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    time.sleep(1.5)
    found = find_volwatch()
    vw = "\n".join("%d  %s" % (pid, cmd) for pid, cmd in found) or "volwatch 没起来!"
    # 2) 延迟自重启 webui（detached：先响应 → 杀旧(精确PID) → 起新）
    #    注意不能用 pkill -f '[w]ebui.py'：本脚本就是一条含 webui.py 字样的
    #    bash -c 进程，pkill 会把自己(脚本)也杀了，导致新 webui 永远起不来。
    #    这里直接杀当前处理的旧 webui 进程 PID，脚本本身安全存活。
    old_pid = os.getpid()
    script = (
        "sleep 2; kill -9 %d 2>/dev/null; sleep 1; "
        "cd %s/webui && nohup %s webui.py 8123 >> webui.log 2>&1 &"
        % (old_pid, BASE, py)
    )
    subprocess.Popen(["bash", "-c", script], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tail = tail_file(os.path.join(BASE, "log", "volwatch_live.out"), 10)
    return ("volwatch: %s\n\nwebui 正在延迟自重启（约 3 秒后生效），"
            "页面会自动恢复，中途刷新一下即可\n--- 最新日志 ---\n%s" % (vw, tail))


CMD_EXEC = {
    "snap": cmd_snap, "vwps": cmd_vwps, "dec": cmd_dec, "decs": cmd_decs,
    "wl": cmd_wl, "ex": cmd_ex, "bal": cmd_bal, "pos": cmd_pos, "tr": cmd_tr,
    "vwres": cmd_vwres, "res": cmd_res,
}
CMD_LABEL = {
    "snap": "行情", "vwps": "进程", "dec": "决策原文", "decs": "决策简",
    "wl": "实时日志", "ex": "执行记录", "bal": "钱包", "pos": "持仓", "tr": "账本",
    "vwres": "启动/重启volwatch", "res": "全套重启(volwatch+webui)",
}


def get_alerts():
    """保护核验失败警示：读 alerts.jsonl，最新在前，最多 10 条（只回未处理的）。"""
    p = os.path.join(STATE, "alerts.jsonl")
    if not os.path.exists(p):
        return []
    try:
        lines = open(p, encoding="utf-8").read().splitlines()
        alerts = [json.loads(l) for l in lines if l.strip()]
    except Exception:
        return []
    alerts = [a for a in alerts if not a.get("acked")]  # 只显示未标记已处理的
    alerts.sort(key=lambda a: a.get("ts", 0), reverse=True)
    return alerts[:10]


def ack_alert(ts=None):
    """把指定 ts（或最新一条）的警报标记为已处理（acked=true）。
    不清文件、不丢历史；只改标记，下次 get_alerts 就不再显示。"""
    p = os.path.join(STATE, "alerts.jsonl")
    if not os.path.exists(p):
        return {"ok": True, "acked": 0}
    try:
        lines = open(p, encoding="utf-8").read().splitlines()
    except Exception:
        return {"ok": False, "error": "read fail"}
    out = []
    acked = 0
    if ts is None:
        # 没给 ts：把「最新一条仍未处理」的记录标记（已 acked 的跳过）
        records = []
        for l in lines:
            if not l.strip():
                continue
            try:
                records.append(json.loads(l))
            except Exception:
                pass
        unacked = [r for r in records if not r.get("acked")]
        if not unacked:
            return {"ok": True, "acked": 0}
        unacked.sort(key=lambda a: a.get("ts", 0))
        target = unacked[-1].get("ts")
    else:
        target = int(ts)
    for l in lines:
        if not l.strip():
            continue
        try:
            rec = json.loads(l)
            if rec.get("ts") == target and not rec.get("acked"):
                rec["acked"] = True
                acked += 1
        except Exception:
            pass
        out.append(json.dumps(rec, ensure_ascii=False))
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except Exception as e:
        return {"ok": False, "error": repr(e)[:120]}
    return {"ok": True, "acked": acked, "ts": target}


def build_payload():
    cfg = get_cfg()
    acc = get_account()
    payload = {
        "ts": int(time.time() * 1000),
        "snapshots": get_snapshots(),
        "account": {
            "totalWalletBalance": acc.get("totalWalletBalance"),
            "availableBalance": acc.get("availableBalance"),
            "totalUnrealizedProfit": acc.get("totalUnrealizedProfit"),
            "positions": acc.get("positions", []),
        },
        "trades": get_trades(),
        "decisions": get_decisions(),
        "alerts": get_alerts(),
        "vol_baseline": get_vol_state(),
        "cfg": {k: cfg.get(k) for k in
                ["vol_threshold_pct", "min_sec_between_orders"] if k in cfg},
        "exit": cfg.get("exit", {}),
        "editable": EDITABLE,
        "version": "0.1",
    }
    return payload


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except Exception:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            self._html(os.path.join(WEBUI, "index.html"))
        elif u.path in ("/help", "/help.html"):
            self._html(os.path.join(WEBUI, "help.html"))
        elif u.path in ("/app", "/app.html"):
            self._html(os.path.join(WEBUI, "app.html"))
        elif u.path == "/api/state":
            self._json(build_payload())
        elif u.path == "/api/cfg":
            self._json(get_cfg())
        elif u.path == "/api/cmd":
            q = parse_qs(u.query)
            name = (q.get("name") or [""])[0]
            fn = CMD_EXEC.get(name)
            if not fn:
                return self._json({"ok": False, "error": "unknown cmd"}, 400)
            try:
                txt = fn()
            except Exception as e:
                return self._json({"ok": False, "error": repr(e)[:200]})
            return self._json({"ok": True, "cmd": name, "label": CMD_LABEL.get(name, name), "out": txt})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/cfg":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            cfg = get_cfg()
            # 只接受 EDITABLE 里的键（exit.* 走嵌套）
            for key, val in data.items():
                if key == "exit" and isinstance(val, dict):
                    for k2 in val:
                        if ("exit." + k2) not in EDITABLE:
                            continue
                        try:
                            if isinstance(val[k2], list):
                                cfg.setdefault("exit", {})[k2] = [float(x) for x in val[k2]]
                            else:
                                cfg.setdefault("exit", {})[k2] = float(val[k2])
                        except Exception:
                            pass
                elif key in ("vol_threshold_pct", "min_sec_between_orders"):
                    try:
                        v = float(data[key])
                        if key == "vol_threshold_pct" and (v <= 0 or v > 20):
                            continue
                        cfg[key] = v
                    except Exception:
                        pass
            save_json(CFG_PATH, cfg)
            return self._json({"ok": True, "cfg": cfg})
        elif u.path == "/api/alerts/ack":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:
                data = {}
            res = ack_alert(data.get("ts") if isinstance(data, dict) else None)
            return self._json(res)
        return self._json({"ok": False, "error": "not found"}, 404)

    def log_message(self, *a):
        pass


def main():
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("WEBUI 启动 http://localhost:%d" % port)
    srv.serve_forever()


if __name__ == "__main__":
    main()