#!/usr/bin/env python3
"""
定投20年 · 历史月份 basis 回填（一次性审计工具，本地或 CI 手动运行）

背景：data/months.json 中 2026-04~11 的条目是建仓时从既有台账录入的，
只有 plan/actual/buy，没有 monthly_calc.py 自动生成条目所带的 basis
（30 月窗口、三标的均值、计算时点点位与权重），仓库内无法独立复算。

本脚本按 monthly_calc.py 的完全相同口径复算每个缺 basis 的月份：
- 窗口 = 截至 M-1 月的 30 个月（monthly_calc.window_months）；
- 月末收盘：纳指100 = Yahoo ^NDX（东方财富 100.NDX100 兜底，与
  monthly_calc.ndx_series 同源）；中证500 = 东方财富 1.000905 月K；
  黄金按条目的 gold_unit 选口径：usd_oz → Yahoo GC=F 日K月末（与当年
  正式计算同源；东方财富 101.GC00Y 月K 的月末取数惯例不同、复算对不上，
  2026-11-08 实测其 2026-11 黄金均值 3579.01 vs 已录 3562.45）；
  cny_g → 上海金 118.Au9999 月K（与 monthly_calc 本身同源）；
- 均值 = 30 个月末收盘算术平均（round 2）；权重 = 均值 ÷ buy 点位；
  金额 = 最大余数法分配 3000（与 compute() 相同实现）。
复算 plan 与已记录 plan 逐项一致才写入 basis 并标 verified: true；
不一致的月份不写、退出码非零，留待人工核查（这正是本脚本的用途）。

用法：
  python3 scripts/backfill_basis.py           # 回填并写回 data/months.json
  python3 scripts/backfill_basis.py --check   # 只复算校验、不写文件
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monthly_calc as mc  # 复用同一套数据源与窗口函数，杜绝口径漂移

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONTHS_PATH = os.path.join(BASE, "data", "months.json")
TOTAL = 3000
ASSETS = ("ndx", "csi500", "gold")
EM_DAILY_SECID = {"ndx": mc.EM_NDX_SECID, "csi500": "1.000905"}
GOLD_MONTHLY_SECID = {"usd_oz": "101.GC00Y", "cny_g": "118.Au9999"}


def allocate(weights, total=TOTAL):
    """最大余数法，与 monthly_calc.compute() 中实现一致。"""
    sw = sum(weights.values())
    exact = {a: total * weights[a] / sw for a in weights}
    base = {a: int(exact[a]) for a in exact}
    rounded = dict(base)
    for a in sorted(exact, key=lambda a: exact[a] - base[a], reverse=True)[: total - sum(base.values())]:
        rounded[a] += 1
    assert sum(rounded.values()) == total
    return rounded


def yahoo_daily_bars(symbol, start_ym, today):
    """Yahoo 日K 原始 bars [(date, close)]（与 monthly_calc.ndx_series 相同的取数方式）。"""
    y, m = int(start_ym[:4]), int(start_ym[5:7])
    p1 = mc.ts_of(f"{y}-{m:02d}-01") - 45 * 86400
    p2 = mc.ts_of(today.strftime("%Y-%m-%d")) + 3 * 86400
    rows = mc.yahoo_daily(symbol, p1, p2)
    if not rows:
        raise RuntimeError(f"Yahoo 无数据: {symbol}")
    return [(datetime.fromtimestamp(t, mc.timezone.utc).strftime("%Y-%m-%d"), round(c, 2))
            for t, c in rows]


def find_point_date(bars, price, month_first):
    """在日K里找与 buy 点位收盘价一致的日期：优先取当月 5 日（含）之前的最晚一天。"""
    cands = [d for d, c in bars if round(c, 2) == round(price, 2)]
    if not cands:
        return None
    limit = (month_first + timedelta(days=4)).strftime("%Y-%m-%d")
    before = [d for d in cands if d <= limit]
    return before[-1] if before else cands[-1]


def main():
    check_only = "--check" in sys.argv
    store = json.load(open(MONTHS_PATH, encoding="utf-8"))
    targets = [e for e in store["months"] if "basis" not in e]
    if not targets:
        print("所有月份都已有 basis，无需回填。")
        return 0
    print(f"待回填月份：{[e['month'] for e in targets]}")

    # 行情一次拉全（以最早窗口为起点），各月共用
    first = targets[0]
    fy, fm = int(first["month"][:4]), int(first["month"][5:7])
    earliest_window = mc.window_months(fy, fm)
    beg_ymd = earliest_window[0].replace("-", "") + "01"
    today = date.today()
    ndx_ends, _ = mc.ndx_series(earliest_window[0], today)
    csi_ends = mc.em_month_ends("1.000905", beg_ymd)
    gold_usd_bars = yahoo_daily_bars("GC=F", earliest_window[0], today)
    gold_ends = {
        "usd_oz": mc.month_ends_from_daily(gold_usd_bars),
        "cny_g": mc.em_month_ends(GOLD_MONTHLY_SECID["cny_g"], beg_ymd),
    }
    daily_bars = {}
    for a in ASSETS:
        secid = EM_DAILY_SECID.get(a) or GOLD_MONTHLY_SECID["usd_oz"]
        daily_bars[a] = mc.eastmoney_kline(secid, "101", "20260201", "20261115")
    daily_bars["gold"] = gold_usd_bars  # usd_oz 的 buy 点位是 Yahoo 口径，日期也在 Yahoo 日K里找
    daily_bars["gold_cny"] = mc.eastmoney_kline("118.Au9999", "101", "20260201", "20261115")

    failures = []
    for entry in targets:
        m = entry["month"]
        y, mo = int(m[:4]), int(m[5:7])
        window = mc.window_months(y, mo)
        ends = {
            "ndx": ndx_ends,
            "csi500": csi_ends,
            "gold": gold_ends[entry.get("gold_unit", "usd_oz")],
        }
        missing = [f"{a}:{ym}" for a in ASSETS for ym in window if ym not in ends[a]]
        if missing:
            print(f"[{m}] 月末数据缺失 {missing[:5]}…，跳过")
            failures.append(m)
            continue
        avg = {a: round(sum(ends[a][ym] for ym in window) / 30, 2) for a in ASSETS}
        cur = entry["buy"]
        w = {a: avg[a] / cur[a] for a in ASSETS}
        recomputed = allocate(w)
        ok = recomputed == entry["plan"]
        month_first = date(y, mo, 1)
        current = {}
        for a in ASSETS:
            bars = daily_bars["gold_cny"] if (a == "gold" and entry.get("gold_unit") == "cny_g") else daily_bars[a]
            current[a] = {"date": find_point_date(bars, cur[a], month_first), "price": cur[a]}
        print(f"[{m}] avg={avg} 复算={recomputed} 已录={entry['plan']} -> {'一致' if ok else '不一致!'}")
        if not ok:
            failures.append(m)
            continue
        entry["basis"] = {
            "window": [window[0], window[-1]],
            "avg": avg,
            "current": current,
            "weight": {a: round(w[a], 5) for a in ASSETS},
            "recomputed": recomputed,
            "verified": True,
            "backfilled_at": datetime.now().strftime("%Y-%m-%d"),
            "backfill_note": "由 scripts/backfill_basis.py 按 monthly_calc 同口径复算回填，复算 plan 与已记录 plan 一致。",
        }

    if failures:
        print(f"未通过月份：{failures}（未写入这些月份的 basis）")
    if check_only:
        print("--check 模式，未写文件。")
    else:
        done = [e["month"] for e in targets if "basis" in e]
        if done:
            with open(MONTHS_PATH, "w", encoding="utf-8") as f:
                json.dump(store, f, ensure_ascii=False, indent=2)
                f.write("\n")
            print(f"已写回 months.json：{done}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
