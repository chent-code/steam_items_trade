#!/usr/bin/env python3
"""检验：信号入选品种，前向一周"平台涨、Steam跌"是否为系统性形态？
并给出两端收敛的落点分布。
"""
import json, os, sqlite3, statistics
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = timezone(timedelta(hours=8))
NET = 0.8696


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

    rows = []
    for t in mondays:
        t7 = (datetime.strptime(t, "%Y-%m-%d").date() + timedelta(days=7)).isoformat()
        pool = []
        for it in items:
            gid = str(it["id"])
            an, sn = ask_n(gid), N.get((gid, "steam", "buy_price"))
            if not an or not sn:
                continue
            ask_t, sb_t = an.at(t), sn.at(t)
            if not ask_t or not sb_t or ask_t < 10:
                continue
            pool.append((gid, ask_t / (sb_t * NET)))
        if len(pool) < 40:
            continue
        for gid, ratio in sorted(pool, key=lambda x: x[1])[:20]:
            an = ask_n(gid)
            ss = N.get((gid, "steam", "sell_price"))
            sb = N.get((gid, "steam", "buy_price"))
            ask_t, ask_7 = an.at(t), an.at(t7)
            ss_t, ss_7 = ss.at(t), ss.at(t7)
            if not all([ask_t, ask_7, ss_t, ss_7]):
                continue
            plat_fwd = ask_7 / ask_t - 1
            steam_fwd = ss_7 / ss_t - 1
            r_t = ask_t / (ss_t)            # 平台ask/Steam卖价(用户口径)
            r_7 = ask_7 / (ss_7)
            rows.append({"t": t, "gid": gid, "plat_fwd": plat_fwd, "steam_fwd": steam_fwd,
                         "gap": plat_fwd - steam_fwd, "r_t": r_t, "r_7": r_7})

    n_ = len(rows)
    pf = [r["plat_fwd"] for r in rows]
    sf = [r["steam_fwd"] for r in rows]
    gp = [r["gap"] for r in rows]
    print(f"入选名-周样本: {n_}")
    print(f"\nA. 前向7日收益(入选品种):")
    print(f"  平台端: 均值 {statistics.mean(pf)*100:+.2f}%  中位 {statistics.median(pf)*100:+.2f}%"
          f"  上涨占比 {sum(1 for x in pf if x > 0)/n_:.0%}")
    print(f"  Steam端: 均值 {statistics.mean(sf)*100:+.2f}%  中位 {statistics.median(sf)*100:+.2f}%"
          f"  下跌占比 {sum(1 for x in sf if x < 0)/n_:.0%}")
    print(f"  收敛差(平台-Steam): 均值 {statistics.mean(gp)*100:+.2f}%")

    print(f"\nB. 四种形态的占比:")
    upup = sum(1 for r in rows if r["plat_fwd"] > 0 and r["steam_fwd"] > 0)
    updn = sum(1 for r in rows if r["plat_fwd"] > 0 and r["steam_fwd"] <= 0)
    dnup = sum(1 for r in rows if r["plat_fwd"] <= 0 and r["steam_fwd"] > 0)
    dndn = sum(1 for r in rows if r["plat_fwd"] <= 0 and r["steam_fwd"] <= 0)
    print(f"  平台涨+Steam涨: {upup/n_:.0%}   平台涨+Steam跌: {updn/n_:.0%}")
    print(f"  平台跌+Steam涨: {dnup/n_:.0%}   平台跌+Steam跌: {dndn/n_:.0%}")

    print(f"\nC. 比值落点(平台ask/Steam卖价):")
    rt = [r["r_t"] for r in rows]
    r7 = [r["r_7"] for r in rows]
    print(f"  入选时: 均值 {statistics.mean(rt):.3f}  中位 {statistics.median(rt):.3f}")
    print(f"  一周后: 均值 {statistics.mean(r7):.3f}  中位 {statistics.median(r7):.3f}")
    zones = [(0, .65), (.65, .70), (.70, .75), (.75, .80), (.80, .90), (.90, 2)]
    print("  一周后落点分布: " + "  ".join(
        f"[{lo:.2f}-{hi:.2f}):{sum(1 for x in r7 if lo <= x < hi)/n_:.0%}" for lo, hi in zones))


if __name__ == "__main__":
    main()
