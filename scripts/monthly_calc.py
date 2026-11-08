#!/usr/bin/env python3
"""
定投20年 · 每月正式分配计算（GitHub Actions 运行，写入 data/months.json）

口径（与本地正式计算 monthly_calc.py 完全一致，用户自定规则）：
- 位置权重 = 30 月均值 ÷ 当前点位；金额 = 3000 × 权重 ÷ 权重合计；
  最大余数法取整，合计严格等于 3000。
- 计算 M 月分配时：30 月均值 = 截至 M-1 月共 30 个月的每月月末收盘算术平均；
  当前点位 = 运行时最新已完成交易日收盘（在 M 月第一个 A股交易日开盘前运行，
  即 M-1 月最后一个交易日收盘）。
- 数据源：纳指100 = Yahoo Finance ^NDX 日K（月末收盘与最新收盘），
  东方财富 100.NDX100 日K 兜底；中证500 = 东方财富 1.000905；
  黄金 = 上海金 Au9999（东方财富 118.Au9999，元/克）。
  黄金口径说明：2026-11 及之前的正式计算为 COMEX 口径、已记录在案不回头改；
  本脚本只计算 2026-12 起的月份（Au9999 口径），对更早月份拒绝写入。
- 双源校验（任一不过 → 非零退出、不写文件）：
  纳指100 / 中证500 最新收盘与腾讯行情核对（容差 0.6%）；
  黄金与东方财富实时报价通道核对（容差 0.6%）+ 单日涨跌 ±3.5% 合理性。

运行关口（schedule 触发时全部满足才计算）：
- 当月（北京时间）尚未记录在 data/months.json 中；
- 今天是当月第一个 A股交易日，或其后 3 天内的交易日（漏跑自动补）；
  A股交易日 = 周一至周五且非法定节假日（chinese-calendar 可用时按其判断，
  不可用时退化为仅按星期判断）。
手动触发可加 --force 跳过交易日窗口判断；--dry-run 只算不写（用于验证管线）。
"""
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    BJ = ZoneInfo("Asia/Shanghai")
    ET = ZoneInfo("America/New_York")
except Exception:
    BJ = timezone(timedelta(hours=8))
    ET = timezone(timedelta(hours=-5))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONTHS_PATH = os.path.join(BASE, "data", "months.json")
TOTAL = 3000
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
EM_SECID = {"csi500": "1.000905", "gold": "118.Au9999"}
EM_NDX_SECID = "100.NDX100"
TENCENT_CODE = {"ndx": "usNDX", "csi500": "sh000905"}
TOLERANCE = 0.006
GOLD_DAY_BAND = 0.035
FIRST_AUTO_MONTH = "2026-12"  # Au9999 口径自 2026-12 起；更早月份已按 COMEX 记录


def http_bytes(url, headers=None, retries=3):
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


def http_json(url, headers=None, retries=3):
    return json.loads(http_bytes(url, headers=headers, retries=retries).decode())


# ---------------- 交易日判断 ----------------

def is_holiday(d):
    try:
        from chinese_calendar import is_holiday as _ih
        return bool(_ih(d))
    except Exception:
        return False  # 日历数据不可用时退化为仅按星期判断


def is_trading_day(d):
    return d.weekday() < 5 and not is_holiday(d)


def first_trading_day(y, m):
    d = date(y, m, 1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


# ---------------- 行情拉取 ----------------

def yahoo_daily(symbol, p1, p2):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={p1}&period2={p2}&interval=1d")
    data = http_json(url)
    res = data["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    return [(t, c) for t, c in zip(ts, closes) if c is not None]


def eastmoney_kline(secid, klt, beg, end):
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&klt={klt}&fqt=0"
           f"&beg={beg}&end={end}&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57")
    data = http_json(url, headers={"Referer": "https://quote.eastmoney.com/"}, retries=4)
    if data.get("rc") != 0 or not data.get("data", {}).get("klines"):
        raise RuntimeError(f"东方财富无数据: {secid} klt={klt}")
    out = []
    for k in data["data"]["klines"]:
        p = k.split(",")
        out.append((p[0], float(p[2])))
    return out


def month_ends_from_daily(bars):
    months = {}
    for d, c in bars:
        months[d[:7]] = round(c, 2)
    return months


def ts_of(d):
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def ndx_series(window_start_ym, today):
    """纳指100：返回 (月末收盘 dict, 最新已完成收盘 (date, price))。Yahoo 主源，东方财富兜底。"""
    y, m = int(window_start_ym[:4]), int(window_start_ym[5:7])
    p1 = ts_of(f"{y}-{m:02d}-01") - 45 * 86400
    p2 = ts_of(today.strftime("%Y-%m-%d")) + 3 * 86400
    bars = None
    for attempt in range(3):
        try:
            rows = yahoo_daily("%5ENDX", p1, p2)
            if rows:
                bars = [(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"),
                         round(c, 2)) for t, c in rows]
                break
        except Exception:
            time.sleep(2)
    if not bars:
        bars = eastmoney_kline(EM_NDX_SECID, "101", "20230101", "20500101")
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    done = [b for b in bars if b[0] != today_utc] or bars
    return month_ends_from_daily(bars), done[-1]


def em_month_ends(secid, beg_ymd):
    bars = eastmoney_kline(secid, "103", beg_ymd, "20500101")
    return {d[:7]: round(c, 2) for d, c in bars}


def em_latest(secid, asset):
    """最新已完成交易日收盘。按市场时钟剔除未完成 bar（与每日脚本 completed_bars 同口径）：
    只取 <= 今天（北京）的 bar；收盘时刻前再剔除今天的 bar——黄金夜盘会提前
    生成下一交易日 bar，周末/盘前运行时尤其要防它混进来。"""
    bars = eastmoney_kline(secid, "101", "20250101", "20500101")
    now_bj = datetime.now(BJ)
    today = now_bj.strftime("%Y-%m-%d")
    bars = [b for b in bars if b[0] <= today]
    close_min = 15 * 60 + (35 if asset == "gold" else 5)
    if now_bj.hour * 60 + now_bj.minute < close_min:
        bars = [b for b in bars if b[0] < today]
    if not bars:
        raise RuntimeError(f"东方财富无已完成日K: {secid}")
    prev = bars[-2] if len(bars) > 1 else None
    return bars[-1], prev


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
    """东方财富实时报价通道的昨收 f60：与「最新已完成日K收盘」是同一交易日的收盘价，
    会话对齐——夜盘/盘前运行时 f43（最新价）属于下一交易日、会系统性偏离，
    交叉核对已完成收盘必须用 f60。"""
    url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
           f"&fields=f43,f60")
    data = http_json(url, headers={"Referer": "https://quote.eastmoney.com/"}, retries=4)
    return float(data["data"]["f60"]) / 100.0


# ---------------- 计算 ----------------

def window_months(year, month):
    out = []
    y, m = year, month
    for _ in range(30):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        out.append(f"{y}-{m:02d}")
    return sorted(out)


def compute(year, month, today):
    months = window_months(year, month)
    beg_ymd = months[0].replace("-", "") + "01"

    ndx_ends, ndx_cur = ndx_series(months[0], today)
    csi_ends = em_month_ends(EM_SECID["csi500"], beg_ymd)
    gold_ends = em_month_ends(EM_SECID["gold"], beg_ymd)
    csi_cur, _ = em_latest(EM_SECID["csi500"], "csi500")
    gold_cur, gold_prev = em_latest(EM_SECID["gold"], "gold")

    ends = {"ndx": ndx_ends, "csi500": csi_ends, "gold": gold_ends}
    cur = {"ndx": ndx_cur, "csi500": csi_cur, "gold": gold_cur}
    for a, mp in ends.items():
        for ym in months:
            if ym not in mp:
                raise RuntimeError(f"{a} 缺失月末收盘: {ym}")

    # ---- 双源校验 ----
    now_et = datetime.now(ET)
    for a in ("ndx", "csi500"):
        ref = tencent_quote(TENCENT_CODE[a], now_et)
        diff = abs(ref - cur[a][1]) / cur[a][1]
        if diff > TOLERANCE:
            raise RuntimeError(f"{a} 双源校验不通过：主源 {cur[a][1]} vs 腾讯 {ref}（偏差 {diff*100:.2f}%）")
    ref = eastmoney_quote_prev_close(EM_SECID["gold"])
    diff = abs(ref - cur["gold"][1]) / cur["gold"][1]
    if diff > TOLERANCE:
        raise RuntimeError(f"gold 双通道校验不通过：日K {cur['gold'][1]} vs 实时通道昨收 {ref}")
    if gold_prev:
        chg = abs(cur["gold"][1] / gold_prev[1] - 1)
        if chg > GOLD_DAY_BAND:
            raise RuntimeError(f"gold 单日涨跌异常：{chg*100:.2f}% 超出 ±{GOLD_DAY_BAND*100}%")

    avg = {a: round(sum(ends[a][ym] for ym in months) / 30, 2) for a in ends}
    w = {a: avg[a] / cur[a][1] for a in avg}
    sw = sum(w.values())
    exact = {a: TOTAL * w[a] / sw for a in w}
    base = {a: int(exact[a]) for a in exact}
    rounded = dict(base)
    for a in sorted(exact, key=lambda a: exact[a] - base[a], reverse=True)[:TOTAL - sum(base.values())]:
        rounded[a] += 1
    assert sum(rounded.values()) == TOTAL

    return {
        "month": f"{year}-{month:02d}",
        "plan": {"ndx": rounded["ndx"], "csi500": rounded["csi500"], "gold": rounded["gold"]},
        "actual": None,
        "basis": {
            "window": [months[0], months[-1]],
            "avg": avg,
            "current": {a: {"date": cur[a][0], "price": cur[a][1]} for a in cur},
            "weight": {a: round(w[a], 5) for a in w},
        },
        "note": "理论分配由 GitHub Actions 自动计算（黄金为上海金 Au9999 口径）。",
        "source": "github-actions",
        "computed_at": datetime.now(BJ).strftime("%Y-%m-%d %H:%M"),
    }


def main():
    force = "--force" in sys.argv or os.environ.get("FORCE") == "1"
    dry_run = "--dry-run" in sys.argv
    today = datetime.now(BJ).date()
    target = f"{today.year}-{today.month:02d}"

    with open(MONTHS_PATH, encoding="utf-8") as f:
        store = json.load(f)
    if any(m["month"] == target for m in store["months"]):
        print(f"SKIP: {target} 已记录在 data/months.json 中")
        return 0

    if target < FIRST_AUTO_MONTH and not dry_run:
        print(f"SKIP: {target} 早于自动计算起始月 {FIRST_AUTO_MONTH}（历史月份已按当时口径记录）")
        return 0

    if not force:
        ftd = first_trading_day(today.year, today.month)
        if today < ftd or today > ftd + timedelta(days=3) or not is_trading_day(today):
            print(f"SKIP: 今天 {today} 不在当月第一个交易日窗口内（第一个交易日 {ftd}，窗口至 {ftd + timedelta(days=3)}）")
            return 0

    entry = compute(today.year, today.month, today)
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    if dry_run:
        print("DRY-RUN: 只算不写")
        return 0
    store["months"].append(entry)
    store["months"].sort(key=lambda m: m["month"])
    with open(MONTHS_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"OK: {target} 已追加到 data/months.json，分配 {entry['plan']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
