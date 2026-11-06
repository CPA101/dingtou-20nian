#!/usr/bin/env python3
"""
定投20年 · 每日数据抓取与分配计算（GitHub Actions 定时运行）

口径（与记账空间「最新分配」模块完全一致）：
- 当前点位 = 最近一个已完成交易日的收盘价（未完成 bar 按市场时钟剔除）；
- 30 月均值（滚动）= 截至最新收盘日、往前 30 个月的全部每日收盘算术平均；
- 位置权重 = 30 月均值 ÷ 当前点位；
- 分配金额 = 3000 × 权重 ÷ 三权重合计，最大余数法取整，合计严格等于 3000。

数据源：
- 主源：东方财富日K（纳指100 secid 100.NDX100 / 中证500 1.000905 / 上海金 Au9999 118.AU9999）；
- 双源校验（最新收盘价交叉核对，偏差超容差 → 报错退出、不发布）：
  纳指100 与中证500 用腾讯行情；黄金用东方财富实时报价接口（与日K不同通道）
  再叠加「单日涨跌幅在 ±3.5% 内」的合理性检查。

产物：
- data/latest.json  最新一期完整数据（页面读取）；
- data/history.csv  每日历史（按中证500点位日期去重，一日一行）。

任一主源拉取失败或校验不通过 → 非零退出，工作流不会提交坏数据。
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # 极端环境兜底：按 EST 固定偏移（仅影响盘中判定）
    ET = timezone(timedelta(hours=-5))

BJ = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOTAL = 3000
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

INSTRUMENTS = {
    "ndx":    {"name": "纳斯达克100", "secid": "100.NDX100", "unit": "点"},
    "csi500": {"name": "中证500",     "secid": "1.000905",   "unit": "点"},
    "gold":   {"name": "黄金（上海金 Au9999）", "secid": "118.AU9999", "unit": "元/克"},
}
TENCENT_CODE = {"ndx": "usNDX", "csi500": "sh000905"}
TOLERANCE = 0.006        # 双源偏差容差 0.6%
GOLD_DAY_BAND = 0.035    # 黄金单日涨跌合理性带宽 ±3.5%


def http_text(url, headers=None, retries=3):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:  # 东方财富偶发断连，重试不改变口径
            last = e
            time.sleep(3)
    raise last


def eastmoney_daily(secid):
    """东方财富日K → [(date, close)] 升序。"""
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&klt=101&fqt=0&beg=20200101&end=20500101"
           f"&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57")
    raw = http_text(url, headers={"Referer": "https://quote.eastmoney.com/"}, retries=4)
    data = json.loads(raw.decode())
    if data.get("rc") != 0 or not data.get("data", {}).get("klines"):
        raise RuntimeError(f"东方财富无数据: {secid}")
    bars = []
    for k in data["data"]["klines"]:
        p = k.split(",")
        bars.append((p[0], float(p[2])))
    return bars


def completed_bars(key, bars, now_bj, now_et):
    """按市场时钟剔除未完成 bar，返回已完成的日K。"""
    if key == "csi500":
        today = now_bj.strftime("%Y-%m-%d")
        if now_bj.hour * 60 + now_bj.minute < 15 * 60 + 5:
            bars = [b for b in bars if b[0] < today]
        else:
            bars = [b for b in bars if b[0] <= today]
    elif key == "gold":
        today = now_bj.strftime("%Y-%m-%d")
        # 夜盘会提前生成下一交易日 bar：一律只取 <= 今天（北京）的 bar
        bars = [b for b in bars if b[0] <= today]
        if now_bj.hour * 60 + now_bj.minute < 15 * 60 + 35:
            bars = [b for b in bars if b[0] < today]
    elif key == "ndx":
        today_et = now_et.strftime("%Y-%m-%d")
        if now_et.hour * 60 + now_et.minute < 16 * 60 + 5:
            bars = [b for b in bars if b[0] < today_et]
        else:
            bars = [b for b in bars if b[0] <= today_et]
    if not bars:
        raise RuntimeError(f"{key} 无已完成日K")
    return bars


def minus_30_months(date_str):
    y, m, d = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10])
    m -= 30
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}-{d:02d}"


def tencent_quote(code, now_et):
    """腾讯行情 → 最新价（美股盘中时返回昨收，保证与已完成收盘可比）。"""
    raw = http_text(f"https://qt.gtimg.cn/q={code}")
    text = raw.decode("gbk", errors="replace")
    body = text.split('="', 1)[1].rsplit('"', 1)[0]
    f = body.split("~")
    price = float(f[3])
    if code.startswith("us"):
        # f[30] 形如 2026-11-06 11:58:59（美东时间）；盘中未收盘则用昨收 f[4]
        try:
            qtime = datetime.strptime(f[30], "%Y-%m-%d %H:%M:%S")
            if qtime.date() == now_et.date() and (qtime.hour * 60 + qtime.minute) < 16 * 60 + 5:
                price = float(f[4])
        except Exception:
            pass
    return price


def eastmoney_quote_price(secid):
    """东方财富实时报价接口（与日K不同通道）→ 最新价。"""
    url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
           f"&fields=f43,f44,f45,f46,f60")
    data = json.loads(http_text(url, headers={"Referer": "https://quote.eastmoney.com/"}, retries=4).decode())
    return float(data["data"]["f43"]) / 100.0


def main():
    now_utc = datetime.now(timezone.utc)
    now_bj = now_utc.astimezone(BJ)
    now_et = now_utc.astimezone(ET)

    result = {}
    for key, meta in INSTRUMENTS.items():
        bars = completed_bars(key, eastmoney_daily(meta["secid"]), now_bj, now_et)
        point_date, point = bars[-1]
        start = minus_30_months(point_date)
        window = [c for d, c in bars if d >= start]
        mean30 = sum(window) / len(window)
        result[key] = {
            "name": meta["name"], "unit": meta["unit"],
            "point": round(point, 2), "point_date": point_date,
            "mean30": round(mean30, 2), "samples": len(window),
            "prev_close": round(bars[-2][1], 2) if len(bars) > 1 else None,
        }

    # ---- 双源校验 ----
    checks = {}
    for key in ("ndx", "csi500"):
        ref = tencent_quote(TENCENT_CODE[key], now_et)
        diff = abs(ref - result[key]["point"]) / result[key]["point"]
        checks[key] = {"source": "腾讯行情", "ref_price": round(ref, 2),
                       "diff_pct": round(diff * 100, 3)}
        if diff > TOLERANCE:
            raise RuntimeError(f"{key} 双源校验不通过：主源 {result[key]['point']} vs "
                               f"腾讯 {ref}（偏差 {diff*100:.2f}% > {TOLERANCE*100}%）")
    # 黄金：异通道核对 + 单日涨跌合理性
    ref = eastmoney_quote_price(INSTRUMENTS["gold"]["secid"])
    diff = abs(ref - result["gold"]["point"]) / result["gold"]["point"]
    day_chg = None
    if result["gold"]["prev_close"]:
        day_chg = abs(result["gold"]["point"] / result["gold"]["prev_close"] - 1)
    checks["gold"] = {"source": "东方财富实时报价通道", "ref_price": round(ref, 2),
                      "diff_pct": round(diff * 100, 3)}
    if diff > TOLERANCE:
        raise RuntimeError(f"gold 双通道校验不通过：日K {result['gold']['point']} vs 实时 {ref}")
    if day_chg is not None and day_chg > GOLD_DAY_BAND:
        raise RuntimeError(f"gold 单日涨跌异常：{day_chg*100:.2f}% 超出 ±{GOLD_DAY_BAND*100}%")

    # ---- 权重与分配（最大余数法） ----
    weights = {k: result[k]["mean30"] / result[k]["point"] for k in result}
    sw = sum(weights.values())
    exact = {k: TOTAL * weights[k] / sw for k in weights}
    base = {k: int(exact[k]) for k in exact}
    alloc = dict(base)
    for k in sorted(exact, key=lambda k: exact[k] - base[k], reverse=True)[:TOTAL - sum(base.values())]:
        alloc[k] += 1
    assert sum(alloc.values()) == TOTAL
    for k in result:
        result[k]["weight"] = round(weights[k], 4)
        result[k]["alloc"] = alloc[k]
        result[k]["check"] = checks[k]

    payload = {
        "updated_at_bj": now_bj.strftime("%Y-%m-%d %H:%M"),
        "total": TOTAL,
        "instruments": result,
        "source": "东方财富日K（主源）+ 腾讯行情/东方财富实时通道（校验）",
        "note": "点位为最近已完成交易日收盘；30月均值为滚动口径；金额为最大余数法取整。",
    }
    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    with open(os.path.join(BASE, "data", "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    hist_path = os.path.join(BASE, "data", "history.csv")
    key_date = result["csi500"]["point_date"]
    row = {"date": key_date}
    for k in ("ndx", "csi500", "gold"):
        row[f"{k}_point_date"] = result[k]["point_date"]
        row[f"{k}_point"] = result[k]["point"]
        row[f"{k}_mean30"] = result[k]["mean30"]
        row[f"{k}_weight"] = result[k]["weight"]
        row[f"{k}_alloc"] = result[k]["alloc"]
    fields = list(row.keys())
    rows = []
    if os.path.exists(hist_path):
        with open(hist_path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["date"] != key_date]
    rows.append(row)
    rows.sort(key=lambda r: r["date"])
    with open(hist_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("OK: data/latest.json 与 data/history.csv 已更新")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
