#!/usr/bin/env python3
"""合成数据生成器：在接真数据之前验证回测引擎。

--alpha 控制注入的"价差收敛速度"：
  alpha=0.4  价差快速收敛 → top-N 应产生明显正超额（引擎应能检测到）
  alpha=0.0  价差不收敛   → top-N 超额应 ≈ 0（零假设）
两个都跑、对比结果，引擎才算验证通过。
"""
import argparse, math, os, random, sqlite3
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
N_ITEMS, N_DAYS, K_EVENT = 300, 70, 35


def start_monday():
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "test.db"))
    ap.add_argument("--alpha", type=float, default=0.4)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--items", type=int, default=N_ITEMS)
    ap.add_argument("--days", type=int, default=N_DAYS)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    if os.path.exists(args.db):
        os.remove(args.db)
    con = sqlite3.connect(args.db)
    with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
        con.executescript(f.read())

    start = start_monday()
    names = [f"Fake Item {i:03d} (Field-Tested)" for i in range(args.items)]
    clearing = {}
    for nm in names:
        p = math.exp(rng.gauss(3.2, 1.1))
        while not (10 <= p <= 800):
            p = math.exp(rng.gauss(3.2, 1.1))
        clearing[nm] = p
    vol24 = {nm: max(10.0, math.exp(rng.gauss(3.3, 0.6))) for nm in names}
    f_state = {nm: None for nm in names}

    rows = []
    for k in range(args.days):
        day = (start + timedelta(days=k)).isoformat()
        ts = f"{day}T21:00:00"
        for nm in names:
            clearing[nm] *= math.exp(rng.gauss(0.0005, 0.02))
            c = clearing[nm]
            f = f_state[nm]
            if f is None:
                if rng.random() < K_EVENT / args.items:
                    f = 0.87
                    f_state[nm] = f
            else:
                f = f + args.alpha * (1 - f) + rng.gauss(0, 0.01)
                if f >= 0.995:
                    f_state[nm] = None
                    f = 1 + rng.gauss(0, 0.015)
                else:
                    f_state[nm] = f
            ask = c * (f if f is not None else 1 + rng.gauss(0, 0.015))
            bid_plat = ask * (1 - rng.uniform(0.01, 0.03))
            bid = c * 1.35 * (1 + rng.gauss(0, 0.02))
            rows.append((ts, "fake", nm, "buff", ask, rng.uniform(1, 50), bid_plat, rng.uniform(1, 30), vol24[nm], None, None))
            rows.append((ts, "fake", nm, "steam", c * 1.5, rng.uniform(1, 50), bid, rng.uniform(1, 2000), vol24[nm], None, None))
    con.executemany("insert into snapshots(ts,source,item,platform,ask,ask_vol,bid,bid_vol,vol24,last_price,raw)"
                    " values(?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    print(f"alpha={args.alpha}: 写入 {len(rows)} 行（{args.items} 品种 × {args.days} 天 × 2 平台） -> {args.db}")


if __name__ == "__main__":
    main()
