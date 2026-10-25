#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""t_selfcheck.py - 出场升级后置自检：语法 + 分档逻辑，跑完应输出 ALL PASS"""
import ast
import json
import os
import re
import sys
from decimal import Decimal

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(BASE, "bin")

def check_py(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    ast.parse(src)
    return os.path.basename(path)

def check_json(path):
    with open(path, encoding="utf-8") as f:
        json.load(f)
    return os.path.basename(path)

def main():
    ok = True
    print("== 1) 语法/JSON 检查 ==")
    for p in ["bin/executor.py", "bin/volwatch.py", "cfg.json"]:
        full = os.path.join(BASE, p)
        if not os.path.exists(full) and p == "cfg.json":
            print("  SKIP ", p, "（发布仓库无 cfg.json，运行时由 cfg.example.json 复制生成）")
            continue
        try:
            if p.endswith(".py"):
                name = check_py(full)
            else:
                name = check_json(full)
            print("  OK  ", p)
        except Exception as e:
            ok = False
            print("  FAIL", p, "->", repr(e))

    print("== 2) split_tiers 分档逻辑 ==")
    src = open(os.path.join(BIN, "executor.py"), encoding="utf-8").read()
    m = re.search(r"def split_tiers\(.*?\n\ndef ", src, re.S)
    if not m:
        print("  FAIL 找不到 split_tiers")
        return 1
    code = m.group(0)[:-5]  # 去掉末尾 'def '
    code += """
from decimal import Decimal
def q_step(sym):
    return Decimal(_info["stepSize"].get(sym, "0.001"))
_info = {"stepSize": {"BTCUSDT": "0.001", "ETHUSDT": "0.001", "SOLUSDT": "0.01"}}
def fmt_qty(sym, qty):
    step = q_step(sym)
    v = (Decimal(str(qty)) // step) * step
    if v <= 0:
        v = step
    return format(v.normalize(), "f")
"""
    ns = {}
    exec(code, ns)
    split_tiers = ns["split_tiers"]

    # 用例1: 标准 30/30/40，数量 0.3
    r, t = split_tiers(0.3, [0.30, 0.30, 0.40], "BTCUSDT")
    assert r[0][1] == "0.09" and r[1][1] == "0.09" and t == "0.12", (r, t)
    print("  OK   0.3 -> 档1/档2=0.09/0.09 尾仓=0.12")

    # 用例2: 数量 0.1，step=0.001，30%档=0.03 >= step，应正常拆档
    r, t = split_tiers(0.1, [0.30, 0.30, 0.40], "BTCUSDT")
    assert r[0][1] == "0.03" and r[1][1] == "0.03" and t == "0.04", (r, t)
    print("  OK   0.1 -> 档1/档2=0.03/0.03 尾仓=0.04")

    # 用例2b: 数量 0.002（< 3×step），30%档=0.0006 < step，拆不出分档 -> 全归尾仓
    r, t = split_tiers(0.002, [0.30, 0.30, 0.40], "BTCUSDT")
    assert r == [] and t == "0.002", (r, t)
    print("  OK   0.002 -> 无分档，尾仓=0.002")

    # 用例3: 数量 1.0
    r, t = split_tiers(1.0, [0.30, 0.30, 0.40], "BTCUSDT")
    assert r[0][1] == "0.3" and r[1][1] == "0.3" and t == "0.4", (r, t)
    print("  OK   1.0 -> 档1/档2=0.3/0.3 尾仓=0.4")

    # 用例4: SOL 步长 0.01，数量 3.0
    r, t = split_tiers(3.0, [0.30, 0.30, 0.40], "SOLUSDT")
    assert r[0][1] == "0.9" and r[1][1] == "0.9" and t == "1.2", (r, t)
    print("  OK   SOL 3.0 -> 0.9/0.9 尾仓=1.2")

    print()
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())