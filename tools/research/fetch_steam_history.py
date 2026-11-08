#!/usr/bin/env python3
"""Steam 官方历史成交回填（1 号指数与结果侧验证）。

前提：config.json 里 steam.steam_login_secure = 登录后的 cookie 值。
注意：返回价格按账号币种；确保账号显示为 CNY，或在 config 里给 usd_cny 手动换算。

用法:
  python fetch_steam_history.py --config config.json --db skin.db
"""
import argparse, json, os, sqlite3, sys, time, urllib.request, urllib.parse, datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def http_json(url, headers, retries=3, timeout=30):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa
            last = e
            time.sleep(2 ** i * 5)
    raise RuntimeError(f"{url}: {last}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=os.path.join(HERE, "config.json"))
    p.add_argument("--db", default=os.path.join(HERE, "skin.db"))
    p.add_argument("--universe", default=os.path.join(HERE, "universe.txt"))
    p.add_argument("--sleep", type=float, default=6.0)
    args = p.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    s = cfg.get("steam", {})
    if not s.get("steam_login_secure"):
        sys.exit("config.json 缺少 steam.steam_login_secure")
    headers = {"Cookie": f"steamLoginSecure={s['steam_login_secure']}",
               "User-Agent": "Mozilla/5.0 (skin-index)"}

    with open(args.universe, encoding="utf-8") as f:
        names = [x.strip() for x in f if x.strip() and not x.startswith("#")]
    con = sqlite3.connect(args.db)
    with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
        con.executescript(f.read())

    done = {r[0] for r in con.execute("select distinct item from steam_sales")}
    fx = s.get("usd_cny")  # 若账号是 USD，手动给汇率；不给则原样入库并标注
    todo = [n for n in names if n not in done]
    print(f"universe {len(names)}, 待拉取 {len(todo)}")

    for i, name in enumerate(todo):
        url = ("https://steamcommunity.com/market/pricehistory/?"
               + urllib.parse.urlencode({"appid": 730, "market_hash_name": name}))
        try:
            data = http_json(url, headers)
        except RuntimeError as e:
            print(f"[{i+1}/{len(todo)}] {name}: {e}")
            continue
        rows = data.get("prices") or []
        for date_s, price, qty in rows:
            d = datetime.datetime.strptime(date_s, "%b %d %Y %H: %M: %S")
            con.execute("insert or replace into steam_sales(item,date,price,qty) values(?,?,?,?)",
                        (name, d.strftime("%Y-%m-%d"), float(price) * (fx or 1.0), float(qty)))
        con.commit()
        print(f"[{i+1}/{len(todo)}] {name}: {len(rows)} 天")
        time.sleep(args.sleep)
    con.close()


if __name__ == "__main__":
    main()
