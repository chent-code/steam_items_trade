#!/usr/bin/env python3
"""CSQAQ 数据回填与每日快照。

子命令:
  universe  拉挂刀榜(按折扣深度排序) → universe.json (含 good_id 与当前全平台价格)
  charts    逐品种拉多平台历史 ask/bid → chart_history 表   (1 req/s, 慢)
  convert   chart_history → snapshots 表(日频, 回测引擎直接可用)
  now       拉当前挂刀榜 → 写今日快照(每日定时任务用, 含 vol24)

口径: 挂刀比 = 平台在售价 / (Steam求购价 × 0.8696); csqaq 的 max_price = 其倒数。
"""
import argparse, json, os, sqlite3, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.csqaq.com/api/v1"
TZ = timezone(timedelta(hours=8))
PLATFORMS = {1: "buff", 2: "uuyp", 3: "steam"}


def post(path, token, payload, retries=4):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(API + path, data=body, headers={
        "ApiToken": token, "Content-Type": "application/json"})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** i + 1)
                last = e
                continue
            raise
        except Exception as e:  # noqa
            last = e
            time.sleep(2 ** i + 1)
    raise RuntimeError(f"{path} failed: {last}")


def bind(token):
    try:
        r = post("/sys/bind_local_ip", token, {})
        print("[bind]", r.get("data") or r.get("msg"))
    except Exception as e:  # noqa
        print("[bind] skip:", e)
    time.sleep(2)


def ensure_db(path):
    con = sqlite3.connect(path)
    with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
        con.executescript(f.read())
    con.execute("""CREATE TABLE IF NOT EXISTS chart_history(
        good_id TEXT, platform TEXT, key TEXT, ts_ms INTEGER, value REAL,
        PRIMARY KEY(good_id, platform, key, ts_ms))""")
    return con


def cmd_universe(args, con):
    bind(args.token)
    items, seen = [], set()
    for p in range(1, args.pages + 1):
        r = post("/info/exchange_detail", args.token, {
            "page_index": p, "res": 0, "platforms": args.platforms, "sort_by": 1,
            "min_price": args.min_price, "max_price": args.max_price, "turnover": args.turnover})
        if r.get("code") != 200:
            print("[universe] stop:", r.get("msg"))
            break
        rows = r.get("data") or []
        if not rows:
            break
        new = 0
        for it in rows:
            gid = str(it.get("id"))
            if gid in seen:
                continue
            seen.add(gid)
            items.append(it)
            new += 1
        print(f"[universe] page {p}: +{new} (累计 {len(items)}), 本页最深 max_price={rows[-1].get('max_price')}")
        if len(items) >= args.max_items:
            break
        time.sleep(1.2)
    items = items[:args.max_items]
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    print(f"[universe] {len(items)} 品种 -> {args.out}")


def cmd_charts(args, con):
    bind(args.token)
    with open(args.universe, encoding="utf-8") as f:
        items = json.load(f)
    jobs = []
    for it in items:
        gid = str(it["id"])
        for plat_num, plat in PLATFORMS.items():
            for key in ("sell_price", "buy_price"):
                jobs.append((gid, plat, plat_num, key, it.get("market_hash_name")))
    if args.limit:
        jobs = jobs[:args.limit * len(PLATFORMS) * 2]
    print(f"[charts] 共 {len(jobs)} 次请求, 约 {len(jobs)} 秒")
    done = skip = 0
    for i, (gid, plat, plat_num, key, name) in enumerate(jobs):
        have = con.execute("select 1 from chart_history where good_id=? and platform=? and key=? limit 1",
                           (gid, plat, key)).fetchone()
        if have and not args.refresh:
            skip += 1
            continue
        try:
            r = post("/info/chart", args.token, {"good_id": gid, "key": key,
                                                 "platform": plat_num,
                                                 "period": str(args.period), "style": "all_style"})
        except RuntimeError as e:
            print(f"[charts] {i+1}/{len(jobs)} {name} {plat}/{key}: {e}")
            continue
        if r.get("code") != 200:
            print(f"[charts] {i+1}/{len(jobs)} {name} {plat}/{key}: {r.get('msg')}")
            continue
        dd = r.get("data") or {}
        rows = [(gid, plat, key, int(t), float(v)) for t, v in
                zip(dd.get("timestamp") or [], dd.get("main_data") or []) if v is not None]
        con.executemany("insert or replace into chart_history values(?,?,?,?,?)", rows)
        con.commit()
        done += 1
        if (i + 1) % 20 == 0:
            print(f"[charts] {i+1}/{len(jobs)} 完成 ({name})")
        time.sleep(1.1)
    con.commit()
    print(f"[charts] 新拉 {done}, 跳过已有 {skip}")


def cmd_convert(args, con):
    # 日频化: 每日取该日最后一个点(约等于固定时点快照)
    rows = con.execute("select good_id, platform, key, ts_ms, value from chart_history order by ts_ms").fetchall()
    series = {}
    for gid, plat, key, ts_ms, v in rows:
        day = datetime.fromtimestamp(ts_ms / 1000, TZ).strftime("%Y-%m-%d")
        series.setdefault((gid, plat), {}).setdefault(day, {})[key] = v
    names = {str(it["id"]): it for it in json.load(open(args.universe, encoding="utf-8"))}
    snaps = []
    for (gid, plat), days in series.items():
        info = names.get(gid)
        if not info:
            continue
        item = info.get("market_hash_name") or gid
        for day, kv in sorted(days.items()):
            ask, bid = kv.get("sell_price"), kv.get("buy_price")
            if ask is None and bid is None:
                continue
            snaps.append((f"{day}T21:00:00", "csqaq", item, plat, ask, None, bid, None, None, None, None))
    with open(args.universe, encoding="utf-8") as f:
        pass
    con.executemany("insert or replace into snapshots(ts,source,item,platform,ask,ask_vol,bid,bid_vol,vol24,last_price,raw)"
                    " values(?,?,?,?,?,?,?,?,?,?,?)", snaps)
    con.commit()
    print(f"[convert] 写入 {len(snaps)} 条日频快照")


def cmd_now(args, con):
    bind(args.token)
    ts = datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S")
    n = 0
    for p in range(1, args.pages + 1):
        r = post("/info/exchange_detail", args.token, {
            "page_index": p, "res": 0, "platforms": args.platforms, "sort_by": 1,
            "min_price": args.min_price, "max_price": args.max_price, "turnover": args.turnover})
        if r.get("code") != 200:
            print("[now] stop:", r.get("msg"))
            break
        rows = r.get("data") or []
        if not rows:
            break
        vals = []
        for it in rows:
            item = it.get("market_hash_name")
            vol = it.get("turnover_number")
            common = (ts, "csqaq", item)
            if it.get("buff_sell_price"):
                vals.append(common + ("buff", it["buff_sell_price"], it.get("buff_sell_num"),
                                      it.get("buff_buy_price"), it.get("buff_buy_num"), None, None, None))
            if it.get("yyyp_sell_price"):
                vals.append(common + ("uuyp", it["yyyp_sell_price"], it.get("yyyp_sell_num"),
                                      it.get("yyyp_buy_price"), it.get("yyyp_buy_num"), None, None, None))
            if it.get("steam_buy_price"):
                vals.append(common + ("steam", it.get("steam_sell_price"), it.get("steam_sell_num"),
                                      it["steam_buy_price"], it.get("steam_buy_num"), vol, None, None))
        con.executemany("insert or replace into snapshots(ts,source,item,platform,ask,ask_vol,bid,bid_vol,vol24,last_price,raw)"
                        " values(?,?,?,?,?,?,?,?,?,?,?)", vals)
        con.commit()
        n += len(vals)
        if len(rows) < 50:
            break
        time.sleep(1.2)
    print(f"[now] 写入 {n} 条今日快照")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["universe", "charts", "convert", "now"])
    ap.add_argument("--token", required=True)
    ap.add_argument("--db", default=os.path.join(here, "skin.db"))
    ap.add_argument("--universe", default=os.path.join(here, "universe_csqaq.json"))
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--max-items", type=int, default=250)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--period", type=int, default=1095)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--platforms", default="BUFF-YYYP")
    ap.add_argument("--min-price", type=float, default=10)
    ap.add_argument("--max-price", type=float, default=5000)
    ap.add_argument("--turnover", type=int, default=10)
    ap.add_argument("--out", default=os.path.join(here, "universe_csqaq.json"))
    args = ap.parse_args()
    con = ensure_db(args.db)
    {"universe": cmd_universe, "charts": cmd_charts,
     "convert": cmd_convert, "now": cmd_now}[args.cmd](args, con)
    con.close()


if __name__ == "__main__":
    main()
