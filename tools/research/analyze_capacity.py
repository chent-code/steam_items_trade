#!/usr/bin/env python3
"""容量分析：
A. 每周一各比值分桶的品种数（名义容量/周，历史时序）
B. 用户假设检验：比值越低 → 前向收益/胜率是否越高（分桶 fwd_ask）
C. 资金容量（今日截面）：各阈值带的品种数、日成交额、可部署资金估计
"""
import json, os, sqlite3, statistics
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = timezone(timedelta(hours=8))
NET = 0.8696
BUCKETS = [(0.0, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 2.0)]


def daily_series(con):
    out = {}
    for gid, plat, key, ts_ms, v in con.execute(
            "select good_id,platform,key,ts_ms,value from chart_history"):
        d = datetime.fromtimestamp(ts_ms / 1000, TZ).strftime("%Y-%m-%d")
        out.setdefault((gid, plat, key), {}).setdefault(d, []).append(v)
    return {k: {d: vals[-1] for d, vals in sorted(days.items())} for k, days in out.items()}


class Near:
    def __init__(self, series):
        items = sorted(series.items())
        self.ord_ = [datetime.strptime(d, "%Y-%m-%d").date().toordinal() for d, _ in items]
        self.vals = [v for _, v in items]

    def at(self, day, tol=2):
        o = datetime.strptime(day, "%Y-%m-%d").date().toordinal()
        i = min(bisect.bisect_left(self.ord_, o), len(self.ord_) - 1)
        j = max(i - 1, 0)
        k = i if abs(self.ord_[i] - o) <= abs(self.ord_[j] - o) else j
        return self.vals[k] if abs(self.ord_[k] - o) <= tol else None


import bisect  # noqa: E402


def ret(n, a, b):
    va, vb = n.at(a), n.at(b)
    return (vb / va - 1.0) if (va and vb and va > 0) else None


def main():
    con = sqlite3.connect(os.path.join(HERE, "skin.db"))
    S = daily_series(con)
    N = {k: Near(v) for k, v in S.items()}
    items = json.load(open(os.path.join(HERE, "universe_csqaq.json"), encoding="utf-8"))

    def ask_n(gid):
        for plat in ("uuyp", "buff"):
            n = N.get((gid, plat, "sell_price"))
            if n:
                return n
        return None

    all_days = sorted({d for (g, p, k) in S if k == "sell_price" for d in S[(g, p, k)]})
    mondays = [d for d in all_days if datetime.strptime(d, "%Y-%m-%d").weekday() == 0]

    cohort_rows = []
    per_bucket = {b: [] for b in BUCKETS}
    for t in mondays:
        t7 = (datetime.strptime(t, "%Y-%m-%d").date() + timedelta(days=7)).isoformat()
        ratios = {}
        for it in items:
            gid = str(it["id"])
            an, sn = ask_n(gid), N.get((gid, "steam", "buy_price"))
            if not an or not sn:
                continue
            ask_t, sb_t = an.at(t), sn.at(t)
            if not ask_t or not sb_t or ask_t < 10:
                continue
            ratios[it["market_hash_name"]] = ask_t / (sb_t * NET)
        if len(ratios) < 40:
            continue
        counts = {b: 0 for b in BUCKETS}
        for name, r in ratios.items():
            for lo, hi in BUCKETS:
                if lo <= r < hi:
                    counts[(lo, hi)] += 1
                    fa = ret(ask_n(next(str(x["id"]) for x in items if x["market_hash_name"] == name)), t, t7)
                    if fa is not None:
                        per_bucket[(lo, hi)].append(fa)
                    break
        cohort_rows.append({"cohort": t, **{f"n_{lo}-{hi}": c for (lo, hi), c in counts.items()}})

    print("A. 每周一各比值带的机会笔数（历史时序汇总）:")
    hdr = "  " + "  ".join(f"[{lo:.2f}-{hi:.2f})" for lo, hi in BUCKETS)
    print(hdr)
    series_by_band = {b: [] for b in BUCKETS}
    for row in cohort_rows:
        for b in BUCKETS:
            series_by_band[b].append(row[f"n_{b[0]}-{b[1]}"])
    for b in BUCKETS:
        xs = series_by_band[b]
        if xs:
            print(f"  [{b[0]:.2f}-{b[1]:.2f}): 均值 {statistics.mean(xs):5.1f}  中位 {statistics.median(xs):5.0f}"
                  f"  最小 {min(xs):3.0f}  最大 {max(xs):4.0f}")

    print("\nB. 假设检验：比值越低 → 前向收益/胜率越高？")
    for b in BUCKETS:
        xs = per_bucket[b]
        if xs:
            xs.sort()
            n_ = len(xs)
            hit = sum(1 for x in xs if x > 0)
            print(f"  [{b[0]:.2f}-{b[1]:.2f}): n={n_:5d}  均值 {statistics.mean(xs)*100:+6.2f}%"
                  f"  中位 {statistics.median(xs)*100:+6.2f}%  胜率 {hit/n_:.0%}"
                  f"  10分位 {xs[n_//10]*100:+6.2f}%")

    print("\nC. 今日资金容量截面（需要扩池后才有 0.75-0.85 带,先用现有 250 池演示口径）:")
    rows = []
    for it in items:
        r = 1.0 / it["max_price"]
        tn = it.get("turnover_number") or 0
        price = it.get("buff_sell_price") or 0
        rows.append((r, tn, price, it["market_hash_name"]))
    for lo, hi in [(0.0, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80)]:
        sub = [x for x in rows if lo <= x[0] < hi]
        daily_val = sum(tn * p for _, tn, p, _ in sub)
        # 可部署假设: 捕获日成交额的 10%~20%,滚动 7 天
        cap10 = daily_val * 0.10 * 7
        cap20 = daily_val * 0.20 * 7
        print(f"  [{lo:.2f}-{hi:.2f}): 品种数 {len(sub):4d}  日成交额¥{daily_val:9,.0f}"
              f"  周可部署(10%捕获)¥{cap10:9,.0f}  (20%捕获)¥{cap20:9,.0f}")


if __name__ == "__main__":
    main()
