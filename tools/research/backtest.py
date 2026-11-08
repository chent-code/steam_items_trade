#!/usr/bin/env python3
"""构建四个指数并回测挂刀比例策略。stdlib only。

用法:
  python backtest.py --db skin.db --out reports/run01 [--config config.json] [--index1 quote|sales]
"""
import argparse, bisect, csv, json, math, os, random, sqlite3, statistics, sys
from datetime import datetime, timedelta

DEFAULTS = {
    "net_multiplier": 0.8696,          # = 1/1.15，与社区"75折"锚口径一致
    "bid_min_volume": 0,               # Steam 求购单最小挂单量（0=不过滤；>0 用于排除富报价）
    "min_price": 10.0, "min_vol24": 10.0,
    "hold_days": 7, "hold_tolerance": 2, "rebalance_weekday": 0,
    "sizes": [10, 20, 50], "primary_n": 20,
    "position_units": 1, "max_name_weight": 0.2,
    "withdrawal_fee": 0.01, "random_seed": 42,
    "ask_platform_priority": ["uuyp", "buff"],
    "primary_exit": "bid",             # 先验指定：平台求购价退出（保守口径），不许回头改
    "rank_mode": "ratio",              # ratio=Steam相对折扣 | tail=安慰剂(ask/自身30日中位数)
    "tail_window": 30, "tail_min_obs": 10,
}


def load_cfg(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cfg_get(cfg, k):
    return cfg.get("backtest", {}).get(k, DEFAULTS.get(k))


def d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def pct(x):
    return f"{x*100:.2f}%" if x is not None else "—"


def load_days(con):
    return sorted({r[0] for r in con.execute("select distinct substr(ts,1,10) from snapshots")})


def cross_section(con, day, ask_priority):
    """item -> {ask, ask_vol, ask_src, steam_bid, steam_bid_vol, vol24}（当日每平台取最晚一条）"""
    rows = con.execute(
        "select item,platform,ask,ask_vol,bid,bid_vol,vol24,ts from snapshots "
        "where substr(ts,1,10)=? order by ts", (day,)).fetchall()
    latest = {}
    for r in rows:
        latest[(r["item"], r["platform"])] = r
    out = {}
    for (item, plat), r in latest.items():
        o = out.setdefault(item, {"vol24": 0.0})
        if plat in ask_priority and r["ask"] and r["ask"] > 0 and "ask" not in o:
            o["ask"] = r["ask"]
            o["ask_vol"] = r["ask_vol"] or 0.0
            o["ask_src"] = plat
            o["plat_bid"] = r["bid"] or 0.0
            o["plat_bid_vol"] = r["bid_vol"] or 0.0
        if plat == "steam":
            b = r["bid"] or 0.0
            if b > 0 and b > (o.get("steam_bid") or 0.0):
                o["steam_bid"] = b
                o["steam_bid_vol"] = r["bid_vol"] or 0.0
        o["vol24"] = max(o["vol24"], r["vol24"] or 0.0)
    return out


def ratio_of(sec, item, net_mult):
    o = sec.get(item) or {}
    ask, bid = o.get("ask"), o.get("steam_bid")
    if not ask or not bid:
        return None
    return ask / (bid * net_mult)


def eligible(sec, cfg, net_mult):
    lo, mv = cfg_get(cfg, "min_price"), cfg_get(cfg, "min_vol24")
    bv = cfg_get(cfg, "bid_min_volume")
    out = {}
    for item, o in sec.items():
        ask, bid = o.get("ask"), o.get("steam_bid")
        if not ask or not bid or ask < lo or o["vol24"] < mv:
            continue
        if (o.get("steam_bid_vol") or 0) < bv:
            continue
        out[item] = ask / (bid * net_mult)
    return out


def name_returns(entry, ex, net_mult, wfee):
    a, b, s = ex.get("ask"), ex.get("plat_bid"), ex.get("steam_bid")
    r_ask = (a / entry - 1.0) * (1 - wfee) if a else None
    r_bid = (b / entry - 1.0) * (1 - wfee) if b else None
    r_steam = (s * net_mult / entry - 1.0) if s else None
    return r_ask, r_bid, r_steam


def portfolio(rets, cap):
    n = len(rets)
    if n == 0:
        return None
    w = [min(1.0 / n, cap)] * n
    tot = sum(w)
    return sum(r * wi for r, wi in zip(rets, w)) / tot


def pick_exit(dates, t, hold, tol):
    target = d(t) + timedelta(days=hold)
    best, gap = None, None
    for x in dates:
        g = abs((d(x) - target).days)
        if gap is None or g < gap:
            best, gap = x, g
    return best if (gap is not None and gap <= tol and best != t) else None


def price_buckets(sec, items, nb=10):
    priced = sorted((sec[i]["ask"], i) for i in items if sec.get(i, {}).get("ask"))
    if not priced:
        return {}
    step = max(1, len(priced) // nb)
    return {item: k // step for k, (_, item) in enumerate(priced)}


def tstat(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 3:
        return None
    sd = statistics.stdev(xs)
    return statistics.mean(xs) / (sd / math.sqrt(len(xs))) if sd else None


def corr(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    mx = statistics.mean(p[0] for p in pairs)
    my = statistics.mean(p[1] for p in pairs)
    num = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    dx = math.sqrt(sum((p[0] - mx) ** 2 for p in pairs))
    dy = math.sqrt(sum((p[1] - my) ** 2 for p in pairs))
    return num / (dx * dy) if dx and dy else None


def index1_quote(cross_by_day, cohorts, cfg):
    per = {}
    for t, t7 in cohorts:
        s_t, s_7 = cross_by_day[t], cross_by_day[t7]
        num = den = 0.0
        eq = []
        for item, o in s_t.items():
            ask = o.get("ask")
            if not ask or ask < cfg_get(cfg, "min_price"):
                continue
            o7 = s_7.get(item)
            if not o7 or not o7.get("ask"):
                continue
            r = o7["ask"] / ask - 1.0
            w = (o.get("vol24") or 0.0) * ask
            num += r * w
            den += w
            eq.append(r)
        per[t] = (num / den if den else None, statistics.mean(eq) if eq else None)
    return per


def index1_sales(con, cohorts):
    """steam_sales 回填后的 1 号指数：按 成交量×成交额 加权"""
    per = {}
    for t, t7 in cohorts:
        rows = con.execute(
            "select item,"
            " sum(case when date=? then price*qty end) ta, sum(case when date=? then qty end) qa,"
            " sum(case when date=? then price*qty end) tb, sum(case when date=? then qty end) qb"
            " from steam_sales where date in (?,?) group by item", (t, t, t7, t7, t, t7)).fetchall()
        num = den = 0.0
        for item, ta, qa, tb, qb in rows:
            if not ta or not qa or not tb or not qb:
                continue
            pa, pb = ta / qa, tb / qb
            r = pb / pa - 1.0
            w = ta + tb
            num += r * w
            den += w
        per[t] = (num / den if den else None, None)
    return per


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(here, "skin.db"))
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=os.path.join(here, "reports", "run"))
    ap.add_argument("--index1", choices=["quote", "sales"], default="quote")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    net_mult = cfg_get(cfg, "net_multiplier")
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    con.execute("CREATE INDEX IF NOT EXISTS idx_snap_day ON snapshots(substr(ts,1,10))")
    con.commit()

    dates = load_days(con)
    if len(dates) < cfg_get(cfg, "hold_days") + 2:
        sys.exit(f"快照天数不足（{len(dates)} 天），先攒数据")

    wd = cfg_get(cfg, "rebalance_weekday")
    cohorts = []
    for t in [x for x in dates if d(x).weekday() == wd]:
        t7 = pick_exit(dates, t, cfg_get(cfg, "hold_days"), cfg_get(cfg, "hold_tolerance"))
        if t7:
            cohorts.append((t, t7))
    label = "冒烟测试（<26，不构成验证）" if len(cohorts) < 26 else "达标（≥26）"
    print(f"cohort 数: {len(cohorts)} —— {label}")
    if not cohorts:
        sys.exit("没有可用 cohort：检查快照是否覆盖固定周几、T+7 是否落在容差内")

    needed = sorted({x for pair in cohorts for x in pair})
    print(f"物化 {len(needed)}/{len(dates)} 个交易日的截面…")
    cross_by_day = {x: cross_section(con, x, cfg_get(cfg, "ask_platform_priority")) for x in needed}

    rank_mode = cfg_get(cfg, "rank_mode")
    hist_dates, hist_asks = None, None
    if rank_mode == "tail":
        askp = cfg_get(cfg, "ask_platform_priority")
        hist = {}
        for r in con.execute("select substr(ts,1,10) d, item, ask from snapshots "
                             "where platform in (?,?) and ask is not null order by item, d",
                             tuple(askp)):
            hist.setdefault(r["item"], ([], []))
            hist[r["item"]][0].append(r["d"])
            hist[r["item"]][1].append(r["ask"])
        hist_dates, hist_asks = hist, hist
        print(f"[rank_mode=tail] 安慰剂排名: ask/自身近{cfg_get(cfg,'tail_window')}日中位数 "
              f"({len(hist)} 品种有历史)")

    i1 = (index1_sales(con, cohorts) if args.index1 == "sales" else
          index1_quote(cross_by_day, cohorts, cfg))
    if args.index1 == "sales" and all(v[0] is None for v in i1.values()):
        print("[index1] steam_sales 无数据，退回 quote 模式")
        i1 = index1_quote(cross_by_day, cohorts, cfg)

    rng = random.Random(cfg_get(cfg, "random_seed"))
    sizes = cfg_get(cfg, "sizes")
    per_reb, per_name = [], []
    excess = {n: [] for n in sizes}
    dratio = {n: [] for n in sizes}

    for t, t7 in cohorts:
        sec_t, sec_7 = cross_by_day[t], cross_by_day[t7]
        ratios = eligible(sec_t, cfg, net_mult)
        if rank_mode == "tail":
            win, minobs = cfg_get(cfg, "tail_window"), cfg_get(cfg, "tail_min_obs")
            lo_day = (d(t) - timedelta(days=win)).isoformat()
            scores = {}
            for it in ratios:
                dts, asks = hist_dates.get(it, (None, None))
                if not dts:
                    continue
                hi = bisect.bisect_left(dts, t)
                lo = bisect.bisect_left(dts, lo_day)
                seg = asks[lo:hi]
                if len(seg) < minobs:
                    continue
                med = statistics.median(seg)
                if med:
                    scores[it] = sec_t[it]["ask"] / med
            ratios = scores
        if len(ratios) < max(sizes) * 2:
            print(f"[{t}] 合格品种 {len(ratios)} 不足，跳过")
            continue
        ranked = sorted(ratios, key=lambda k: ratios[k])
        bmap = price_buckets(sec_t, list(ratios))
        byb = {}
        for it in ratios:
            byb.setdefault(bmap.get(it, -1), []).append(it)

        for n in sizes:
            names = ranked[:n]
            rets = {"ask": [], "bid": [], "steam": []}
            thin, r_t7s = 0, []
            for it in names:
                entry = sec_t[it]["ask"]
                if (sec_t[it].get("ask_vol") or 0) < cfg_get(cfg, "position_units"):
                    thin += 1
                ra, rb, rs = name_returns(entry, sec_7.get(it, {}), net_mult,
                                          cfg_get(cfg, "withdrawal_fee"))
                for k, v in (("ask", ra), ("bid", rb), ("steam", rs)):
                    if v is not None:
                        rets[k].append(v)
                r7 = ratio_of(sec_7, it, net_mult)
                if r7 is not None:
                    r_t7s.append(r7 - ratios[it])
                per_name.append({"cohort": t, "n": n, "item": it, "entry": entry,
                                 "ratio_t": ratios[it], "ratio_t7": r7,
                                 "ret_ask": ra, "ret_bid": rb, "ret_steam": rs})
            dr = statistics.mean(r_t7s) if r_t7s else None

            picks = []
            ctrl_n = min(sum(len(v) for v in byb.values()), n * 3)
            keys = [k for k in byb if byb[k]]
            while len(picks) < ctrl_n and keys:
                k = rng.choice(keys)
                picks.append(byb[k].pop(rng.randrange(len(byb[k]))))
                if not byb[k]:
                    keys.remove(k)
            c_rets = []
            for it in picks:
                _, rb, _ = name_returns(sec_t[it]["ask"], sec_7.get(it, {}), net_mult,
                                        cfg_get(cfg, "withdrawal_fee"))
                if rb is not None:
                    c_rets.append(rb)
            ctrl = statistics.mean(c_rets) if c_rets else None

            row = {"cohort": t, "n": n,
                   "ret_ask": portfolio(rets["ask"], cfg_get(cfg, "max_name_weight")),
                   "ret_bid": portfolio(rets["bid"], cfg_get(cfg, "max_name_weight")),
                   "ret_steam": portfolio(rets["steam"], cfg_get(cfg, "max_name_weight")),
                   "control_bid": ctrl,
                   "index1_tw": (i1.get(t) or (None, None))[0],
                   "index1_eq": (i1.get(t) or (None, None))[1],
                   "thin_top": thin, "delta_ratio": dr}
            row["excess_ctrl"] = (row["ret_bid"] - ctrl
                                  if row["ret_bid"] is not None and ctrl is not None else None)
            row["excess_i1"] = (row["ret_bid"] - row["index1_tw"]
                                if row["ret_bid"] is not None and row["index1_tw"] is not None else None)
            per_reb.append(row)
            if row["excess_ctrl"] is not None:
                excess[n].append(row["excess_ctrl"])
            if dr is not None:
                dratio[n].append(dr)

    os.makedirs(args.out, exist_ok=True)
    write_report(args, cfg, per_reb, excess, dratio, i1, len(cohorts), sizes)
    with open(os.path.join(args.out, "per_rebalance.csv"), "w", newline="", encoding="utf-8") as f:
        if per_reb:
            w = csv.DictWriter(f, fieldnames=list(per_reb[0].keys()))
            w.writeheader()
            w.writerows(per_reb)
    with open(os.path.join(args.out, "per_name.csv"), "w", newline="", encoding="utf-8") as f:
        if per_name:
            w = csv.DictWriter(f, fieldnames=list(per_name[0].keys()))
            w.writeheader()
            w.writerows(per_name)
    print(f"报告: {args.out}/summary.md")


def write_report(args, cfg, per_reb, excess, dratio, i1, ncohorts, sizes):
    prim = cfg_get(cfg, "primary_n")
    pexit = "ret_" + cfg_get(cfg, "primary_exit")
    L = ["# 挂刀比例策略回测报告", "",
         f"- cohort 数：{ncohorts}" + ("—— **冒烟测试，不构成验证**（≥26 才算）" if ncohorts < 26 else ""),
         f"- 主指标（先验指定）：N={prim}，退出=平台求购价（保守口径），对照=同价位分桶随机",
         f"- 折扣比口径：平台在售价 ÷ (Steam求购价 × {cfg_get(cfg, 'net_multiplier')})",
         f"- 1 号指数模式：{args.index1}", ""]
    for n in sizes:
        rows = [r for r in per_reb if r["n"] == n]
        if not rows:
            continue
        ex = excess.get(n, [])
        c = corr(dratio.get(n, []), ex)
        L.append(f"## 指数{'★' if n == prim else ''}：折扣最低前 {n} 名")
        L.append("| cohort | ret_ask | ret_bid(主) | ret_steam | 对照 | 指数1(换手) | 超额vs对照 | Δratio | thin |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            L.append(f"| {r['cohort']} | {pct(r['ret_ask'])} | {pct(r['ret_bid'])} | {pct(r['ret_steam'])} | "
                     f"{pct(r['control_bid'])} | {pct(r['index1_tw'])} | {pct(r['excess_ctrl'])} | "
                     f"{'—' if r['delta_ratio'] is None else format(r['delta_ratio'], '+.4f')} | {r['thin_top']} |")
        if ex:
            ts = tstat(ex)
            L += ["", f"- 期数 {len(ex)}，命中率 {sum(1 for x in ex if x > 0)}/{len(ex)}",
                  f"- 超额均值 {statistics.mean(ex)*100:.2f}%，中位 {statistics.median(ex)*100:.2f}%，"
                  f"最差 {min(ex)*100:.2f}%",
                  f"- 超额 t 值 {ts:.2f}" if ts is not None else "- t 值：样本不足",
                  f"- Δratio 与超额相关 {c:.3f}" if c is not None else "- Δratio 相关：样本不足",
                  "  - 正相关 → 收敛机制确认；≈0 → 超额来自 beta，不是价差", ""]
        else:
            L += ["", "- 无可计算超额（缺退出数据）", ""]
    L += ["## 1 号指数（全品种基准）", "", "| cohort | 换手加权 | 等权 |", "|---|---|---|"]
    for t in sorted(i1):
        tw, eq = i1[t]
        if tw is not None:
            L.append(f"| {t} | {pct(tw)} | {pct(eq)} |")
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
