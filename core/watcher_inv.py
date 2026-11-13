#!/usr/bin/env python3
"""L0 · UUYP 库存倒余额看板 v2

决策口径:与成本无关,只比较当前两端报价。
  每件库存:Steam退出到手(S×0.8696) vs UUYP净售(P×0.99) → 优势大者优先。
用法:
  python watcher_inv.py login              # 短信/上行短信登录,token 落本机配置
  python watcher_inv.py run --dry-run      # 本地打印,不推送
  python watcher_inv.py run                # 推送到微信(需配置 wxpusher)
  python watcher_inv.py run --mock         # 合成数据自检
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta

import requests

from uu_auth import UUClient, sms_login
from paths import DATA_DIR, REPORT_DIR, LOG_DIR, CONFIG_FILE

HERE = DATA_DIR  # 运行时数据基准:repo/data/(cookie/state 等沿用 HERE 拼接)
CFG = CONFIG_FILE
CSQAQ = "https://api.csqaq.com/api/v1"


def load_cfg():
    if os.path.exists(CFG):
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cfg(cfg):
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"[cfg] 已写入 {CFG}")


def cmd_login(cfg):
    print("== UUYP 短信登录(验证码发到你手机,token 只落在本机配置) ==")
    phone = input("输入手机号: ").strip()
    token, device_token = sms_login(phone)
    cfg["uu_token"] = token
    cfg["uu_device_token"] = device_token
    save_cfg(cfg)
    print("== 完成。后续 run 不再需要登录,直到 token 失效(重新 login 即可) ==")


def http_json(method, url, token, payload=None, retries=3):
    hdr = {"ApiToken": token, "Content-Type": "application/json"}
    last = None
    for i in range(retries):
        try:
            if method == "POST":
                r = requests.post(url, json=payload, headers=hdr, timeout=30)
            else:
                r = requests.get(url, params=payload, headers=hdr, timeout=30)
            return r.json()
        except Exception as e:  # noqa
            last = e
            time.sleep(2 ** i + 1)
    raise RuntimeError(f"{url}: {last}")


def uu_inventory(cfg):
    uu = UUClient(cfg["uu_token"], cfg.get("uu_device_token", ""))
    print(f"[uu] 账号: {uu.nickname}")
    return uu.inventory()


def extract(inv_items):
    """从 ItemsInfos 提取:MarketHashName(csqaq匹配)/ShotName/Tradable/资产ID/templateId"""
    out = []
    for it in inv_items:
        name = (it.get("MarketHashName") or it.get("commodityName")
                or it.get("ShotName") or it.get("name") or "")
        if not name:
            continue
        out.append({"name": name, "shot": it.get("ShotName", ""),
                    "asset_id": it.get("SteamAssetId", ""),
                    "tradable": it.get("Tradable"),
                    "template_id": (it.get("TemplateInfo") or {}).get("Id"),
                    "add_time": it.get("AssetAddTime", "")})
    return out


def cost_basis(uu, max_pages=6):
    print("[cost] 拉取已完成购入订单…")
    return uu.buy_costs(max_pages=max_pages)


_BIND_TS = {"t": 0.0}  # 进程内绑定节流(接口限 30s/次,同轮重复调用直接跳过)


_STEAM_PO = {}  # market_hash_name → {"ts": 抓取时间, "lowest": Steam实时卖一|None}


def _steam_spike(steam_v, deal):
    """Steam卖一相对成交价7的溢价 → 尖峰标记(≥10% 视为尖峰回落风险)"""
    try:
        p = (float(steam_v) - float(deal)) / float(deal) * 100
        return f"(尖峰+{p:.0f}%)" if p >= 10 else ""
    except (TypeError, ZeroDivisionError, ValueError):
        return ""


def steam_real_ask(market_hash_name, ttl=1800):
    """Steam 官方 priceoverview 实时卖一 → float | None
    csqaq 的 steamSellPrice 是滞后快照(实测 37.86 vs 真实 35.45,差 6.8%),
    灰色带/下架判定必须用真实值。TTL 缓存 30 分钟,失败态短缓存 5 分钟,429 退避一次"""
    now = time.time()
    c = _STEAM_PO.get(market_hash_name)
    if c and now - c["ts"] < (ttl if c["lowest"] else 300):
        return c["lowest"]
    try:
        for attempt in (1, 2):
            r = requests.get("https://steamcommunity.com/market/priceoverview/",
                             params={"appid": 730, "market_hash_name": market_hash_name},
                             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                             timeout=12)
            if r.status_code == 429 and attempt == 1:
                time.sleep(15)
                continue
            break
        if r.status_code != 200:
            _STEAM_PO[market_hash_name] = {"ts": now, "lowest": None}
            return None
        d = r.json()
        if not d.get("success"):
            _STEAM_PO[market_hash_name] = {"ts": now, "lowest": None}
            return None
        lp = d.get("lowest_price") or ""
        m = re.search(r"(\d+(?:\.\d+)?)", lp.replace(",", ""))
        lowest = round(float(m.group(1)), 2) if m else None
        _STEAM_PO[market_hash_name] = {"ts": now, "lowest": lowest}
        return lowest
    except Exception:  # noqa
        return None


def csqaq_bind(cfg, force=False):
    """把当前出口 IP 绑定到 csqaq token(失败不致命);30s 内已绑过则跳过"""
    if not force and time.time() - _BIND_TS["t"] < 30:
        return
    try:
        r = requests.post(f"{CSQAQ}/sys/bind_local_ip", json={},
                          headers={"ApiToken": cfg["csqaq_token"]}, timeout=20).json()
        print(f"[csqaq] IP绑定: {r.get('data') or r.get('msg')}")
        _BIND_TS["t"] = time.time()
    except Exception as e:  # noqa
        print(f"[csqaq] IP绑定跳过: {e}")
    time.sleep(2)


def csqaq_prices(names, cfg):
    csqaq_bind(cfg)
    out, errs = {}, []
    lst = sorted(set(names))
    for i in range(0, len(lst), 50):
        chunk = lst[i:i + 50]
        r = http_json("POST", f"{CSQAQ}/goods/getPriceByMarketHashName",
                      cfg["csqaq_token"], {"marketHashNameList": chunk})
        if r.get("code") != 200:
            print(f"[csqaq] chunk {i//50}: {r.get('msg')}")
            continue
        d = r.get("data") or {}
        for name, obj in (d.get("success") or {}).items():
            out[name] = obj
        errs.extend(d.get("error") or [])
        time.sleep(1.2)
    print(f"[csqaq] 定价 {len(out)}/{len(lst)},未匹配 {len(errs)}")
    return out, errs


def yyyp_bid(cfg, good_id):
    """UUYP 买一(最高求购价):chart platform=2 buy_price 最新点;失败返回 None"""
    if not good_id:
        return None
    try:
        r = http_json("POST", f"{CSQAQ}/info/chart", cfg["csqaq_token"],
                      {"good_id": good_id, "key": "buy_price", "platform": 2,
                       "period": "7", "style": "all_style"})
        d = r.get("data") or {}
        md = d.get("main_data") or []
        return float(md[-1]) if md else None
    except Exception:  # noqa
        return None


def build_rows(inv, prices, costs, cfg):
    costs_name, costs_asset = costs
    net_m = 1 - cfg.get("sell_fee", 0.01)
    bal_m = cfg.get("net_multiplier", 0.8696)
    sat = cfg.get("satisfy_ratio", 0.70)
    acc = cfg.get("accept_ratio", 0.80)
    rows = []
    for it in inv:
        name = it["name"]
        p = prices.get(name)
        tradable = it.get("tradable")
        if not p:
            rows.append({"name": name, "asset_id": it.get("asset_id", ""),
                         "tradable": tradable, "yyyp": "", "steam": "",
                         "ratio": "", "steam_bal": "", "uuyp_net": "", "steam_adv": "",
                         "rec": "无报价"})
            continue
        yyyp, steam = p.get("yyypSellPrice"), p.get("steamSellPrice")
        if not yyyp or not steam:
            rows.append({"name": name, "asset_id": it.get("asset_id", ""),
                         "tradable": tradable, "yyyp": yyyp or "",
                         "steam": steam or "", "ratio": "", "steam_bal": "",
                         "uuyp_net": "", "steam_adv": "", "rec": "报价缺失"})
            continue
        yyyp, steam = round(float(yyyp), 2), round(float(steam), 2)
        steam_bal = round(steam * bal_m, 2)
        uuyp_net = round(yyyp * net_m, 2)
        ratio = round(uuyp_net / steam_bal, 3)
        adv = round(steam_bal - uuyp_net, 2)
        if ratio <= sat:
            rec = "Steam(优)"
        elif ratio <= acc:
            rec = "Steam(可)"
        elif ratio < 1:
            rec = "Steam(薄)"
        else:
            rec = "UUYP现金"
        cost = costs_asset.get(str(it.get("asset_id", ""))) or costs_name.get(it.get("shot", "")) or costs_name.get(name)
        rows.append({"name": name, "asset_id": it.get("asset_id", ""),
                     "tradable": tradable, "shot": it.get("shot", ""),
                     "yyyp": yyyp, "steam": steam, "ratio": ratio,
                     "good_id": p.get("goodId"),
                     "template_id": it.get("template_id"),
                     "cn": p.get("name") or name,
                     "steam_bal": steam_bal, "uuyp_net": uuyp_net,
                     "steam_adv": adv, "cost": round(cost, 2) if cost else None,
                     "rec": rec})
    rows.sort(key=lambda x: (float(x["ratio"]) if x["ratio"] != "" else 999))
    return rows


def dwidth(s):
    """显示宽度:东亚字符按 2 计"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def trunc(s, w):
    if dwidth(s) <= w:
        return s
    out = ""
    for c in s:
        if dwidth(out) + dwidth(c) > w - 2:
            return out + "…"
        out += c
    return out


def fmt_table(headers, rows, aligns):
    """等宽对齐的纯文本表格。aligns: 'l'左 / 'r'右"""
    widths = [dwidth(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], dwidth(str(c)))
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(cells):
        cells = [str(c) for c in cells]
        out = "|"
        for i, c in enumerate(cells):
            pad = widths[i] - dwidth(c)
            if aligns[i] == "r":
                out += " " * (pad + 1) + c + " |"
            else:
                out += " " + c + " " * pad + " |"
        return out

    lines = [sep, line(headers), sep]
    for r in rows:
        lines.append(line(r))
    lines.append(sep)
    return lines


def render(rows, cfg):
    sat = cfg.get("satisfy_ratio", 0.70)
    acc = cfg.get("accept_ratio", 0.80)
    band = cfg.get("band", [0.65, 0.70])
    in_band = [r for r in rows if isinstance(r["ratio"], float)
               and band[0] <= r["ratio"] < band[1]]

    headers = ["优势", "建议", "品种", "比值", "UUYP现价", "UUYP净售", "Steam现价", "Steam到手"]
    aligns = ["r", "l", "l", "r", "r", "r", "r", "r"]

    def rowcells(r):
        return [str(r["steam_adv"]), r["rec"], trunc(r["name"], 44), str(r["ratio"]),
                str(r["yyyp"]), str(r["uuyp_net"]), str(r["steam"]), str(r["steam_bal"])]

    def table(sub, title):
        cells = [rowcells(r) for r in sub]
        lines = [f"◆ {title}({len(sub)} 件)"]
        lines += fmt_table(headers, cells, aligns)
        if len(sub) > cfg.get("top_n", 40):
            lines.append(f"(其余 {len(sub) - cfg.get('top_n', 40)} 件见 CSV)")
        return lines

    sellable = [r for r in rows if r["tradable"] is True]
    locked = [r for r in rows if r["tradable"] is not True]
    tot = {k: sum(float(r[k]) for r in sellable if r[k] != "")
           for k in ("steam_bal", "uuyp_net", "steam_adv")}
    lines = [f"📊 库存倒余额看板 {datetime.now().strftime('%m-%d %H:%M')}",
             f"共 {len(rows)} 件 = 可售 {len(sellable)} + 保护期锁定 {len(locked)} | "
             f"目标带 {band[0]:.2f}-{band[1]:.2f}: {len(in_band)} 件",
             f"【可售部分】Steam退出余额合计 ¥{tot['steam_bal']:,.0f} | "
             f"UUYP净售合计 ¥{tot['uuyp_net']:,.0f} | "
             f"Steam退出总优势 ¥{tot['steam_adv']:,.0f}",
             ""]
    lines += table(sellable, "可立即出售(已过保护期)")
    lines += ["", "◆ 保护期锁定(未到 7 天,暂不可卖)"]
    lines += table(locked, "锁定中")
    lines += ["", f"口径:比值=UUYP净售÷Steam到手 | ≤{sat} 优(Steam到手×0.8 仍>UUYP净售) | "
              f"{sat}-{acc} 可 | ≥{acc} 走 UUYP 现金 | 优势=Steam到手−UUYP净售(参考)"]
    return "\n".join(lines)


def notify(cfg, title, content):
    wx = cfg.get("wxpusher") or {}
    if not wx.get("app_token") or not wx.get("uid"):
        print("[notify] 未配置 wxpusher,仅本地输出")
        return False
    r = requests.post("https://wxpusher.zjiecode.com/api/send/message", json={
        "appToken": wx["app_token"], "content": content[:3900],
        "summary": title, "contentType": 1, "uids": [wx["uid"]]}, timeout=20).json()
    ok = r.get("code") == 1000
    print(f"[notify] {'已推送' if ok else '失败: ' + str(r)}")
    return ok


def prepare(cfg, mock):
    if mock:
        print("== MOCK 自检模式 ==")
        names = [f"Mock Item {i:02d} (Field-Tested)" for i in range(12)]
        inv = extract([{"commodityName": n, "Tradable": (i % 3 != 0)}
                       for i, n in enumerate(names)])
        prices = {n: {"yyypSellPrice": round(10 + i * 3.1, 2),
                      "steamSellPrice": round(14 + i * 4.2, 2)}
                  for i, n in enumerate(names)}
        uu, costs_name, costs_asset = None, {}, {}
        return uu, inv, prices, costs_name, costs_asset
    uu = UUClient(cfg["uu_token"], cfg.get("uu_device_token", ""))
    print(f"[uu] 账号: {uu.nickname}")
    inv_items = uu.inventory()
    inv = extract(inv_items)
    print(f"[inv] {len(inv)} 件")
    costs_name, costs_asset = cost_basis(uu)
    prices, errs = csqaq_prices({r["name"] for r in inv}, cfg)
    for n in errs:
        print(f"  未匹配: {n}")
    return uu, inv, prices, costs_name, costs_asset


def cmd_run(cfg, dry, mock):
    uu, inv, prices, costs_name, costs_asset = prepare(cfg, mock)
    rows = build_rows(inv, prices, (costs_name, costs_asset), cfg)
    if not rows:
        sys.exit("看板为空:库存名称提取或报价匹配失败,把 inv_raw/last_inventory.json 发我校准")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = os.path.join(REPORT_DIR, f"inventory_{stamp}.csv")
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    content = render(rows, cfg)
    print("\n" + content)
    print(f"\n[csv] {csv_path}")
    if not dry:
        notify(cfg, "库存倒余额看板", content)




def cmd_sell(cfg, uu, inv, prices, costs_name, costs_asset, args):
    sat = cfg.get("satisfy_ratio", 0.70)
    acc = cfg.get("accept_ratio", 0.80)
    min_profit = args.min_profit / 100.0
    plan = []
    for it in inv:
        if it["tradable"] is not True:
            continue
        if args.asset and it["asset_id"] not in args.asset:
            continue
        p = prices.get(it["name"])
        if not p or not p.get("yyypSellPrice"):
            print(f"  跳过(无UUYP报价): {it['name'][:40]}")
            continue
        ask = float(p["yyypSellPrice"])
        cost = costs_asset.get(str(it["asset_id"])) or costs_name.get(it["shot"])
        if cost:
            floor = round(cost * (1 + min_profit), 2)
            floor_note = f"成本¥{cost:.2f}+{args.min_profit}%"
        else:
            floor = round(float(p.get("steamSellPrice") or 0) * cfg.get("net_multiplier", 0.8696) * 0.70, 2)
            floor_note = "无成本记录,按Steam到手×0.7"
        price = round(max(floor, ask - 0.01), 2)
        plan.append({"asset_id": it["asset_id"], "name": it["name"], "cost": cost,
                     "ask": ask, "floor": floor, "floor_note": floor_note,
                     "price": price, "ratio": round(yyyp_ratio(it, p, cfg), 3)})
    if args.asset:
        pass
    else:
        plan = [r for r in plan if float(r["ratio"]) < 1.0]  # 比值≥1(平台比Steam贵)不挂
    if not plan:
        print("没有符合条件可上架的物品")
        return
    plan.sort(key=lambda x: x["price"], reverse=True)
    print(f"\n== 上架预览(共 {len(plan)} 件) ==")
    print(f"{'挂单价':>9} {'底价':>9} {'UUYP最低在售':>10} {'成本':>8}  品种")
    for r in plan:
        print(f"{r['price']:>9.2f} {r['floor']:>9.2f} {r['ask']:>10.2f} "
              f"{(f'{r[chr(99)+chr(111)+chr(115)+chr(116)]:.2f}' if r['cost'] else '~'):>8}  {r['name'][:36]}")
    print(f"\n合计挂单金额: ¥{sum(r['price'] for r in plan):,.2f}")
    print("定价规则: max(UUYP最低在售-0.01, 底价) | 底价=成本×(1+最低利润%) 或 Steam到手×0.7")
    if not yes_input(args):
        print("已取消,未执行任何上架")
        return
    assets = {r["asset_id"]: r["price"] for r in plan if r["asset_id"]}
    n = uu.sell_items(assets)
    print(f"\n== 上架完成: 成功 {n}/{len(assets)} ==")


def yes_input(args):
    if getattr(args, "yes", False):
        return True
    return input("确认按上述价格上架? 输入 yes 执行,其他任意键取消: ").strip().lower() == "yes"


def yyyp_ratio(it, p, cfg):
    try:
        return (float(p["yyypSellPrice"]) * (1 - cfg.get("sell_fee", 0.01))) / \
               (float(p["steamSellPrice"]) * cfg.get("net_multiplier", 0.8696))
    except Exception:  # noqa
        return 9.99





def load_state(cfg):
    p = cfg.get("auto_state_file", os.path.join(DATA_DIR, "auto_state.json"))
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f), p
    return {}, p


def save_state(p, state):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def check_deliver(cfg, uu):
    """待发货订单检测:新订单立即推送微信,滞留>5分钟补发紧急提醒
    (超时未发货会被平台取消——本检测即为此兜底)"""
    state, state_path = load_state(cfg)
    try:
        orders, cancels = uu.wait_deliver_list()
    except Exception as e:  # noqa
        log(cfg, f"待发货检测失败: {str(e)[:80]}")
        return
    log(cfg, f"待发货检测: {len(orders)} 单在途")
    # 惯犯登记:有过已取消订单的买家 ID(恶意拍单识别)
    grief = state.setdefault("grief_buyers", {})
    new_grief = [uid for uid in cancels if not grief.get(uid)]
    for uid in new_grief:
        grief[uid] = True
    now = time.time()
    changed = bool(new_grief)
    for o in orders:
        key = "deliver_" + o["order_no"]
        rec = state.get(key)
        if not rec:
            griefer = "⚠该买家有取消前科(疑似恶意拍单) " if (
                o.get("buyer_uid") and grief.get(o["buyer_uid"])) else ""
            if o.get("offer_id"):
                act = f"{griefer}买家已发报价(offer {o['offer_id']}),去 Steam 手机端接受即可"
            else:
                act = f"{griefer}无买家报价,请尽快打开 UUYP App 点击发货(超时或买家取消!)"
            msg = (f"出售订单待发货\n{o['name']}  ¥{o.get('price')}\n"
                   f"订单号 {o['order_no']}\n→ {act}")
            log(cfg, f"发现待发货订单: {o['name']} (订单号{o['order_no']})")
            notify(cfg, "UUYP 待发货提醒", msg)
            state[key] = {"ts": now, "name": o["name"], "urgent": False}
            changed = True
        elif not rec.get("urgent") and now - rec.get("ts", now) > 300:
            msg = (f"待发货订单滞留超5分钟!\n{o['name']} 订单号{o['order_no']}\n"
                   f"→ 请立即处理,否则可能被平台取消")
            log(cfg, f"待发货滞留提醒: {o['name']}")
            notify(cfg, "UUYP 待发货滞留警告", msg)
            rec["urgent"] = True
            changed = True
    if changed:
        save_state(state_path, state)


def push_buy_hits(cfg, hits):
    """收购扫描命中 → 微信推送对齐表格(仅新标的,冷却去重后天然增量)
    主口径 比值(修正)=成本÷[min(成交价7,尖峰回落基准)×(1−周跌幅×持有周数)×0.8696]
    即把预期持有期阴跌 + 尖峰回落都从退出基准扣掉,防"比值虚高最后亏损" """
    if not hits:
        return

    def r_deal(c):
        d = c.get("deal7")
        try:
            return round(c["fresh_ask"] / (float(d) * 0.8696), 3) if d else None
        except (TypeError, ZeroDivisionError, ValueError):
            return None

    for c in hits:
        c["ratio_deal"] = r_deal(c)
        c.setdefault("ratio_signal", c["ratio_deal"])
        # 无信号时兜底按成交口径(旧版数据兼容)
    # 排序按修正口径(主决策),缺失的排后面
    hits = sorted(hits, key=lambda x: (x["ratio_signal"] if x.get("ratio_signal") is not None
                                       else (x["ratio_deal"] if x["ratio_deal"] is not None
                                             else x["fresh_ratio"]) + 1))

    headers = ["比值(修正)", "跌/周", "比值(成交)", "场地", "求购比", "UUYP卖一", "BUFF卖一",
               "Steam卖一", "成交价7", "毛利(修正)", "形态", "品种"]
    aligns = ["r", "r", "r", "l", "r", "r", "r", "r", "r", "r", "l", "l"]
    cells = []
    any70, any_spike = False, False
    for c in hits:
        d = c.get("deal7")
        rs = c.get("ratio_signal")
        rd = c["ratio_deal"]
        # 行标记:★=修正口径<0.70 硬机会 | ⚠=>0.75 谨慎 | 尖=尖峰按回落基准估值
        if rs is None:
            mark = "⚠"
        elif rs < 0.70:
            mark, any70 = "★", True
        elif rs > 0.75:
            mark = "⚠"
        else:
            mark = ""
        if c.get("spike"):
            mark += "尖"
            any_spike = True
        exp = c.get("exit_expected")
        gp = round(float(exp) * 0.8696 - c["fresh_ask"], 2) if exp else None
        venue_txt = c.get("venue", "UUYP") + ("≈" if c.get("snap") else "")
        if c.get("qty") and c["qty"] > 1:
            venue_txt += f"×{c['qty']}"
        disp = c.get("cn_name") or c["name"]  # 中文名优先(csqaq name 字段)
        ba = c.get("bid_ask")
        dec = (c.get("trend") or {}).get("weekly_decay")
        cls = c["curve"]["cls"] + ("·尖峰" if c.get("spike") else "")
        cells.append([f"{rs:.3f}{mark}" if rs is not None else "-",
                      (f"{dec:.2f}%" if dec is not None else "-"),
                      (f"{rd:.3f}" if rd is not None else "-"),
                      venue_txt,
                      (f"{ba:.2f}" if ba is not None else "-"),
                      str(c.get("yyyp_ask") or c.get("fresh_ask") or "-"),
                      str(c.get("buff_sell") or "-"),
                      str(c.get("steam_sell") or "-"),
                      (f"{d:.2f}" if d else "-"),
                      (f"+{gp:.2f}" if gp is not None else "-"),
                      trunc(cls, 16), trunc(disp, 40)])
    tbl = "\n".join(fmt_table(headers, cells, aligns))
    total = sum((c["fresh_ask"] or 0) * (c.get("qty") or 1) for c in hits)
    title = "收购扫描命中" + ("【含<0.70★】" if any70 else "")
    text = (f"新命中 {len(hits)} 件/可买 {sum(c.get('qty') or 1 for c in hits)} 件 | 全买需 ¥{total:.0f}\n"
            f"比值(修正)=成本÷[退出基准×(1−周跌幅×持有周数)×0.8696] 最终决策\n"
            f"退出基准=min(成交价7,尖峰回落基准);尖=短期急涨按回落基准估值\n"
            f"★=修正口径<0.70 优先看 | ⚠=>0.75或数据缺失 | 跌/周=180天稳健斜率\n"
            f"求购比=Steam买一÷卖一(<0.85已过滤) | BUFF≈=快照价"
            + ("\n本轮含尖峰标的,已按回落基准折算" if any_spike else "") + f"\n\n{tbl}")
    log(cfg, f"收购扫描命中 {len(hits)} 件,已推送" + ("(含<0.70★)" if any70 else ""))
    notify(cfg, title, text)


def exit_check(cfg, rows, listings, state, dry, now):
    """三场地(UUYP/BUFF/Steam) × 双模式(挂卖一/丢买一) 出口比较(只提醒不动资产)

    用户模型(2026-11-08 定稿):
    - 挂卖一的缺陷 = 僵尸价风险:没人抬就随行阴跌,几天后卖一不如当初直接丢买一——三边同构
    - 判定"卖一是否虚高"看成交与买一的贴近度:
        Steam:  卖一 vs 成交价7(pricehistory,唯一有真实成交数据的场地)
        UUYP/BUFF: 无成交价数据 → 用 买一/卖一 比价代理(贴=双边活跃,卖一是真的;
                   远=成交靠卖家砸单,卖一是假的)
    - 虚高时:要么挂贴成交价的卖一(anchor 下调),要么直接丢买一
    - 场地选择按到手价比较(挂刀比例本质);费率:UUYP 1% / BUFF 1.5% / Steam 15.04%

    数据与限速:csqaq 批量卖价每轮 1 次;UUYP 买一主循环已取(row["yyyp_bid"]);
    BUFF 求购簿按需(gate + goods_id 磁盘缓存 + 每轮限量 8);Steam 成交价7 只在
    Steam 挂单具有竞争力且缺数据时补拉(每轮限量 4)
    跳过 steam_gray/steam_suggest 物品(转Steam提醒已覆盖,其行内附 UUYP 砸单选项)"""
    fee_u = 1 - cfg.get("sell_fee", 0.01)        # UUYP 卖家净得系数
    fee_b = 1 - cfg.get("buff_sell_fee", 0.015)  # BUFF 卖家净得系数
    bal_m = cfg.get("net_multiplier", 0.8696)    # Steam 到手系数
    edge_min = cfg.get("exit_edge_min", cfg.get("buff_edge_min", 0.2))  # 换通道优势阈值(元)
    notify_hours = cfg.get("exit_notify_hours", 12)
    spread_th = cfg.get("spread_warn", 0.88)     # 买一/卖一低于此 = 卖一虚高(UUYP/BUFF)
    if not cfg.get("exit_check_enable", True):
        return []

    # 按名聚合持有(同名多件合并;记录挂单价与买一价)
    byname = {}
    for row in rows:
        nm = row.get("name")
        if not nm:
            continue
        rec = byname.setdefault(nm, {"cn": row.get("cn") or nm, "qty": 0,
                                     "listed_min": None, "yyyp_bid": None,
                                     "steam_ref": None, "asset_ids": []})
        rec["qty"] += 1
        rec["asset_ids"].append(row.get("asset_id", ""))
        if row.get("asset_id") in listings:
            try:
                p = float(str(listings[row["asset_id"]]["price"]).replace("¥", ""))
                rec["listed_min"] = p if rec["listed_min"] is None else min(rec["listed_min"], p)
            except (TypeError, ValueError):
                pass
        for k in ("yyyp_bid", "steam_ref"):
            v = row.get(k)
            if v and not rec[k]:
                rec[k] = float(v)
    # 跳过已走 Steam 出口通道的物品(转Steam提醒覆盖,避免重复轰炸)
    for nm in list(byname):
        if any(state.get(sid, {}).get("state") in ("steam_gray", "steam_suggest")
               for sid in byname[nm]["asset_ids"]):
            del byname[nm]
    if not byname:
        return []

    # csqaq 批量卖价(每轮 1 次调用,BUFF/Steam 卖一快照)
    price_map = {}
    names = list(byname)
    try:
        for i in range(0, len(names), 40):
            r = http_json("POST", CSQAQ + "/goods/getPriceByMarketHashName",
                          cfg["csqaq_token"], {"marketHashNameList": names[i:i + 40]})
            ok = (r.get("data") or {}).get("success") or {}
            for _, o in (ok.items() if isinstance(ok, dict) else []):
                if o.get("marketHashName"):
                    price_map[o["marketHashName"]] = o
    except Exception as e:  # noqa
        log(cfg, f"出口评估: csqaq 批量价失败 {str(e)[:60]}")
        return []

    buff_calls, deal_calls = 0, 0
    cookie = None
    hits = []
    for nm, rec in byname.items():
        o = price_map.get(nm)
        if not o:
            continue
        try:
            buff_ask = float(o.get("buffSellPrice") or 0)
            yyyp_ask = float(o.get("yyypSellPrice") or 0)
            steam_ask = rec["steam_ref"] or float(o.get("steamSellPrice") or 0)
        except (TypeError, ValueError):
            continue
        if yyyp_ask <= 0:
            continue

        # ---- UUYP 通道 ----
        ref_price = rec["listed_min"] or round(yyyp_ask * (1 - cfg.get("undercut_pct", 1.0) / 100.0), 2)
        uuyp_bid = rec["yyyp_bid"]
        if uuyp_bid and ref_price > 0:
            uuyp_tight = (uuyp_bid / ref_price) >= spread_th
            # 虚高 → 锚点下调到贴买一(买一×1.03);否则卖一是真的
            uuyp_anchor = ref_price if uuyp_tight else max(uuyp_bid * 1.03, 0.01)
        else:
            uuyp_tight, uuyp_anchor = True, ref_price
        uuyp_list = uuyp_anchor * fee_u
        uuyp_dump = round(uuyp_bid * fee_u, 2) if uuyp_bid else None

        # ---- BUFF 通道(卖一为 csqaq 快照;买一按需深查) ----
        buff_list = round(buff_ask * fee_b, 2) if buff_ask else None
        buff_bid, buff_dump = None, None
        last = state.get("exitnote_" + nm) or {}
        cooled = time.time() - last.get("ts", 0) < notify_hours * 3600
        if buff_ask and buff_ask <= yyyp_ask * cfg.get("buff_dump_gate", 1.02) \
                and buff_calls < 8 and not cooled:
            from auto_buy_test import buff_goods_id as _bgid, buff_buy_orders as _bbo
            gid = _bgid(cfg, nm)
            if gid:
                buff_calls += 1
                buff_bid = _bbo(cfg, gid)
                if buff_bid:
                    buff_dump = round(buff_bid * fee_b, 2)

        # ---- Steam 通道(卖一实时;成交价7 判虚高并作锚点) ----
        deal7, steam_spike = None, False
        steam_list = (steam_ask * bal_m) if steam_ask else None
        if steam_ask and deal_calls < 4 and steam_list and uuyp_list \
                and steam_list >= max(uuyp_list, buff_list or 0) * 0.9:
            # Steam 挂单有竞争力时才值得花 3.5s 拉成交价
            if cookie is None:
                try:
                    from auto_buy_test import steam_cookie as _sc
                    cookie = _sc(cfg) or ""
                except Exception:  # noqa
                    cookie = ""
            if cookie:
                from auto_buy_test import steam_pricehistory as _sph
                deal7 = _sph(cookie, nm)
                deal_calls += 1
                time.sleep(3.5)
        if deal7 and steam_ask and steam_ask > deal7 * 1.10:
            steam_spike = True
            steam_list = round(deal7 * bal_m, 2)  # 锚点下调:贴成交价挂

        # ---- 汇总:最优挂单场地 vs 最优即时通道 ----
        list_opts = [(v, n, p) for v, n, p in
                     [("UUYP", uuyp_list, ref_price), ("BUFF", buff_list, buff_ask),
                      ("Steam", steam_list, steam_ask)] if n]
        best_list = max(list_opts, key=lambda x: x[1]) if list_opts else None
        dump_opts = [(v, n) for v, n in [("UUYP", uuyp_dump), ("BUFF", buff_dump)] if n]
        best_dump = max(dump_opts, key=lambda x: x[1]) if dump_opts else None

        # 推送门槛:有行动建议,或双挂可行(均带 12h 冷却)
        dual_ok = buff_ask >= yyyp_ask * 0.97
        action = None
        if best_dump and best_list and best_dump[1] >= best_list[1] - 0.1:
            action = f"丢买一:{best_dump[0]} 到手 {best_dump[1]:.2f}(即时,免阴跌)"
        elif best_dump and best_dump[1] >= uuyp_list + edge_min:
            action = f"可砸{best_dump[0]}求购:到手 {best_dump[1]:.2f} (+{best_dump[1] - uuyp_list:.2f} vs UUYP挂单)"
        if steam_spike and best_list and best_list[0] == "Steam":
            spike_act = f"Steam卖一虚高(挂{steam_ask} vs 成交{deal7}) → 贴成交挂 ≤{deal7}(到手 {steam_list:.2f})"
            action = f"{spike_act};或{action}" if action else spike_act
        if not action and not dual_ok:
            state.setdefault("exitnote_" + nm, {"ts": time.time()})
            continue
        if cooled:
            continue  # 12h 内已提醒过
        lines = [f"· {trunc(rec['cn'], 40)} ×{rec['qty']}"]
        ch = "挂单" if rec["listed_min"] else "可挂"
        lines.append(f"  UUYP{ch} {ref_price:.2f}→到手 {uuyp_list:.2f}"
                     + (f" | 砸买一 {uuyp_dump:.2f}" if uuyp_dump else ""))
        if buff_ask:
            lines.append(f"  BUFF卖一 {buff_ask}≈→到手 {buff_list:.2f}"
                         + (f" | 砸求购 {buff_dump:.2f}" if buff_dump else "")
                         + (f" | 双挂 ¥{math.ceil(ref_price * fee_u / fee_b * 100) / 100:.2f}(等净)" if dual_ok else ""))
        if deal7:
            lines.append(f"  Steam卖一 {steam_ask} | 成交7 {deal7}"
                         + ("(卖一虚高,贴成交挂)" if steam_spike else ""))
        if action:
            lines.append(f"  ▶ {action}")
        state["exitnote_" + nm] = {"ts": time.time(), "edge": (best_dump or (0, 0))[1]}
        hits.append("\n".join(lines))
    if hits:
        hdr = (f"到手=价×费率(UUYP {fee_u:.2f}/BUFF {fee_b:.3f}/Steam {bal_m:.4f}) | "
               f"虚高判定:买一/卖一<{spread_th} 或 卖一>成交7×1.10\n"
               f"卖一虚高→贴成交价挂或直接丢买一 | BUFF执行链:UUYP提取→Steam库存→BUFF\n")
        if not dry:
            notify(cfg, "最优出口提醒", hdr + "\n".join(hits))
        log(cfg, f"出口评估: {len(hits)} 项已推送")
    else:
        log(cfg, f"出口评估: {len(byname)} 项,无提醒")
    return hits



def cmd_auto(cfg, uu, rows, dry):
    acc = cfg.get("accept_ratio", 0.80)
    sat = cfg.get("satisfy_ratio", 0.70)
    dlist = cfg.get("delist_ratio", 0.75)  # 已上架物品的下架阈值(滞后带防反复横跳)
    undercut = cfg.get("undercut_pct", 1.0) / 100.0  # 插队幅度:卖一减N%(默认1%)
    # 观察模式:计划照算,但不上架/不改价/不跟涨/不下架,改为推送"建议动作"由人工执行
    notify_only = cfg.get("notify_only", False)
    listings = {l["steam_asset_id"]: l for l in uu.get_sell_list()}
    log(cfg, f"在售挂单 {len(listings)} 件")
    state, state_path = load_state(cfg)
    actions, notes = [], []
    gray, delisted = [], []  # 灰色带(0.75-0.80)提醒 / 已下架转Steam
    now = datetime.now().strftime("%m-%d %H:%M")

    # ---- Phase 1: 计划(不动任何资产) ----
    plans = {"list": {}, "reprice": {}, "delist": []}
    prev_snapshot = {}
    net_m = 1 - cfg.get("sell_fee", 0.01)
    bal_m = cfg.get("net_multiplier", 0.8696)

    def real_book(template_id):
        """UUYP 实时挂单簿(GetCsGoPagedList) → (own_prices, others_min, all_prices)
        按 UserId 区分自己/他人——比 csqaq 快照权威(csqaq 有滞后幽灵价,曾致级联追砍)
        该接口限频时会静默返回空(实测):持久缓存 8 分钟 TTL + 空响应重试一次 + 间隔 1.5s"""
        key = "book_" + str(template_id)
        cached = state.get(key)
        if cached and time.time() - cached.get("ts", 0) < 480:
            return cached.get("own", []), cached.get("others_min"), cached.get("all", [])
        res = ([], None, [])

        def fetch():
            r = uu.call("POST", "/api/homepage/es/commodity/GetCsGoPagedList",
                        data={"listSortType": 1, "sortType": 1, "gameId": "730",
                              "pageIndex": 1, "pageSize": 20, "templateId": template_id})
            own_p, other_p = [], []
            for c in (r.get("Data") or {}).get("CommodityList") or []:
                try:
                    p = float(str(c.get("Price")).replace("¥", ""))
                    (own_p if c.get("UserId") == uu.user_id else other_p).append(p)
                except (TypeError, ValueError):
                    pass
            return own_p, other_p

        try:
            own_p, other_p = fetch()
            if not own_p and not other_p:
                time.sleep(3)  # 空响应=可能被限频,退避一次
                own_p, other_p = fetch()
            if own_p or other_p:
                others_min = min(other_p) if other_p else None
                res = (own_p, others_min, sorted(own_p + other_p))
                state[key] = {"ts": time.time(), "own": own_p,
                              "others_min": others_min, "all": res[2]}
        except Exception as e:  # noqa
            log(cfg, f"实时挂单簿获取失败 tid={template_id}: {str(e)[:60]}")
        time.sleep(1.5)
        return res

    for row in rows:
        sid = row.get("asset_id", "")
        r = row.get("ratio")
        if not sid or not isinstance(r, float):
            continue
        listed = sid in listings
        prev = state.get(sid, {}).get("state")
        mkt = row.get("yyyp")
        mkt = float(mkt) if mkt else None

        # 实时挂单簿:有 templateId 且(已上架 或 csqaq比值≥sat)才拉,控制调用量
        tid = row.get("template_id")
        own_list, others_min, all_prices = ([], None, [])
        if tid and (listed or r >= sat):
            own_list, others_min, all_prices = real_book(tid)

        # 有效参考价:他人最低价(实时)优先,csqaq 快照仅兜底
        # 有效比值 r_eff 基于"现在改价实际能挂到的价"(他人最低×(1-插队))
        steam = row.get("steam")
        steam_ref = steam
        if tid and (listed or r >= sat) and steam:
            # Steam 卖一实时化:csqaq 的 steamSellPrice 是滞后快照(实测差 6.8%)
            steam_ref = steam_real_ask(row["name"]) or steam
        if others_min is not None:
            eff_yyyp = round(others_min * (1 - undercut), 2)
            r_eff = (round(eff_yyyp * net_m / (steam_ref * bal_m), 3)
                     if steam_ref else r)
        elif own_list:
            # 无他人挂单:自己的最低件就是市场价,保持不动
            eff_yyyp = round(min(own_list), 2)
            r_eff = (round(eff_yyyp * net_m / (steam_ref * bal_m), 3)
                     if steam_ref else r)
        else:
            eff_yyyp = mkt
            r_eff = r
        row["steam_ref"] = steam_ref   # 出口评估用(实时 Steam 卖一)
        row["eff_yyyp"] = eff_yyyp     # 出口评估用(实时 UUYP 可挂价)
        row["yyyp_bid"] = None         # UUYP 买一(出口评估用,下方填充)

        supply_p = None
        if eff_yyyp:
            bid = yyyp_bid(cfg, row.get("good_id"))
            row["yyyp_bid"] = bid      # csqaq 7日买价曲线最新点
            if bid and 0 < bid < eff_yyyp:
                supply_p = round(bid, 2)
        target = (eff_yyyp if others_min is not None
                  else round(eff_yyyp * (1 - undercut), 2))
        newp = supply_p if (supply_p and target <= supply_p) else target
        via_cut = (f"供给求购(买一{supply_p})" if newp == supply_p
                   else f"插队-{cfg.get('undercut_pct', 1.0):g}%")

        if not listed:
            if r_eff >= acc:
                if row.get("tradable") is True and eff_yyyp:
                    plans["list"][sid] = newp
                    prev_snapshot[sid] = dict(state.get(sid) or {})
                    actions.append(("自动上架", sid, row["name"], r_eff, f"{newp} ({via_cut})"))
                    state[sid] = {"state": "listed", "ratio": r_eff, "ts": now}
            elif prev != "steam_suggest":
                notes.append(f"  {row['name'][:44]}  比值 {r_eff:.3f} → 建议 Steam 出售")
                state[sid] = {"state": "steam_suggest", "ratio": r_eff, "ts": now}
        else:
            ours = float(listings[sid]["price"] or 0)
            acted = False
            if others_min is not None and ours > others_min + 0.005:
                # 对手低于我:追价;但若追完跌破 0.70 下架线 → 直接下架转 Steam
                if r_eff >= dlist:
                    plans["reprice"][listings[sid]["commodity_id"]] = newp
                    prev_snapshot[sid] = dict(state.get(sid) or {})
                    actions.append(("自动改价", sid, row["name"], r_eff,
                                    f"{ours}→{newp} (对手{others_min} Steam{steam_ref} {via_cut})"))
                    state[sid] = {"state": "listed", "ratio": r_eff, "ts": now}
                    acted = True
                else:
                    plans["delist"].append(listings[sid]["commodity_id"])
                    prev_snapshot[sid] = dict(state.get(sid) or {})
                    actions.append(("自动下架→转Steam", sid, row["name"], r_eff,
                                    f"{ours}→追价{newp}将跌破{dlist}(Steam{steam_ref}),直接转Steam"))
                    delisted.append((row, r_eff))
                    state[sid] = {"state": "steam_suggest", "ratio": r_eff, "ts": now}
                    acted = True
            elif others_min is not None and ours < newp - 0.005 and r_eff >= dlist:
                # 跟涨:对手涨价后自己让利过头(灰色带也跟,涨价只会提升比值)
                plans["reprice"][listings[sid]["commodity_id"]] = newp
                prev_snapshot[sid] = dict(state.get(sid) or {})
                actions.append(("自动跟涨", sid, row["name"], r_eff,
                                f"{ours}→{newp} (对手{others_min})"))
                state[sid] = {"state": "listed", "ratio": r_eff, "ts": now}
                acted = True
            if not acted and r_eff < acc:
                # 未动作且比值落入灰色带 → 提醒一次(状态去重)
                if prev != "steam_gray":
                    gray.append((row, r_eff))
                    state[sid] = {"state": "steam_gray", "ratio": r_eff, "ts": now}
                # 灰色带内停留不重复提醒(状态保持 steam_gray)

    # ---- Phase 2: 分组执行(每组独立,失败回滚该组状态) ----
    # 观察模式(notify_only):不执行任何资产动作,并回滚 Phase 1 提前写入的"已上架/已下架"状态
    # ——否则下一轮会误判已处理而不再提醒
    if notify_only and (plans["list"] or plans["reprice"] or plans["delist"]):
        for a in actions:
            if a[0] in ("自动上架", "自动改价", "自动跟涨", "自动下架→转Steam"):
                if prev_snapshot.get(a[1]):
                    state[a[1]] = prev_snapshot[a[1]]
                else:
                    state.pop(a[1], None)
        log(cfg, f"观察模式: {len(actions)} 个动作未执行(notify_only=true)")
    if not dry and not notify_only:
        if plans["delist"]:
            try:
                uu.off_shelf(plans["delist"])
                log(cfg, f"批量下架 {len(plans['delist'])} 件")
            except Exception as e:  # noqa
                log(cfg, f"批量下架失败(下轮重试): {e}")
                for cid in plans["delist"]:
                    sid_ = next((a[1] for a in actions
                                 if a[0] == "自动下架→转Steam"
                                 and listings.get(a[1], {}).get("commodity_id") == cid), None)
                    if sid_ and prev_snapshot.get(sid_):
                        state[sid_] = prev_snapshot[sid_]
        if plans["reprice"]:
            try:
                uu.change_price(plans["reprice"])
                log(cfg, f"批量改价 {len(plans['reprice'])} 笔")
            except Exception as e:  # noqa
                log(cfg, f"批量改价失败(下轮重试): {e}")
                for sid_ in plans["reprice"]:
                    pass
                for a in actions:
                    if a[0] == "自动改价" and prev_snapshot.get(a[1]):
                        state[a[1]] = prev_snapshot[a[1]]
        if plans["list"]:
            try:
                n, failed_assets = uu.sell_items(plans["list"])
                log(cfg, f"自动上架 {n}/{len(plans['list'])}")
                for fa in failed_assets:
                    if prev_snapshot.get(fa):
                        state[fa] = prev_snapshot[fa]
                    else:
                        state.pop(fa, None)
            except Exception as e:  # noqa
                log(cfg, f"自动上架失败(下轮重试): {e}")
                for sid_ in plans["list"]:
                    if prev_snapshot.get(sid_):
                        state[sid_] = prev_snapshot[sid_]
                    else:
                        state.pop(sid_, None)

    # ---- Phase 2.5: 出口评估(三场地×挂/砸,只提醒不动资产;state 变更随 Phase 3 落盘) ----
    try:
        exit_check(cfg, rows, listings, state, dry, now)
    except Exception as e:  # noqa
        log(cfg, f"出口评估异常: {str(e)[:80]}")

    # ---- Phase 3: 通知与存档 ----
    # 转Steam提醒(事件型推送,不受 push_dispatch 开关控制):
    # 灰色带=双边都可出、尽快出为上策; <0.70 已自动下架
    # 附两侧价格快照 + Steam成交价7(尖峰回落识别),让用户能判断信息新鲜度
    if gray or delisted:
        cookie = ""
        try:
            from auto_buy_test import steam_cookie as _sc, steam_pricehistory as _sph
            cookie = _sc(cfg)
        except Exception:  # noqa
            pass
        if cookie:
            for i, (row, _r) in enumerate(gray + delisted):
                if row.get("deal7") is None and row.get("steam"):
                    row["deal7"] = _sph(cookie if i == 0 else None, row["name"])
                    time.sleep(3.5)

        lines = []
        for row, r in gray + delisted:
            is_gray = (row, r) in gray
            act = ("建议尽快挂 Steam(挂单暂保留)" if is_gray else
                   "建议下架转 Steam(观察模式,未执行)" if notify_only else
                   "已自动下架,请转 Steam 出售")
            deal = row.get("deal7")
            yyyp_v = row.get("eff_yyyp") or row.get("yyyp") or row.get("yyyp_ask") or "?"
            steam_v = row.get("steam_ref") or row.get("steam") or "?"
            extra = ""
            yb = row.get("yyyp_bid")
            if yb:
                # UUYP 砸买一也是即时出口,有时比挂 Steam 更快更优
                extra = f" | UUYP砸买一到手 {round(float(yb) * net_m, 2):.2f}"
            lines.append(
                f"· {trunc(row.get('cn') or row['name'], 40)}  比值 {r:.3f} → {act}\n"
                f"  UUYP卖一 {yyyp_v} | Steam卖一 {steam_v}{_steam_spike(steam_v, deal)} | 成交7 {deal if deal else '-'}{extra}")
        if not dry:
            notify(cfg, "转 Steam 提醒", f"价格快照 {now},以当时为准\n\n" + "\n".join(lines))
        log(cfg, f"转Steam提醒: 灰色带 {len(gray)} 件,已下架 {len(delisted)} 件")
    if actions or notes:
        # 观察模式:动作只作为"建议"推送,不改资产;签名去重防止每 10 分钟重复轰炸
        prefix = "仅通知·建议动作(未执行)" if notify_only else "自动调度"
        content = [f"🤖 {prefix} {now}"]
        for a in actions:
            tag = ("建议" + a[0].removeprefix("自动")) if notify_only else a[0]
            content.append(f"[{tag}] {a[2][:44]}  比值 {a[3]:.3f}  {a[4]}")
        content += notes
        text = "\n".join(content)
        print("\n" + text)
        _append_log(text + "\n" + "-" * 50)
        if not dry:
            if notify_only:
                # 签名 = 动作类型+标的+目标价/原因;内容不变则不重推,价格变了或新动作出现才推
                sig = "|".join(f"{a[0]}:{a[1]}:{a[4]}" for a in actions)
                last = state.get("notify_only_digest") or {}
                if sig == last.get("sig") and time.time() - last.get("ts", 0) < 12 * 3600:
                    log(cfg, "观察模式: 建议内容未变化,跳过重复推送")
                elif sig:
                    notify(cfg, "调度建议(仅通知)", text)
                    state["notify_only_digest"] = {"sig": sig, "ts": time.time()}
            elif cfg.get("push_dispatch"):
                # 例行调度默认不推微信(消息太多),仅记日志;需要时在配置加 "push_dispatch": true
                notify(cfg, "倒余额自动调度", text)
    else:
        log(cfg, "无状态变化")
    if not dry:
        save_state(state_path, state)
    return actions, notes


# LOG_DIR 由 paths 提供(repo/data/logs)


def weekly_log_path():
    """按周建日志文件:以本周周一日期命名,如 logs/auto_log_2026-10-19.txt"""
    os.makedirs(LOG_DIR, exist_ok=True)
    now = datetime.now()
    monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"auto_log_{monday}.txt")


def _append_log(text):
    with open(weekly_log_path(), "a", encoding="utf-8") as f:
        f.write(text + "\n")


def log(cfg, text):
    ts = datetime.now().strftime("%m-%d %H:%M:%S")
    line = f"[{ts}] {text}"
    print(line)
    _append_log(line)


def cmd_auto_loop(cfg, args):
    if args.loop:
        log(cfg, f"进入常驻循环,每 {args.interval} 秒一轮 (dry_run={args.dry_run})")
    while True:
        log(cfg, f"轮次开始 (dry_run={args.dry_run})")
        try:
            uu, inv, prices, cn, ca = prepare(cfg, False)
            rows = build_rows(inv, prices, (cn, ca), cfg)
            cmd_auto(cfg, uu, rows, args.dry_run)
            if not args.dry_run:
                check_deliver(cfg, uu)  # 待发货订单检测+微信提醒
                if cfg.get("buy_scan_enable", True):
                    try:
                        from auto_buy_test import run_once
                        hits = run_once(cfg, uu, pages=int(cfg.get("buy_scan_pages", 8)))
                        if hits:
                            push_buy_hits(cfg, hits)  # 命中新标的→微信推送
                        else:
                            log(cfg, "收购扫描:无新命中")
                    except Exception as e:  # noqa
                        log(cfg, f"收购扫描异常: {str(e)[:80]}")
        except Exception as e:  # noqa
            log(cfg, f"轮次异常: {e}")
        if not args.loop:
            log(cfg, "单轮模式结束")
            break
        log(cfg, f"休眠 {args.interval} 秒")
        time.sleep(args.interval)



def push_ready(cfg):
    return bool((cfg.get("wxpusher") or {}).get("app_token"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["login", "run", "sell", "auto"])
    ap.add_argument("--loop", action="store_true", help="auto 模式:常驻循环(默认单轮,配任务计划用)")
    ap.add_argument("--interval", type=int, default=600, help="循环间隔秒(默认600)")
    ap.add_argument("--yes", action="store_true", help="跳过确认直接上架")
    ap.add_argument("--min-profit", type=float, default=5.0, help="底价最低利润%%(默认5)")
    ap.add_argument("--asset", action="append", default=[], help="只上架指定 SteamAssetId(可多次)")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    ap.add_argument("--mock", action="store_true", help="合成数据自检")
    args = ap.parse_args()
    cfg = load_cfg()
    if args.cmd == "login":
        cmd_login(cfg)
        return
    if args.cmd == "sell":
        uu, inv, prices, cn, ca = prepare(cfg, False)
        cmd_sell(cfg, uu, inv, prices, cn, ca, args)
        return
    if args.cmd == "auto":
        cmd_auto_loop(cfg, args)
        return
    if args.mock:
        cmd_run(cfg, args.dry_run or not (cfg.get("wxpusher") or {}).get("app_token"), True)
        return
    if not cfg.get("uu_token"):
        sys.exit("config 缺少 uu_token:先跑 `python watcher_inv.py login`")
    if not cfg.get("csqaq_token"):
        sys.exit("config 缺少 csqaq_token")
    push_ready = bool((cfg.get("wxpusher") or {}).get("app_token"))
    if not push_ready:
        print("[warn] 未配置 wxpusher,本次仅本地输出(等效 --dry-run)")
    cmd_run(cfg, args.dry_run or not push_ready, False)


if __name__ == "__main__":
    main()
