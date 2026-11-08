#!/usr/bin/env python3
"""每日快照采集。stdlib only，无第三方依赖。

用法:
  python snapshot.py --source manual --file today.csv [--db skin.db] [--ts "2026-10-22T21:00:00"]
  python snapshot.py --source buff  --config config.json [--max-pages 30]
  python snapshot.py --source cs2sh --config config.json
  python snapshot.py --source steam_search --pages 20 --out universe.txt   # 生成品种清单

manual CSV 列: item,platform,ask,ask_vol,bid,bid_vol,vol24
"""
import argparse, csv, json, os, sqlite3, sys, time, urllib.request, urllib.parse, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "Mozilla/5.0 (skin-index snapshotter)"}


def db(path):
    con = sqlite3.connect(path)
    with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
        con.executescript(f.read())
    return con


def insert(con, ts, source, item, platform, ask, ask_vol, bid, bid_vol, vol24, raw=None):
    con.execute(
        "insert into snapshots(ts,source,item,platform,ask,ask_vol,bid,bid_vol,vol24,last_price,raw)"
        " values(?,?,?,?,?,?,?,?,?,?,?)",
        (ts, source, item, platform, ask, ask_vol, bid, bid_vol, vol24, None,
         json.dumps(raw, ensure_ascii=False) if raw is not None else None))


def http_json(url, headers=None, data=None, retries=3, timeout=30):
    hdr = dict(UA)
    if headers:
        hdr.update(headers)
    if data is not None:
        hdr["Content-Type"] = "application/json"
    body = json.dumps(data).encode() if data is not None else None
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa
            last = e
            time.sleep(2 ** i * 3)
    raise RuntimeError(f"GET {url} failed: {last}")


def source_manual(args, con, ts):
    n = 0
    with open(args.file, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            def num(k):
                v = (row.get(k) or "").strip()
                return float(v) if v else None
            insert(con, ts, "manual", row["item"].strip(), row["platform"].strip(),
                   num("ask"), num("ask_vol"), num("bid"), num("bid_vol"), num("vol24"))
            n += 1
    con.commit()
    print(f"[manual] inserted {n} rows")


def source_buff(args, cfg, con, ts):
    b = cfg.get("buff", {})
    if not b.get("session_cookie"):
        sys.exit("config.json 缺少 buff.session_cookie（登录 BUFF 后从浏览器复制）")
    hdr = {"Cookie": f"Session={b['session_cookie']}",
           "X-Requested-With": "XMLHttpRequest",
           "Referer": "https://buff.163.com/market/csgo"}
    game = b.get("game", "csgo")
    n, first_page_saved = 0, False
    for page in range(1, args.max_pages + 1):
        url = ("https://buff.163.com/api/market/goods?" + urllib.parse.urlencode(
            {"game": game, "page_num": page, "page_size": 80,
             "min_price": b.get("min_price", 10), "max_price": b.get("max_price", 500)}))
        try:
            data = http_json(url, hdr)
        except RuntimeError as e:
            print(f"[buff] page {page} failed: {e}")
            break
        if not first_page_saved:
            with open(os.path.join(HERE, "debug_buff_page1.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            first_page_saved = True
            print("[buff] 第一页原始 JSON 已存到 debug_buff_page1.json —— 请人工核对字段名")
        items = (data.get("data") or {}).get("items") or []
        if not items:
            break
        for it in items:
            name = it.get("market_hash_name") or it.get("name")
            ask = it.get("sell_min_price")
            bid = it.get("steam_buy_max_price")   # 字段可能变动，见 debug 文件核对
            insert(con, ts, "buff", name, "buff",
                   _f(ask), _f(it.get("sell_num")), _f(bid), None, None, raw=it)
            n += 1
        print(f"[buff] page {page}: +{len(items)}")
        time.sleep(b.get("sleep", 3))
    con.commit()
    print(f"[buff] inserted {n} rows")


def source_cs2sh(args, cfg, con, ts):
    c = cfg.get("cs2sh", {})
    if not c.get("api_key"):
        sys.exit("config.json 缺少 cs2sh.api_key")
    universe = load_universe(args.universe or os.path.join(HERE, "universe.txt"))
    if not universe:
        sys.exit("universe.txt 为空（先 --source steam_search 生成，或手工填写）")
    hdr = {"Authorization": f"Bearer {c['api_key']}"}
    n = 0
    for i in range(0, len(universe), 100):
        chunk = universe[i:i + 100]
        try:
            data = http_json(f"{c.get('base','https://api.cs2.sh')}/v1/prices/latest",
                             hdr, data={"items": chunk})
        except RuntimeError as e:
            print(f"[cs2sh] chunk {i} failed: {e}")
            break
        for item, obj in (data.get("items") or data).items() if isinstance(data, dict) else []:
            if not isinstance(obj, dict):
                continue
            for plat, key in (("buff", "buff"), ("steam", "steam")):
                s = obj.get(key)
                if isinstance(s, dict):
                    insert(con, ts, "cs2sh", item, plat,
                           _f(s.get("ask")), _f(s.get("ask_volume")),
                           _f(s.get("bid")), _f(s.get("bid_volume")), None, raw=s)
                    n += 1
        print(f"[cs2sh] chunk {i//100 + 1}: total {n}")
        time.sleep(c.get("sleep", 2))
    con.commit()
    print(f"[cs2sh] inserted {n} rows")


def source_steam_search(args, cfg, con, ts):
    s = cfg.get("steam", {}) if cfg else {}
    url = "https://steamcommunity.com/market/search/render/?"
    out, start = [], 0
    for _ in range(args.pages):
        q = urllib.parse.urlencode({"appid": 730, "norender": 1, "count": 100, "start": start})
        try:
            data = http_json(url + q)
        except RuntimeError as e:
            print(f"[steam_search] start={start} failed: {e}")
            break
        res = data.get("results") or []
        if not res:
            break
        for it in res:
            if it.get("hash_name"):
                out.append(it["hash_name"])
        start += 100
        time.sleep(3)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"[steam_search] {len(out)} names -> {args.out}")


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def load_universe(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [x.strip() for x in f if x.strip() and not x.startswith("#")]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True,
                   choices=["manual", "buff", "cs2sh", "steam_search"])
    p.add_argument("--config", default=os.path.join(HERE, "config.json"))
    p.add_argument("--db", default=os.path.join(HERE, "skin.db"))
    p.add_argument("--file")
    p.add_argument("--ts", default=datetime.datetime.utcnow().isoformat(timespec="seconds"))
    p.add_argument("--max-pages", type=int, default=30)
    p.add_argument("--pages", type=int, default=20)
    p.add_argument("--universe")
    p.add_argument("--out", default=os.path.join(HERE, "universe.txt"))
    args = p.parse_args()

    cfg = {}
    if os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
    con = db(args.db)
    if args.source == "manual":
        if not args.file:
            sys.exit("--source manual 需要 --file")
        source_manual(args, con, args.ts)
    elif args.source == "buff":
        source_buff(args, cfg, con, args.ts)
    elif args.source == "cs2sh":
        source_cs2sh(args, cfg, con, args.ts)
    elif args.source == "steam_search":
        source_steam_search(args, cfg, con, args.ts)
    con.close()


if __name__ == "__main__":
    main()
