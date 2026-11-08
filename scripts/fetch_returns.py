#!/usr/bin/env python3
"""
定投20年 · 已投收益估算（GitHub Actions 每日运行，产物 data/returns.json）

口径（指数口径估算，非基金账户实际收益）：
- 每月每标的的买入价 = 该月计算参考点位（months.json 的 buy 字段，即当月正式
  计算所用的最新收盘；2026-04~09 为补算所用月末点位）；
- 当前价 = 最近一个已完成交易日收盘（按市场时钟剔除未完成 bar，与每日脚本同口径）；
- 某标的当前估值 = Σ 各月实际投入 × (当前价 ÷ 当月买入价)；
  收益率 = (估值 − 投入) ÷ 投入。
- 黄金分口径：2026-04~11 的买入点位为 COMEX（美元/盎司），当前价用 COMEX
  连续合约（东方财富 101.GC00Y）；2026-12 起买入点位为上海金 Au9999（元/克），
  当前价用 Au9999（118.Au9999）。比值无量纲，口径内自洽。
- 不含基金申赎费、跟踪误差与 QDII 汇率影响，结果只作参考。

校验：纳指100/中证500 最新收盘与腾讯行情核对（容差 0.6%）；
上海金与东方财富实时通道昨收核对（0.6%）；COMEX 与上海金另加单日涨跌
±3.5% 合理性检查。任一主源失败或校验不过 → 非零退出（工作流该步
continue-on-error，保留上一版 returns.json，不发布坏数据）。

另生成逐日收益序列 series（自首笔入场日起的日度成本/估值/盈亏/收益率，
加上证、沪深300、创业板指三条基准收盘序列），供页面「收益走势」图使用，
口径与点估算完全一致（序列末点必等于上方总盈亏/总收益率）。
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-5))

BJ = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOTAL_TOL = 0.006
DAY_BAND = 0.035
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

SERIES = {
    "ndx":        {"name": "纳斯达克100", "secid": "100.NDX100", "clock": "ndx"},
    "csi500":     {"name": "中证500",     "secid": "1.000905",   "clock": "csi500"},
    "gold_cny":   {"name": "黄金（上海金 Au9999）", "secid": "118.Au9999", "clock": "gold"},
    "gold_comex": {"name": "黄金（COMEX）", "secid": "101.GC00Y", "clock": "gold"},
}
TENCENT_CODE = {"ndx": "usNDX", "csi500": "sh000905"}
BENCH = {
    "sh":    {"name": "上证指数", "secid": "1.000001"},
    "hs300": {"name": "沪深300",  "secid": "1.000300"},
    "cyb":   {"name": "创业板指", "secid": "0.399006"},
}


def http_bytes(url, headers=None, retries=4):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(3)
    raise last


def eastmoney_daily(secid):
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&klt=101&fqt=0&beg=20200101&end=20500101"
           f"&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57")
    data = json.loads(http_bytes(url, headers={"Referer": "https://quote.eastmoney.com/"}).decode())
    if data.get("rc") != 0 or not data.get("data", {}).get("klines"):
        raise RuntimeError(f"东方财富无数据: {secid}")
    return [(k.split(",")[0], float(k.split(",")[2])) for k in data["data"]["klines"]]


def completed_bars(clock, bars, now_bj, now_et):
    """与 scripts/fetch_data.py 同口径：按市场时钟剔除未完成 bar。"""
    if clock == "csi500":
        today = now_bj.strftime("%Y-%m-%d")
        if now_bj.hour * 60 + now_bj.minute < 15 * 60 + 5:
            bars = [b for b in bars if b[0] < today]
        else:
            bars = [b for b in bars if b[0] <= today]
    elif clock == "gold":
        today = now_bj.strftime("%Y-%m-%d")
        bars = [b for b in bars if b[0] <= today]
        if now_bj.hour * 60 + now_bj.minute < 15 * 60 + 35:
            bars = [b for b in bars if b[0] < today]
    elif clock == "ndx":
        today_et = now_et.strftime("%Y-%m-%d")
        if now_et.hour * 60 + now_et.minute < 16 * 60 + 5:
            bars = [b for b in bars if b[0] < today_et]
        else:
            bars = [b for b in bars if b[0] <= today_et]
    if not bars:
        raise RuntimeError(f"{clock} 无已完成日K")
    return bars


def tencent_quote(code, now_et):
    raw = http_bytes(f"https://qt.gtimg.cn/q={code}")
    text = raw.decode("gbk", errors="replace")
    body = text.split('="', 1)[1].rsplit('"', 1)[0]
    f = body.split("~")
    price = float(f[3])
    if code.startswith("us"):
        try:
            qtime = datetime.strptime(f[30], "%Y-%m-%d %H:%M:%S")
            if qtime.date() == now_et.date() and (qtime.hour * 60 + qtime.minute) < 16 * 60 + 5:
                price = float(f[4])
        except Exception:
            pass
    return price


def eastmoney_quote_prev_close(secid):
    url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f60")
    data = json.loads(http_bytes(url, headers={"Referer": "https://quote.eastmoney.com/"}).decode())
    return float(data["data"]["f60"]) / 100.0


def main():
    now_utc = datetime.now(timezone.utc)
    now_bj = now_utc.astimezone(BJ)
    now_et = now_utc.astimezone(ET)

    # ---- 拉取四条价格序列（保留全量日K，供逐日收益序列使用） ----
    latest = {}   # key -> (date, price, prev_price)
    all_bars = {}  # key -> [(date, close), ...] 已完成 bar
    for key, meta in SERIES.items():
        bars = completed_bars(meta["clock"], eastmoney_daily(meta["secid"]), now_bj, now_et)
        all_bars[key] = bars
        latest[key] = (bars[-1][0], bars[-1][1], bars[-2][1] if len(bars) > 1 else None)
    bench_bars = {}
    for bkey, meta in BENCH.items():
        bench_bars[bkey] = completed_bars("csi500", eastmoney_daily(meta["secid"]), now_bj, now_et)

    # ---- 校验 ----
    for key in ("ndx", "csi500"):
        ref = tencent_quote(TENCENT_CODE[key], now_et)
        diff = abs(ref - latest[key][1]) / latest[key][1]
        if diff > TOTAL_TOL:
            raise RuntimeError(f"{key} 双源校验不通过：{latest[key][1]} vs 腾讯 {ref}（{diff*100:.2f}%）")
    ref = eastmoney_quote_prev_close(SERIES["gold_cny"]["secid"])
    diff = abs(ref - latest["gold_cny"][1]) / latest["gold_cny"][1]
    if diff > TOTAL_TOL:
        raise RuntimeError(f"gold_cny 双通道校验不通过：{latest['gold_cny'][1]} vs 昨收 {ref}")
    for key in ("gold_cny", "gold_comex"):
        prev = latest[key][2]
        if prev:
            chg = abs(latest[key][1] / prev - 1)
            if chg > DAY_BAND:
                raise RuntimeError(f"{key} 单日涨跌异常：{chg*100:.2f}% 超出 ±{DAY_BAND*100}%")

    # ---- 按 months.json 逐月估值 ----
    with open(os.path.join(BASE, "data", "months.json"), encoding="utf-8") as f:
        store = json.load(f)

    assets = {}
    excluded = []
    for akey in ("ndx", "csi500", "gold"):
        cost = 0.0
        value = 0.0
        used_point = None
        for m in store["months"]:
            if not m.get("actual"):
                continue
            amt = m["actual"].get(akey) or 0
            if amt == 0:
                continue
            buy = (m.get("buy") or {}).get(akey)
            if not buy:
                excluded.append(f"{m['month']}/{akey}")
                continue
            if akey == "gold":
                skey = "gold_comex" if m.get("gold_unit") == "usd_oz" else "gold_cny"
            else:
                skey = akey
            pdate, pprice, _ = latest[skey]
            used_point = (pdate, pprice)
            cost += amt
            value += amt * pprice / buy
        if cost == 0:
            continue
        pnl = value - cost
        assets[akey] = {
            "name": SERIES[akey]["name"] if akey != "gold" else "黄金",
            "cost": round(cost, 2),
            "value": round(value, 2),
            "pnl": round(pnl, 2),
            "rate": round(pnl / cost * 100, 2),
            "point": used_point[1],
            "point_date": used_point[0],
        }

    tcost = sum(a["cost"] for a in assets.values())
    tvalue = sum(a["value"] for a in assets.values())
    tpnl = tvalue - tcost
    payload = {
        "updated_at_bj": now_bj.strftime("%Y-%m-%d %H:%M"),
        "asof": min(a["point_date"] for a in assets.values()),
        "assets": assets,
        "total": {
            "cost": round(tcost, 2),
            "value": round(tvalue, 2),
            "pnl": round(tpnl, 2),
            "rate": round(tpnl / tcost * 100, 2) if tcost else 0,
        },
        "excluded": excluded,
        "method": ("指数口径估算：每笔买入价取当月计算参考点位，当前价取最近已完成交易日收盘；"
                   "不含基金申赎费、跟踪误差与 QDII 汇率影响，非基金账户实际收益。"),
    }

    # ---- 逐日收益序列（收益走势图用，与上方估值同口径） ----
    price_maps = {k: dict(v) for k, v in all_bars.items()}
    bench_maps = {k: dict(v) for k, v in bench_bars.items()}

    def price_on(pmap, date):
        """<= date 的最近收盘（前向填充）；早于序列首日时取首日。"""
        if date in pmap:
            return pmap[date]
        cand = [d for d in pmap if d <= date]
        return pmap[max(cand)] if cand else pmap[min(pmap)]

    entries = []  # (entry_date, series_key, amount, buy_price)
    for m in store["months"]:
        if not m.get("actual"):
            continue
        for akey in ("ndx", "csi500", "gold"):
            amt = m["actual"].get(akey) or 0
            buy = (m.get("buy") or {}).get(akey)
            if not amt or not buy:
                continue
            skey = ("gold_comex" if m.get("gold_unit") == "usd_oz" else "gold_cny") if akey == "gold" else akey
            edate = None
            cur = ((m.get("basis") or {}).get("current") or {}).get(akey)
            if cur and abs(cur.get("price", 0) - buy) < 1e-9:
                edate = cur.get("date")
            if not edate:
                for d, p in all_bars[skey]:
                    if abs(p - buy) < 1e-9:
                        edate = d
                        break
            entries.append((edate or (m["month"] + "-01"), skey, float(amt), float(buy)))

    # 轴起点 = 台账首个买入日（months.json 的 start_date，2026-04-01）；
    # 首笔入场参考点在 2026-03-31，入场判定仍按各自参考点日期。
    start = store.get("start_date") or min(e[0] for e in entries)
    axis_set = set()
    for bars in list(all_bars.values()) + list(bench_bars.values()):
        for d, _ in bars:
            if start <= d <= payload["asof"]:
                axis_set.add(d)
    s_dates, s_cost, s_value, s_pnl, s_rate = [], [], [], [], []
    bench_series = {k: [] for k in BENCH}
    for d in sorted(axis_set):
        cost = 0.0
        value = 0.0
        for edate, skey, amt, buy in entries:
            if edate <= d:
                cost += amt
                value += amt * price_on(price_maps[skey], d) / buy
        if cost == 0:
            continue
        s_dates.append(d)
        s_cost.append(round(cost, 2))
        s_value.append(round(value, 2))
        s_pnl.append(round(value - cost, 2))
        s_rate.append(round((value - cost) / cost * 100, 2))
        for bkey in BENCH:
            bench_series[bkey].append(round(price_on(bench_maps[bkey], d), 2))
    if s_dates:
        # 末点与上方点估算逐分对齐（同一时点同一口径，避免逐点四舍五入差 1 分）
        s_cost[-1] = payload["total"]["cost"]
        s_value[-1] = payload["total"]["value"]
        s_pnl[-1] = payload["total"]["pnl"]
        s_rate[-1] = payload["total"]["rate"]
    payload["series"] = {
        "start": s_dates[0] if s_dates else None,
        "dates": s_dates,
        "cost": s_cost,
        "value": s_value,
        "pnl": s_pnl,
        "rate": s_rate,
        "bench": {k: {"name": BENCH[k]["name"], "close": bench_series[k]} for k in BENCH},
        "note": ("逐日序列与上方估算同口径：每笔按其买入参考点位入场（入场日取该月计算参考点日期），"
                 "之后按各标的当日收盘估值；基准为指数收盘价序列，页面按所选区间起点重订基。"),
    }
    with open(os.path.join(BASE, "data", "returns.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("OK: data/returns.json 已更新")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
