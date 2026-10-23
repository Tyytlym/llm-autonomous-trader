#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decide.py - 零号决策器（脱离 Operit 版）

职责：把快照文本 + zero_prompt.md 拼成一次请求，POST 硅基流动 chat/completions，
      把模型原始回复原样落盘成决策 JSON 文件，交给 guard.py 校验。

不做任何校验、不判定对错、不重试改写决策——决策的唯一裁判是 guard.py。
本脚本只负责三件事：拼、问、落盘。

用法:
  python3 decide.py BTCUSDT                 # 用最新快照做一次决策
  python3 decide.py BTCUSDT --dry-run       # 只拼 prompt 打印，不调模型
  python3 decide.py BTCUSDT --model deepseek-ai/DeepSeek-V3

退出码:
  0 正常（决策文件已落盘）
  2 提示词/配置缺失
  3 模型通信失败
  4 模型回复无法解析成 JSON 数组
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(BASE, "state")
LOG = os.path.join(BASE, "log")
CFG_PATH = os.path.join(BASE, "cfg.json")
KEYS_PATH = os.path.join(BASE, "keys.json")
PROMPT_PATH = os.path.join(BASE, "zero_prompt.md")

os.makedirs(STATE, exist_ok=True)
os.makedirs(LOG, exist_ok=True)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def log_event(obj):
    """每次决策的全量记录，一行一条，供复盘。"""
    path = os.path.join(LOG, "decide_%s.jsonl" % time.strftime("%Y-%m-%d"))
    obj = dict(obj)
    obj["logged_at"] = int(time.time() * 1000)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_prompt():
    if not os.path.exists(PROMPT_PATH):
        print("[FATAL] 缺少 zero_prompt.md: %s" % PROMPT_PATH)
        sys.exit(2)
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        print("[FATAL] zero_prompt.md 是空的")
        sys.exit(2)
    return text


def load_snapshot(symbol):
    """读最新快照。返回 (dict, raw_text)。"""
    path = os.path.join(STATE, "latest_%s.json" % symbol)
    if not os.path.exists(path):
        print("[FATAL] 缺少快照: %s" % path)
        sys.exit(2)
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    return json.loads(raw), raw


def build_user_message(symbol, raw_snapshot, wake_reason, extra_symbols=None):
    """把快照原文包进用户消息。原文不改写，避免我这边引入失真。"""
    parts = []
    parts.append("【唤醒】")
    parts.append("品种: %s" % symbol)
    parts.append("唤醒原因: %s" % wake_reason)
    parts.append("送入时间(毫秒): %d" % int(time.time() * 1000))
    parts.append("")
    parts.append("【市场快照（原始 JSON）】")
    parts.append(raw_snapshot.strip())
    if extra_symbols:
        for sym, raw in extra_symbols:
            parts.append("")
            parts.append("【其他品种快照（参考，非本次主决策对象）%s】" % sym)
            parts.append(raw.strip())
    parts.append("")
    parts.append("请按输出契约返回 JSON 数组。")
    return "\n".join(parts)


def extract_json_array(text):
    """
    从模型回复里抠出 JSON 数组。
    模型有时会包 Markdown 代码围栏，或前后带一句话。这里做最小容错：
    先直解，再剥围栏，再按第一个 [ 到最后一个 ] 截取。
    """
    t = (text or "").strip()
    if not t:
        return None

    # 1) 直接解析
    try:
        v = json.loads(t)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            return [v]
    except Exception:
        pass

    # 2) 剥 Markdown 围栏
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    if m:
        inner = m.group(1).strip()
        try:
            v = json.loads(inner)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                return [v]
        except Exception:
            t = inner

    # 3) 按方括号截取
    start = t.find("[")
    end = t.rfind("]")
    if start >= 0 and end > start:
        try:
            v = json.loads(t[start:end + 1])
            if isinstance(v, list):
                return v
        except Exception:
            pass

    return None


def call_model(cfg, keys, system_prompt, user_message, model, dry_run=False):
    sf = keys.get("siliconflow") or ""
    if not sf or sf.startswith("请") or len(sf) < 20:
        print("[FATAL] keys.json 里 siliconflow 字段未填或明显是占位符"
              "（该字段存放任意 OpenAI 兼容接口的 API Key）")
        sys.exit(2)

    endpoint = cfg.get("model_endpoint") or "https://api.siliconflow.cn/v1/chat/completions"
    model = model or cfg.get("model") or "deepseek-ai/DeepSeek-V3"
    temperature = cfg.get("model_temperature", 0.2)
    max_tokens = cfg.get("model_max_tokens", 2000)
    timeout = cfg.get("model_timeout_sec", 90)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    if dry_run:
        print("=== DRY RUN: 不调模型 ===")
        print("model     :", model)
        print("endpoint  :", endpoint)
        print("temp      :", temperature)
        print("max_tokens:", max_tokens)
        print("system 长度: %d 字符" % len(system_prompt))
        print("user   长度: %d 字符" % len(user_message))
        print()
        print("--- user message 前 1200 字符 ---")
        print(user_message[:1200])
        print("--- 完 ---")
        return None, model, {}

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Authorization": "Bearer %s" % sf,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
            obj = json.loads(body)
            content = obj["choices"][0]["message"]["content"]
            usage = obj.get("usage") or {}
            return content, model, usage
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            last_err = "HTTP %s %s" % (e.code, detail)
            # 4xx 除限频外不重试
            if 400 <= e.code < 500 and e.code != 429:
                break
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
        time.sleep(2 + attempt * 2)

    print("[FATAL] 模型调用失败: %s" % last_err)
    sys.exit(3)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    if not args:
        print("用法: python3 decide.py <SYMBOL> [--dry-run] [--model xxx] [--reason 文本]")
        sys.exit(2)
    symbol = args[0].upper()

    dry_run = "--dry-run" in flags
    model = None
    wake_reason = "manual"
    for i, f in enumerate(sys.argv):
        if f == "--model" and i + 1 < len(sys.argv):
            model = sys.argv[i + 1]
        if f == "--reason" and i + 1 < len(sys.argv):
            wake_reason = sys.argv[i + 1]

    cfg = load_json(CFG_PATH)
    prompt = read_prompt()
    snap, raw = load_snapshot(symbol)

    # 只做最基本的可读性提醒，不代替守门员
    snap_ts = snap.get("ts_ms") or snap.get("timestamp") or 0
    age_ms = int(time.time() * 1000) - int(snap_ts)
    if age_ms > 10 * 60 * 1000:
        print("[WARN] 快照已 %d 分钟，超过 10 分钟，零号大概率会 hold（守门员也会拒）" % (age_ms // 60000))

    if dry_run:
        try:
            keys = load_json(KEYS_PATH)
        except Exception:
            keys = {}
        user_msg = build_user_message(symbol, raw, wake_reason)
        call_model(cfg, keys, prompt, user_msg, model, dry_run=True)
        sys.exit(0)

    keys = load_json(KEYS_PATH)
    user_msg = build_user_message(symbol, raw, wake_reason)

    t0 = time.time()
    content, used_model, usage = call_model(cfg, keys, prompt, user_msg, model)
    elapsed = round(time.time() - t0, 2)

    decisions = extract_json_array(content)
    if decisions is None:
        log_event({
            "symbol": symbol,
            "model": used_model,
            "wake_reason": wake_reason,
            "elapsed_sec": elapsed,
            "parse_ok": False,
            "raw_reply": content,
            "usage": usage,
        })
        print("[FATAL] 模型回复无法解析成 JSON 数组")
        print("--- 原始回复 ---")
        print(content[:1500])
        sys.exit(4)

    out_path = os.path.join(STATE, "decision_%s.json" % symbol)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(decisions, f, ensure_ascii=False, indent=2)

    log_event({
        "symbol": symbol,
        "model": used_model,
        "wake_reason": wake_reason,
        "elapsed_sec": elapsed,
        "parse_ok": True,
        "decisions": decisions,
        "raw_reply": content,
        "usage": usage,
        "snapshot_timestamp": snap.get("timestamp"),
    })

    print("[OK] %s 决策已落盘 -> %s" % (symbol, out_path))
    print("     模型: %s  耗时: %ss  条数: %d" % (used_model, elapsed, len(decisions)))
    for d in decisions:
        print("     %-11s %-10s notional=%-8s sl=%-10s tp=%-10s | %s" % (
            d.get("symbol", "?"),
            d.get("action", "?"),
            d.get("notional_usdt", "?"),
            d.get("stop_loss", "?"),
            d.get("take_profit", "?"),
            (d.get("entry_reason") or "")[:40],
        ))
    sys.exit(0)


if __name__ == "__main__":
    main()
