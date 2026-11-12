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
  的昨收 f60 核对（f43 最新价含夜盘报价、与已完成收盘系统性偏离，不用），
  再叠加「单日涨跌幅在 ±3.5% 内」的合理性检查。

产物：
- data/latest.json  最新一期完整数据（页面读取）；
- data/history.csv  每日历史（按中证500点位日期去重，一日一行）。

任一主源拉取失败或校验不通过 → 非零退出，工作流不会提交坏数据。
"""
import calendar
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


def http_text(url, headers=None, retries=6):
    # 东方财富/腾讯偶发断连（Remote end closed / 502）：固定 3 秒短间隔、
    # 3–4 次重试扛不过分钟级抽风窗口（2026-11-12 外部触发首跑 08:41 与
    # 手动补跑 08:46 连续两次被断连杀掉、抓数步仅 7–16 秒即退出）。
    # 改为递增间隔 3/8/20/45/90 秒、共 6 次尝试：抽风窗口内自动等恢复；
    # 全部失败才报错退出，数据口径与校验逻辑不变。
    delays = [3, 8, 20, 45, 90]
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(delays[min(attempt, len(delays) - 1)])
    raise last


def eastmoney_daily(secid):
    """东方财富日K → [(date, close)] 升序。"""
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&klt=101&fqt=0&beg=20200101&end=20500101"
           f"&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57")
    raw = http_text(url, headers={"Referer": "https://quote.eastmoney.com/"}, retries=6)
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
    d = min(d, calendar.monthrange(y, m)[1])  # 钳制到当月最后一天，避免 09-31 这类非法日期
    return f"{y:04d}-{m:02d}-{d:02d}"


def tencent_quote(code, now_et, now_bj):
    """腾讯行情 → 与主源最新已完成收盘同口径的核对价。

    美股与 A 股各按自己的市场时钟处理：未收盘时段主源最新已完成收盘就是
    昨收，核对价取昨收 f[4]；直接用实时价 f[3] 会在盘中与昨收系统性偏离、
    误杀流水线（中证500 于 2026-11-10 11:11 手动补跑实测：主源周一收盘
    6586.18 vs 实时 6647.67，偏差 0.93% > 0.6% 容差，运行 #4 被杀；
    此前只有美股分支有此处理、A 股漏了）。
    """
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
    elif now_bj.hour * 60 + now_bj.minute < 15 * 60 + 5:
        # A股主源当日 bar 到北京时间 15:05 后才算已完成（与 completed_bars
        # 的 csi500 判定线一致），在此之前核对价取昨收 f[4]
        price = float(f[4])
        # 例外——盘前（约 09:15 前，08:41 主档正落在此窗口）腾讯快照还停
        # 在上一交易日：f[30] 日期小于今天，此时 f[3] 就是上一交易日收盘
        # （= 主源最新已完成收盘），f[4] 反而是上上个交易日的收盘、会误杀
        # （2026-11-12 08:52 本地实测：主源周三收盘 6713.18 vs f[4] 周二
        # 收盘 6645.89，偏差 1.00% > 0.6%）。按快照日期改取 f[3]。
        try:
            qdate = datetime.strptime(f[30][:8], "%Y%m%d").date()
            if qdate < now_bj.date():
                price = float(f[3])
        except Exception:
            pass
    return price


def eastmoney_quote_prev_close(secid):
    """东方财富实时报价接口（与日K不同通道）→ 昨收 f60。

    不用 f43：f43 是当前最新价（含夜盘/下一时段报价），与「最近一个已完成
    交易日收盘」系统性偏离——隔周末或盘后运行时金价正常波动即可超过容差、
    误杀流水线（2026-11-07 已在收益脚本上确认此坑、2026-11-09 在本脚本
    下午时点复现：日K 868.57 vs f43 879.36，差 1.24%）。f60 昨收与日K最新
    已完成收盘同口径（2026-11-07 实测 f60=868.57 与 11-06 日K收盘分毫不差），
    与 scripts/fetch_returns.py 的黄金校验一致。
    """
    # 节点兜底：2026-11-12 实测 push2.eastmoney.com 主机名整段 502（重试
    # 6 次、约 3 分钟仍不通），其编号镜像节点（82/63/7.push2）同接口正常。
    # 改为多节点依次尝试：仍是与日K不同的报价通道，校验口径不变；单节点
    # 重试 3 次（递增间隔），全灭才报错。
    hosts = ["push2.eastmoney.com", "82.push2.eastmoney.com",
             "63.push2.eastmoney.com", "7.push2.eastmoney.com"]
    last = None
    for host in hosts:
        url = (f"https://{host}/api/qt/stock/get?secid={secid}"
               f"&fields=f43,f44,f45,f46,f60")
        try:
            data = json.loads(http_text(url, headers={"Referer": "https://quote.eastmoney.com/"}, retries=3).decode())
            return float(data["data"]["f60"]) / 100.0
        except Exception as e:
            last = e
    raise last


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
        ref = tencent_quote(TENCENT_CODE[key], now_et, now_bj)
        diff = abs(ref - result[key]["point"]) / result[key]["point"]
        checks[key] = {"source": "腾讯行情", "ref_price": round(ref, 2),
                       "diff_pct": round(diff * 100, 3)}
        if diff > TOLERANCE:
            raise RuntimeError(f"{key} 双源校验不通过：主源 {result[key]['point']} vs "
                               f"腾讯 {ref}（偏差 {diff*100:.2f}% > {TOLERANCE*100}%）")
    # 黄金：异通道核对（昨收 vs 日K最新已完成收盘） + 单日涨跌合理性
    ref = eastmoney_quote_prev_close(INSTRUMENTS["gold"]["secid"])
    diff = abs(ref - result["gold"]["point"]) / result["gold"]["point"]
    day_chg = None
    if result["gold"]["prev_close"]:
        day_chg = abs(result["gold"]["point"] / result["gold"]["prev_close"] - 1)
    checks["gold"] = {"source": "东方财富实时报价通道（昨收）", "ref_price": round(ref, 2),
                      "diff_pct": round(diff * 100, 3)}
    if diff > TOLERANCE:
        raise RuntimeError(f"gold 双通道校验不通过：日K {result['gold']['point']} vs 昨收 {ref}")
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
