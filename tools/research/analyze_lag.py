#!/usr/bin/env python3
"""检验两个命题：
A. 用户场景一：真实信号选中的是"Steam先涨、平台滞后"的品种吗？
   - 入选品种的 prior_gap（Steam 7日涨幅 - 平台 7日涨幅）是否显著大于全池基线
   - prior_gap 高低分组的前向收益是否单调（lead-lag 收敛则应为正且递减）
B. 用户场景二：Steam 涨幅尖峰（短期暴涨）是否对应亏损？
   - 按 prior_gap 分四分位，看前向 fwd_ask / fwd_bid
   - 尖峰嫌疑（prior_gap 最高分位）单独报告
"""
import bisect, json, os, sqlite3, statistics
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
    flat = {}
    for k, days in out.items():
        flat[k] = {d: vals[-1] for d, vals in sorted(days.items())}
    return flat


class Near:
    """有序日期序列的快速就近取值"""
    def __init__(self, series):
        items = sorted(series.items())
        self.days = [d for d, _ in items]
        self.vals = [v for _, v in items]
        self.epoch = [datetime.strptime(d, "%Y-%m-%d").date().toordinal() for d in self.days]

    def at(self, day, tol=2):
        o = datetime.strptime(day, "%Y-%m-%d").date().toordinal()
        i = bisect.bisect_left(self.epoch, o)
        cands = []
        if i < len(self.epoch):
            cands.append(i)
        if i > 0:
            cands.append(i - 1)
        best, bg = None, None
        for i2 in cands:
            g = abs(self.epoch[i2] - o)
            if bg is None or g < bg:
                best, bg = self.vals[i2], g
        return best if bg is not None and bg <= tol else None

    def window(self, d1, d2):
        lo = bisect.bisect_left(self.epoch, datetime.strptime(d1, "%Y-%m-%d").date().toordinal())
        hi = bisect.bisect_left(self.epoch, datetime.strptime(d2, "%Y-%m-%d").date().toordinal())
        return self.vals[lo:hi]


def ret(n, a, b):
    va, vb = n.at(a), n.at(b)
    return (vb / va - 1.0) if (va and vb and va > 0) else None


def main():
    con = sqlite3.connect(os.path.join(HERE, "skin.db"))
    items = {str(it["id"]): it for it in json.load(open(
        os.path.join(HERE, "universe_csqaq.json"), encoding="utf-8"))}
    S = daily_series(con)
    N = {k: Near(v) for k, v in S.items()}

    def get(gid, plat, key):
        return N.get((gid, plat, key))

    def ask_n(gid):
        for plat in ("uuyp", "buff"):
            n = get(gid, plat, "sell_price")
            if n:
                return n
        return None

    def pbid_n(gid):
        for plat in ("uuyp", "buff"):
            n = get(gid, plat, "buy_price")
            if n:
                return n
        return None

    all_days = sorted({d for (gid, plat, key) in S if key == "sell_price" for d in S[(gid, plat, key)]})
    mondays = [d for d in all_days if datetime.strptime(d, "%Y-%m-%d").weekday() == 0]

    sel, pool_prior = [], []
    for t in mondays:
        t7 = (datetime.strptime(t, "%Y-%m-%d").date() + timedelta(days=7)).isoformat()
        tp = (datetime.strptime(t, "%Y-%m-%d").date() - timedelta(days=7)).isoformat()
        pool = []
        for gid in items:
            an, sn = ask_n(gid), get(gid, "steam", "buy_price")
            if not an or not sn:
                continue
            ask_t, sb_t = an.at(t), sn.at(t)
            if not ask_t or not sb_t or ask_t < 10:
                continue
            pool.append((gid, ask_t / (sb_t * NET)))
        if len(pool) < 40:
            continue
        ranked = sorted(pool, key=lambda x: x[1])[:20]

        gaps = []
        for gid, _ in pool:
            an, sn = ask_n(gid), get(gid, "steam", "buy_price")
            pa, ps = ret(an, tp, t), ret(sn, tp, t)
            if pa is not None and ps is not None:
                gaps.append(ps - pa)
        if gaps:
            pool_prior.append(statistics.mean(gaps))

        for gid, ratio in ranked:
            an, sn, bn = ask_n(gid), get(gid, "steam", "buy_price"), pbid_n(gid)
            ask_t = an.at(t)
            seg = an.window((datetime.strptime(t, "%Y-%m-%d").date()
                             - timedelta(days=30)).isoformat(), t)
            if len(seg) < 10 or not ask_t:
                continue
            pa, ps = ret(an, tp, t), ret(sn, tp, t)
            fa, psf = ret(an, t, t7), ret(sn, t, t7)
            fb = ret(bn, t, t7) if bn else None
            sel.append({
                "cohort": t, "tail": ask_t / statistics.median(seg),
                "prior_gap": (ps - pa) if (pa is not None and ps is not None) else None,
                "fwd_ask": fa, "fwd_bid": fb,
                "fwd_gap": (fa - psf) if (fa is not None and psf is not None) else None,
            })

    print(f"入选名-周样本: {len(sel)}; 全池 prior_gap 基线: {statistics.mean(pool_prior)*100:+.2f}%"
          f" (n={len(pool_prior)} 个 cohort)\n")

    def stat(rows, label):
        if not rows:
            print(f"  {label}: 无样本")
            return
        fa = [r["fwd_ask"] for r in rows if r["fwd_ask"] is not None]
        fb = [r["fwd_bid"] for r in rows if r["fwd_bid"] is not None]
        fg = [r["fwd_gap"] for r in rows if r["fwd_gap"] is not None]
        pg = [r["prior_gap"] for r in rows if r["prior_gap"] is not None]
        s = f"  {label}: n={len(rows)}"
        if pg: s += f"  prior_gap={statistics.mean(pg)*100:+.2f}%"
        if fa: s += f"  fwd_ask={statistics.mean(fa)*100:+.2f}%"
        if fb: s += f"  fwd_bid={statistics.mean(fb)*100:+.2f}%"
        if fg: s += f"  fwd_gap(平台-Steam)={statistics.mean(fg)*100:+.2f}%"
        print(s)

    print("A. 入选品种 vs 全池基线（是否选中 Steam 领先品种）:")
    stat(sel, "全部入选")
    stat([r for r in sel if r["tail"] is not None and r["tail"] >= 1], "tail_score>=1（ask不低于自身常态）")
    stat([r for r in sel if r["tail"] is not None and r["tail"] < 1], "tail_score<1 （经典低价尾）")

    print("\nB. 按 prior_gap 四分位分组（Steam 7日领先幅度）:")
    pgs = sorted([r["prior_gap"] for r in sel if r["prior_gap"] is not None])
    if len(pgs) >= 8:
        q1, q2, q3 = statistics.quantiles(pgs, n=4)
        buckets = [("Q1 最低(含尖峰回落嫌疑的反面)", lambda g: g <= q1),
                   ("Q2", lambda g: q1 < g <= q2),
                   ("Q3", lambda g: q2 < g <= q3),
                   ("Q4 最高(尖峰嫌疑)", lambda g: g > q3)]
        for label, f in buckets:
            stat([r for r in sel if r["prior_gap"] is not None and f(r["prior_gap"])], label)

    print("\nC. 尖峰嫌疑专项（prior_gap > 30pp，即 Steam 7日领先平台 30pp 以上）:")
    hi = [r for r in sel if r["prior_gap"] is not None and r["prior_gap"] > 0.30]
    stat(hi, "尖峰嫌疑组")
    lo = [r for r in sel if r["prior_gap"] is not None and r["prior_gap"] <= 0.30]
    stat(lo, "其余组")

    print("\nD. 尖峰组左尾体检（fwd_ask 分布）:")
    fa_hi = sorted([r["fwd_ask"] for r in hi if r["fwd_ask"] is not None])
    if fa_hi:
        n_ = len(fa_hi)
        print(f"  命中率(>0): {sum(1 for x in fa_hi if x > 0)}/{n_} = {sum(1 for x in fa_hi if x > 0)/n_:.0%}")
        print(f"  中位 {statistics.median(fa_hi)*100:+.2f}%  10分位 {fa_hi[n_//10]*100:+.2f}%  最差 {fa_hi[0]*100:+.2f}%")

    print("\nE. 全池 prior_gap 基线（中位数口径, 修均值被坏点拖垮的问题）:")
    if pool_prior:
        print(f"  逐cohort均值的中位: {statistics.median(pool_prior)*100:+.2f}%  (均值中位 {statistics.mean(pool_prior)*100:+.2f}%)")


if __name__ == "__main__":
    main()
