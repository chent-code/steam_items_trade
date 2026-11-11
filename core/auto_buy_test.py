#!/usr/bin/env python3
"""自动收购测试脚本(独立于 auto 调度,先测试后整合)

流程:
  ① 扫描:csqaq 挂刀榜(exchange_detail,分页) → 双扣费比值 = yyyp卖价/(steam卖价×0.8696)
  ② 过滤:比值 ≤ 0.70 | 平台价 ≥ 10 | Steam日成交 ≥ 10
  ③ 实时验证:每候选经 GetCsGoPagedList(templateId=yyyp_id) 取 UUYP 当前卖一 → 重算比值 → 仍达标才确认
  ④ 报告:CSV + 控制台(默认);--mode qiugou 且 --yes 时对确认标的挂求购单

用法:
  python auto_buy_test.py scan                       # 扫描+验证+报告
  python auto_buy_test.py scan --pages 10            # 扫更多页
  python auto_buy_test.py scan --mode qiugou --yes   # 确认后挂求购单
"""
import argparse
import base64
import csv
import json
import os
import re
import sys
import time
from datetime import datetime

import statistics
import requests
from urllib.parse import quote

from uu_auth import UUClient
from watcher_inv import dwidth, trunc, fmt_table
from paths import DATA_DIR, REPORT_DIR, LOG_DIR, CONFIG_FILE

HERE = DATA_DIR  # 运行时数据基准:repo/data/(cookie/state 等沿用 HERE 拼接)
CFG = CONFIG_FILE
CSQAQ = "https://api.csqaq.com/api/v1"
NET = 0.8696


def load_cfg():
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def weekly_buy_log_path():
    os.makedirs(LOG_DIR, exist_ok=True)
    now = datetime.now()
    monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"buy_log_{monday}.txt")


import time  # noqa: E402
from datetime import timedelta  # noqa: E402


def log(text):
    ts = datetime.now().strftime("%m-%d %H:%M:%S")
    line = f"[{ts}] {text}"
    print(line)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(weekly_buy_log_path(), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def csqaq_post(cfg, path, payload):
    hdr = {"ApiToken": cfg["csqaq_token"], "Content-Type": "application/json"}
    for i in range(3):
        try:
            r = requests.post(CSQAQ + path, json=payload, headers=hdr, timeout=30)
            if r.status_code == 429:
                time.sleep(10 * (i + 1))
                continue
            return r.json()
        except Exception as e:  # noqa
            time.sleep(3)
            last = e
    print(f"[csqaq] {path} 失败: {last}")
    return {}


_BIND_TS = {"t": 0.0}  # 进程内绑定节流(接口限 30s/次)


def csqaq_bind(cfg, force=False):
    """运行环境 IP 变化时自动重绑(谁跑绑谁,30s 限速,失败不致命);
    30s 内已绑过则跳过(force=True 强制重绑)"""
    if not force and time.time() - _BIND_TS["t"] < 30:
        return
    try:
        r = requests.post(CSQAQ + "/sys/bind_local_ip", json={},
                          headers={"ApiToken": cfg["csqaq_token"]}, timeout=20).json()
        print(f"[csqaq] IP绑定: {r.get('data') or r.get('msg')}")
        _BIND_TS["t"] = time.time()
    except Exception as e:  # noqa
        print(f"[csqaq] IP绑定跳过: {e}")
    time.sleep(2)


def scan_candidates(cfg, pages):
    csqaq_bind(cfg)
    """csqaq 挂刀榜分页扫描 → ratio ≤ 上限的候选(快照口径)"""
    out, scanned = [], 0
    for page in range(1, pages + 1):
        r = csqaq_post(cfg, "/info/exchange_detail", {
            "page_index": page, "res": 0, "platforms": "BUFF-YYYP",
            "sort_by": 1, "min_price": cfg.get("min_price", 10),
            "max_price": cfg.get("max_price", 5000),
            "turnover": cfg.get("min_turnover", 10)})
        if r.get("code") != 200:
            log(f"扫描 page {page} 失败: {r.get('msg')}")
            break
        rows = r.get("data") or []
        if not rows:
            break
        scanned += len(rows)
        for it in rows:
            yyyp, steam = it.get("yyyp_sell_price"), it.get("steam_sell_price")
            steam_bid = it.get("steam_buy_price")  # Steam 最高求购价(最差即时退出参照)
            buff = it.get("buff_sell_price")  # BUFF 卖一(买家无手续费,可直接比价)
            if not yyyp or not steam or steam <= 0:
                continue
            ratio_ask = round(yyyp / (steam * NET), 3)  # UUYP 买入口径
            ratio_buff = (round(buff / (steam * NET), 3)
                          if buff and buff > 0 else None)  # BUFF 买入口径
            ratio_bid = (round(yyyp / (steam_bid * NET), 3)
                         if steam_bid and steam_bid > 0 else None)  # 参照:立刻砸单
            eff = min([x for x in (ratio_ask, ratio_buff) if x is not None], default=ratio_ask)
            # 流动性过滤 v2(用户洞察:清货天数公式错误——挂最低价不需要等全簿清完,
            # 只需要等买家抬卖一;而低成交量品类成交多为卖家砸求购单,卖一无人抬)
            # 求购比 = Steam买一/卖一:接近1=双边活跃(买家贴着卖一买),远低于1=成交靠卖家砸单,挂卖单极慢
            # (Steam 常态价差因 15% 卖家费约 0.87-0.9;分布 p10=0.85,取 0.85 滤掉最差一档)
            turnover = it.get("turnover_number") or 0
            bid_ask = (round(steam_bid / steam, 2) if steam_bid and steam_bid > 0 else None)
            if turnover < cfg.get("min_turnover", 10):  # 成交量=买家到货率上限,过低必有长期空窗
                continue
            if bid_ask is None or bid_ask < cfg.get("min_bid_ask", 0.85):
                continue  # 无人抬卖一的市场,挂最低价也卖不掉,阴跌吃利润
            if eff <= cfg.get("ratio_max", 0.70):
                # 场地:谁便宜买谁(同价优先 UUYP,余额支付链路更顺)
                venue = "UUYP" if (ratio_buff is None or ratio_ask <= ratio_buff) else "BUFF"
                to_hand = round(steam_bid * NET, 2)
            else:
                continue
            out.append({"name": it.get("market_hash_name", ""),
                        "cn_name": it.get("name", ""),
                        "template_id": str(it.get("yyyp_id", "")),
                        "csqaq_id": it.get("id"),
                        "buff_id": it.get("buff_id"),
                        "yyyp_ask": yyyp, "buff_sell": buff,
                        "steam_sell": steam,
                        "steam_bid": steam_bid, "ratio_bid": ratio_bid,
                        "ratio_ask": ratio_ask, "ratio_buff": ratio_buff,
                        "venue": venue, "ratio": eff,
                        "turnover": turnover,
                        "bid_ask": bid_ask,
                        "to_hand": to_hand,
                        "spread": round(to_hand - yyyp, 2)})
        log(f"扫描 page {page}: 累计 {scanned} 件,候选 {len(out)}")
        if len(rows) < 50:
            break
        time.sleep(1.2)
    log(f"扫描完成: {scanned} 件中候选 {len(out)}")
    return out, scanned


def chart_pairs(cfg, good_id, period="180"):
    """csqaq Steam 卖价曲线 → [(datetime, price)] 升序(180天≈半年/90天≈一季度)"""
    r = csqaq_post(cfg, "/info/chart", {"good_id": good_id, "key": "sell_price",
                                        "platform": 3, "period": str(period),
                                        "style": "all_style"})
    d = r.get("data") or {}
    return sorted((datetime.fromtimestamp(t / 1000), float(v))
                  for t, v in zip(d.get("timestamp") or [], d.get("main_data") or []))


def trend_signal(pairs, cfg):
    """长周期趋势信号(用户策略:把周跌幅和尖峰回落纳入挂刀比例)

    weekly_decay: 周化跌幅%(正=跌)。按 7 日窗口取每周中位价,再用 Theil-Sen
      (所有点对斜率的中位数)估计斜率——抗单点尖峰,不会把一次暴涨误当趋势
    spike: 近 7 日中位价相对 8~90 日前基线中位涨幅 ≥ spike_pct → 尖峰回落风险
    spike_base: 回落基准价(尖峰前的价格中枢,退出估值按它算而不是尖峰值)
    """
    out = {"weekly_decay": None, "spike": False, "spike_base": None,
           "spike_rise": None, "med_long": None}
    if len(pairs) < 20:
        return out
    prices = [float(p) for _, p in pairs if p and p > 0]
    if len(prices) < 20:
        return out
    t0 = pairs[0][0]
    buckets = {}
    for dt, p in pairs:
        if not p or p <= 0:
            continue
        w = int((dt - t0).total_seconds() // (7 * 86400))
        buckets.setdefault(w, []).append(float(p))
    wmed = sorted((w, statistics.median(v)) for w, v in buckets.items())
    if len(wmed) >= 4:
        slopes = []
        for i in range(len(wmed)):
            for j in range(i + 1, len(wmed)):
                dw = wmed[j][0] - wmed[i][0]
                if dw >= 2 and wmed[i][1] > 0:
                    slopes.append((wmed[j][1] - wmed[i][1]) / dw / wmed[i][1])
        if slopes:
            decay = -statistics.median(slopes) * 100  # %/周,正=下跌
            out["weekly_decay"] = round(max(0.0, min(decay, cfg.get("decay_cap_pct", 5.0))), 2)
    last = pairs[-1][0]
    recent = [float(p) for dt, p in pairs if (last - dt).days <= 7 and p and p > 0]
    # 尖峰基线 = 紧邻前置平台(7~30 日前),不是长周期中枢——否则"长期下跌后反弹回旧价"会被误判
    near_pool = [float(p) for dt, p in pairs
                 if 8 <= (last - dt).days <= cfg.get("spike_base_days", 30) and p and p > 0]
    long_pool = [float(p) for dt, p in pairs
                 if 8 <= (last - dt).days <= 90 and p and p > 0]
    base_pool = near_pool if len(near_pool) >= 5 else long_pool
    if recent and base_pool:
        cur = statistics.median(recent)
        base = statistics.median(base_pool)
        rise = (cur / base - 1) if base > 0 else 0
        out["med_long"] = round(statistics.median(long_pool), 2) if long_pool else None
        out["spike_rise"] = round(rise * 100)
        if rise >= cfg.get("spike_pct", 0.15):
            out["spike"] = True
            # 回落基准:给 5% 容忍(真实重估会保留一点涨幅),但绝不用尖峰值
            out["spike_base"] = round(min(base * (1 + cfg.get("spike_tolerance", 0.05)), cur), 2)
    return out


def apply_signal(c, cfg):
    """折算最终决策口径 ratio_signal = 成本 ÷ 修正后退出到手

    修正后退出基准 = min(近7日成交均价或Steam卖一, 尖峰回落基准)
                    × (1 - 周跌幅 × 预期持有周数)
    即:预期持有期内的阴跌 + 尖峰回落都从分母扣掉,避免"比值虚高最后亏损"
    """
    sig = c.get("trend") or {}
    d = c.get("deal7")
    try:
        base = float(d) if d else float(c.get("steam_sell") or 0)
    except (TypeError, ValueError):
        base = 0.0
    spike = bool(sig.get("spike"))
    if spike and sig.get("spike_base"):
        base = min(base, float(sig["spike_base"]))
    decay = sig.get("weekly_decay")
    if decay:
        base *= max(0.5, 1 - decay / 100 * cfg.get("hold_weeks", 1))
    c["exit_expected"] = round(base, 2) if base else None
    try:
        c["ratio_signal"] = (round(c["fresh_ask"] / (base * NET), 3)
                             if base and c.get("fresh_ask") else None)
    except (TypeError, ZeroDivisionError, ValueError):
        c["ratio_signal"] = None
    c["spike"] = spike
    return c


def classify_curve(pairs, steam_now=None):
    """跳涨形态分类器(用户洞察:重估与尖峰的区别在跳涨后的稳定性)
    pairs: [(datetime, price)] 升序;steam_now: 当前实时价(比曲线末端新)
      → {cls, jump_pct, days, detail}
    """
    empty = {"cls": "数据不足", "jump_pct": None, "days": None, "detail": ""}
    if len(pairs) < 4:
        return empty
    prices = [p for _, p in pairs]
    # 跳涨检测:最大单步涨幅
    jump_idx, jump_ratio = None, 1.0
    for i in range(1, len(prices)):
        if prices[i - 1] > 0 and prices[i] / prices[i - 1] > jump_ratio:
            jump_ratio, jump_idx = prices[i] / prices[i - 1], i
    if jump_idx is None or jump_ratio < 1.3:
        return {"cls": "常态", "jump_pct": None, "days": None,
                "detail": f"无跳涨(最大单步 {round((jump_ratio-1)*100)}%)"}
    jump_pct = round((jump_ratio - 1) * 100)
    jump_dt = pairs[jump_idx][0]
    post = prices[jump_idx:]
    days = (datetime.now() - jump_dt).days
    latest = steam_now if (steam_now and steam_now > 0) else prices[-1]
    peak = max(post)
    decay = round((1 - latest / peak) * 100)
    if len(post) < 3:
        if decay < 10:
            return {"cls": f"重估·稳({days}天,延续)", "jump_pct": jump_pct,
                    "days": days, "detail": f"跳涨+{jump_pct}%,当前价稳在高位"}
        return {"cls": f"尖峰·回落中(-{decay}%)", "jump_pct": jump_pct,
                "days": days, "detail": f"已从峰值回落{decay}%"}
    cv = statistics.pstdev(post) / statistics.mean(post)
    if cv <= 0.10 and decay < 15:
        return {"cls": f"重估·稳({days}天)", "jump_pct": jump_pct, "days": days,
                "detail": f"跳涨+{jump_pct}%后 CV {cv:.2f} 稳定"}
    if decay >= 15:
        return {"cls": f"尖峰·回落中(-{decay}%)", "jump_pct": jump_pct,
                "days": days, "detail": f"峰值回落{decay}%"}
    return {"cls": "混合·观察", "jump_pct": jump_pct, "days": days,
            "detail": f"跳涨+{jump_pct}%,CV {cv:.2f}"}


def verify_fresh(uu, cand, cfg):
    """UUYP 实时验证:返回 (确认?, 当前卖一, fresh_ratio, 可买件数)
    可买件数 = 挂单簿上所有 ≤ ratio_max×Steam到手 的档位数(不只卖一),
    上限 buy_max_per_item(默认3);深度买入策略的基础"""
    tid = cand["template_id"]
    if not tid:
        return False, None, None, 0
    try:
        r = uu.call("POST", "/api/homepage/es/commodity/GetCsGoPagedList",
                    data={"listSortType": 1, "sortType": 1, "gameId": "730",
                          "pageIndex": 1, "pageSize": 20, "templateId": tid})
    except Exception as e:  # noqa
        print(f"  验证异常 {cand['name'][:36]}: {str(e)[:80]}")
        return False, None, None, 0
    if (r.get("Code") not in (0, "0", 200)) and (r.get("code") not in (0, "0", 200)):
        print(f"  验证被拒 {cand['name'][:36]}: {str(r)[:80]}")
        return False, None, None, 0
    lst = (r.get("Data") or {}).get("CommodityList") or []
    prices = []
    for c in lst:
        try:
            prices.append(float(str(c.get("Price")).replace("¥", "")))
        except (TypeError, ValueError):
            pass
    if not prices:
        return False, None, None, 0
    fresh_ask = round(min(prices), 2)
    steam = cand["steam_sell"]
    fresh_ratio = round(fresh_ask / (steam * NET), 3)
    ok = fresh_ratio <= cfg.get("ratio_max", 0.70)
    # 深度:所有 ≤ 阈值线的档位都可买(卖三卖四达标也吃)
    line = cfg.get("ratio_max", 0.70) * steam * NET
    cap = cfg.get("buy_max_per_item", 3)
    qty = sum(1 for p in prices if p <= line + 0.005)
    qty = min(qty, cap) if cap else qty
    return ok, fresh_ask, fresh_ratio, qty


def steam_cookie(cfg):
    """读取 Steam cookie(steam_cookie.txt 两行制):
    第 1 行 = steamLoginSecure(短效 24h 访问令牌,steamcommunity 域)
    第 2 行 = steamRefresh_steam(长效 ~200天 刷新令牌,login.steampowered.com 域)
    兼容旧行格式(仅 1 行)与完整 Cookie 头粘贴;返回第 1 行"""
    txt_p = os.path.join(HERE, "steam_cookie.txt")
    raw = ""
    if os.path.exists(txt_p):
        with open(txt_p, encoding="utf-8") as f:
            raw = f.read().strip()
    raw = raw or cfg.get("steam_cookie", "")
    if not raw:
        return ""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    access = lines[0] if lines else ""
    refresh = lines[1] if len(lines) > 1 else ""
    if "steamLoginSecure=" in access:
        for part in access.split(";"):
            part = part.strip()
            if part.startswith("steamLoginSecure="):
                access = part.split("=", 1)[1]
                break
    _STEAM_COOKIE["v"] = access or _STEAM_COOKIE["v"]
    _STEAM_COOKIE["refresh"] = refresh or _STEAM_COOKIE["refresh"]
    _STEAM_COOKIE["exp"] = _steam_decode_exp(_STEAM_COOKIE["v"])
    return access


def _steam_decode_exp(jwt_value):
    """解出 steamLoginSecure JWT 的 exp 时间戳,失败返回 0"""
    try:
        jwt = (jwt_value.split("%7C%7C", 1)[1] if "%7C%7C" in jwt_value
               else jwt_value.split("||", 1)[1])
        p = jwt.split(".")[1]
        p += "=" * (-len(p) % 4)
        return int(json.loads(base64.urlsafe_b64decode(p)).get("exp", 0))
    except Exception:  # noqa
        return 0


_STEAM_COOKIE = {"v": None, "refresh": None, "exp": 0, "expired": False}
_STEAM_429 = {"until": 0.0}  # 全局 429 熔断:撞到限频后 5 分钟内不再逐件重试(防每轮耗时爆炸)
_STEAM_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

_BUFF_COOKIE = {"v": None}
_BUFF_FAIL = {"until": 0.0}  # BUFF 域名不可达时全局熔断 5 分钟,避免每轮 7×20s 超时拖死调度


def buff_down():
    """BUFF API 是否处于熔断期"""
    return time.time() < _BUFF_FAIL["until"]


def buff_trip(reason):
    """标记 BUFF 不可达(5 分钟内不再逐件重试)"""
    if not buff_down():
        log(f"[buff] 接口不可达,本轮剩余商品跳过(熔断 5 分钟): {reason[:50]}")
    _BUFF_FAIL["until"] = time.time() + 300


def buff_session(cfg):
    """BUFF 登录态(buff_cookie.txt,session 值或完整 Cookie 头)"""
    if _BUFF_COOKIE["v"]:
        return _BUFF_COOKIE["v"]
    p = os.path.join(HERE, "buff_cookie.txt")
    raw = ""
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            raw = f.read().strip()
    raw = raw or cfg.get("buff_cookie", "")
    if not raw:
        return ""
    if "session=" in raw:
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith("session="):
                raw = part.split("=", 1)[1]
                break
    _BUFF_COOKIE["v"] = raw
    return raw


def buff_sell_order(cfg, goods_id):
    """BUFF 实时挂单簿 → (最低价, 在售档位数) | None(无cookie/失败/登录失效)"""
    if not goods_id or buff_down():
        return None
    ck = buff_session(cfg)
    if not ck:
        return None
    try:
        r = requests.get("https://api.buff.163.com/api/market/goods/sell_order",
                         params={"game": "csgo", "page_num": 1, "page_size": 20,
                                 "goods_id": goods_id},
                         headers={"Cookie": f"session={ck}", "User-Agent": _STEAM_UA},
                         timeout=20)
        if r.status_code != 200:
            log(f"[buff] sell_order HTTP {r.status_code}: goods {goods_id}")
            return None
        d = r.json()
        if d.get("code") not in (200, "200"):
            log(f"[buff] 响应异常: {str(d.get('error'))[:60]}(session 可能失效,重新抓)")
            return None
        items = (d.get("data") or {}).get("items") or []
        prices = []
        for i in items:
            try:
                prices.append(float(i.get("sell_price") or i.get("price")))
            except (TypeError, ValueError):
                pass
        if not prices:
            return None
        return round(min(prices), 2), len(prices)
    except Exception as e:  # noqa
        buff_trip(f"sell_order: {str(e)[:60]}")
        return None


def _buff_ids_cache():
    p = os.path.join(HERE, "buff_goods_ids.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return {}


def buff_goods_id(cfg, market_hash_name):
    """名称 → BUFF goods_id(磁盘缓存 data/buff_goods_ids.json,每名只搜一次)"""
    cache = _buff_ids_cache()
    if market_hash_name in cache:
        return cache[market_hash_name]
    if buff_down():
        return None
    ck = buff_session(cfg)
    if not ck:
        return None
    try:
        r = requests.get("https://api.buff.163.com/api/market/goods",
                         params={"game": "csgo", "page_num": 1, "page_size": 10,
                                 "keyword": market_hash_name},
                         headers={"Cookie": f"session={ck}", "User-Agent": _STEAM_UA},
                         timeout=20)
        time.sleep(2)  # BUFF 风控较严,统一 2s 间隔
        if r.status_code != 200:
            log(f"[buff] 搜索 HTTP {r.status_code}: {market_hash_name[:30]}")
            return None
        d = r.json()
        if d.get("code") not in (200, "200"):
            log(f"[buff] 搜索响应异常: {str(d.get('error'))[:60]}")
            return None
        for it in (d.get("data") or {}).get("items") or []:
            if it.get("market_hash_name") == market_hash_name and it.get("id"):
                cache[market_hash_name] = it["id"]
                try:
                    with open(os.path.join(HERE, "buff_goods_ids.json"), "w", encoding="utf-8") as f:
                        json.dump(cache, f, ensure_ascii=False)
                except Exception:  # noqa
                    pass
                return it["id"]
        log(f"[buff] 搜索无精确匹配: {market_hash_name[:36]}")
    except Exception as e:  # noqa
        buff_trip(f"搜索异常: {str(e)[:60]}")
    return None


def buff_buy_orders(cfg, goods_id):
    """BUFF 求购簿最高买一价 | None(无cookie/失败)"""
    if not goods_id or buff_down():
        return None
    ck = buff_session(cfg)
    if not ck:
        return None
    try:
        r = requests.get("https://api.buff.163.com/api/market/goods/buy_order",
                         params={"game": "csgo", "page_num": 1, "page_size": 5,
                                 "goods_id": goods_id},
                         headers={"Cookie": f"session={ck}", "User-Agent": _STEAM_UA},
                         timeout=20)
        time.sleep(2)
        if r.status_code != 200:
            log(f"[buff] buy_order HTTP {r.status_code}: goods {goods_id}")
            return None
        d = r.json()
        if d.get("code") not in (200, "200"):
            log(f"[buff] 求购簿响应异常: {str(d.get('error'))[:60]}")
            return None
        prices = []
        for i in (d.get("data") or {}).get("items") or []:
            try:
                prices.append(float(i.get("price")))
            except (TypeError, ValueError):
                pass
        return round(max(prices), 2) if prices else None
    except Exception as e:  # noqa
        buff_trip(f"buy_order: {str(e)[:60]}")
        return None


def _steam_save_cookie(new_ck):
    """新访问令牌落盘(保留第 2 行刷新令牌) + 更新进程内值,返回 True=发生了续期"""
    cookie_path = os.path.join(HERE, "steam_cookie.txt")
    _STEAM_COOKIE["v"] = new_ck
    _STEAM_COOKIE["exp"] = _steam_decode_exp(new_ck)
    _STEAM_COOKIE["expired"] = False
    try:
        rt = _STEAM_COOKIE.get("refresh") or ""
        with open(cookie_path, "w", encoding="utf-8") as f:
            f.write(new_ck + (f"\n{rt}" if rt else ""))
        log(f"[steam] steamLoginSecure 已自动续期(新令牌 {len(new_ck)} chars)")
        return True
    except Exception:  # noqa
        return False


def steam_refresh_session():
    """用第 2 行的长命凭证换新短效访问令牌(steamcommunity 域),按令牌形态分两路:
    A. steamRefresh_steam(旧版,login.steampowered.com 域):两步 ajaxrefresh→settoken
    B. .steampowered.com 长命 steamLoginSecure(2027,新版):GET /jwt/refresh 走重定向链
    返回 True=续期成功"""
    rt = _STEAM_COOKIE.get("refresh")
    if not rt:
        log("[steam] 无刷新令牌(steam_cookie.txt 第2行),无法自动续期")
        return False
    if not rt.startswith("25") and "ey" not in rt[:200]:
        pass  # 形态无法可靠区分,两路都试,谁成谁算
    # ---- 路 B:长命令牌走 /jwt/refresh 重定向链(新版机制) ----
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": _STEAM_UA})
        s.cookies.set("steamLoginSecure", rt, domain=".steampowered.com")
        r = s.get("https://login.steampowered.com/jwt/refresh?redir=https%3A%2F%2Fsteamcommunity.com",
                  timeout=30, allow_redirects=True)
        # 会话 cookie 里找 steamcommunity 域的新令牌
        for c in s.cookies:
            if c.name == "steamLoginSecure" and "steamcommunity" in (c.domain or "") \
                    and c.value != rt:
                return _steam_save_cookie(c.value)
        # 兜底:重定向链终点 HTML 里可能内嵌 settoken 链接
        m = re.search(r'https://steamcommunity\.com/login/settoken\?[^"\'\s]+', r.text or "")
        if m:
            s.get(m.group(0).replace("&amp;", "&"), timeout=30)
            for c in s.cookies:
                if c.name == "steamLoginSecure" and "steamcommunity" in (c.domain or ""):
                    return _steam_save_cookie(c.value)
        log(f"[steam] 路B(/jwt/refresh)未产出新令牌(状态{r.status_code})")
    except Exception as e:  # noqa
        log(f"[steam] 会话刷新路B失败: {str(e)[:60]}")
    # ---- 路 A:旧版两步 ajaxrefresh→settoken ----
    b1 = "----WebKitFormBoundaryUUYP730A"
    b2 = "----WebKitFormBoundaryUUYP730B"
    try:
        r1 = requests.post(
            "https://login.steampowered.com/jwt/ajaxrefresh",
            headers={"Content-Type": f"multipart/form-data; boundary={b1}",
                     "Origin": "https://steamcommunity.com",
                     "Referer": "https://steamcommunity.com/",
                     "User-Agent": _STEAM_UA},
            cookies={"steamRefresh_steam": rt},
            data=(f'--{b1}\r\nContent-Disposition: form-data; name="redir"\r\n\r\n'
                  f'https://steamcommunity.com\r\n--{b1}--\r\n'),
            timeout=30)
        j = r1.json()
    except Exception as e:  # noqa
        log(f"[steam] 会话刷新①失败: {str(e)[:60]}")
        return False
    if not all(k in j for k in ("steamID", "nonce", "auth")):
        log(f"[steam] 会话刷新①响应异常: {str(j)[:100]}(刷新令牌可能已失效,需重新抓)")
        return False
    try:
        r2 = requests.post(
            "https://steamcommunity.com/login/settoken",
            headers={"Content-Type": f"multipart/form-data; boundary={b2}",
                     "Origin": "https://steamcommunity.com",
                     "Referer": "https://steamcommunity.com",
                     "User-Agent": _STEAM_UA},
            data=(f'--{b2}\r\nContent-Disposition: form-data; name="steamID"\r\n\r\n'
                  f'{j["steamID"]}\r\n--{b2}\r\n'
                  f'Content-Disposition: form-data; name="nonce"\r\n\r\n'
                  f'{j["nonce"]}\r\n--{b2}\r\n'
                  f'Content-Disposition: form-data; name="redir"\r\n\r\n'
                  f'{j["redir"]}\r\n--{b2}\r\n'
                  f'Content-Disposition: form-data; name="auth"\r\n\r\n'
                  f'{j["auth"]}\r\n--{b2}--\r\n'),
            timeout=30)
        sc = r2.headers.get("Set-Cookie") or ""
        if "steamLoginSecure=" in sc:
            new_ck = sc.split("steamLoginSecure=")[1].split(";")[0]
            return _steam_save_cookie(new_ck)
        log("[steam] 会话刷新②未返回新令牌")
        return False
    except Exception as e:  # noqa
        log(f"[steam] 会话刷新②失败: {str(e)[:60]}")
        return False


def steam_pricehistory(cookie, market_hash_name):
    """Steam pricehistory:返回近7日成交参考价(成交量加权均值),无数据返回 None
    429 → 退避 60s 重试两次;302 → 刷新令牌续期一次并重试"""
    if cookie:  # 首次调用以显式传入为准
        _STEAM_COOKIE["v"] = cookie
    cookie = cookie or _STEAM_COOKIE["v"]
    if not cookie:
        return None
    # 全局熔断:一次限频后本轮剩余商品直接跳过,避免每件都退避 120s 把单轮拖过任务时限
    if time.time() < _STEAM_429["until"]:
        return None
    url = (f"https://steamcommunity.com/market/pricehistory/"
           f"?appid=730&market_hash_name={quote(market_hash_name)}")
    headers = {"Cookie": f"steamLoginSecure={cookie}", "User-Agent": _STEAM_UA}
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=30, headers=headers, allow_redirects=False)
            # 续期捕获:响应携带新令牌则更新进程内值并落盘
            new_ck = r.cookies.get("steamLoginSecure")
            if new_ck and new_ck != cookie:
                _steam_save_cookie(new_ck)
            if r.status_code == 429:
                # 首件撞限频:只退避一次(60s),仍限则全局熔断 5 分钟;后续商品本轮不再拉
                log(f"[steam] 429 限频,退避 60s 重试({attempt + 1}/2): {market_hash_name[:30]}")
                if attempt >= 1:
                    _STEAM_429["until"] = time.time() + 300
                    log("[steam] 429 持续,本轮剩余商品跳过成交价拉取(全局熔断 5 分钟)")
                    return None
                time.sleep(60)
                continue
            if r.status_code == 302:
                if attempt == 0 and steam_refresh_session():
                    headers["Cookie"] = f"steamLoginSecure={_STEAM_COOKIE['v']}"
                    cookie = _STEAM_COOKIE["v"]
                    continue  # 续期成功,重试本次请求
                _STEAM_COOKIE["expired"] = True
                log("[steam] cookie 已失效且刷新失败,成交价列停用(需重新抓双令牌)")
                return None
            if r.status_code != 200:
                log(f"[steam] pricehistory {r.status_code}: {market_hash_name[:30]}")
                return None
            data = r.json()
            if not data.get("success"):
                return None
            break
        except Exception as e:  # noqa
            log(f"[steam] 异常: {str(e)[:60]}")
            return None
    else:
        return None
    now = datetime.now()
    w_sum, w_cnt = 0.0, 0.0
    for entry in data.get("prices") or []:
        ds, ps, vs = entry[0], entry[1], entry[2] if len(entry) > 2 else "1"
        try:
            d = datetime.strptime(ds, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            try:
                d = datetime.strptime(ds.replace(" +0", ""), "%b %d %Y %H:")
            except ValueError:
                continue
        if (now - d).total_seconds() > 7 * 86400:
            continue
        vol = float(vs or 1)
        w_sum += float(ps) * vol
        w_cnt += vol
    return round(w_sum / w_cnt, 2) if w_cnt > 0 else None


def run_once(cfg, uu, pages=8, max_verify=20, cooldown_hours=24):
    """供 10 分钟调度调用的轻量扫描:冷却去重后只验新面孔,返回实时确认标的列表
    (与 main() 共用扫描/验证/分类逻辑,冷却状态文件共用)"""
    cfg.setdefault("ratio_max", 0.70)
    cfg.setdefault("min_price", 10)
    cfg.setdefault("min_turnover", 10)
    cands, scanned = scan_candidates(cfg, pages)
    if not cands:
        return []
    cands.sort(key=lambda x: x["ratio"])

    state_p = os.path.join(HERE, "auto_buy_state.json")
    state = {}
    if os.path.exists(state_p):
        with open(state_p, encoding="utf-8") as f:
            state = json.load(f)
    now_ts = time.time()

    verified = []
    for c in cands[:max_verify]:
        tid = c["template_id"]
        last = state.get("t_" + tid)
        if last and now_ts - last.get("ts", 0) < cooldown_hours * 3600:
            continue
        if c.get("venue") == "BUFF":
            rt = buff_sell_order(cfg, c.get("buff_id"))  # BUFF 实时挂单簿(需 buff_cookie.txt)
            if rt:
                c["fresh_ask"] = rt[0]
                c["qty"] = min(rt[1], cfg.get("buy_max_per_item", 3) or rt[1])
                c["fresh_ratio"] = round(c["fresh_ask"] / (c["steam_sell"] * NET), 3)
                c["snap"] = False
                ok = c["fresh_ratio"] <= cfg.get("ratio_max", 0.70)
            else:
                # cookie 缺失/接口失败 → csqaq 快照兜底(标 ≈)
                c["fresh_ask"], c["fresh_ratio"] = c["buff_sell"], c["ratio_buff"]
                c["snap"], c["qty"], ok = True, 1, True
            if ok:
                verified.append(c)
                state["t_" + tid] = {"ts": now_ts, "ratio": c["fresh_ratio"]}
            time.sleep(2)
            continue
        ok, fresh_ask, fresh_ratio, qty = verify_fresh(uu, c, cfg)
        c["fresh_ask"], c["fresh_ratio"], c["qty"] = fresh_ask, fresh_ratio, qty
        if ok:
            verified.append(c)
            state["t_" + tid] = {"ts": now_ts, "ratio": fresh_ratio}
        time.sleep(2.5)
    with open(state_p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

    for c in verified:  # 形态分类 + 长周期趋势信号(单次 180 天曲线调用)
        try:
            pairs = chart_pairs(cfg, c["csqaq_id"], cfg.get("decay_window_days", 180))
            # 形态分类器保持原 30 天语义(近 30 天切片)
            tail30 = [(dt, p) for dt, p in pairs
                      if (pairs[-1][0] - dt).days <= 30] if pairs else []
            c["curve"] = classify_curve(tail30 or pairs, steam_now=c.get("steam_sell"))
            c["trend"] = trend_signal(pairs, cfg)
        except Exception:  # noqa
            c["curve"] = {"cls": "数据不足", "jump_pct": None, "days": None, "detail": ""}
            c["trend"] = {"weekly_decay": None, "spike": False, "spike_base": None}
        time.sleep(1.5)

    cookie = steam_cookie(cfg)  # 成交价7:买入决策的现实退出基准
    if cookie:
        # 主动续期:访问令牌剩余寿命 <6h 或已过期时,用刷新令牌换新
        if time.time() > _STEAM_COOKIE.get("exp", 0) - 6 * 3600:
            log("[steam] 访问令牌临期/过期,尝试用刷新令牌续期...")
            steam_refresh_session()
    # deal7 拉取:按比值优先,每轮限量(防 Steam 限频 429),未拉到的留空下轮再试
    if cookie:
        deal_targets = sorted(verified, key=lambda x: x["fresh_ratio"])
        max_deal = cfg.get("deal7_max_per_round", 12)
        for i, c in enumerate(deal_targets):
            if i >= max_deal:
                c["deal7"] = None
                continue
            c["deal7"] = steam_pricehistory(cookie if i == 0 else None, c["name"])
            time.sleep(5)
        for c in verified:
            c.setdefault("deal7", None)
    # 修正口径:把周跌幅×持有期 + 尖峰回落从退出基准扣掉,得到最终决策比值
    for c in verified:
        apply_signal(c, cfg)
    if cfg.get("signal_filter_enable", True) and cfg.get("spike_guard", "reprice") != "warn":
        sig_max = cfg.get("signal_filter_max", 0.75)
        guard = cfg.get("spike_guard", "reprice")  # reprice=按回落基准重算 | filter=尖峰全剔 | warn=只标不过滤
        before = len(verified)
        kept = []
        for c in verified:
            if guard == "filter" and c.get("spike"):
                continue
            rs = c.get("ratio_signal")
            if rs is not None and rs > sig_max:
                continue  # 修正后性价比不够 → 不推(防"尖峰虚高/阴跌吃掉利润")
            kept.append(c)
        if before - len(kept):
            log(f"修正口径过滤: 剔除 {before - len(kept)}/{before} 件(比值>{sig_max} 或尖峰)")
        verified = kept
    # cookie 失效且刷新也失败 → 微信推送提醒(每轮失效期只提醒一次,续期成功自动解除)
    if cookie and _STEAM_COOKIE.get("expired") and not state.get("steam_cookie_expired"):
        state["steam_cookie_expired"] = True
        try:
            from watcher_inv import notify
            notify(cfg, "Steam 会话无法自动续期",
                   "成交价7列已停用\n请重新抓取两个 cookie 覆盖保存到 skin-index/steam_cookie.txt:\n"
                   "第1行=steamLoginSecure(steamcommunity域)\n"
                   "第2行=steamRefresh_steam(login.steampowered.com域)")
        except Exception:  # noqa
            pass
    if not _STEAM_COOKIE.get("expired"):
        state.pop("steam_cookie_expired", None)
    return verified


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan"], nargs="?", default="scan")
    ap.add_argument("--pages", type=int, default=8)
    ap.add_argument("--mode", choices=["report", "qiugou"], default="report")
    ap.add_argument("--yes", action="store_true", help="跳过确认")
    ap.add_argument("--max-verify", type=int, default=20)
    ap.add_argument("--cooldown-hours", type=int, default=24)
    args = ap.parse_args()

    cfg = load_cfg()
    cfg.setdefault("ratio_max", 0.70)
    cfg.setdefault("min_price", 10)
    cfg.setdefault("min_turnover", 10)

    uu = UUClient(cfg["uu_token"], cfg.get("uu_device_token", ""))
    cookie = steam_cookie(cfg)
    log(f"== 收购扫描开始 (pages={args.pages}, mode={args.mode}) =="
        + (" [steam成交价: 开启]" if cookie else " [steam成交价: 未配置cookie,跳过]"))

    cands, scanned = scan_candidates(cfg, args.pages)
    if not cands:
        log("本轮无候选")
        return
    cands.sort(key=lambda x: x["ratio"])

    # 冷却去重
    state_p = os.path.join(HERE, "auto_buy_state.json")
    state = {}
    if os.path.exists(state_p):
        with open(state_p, encoding="utf-8") as f:
            state = json.load(f)
    now_ts = time.time()

    verified, skipped_cool = [], []
    for c in cands[:args.max_verify]:
        tid = c["template_id"]
        last = state.get("t_" + tid)
        if last and now_ts - last.get("ts", 0) < args.cooldown_hours * 3600:
            skipped_cool.append(c["name"])
            continue
        if c.get("venue") == "BUFF":
            rt = buff_sell_order(cfg, c.get("buff_id"))  # BUFF 实时挂单簿
            if rt:
                c["fresh_ask"] = rt[0]
                c["qty"] = min(rt[1], cfg.get("buy_max_per_item", 3) or rt[1])
                c["fresh_ratio"] = round(c["fresh_ask"] / (c["steam_sell"] * NET), 3)
                c["snap"] = False
                ok = c["fresh_ratio"] <= cfg.get("ratio_max", 0.70)
            else:
                c["fresh_ask"], c["fresh_ratio"] = c["buff_sell"], c["ratio_buff"]
                c["snap"], c["qty"], ok = True, 1, True
            if ok:
                verified.append(c)
                state["t_" + tid] = {"ts": now_ts, "ratio": c["fresh_ratio"]}
            time.sleep(2)
            continue
        ok, fresh_ask, fresh_ratio, qty = verify_fresh(uu, c, cfg)
        c["fresh_ask"], c["fresh_ratio"], c["qty"] = fresh_ask, fresh_ratio, qty
        c["verified"] = ok
        if ok:
            verified.append(c)
            state["t_" + tid] = {"ts": now_ts, "ratio": fresh_ratio}
        time.sleep(2.5)

    log(f"验证完成: 候选 {len(cands)} → 实时确认 {len(verified)}"
        + (f"(冷却跳过 {len(skipped_cool)})" if skipped_cool else ""))

    # 冷却状态落盘(之前只在内存更新,冷却从未生效)
    with open(state_p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

    # CSV


    # Steam 曲线形态分类 + 长周期趋势信号(单次 180 天曲线调用)
    for c in verified:
        try:
            pairs = chart_pairs(cfg, c["csqaq_id"], cfg.get("decay_window_days", 180))
            tail30 = [(dt, p) for dt, p in pairs
                      if (pairs[-1][0] - dt).days <= 30] if pairs else []
            c["curve"] = classify_curve(tail30 or pairs, steam_now=c.get("steam_sell"))
            c["trend"] = trend_signal(pairs, cfg)
        except Exception as e:  # noqa
            c["curve"] = {"cls": "数据不足", "jump_pct": None, "days": None, "detail": str(e)[:40]}
            c["trend"] = {"weekly_decay": None, "spike": False, "spike_base": None}
        time.sleep(1.5)

    # Steam 真实成交价(近7日成交量加权均值,pricehistory 需 cookie)
    if cookie:
        log(f"拉取 Steam 近7日成交价({len(verified)} 件,限速约3.5s/件)...")
        for i, c in enumerate(verified):
            c["deal7"] = steam_pricehistory(cookie if i == 0 else None, c["name"])
            time.sleep(3.5)
        got = sum(1 for c in verified if c.get("deal7"))
        log(f"成交价获取完成: {got}/{len(verified)}")
    else:
        for c in verified:
            c["deal7"] = None
    # 独立脚本也用同一修正口径(展示与调度一致),不做过滤(调试要看全量)
    for c in verified:
        apply_signal(c, cfg)

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    os.makedirs(REPORT_DIR, exist_ok=True)
    csv_path = os.path.join(REPORT_DIR, f"buy_scan_{stamp}.csv")

    # 控制台报告(等宽对齐)
    headers = ["比值(修正)", "跌/周", "比值(买)", "场地", "求购比", "UUYP卖一", "BUFF卖一",
               "Steam卖一", "成交价7", "毛利(修正)", "形态", "品种"]
    aligns = ["r", "r", "r", "l", "r", "r", "r", "r", "r", "r", "l", "l"]
    cells = []
    for c in verified:
        ask = c["fresh_ask"]
        deal = c.get("deal7")
        exp = c.get("exit_expected")
        p_fix = round(float(exp) * NET - ask, 2) if exp else None  # 修正后退出基准的毛利
        venue = c.get("venue", "UUYP")
        venue_txt = venue + ("≈" if c.get("snap") else "")
        if c.get("qty") and c["qty"] > 1:
            venue_txt += f"×{c['qty']}"
        ba = c.get("bid_ask")
        dec = (c.get("trend") or {}).get("weekly_decay")
        rs = c.get("ratio_signal")
        rtxt = (f"{rs:.3f}" + ("尖" if c.get("spike") else "")) if rs is not None else "-"
        cls = c["curve"]["cls"] + ("·尖峰" if c.get("spike") else "")
        cells.append([rtxt,
                      (f"{dec:.2f}%" if dec is not None else "-"),
                      str(c["fresh_ratio"]), venue_txt,
                      (f"{ba:.2f}" if ba is not None else "-"),
                      str(c.get("yyyp_ask") or c.get("fresh_ask")), str(c.get("buff_sell") or "-"),
                      str(c["steam_sell"]),
                      (f"{deal:.2f}" if deal else "-"),
                      (f"{p_fix:.2f}" if p_fix is not None else "-"),
                      trunc(cls, 20), trunc(c["name"], 34)])
    print(f"\n== 确认收购标的({len(verified)} 件) ==")
    for ln in fmt_table(headers, cells, aligns):
        print(ln)
    print("口径:比值(修正)=成本÷[min(成交价7/基准,尖峰回落基准)×(1−周跌幅×持有周数)×0.8696] 最终决策 | "
          "跌/周=180天周中位价 Theil-Sen 稳健斜率 | 尖=短期急涨按回落基准估值 | "
          "比值(买)=场地卖一÷(Steam卖一×0.8696) 预过滤 | 毛利(修正)=修正退出到手−买价")
    all_rows = cands[:args.max_verify]
    if all_rows:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
    print(f"\n[csv] {csv_path}")

    # 求购单模式
    if args.mode == "qiugou" and verified:
        if not args.yes:
            ans = input("对以上标的挂求购单? 输入 yes 执行: ").strip().lower()
            if ans != "yes":
                print("已取消")
                return
        budget = cfg.get("buy_budget_per_item", 0)
        ok_count = 0
        for c in verified:
            if c.get("venue") == "BUFF":
                log(f"求购单跳过(BUFF场地暂不支持求购): {c['name'][:36]}")
                continue
            target = c["fresh_ask"]  # 求购价 = 当前卖一(买到即达标)
            qty = max(1, c.get("qty") or 1)
            if budget and target > budget:
                print(f"  跳过(超单品预算): {c['name'][:36]}")
                continue
            r = uu.call("POST", "/api/youpin/bff/trade/purchase/order/savePurchaseOrder",
                        data={"templateId": int(c["template_id"]),
                              "templateHashName": c["name"],
                              "commodityName": c["cn_name"] or c["name"],
                              "purchasePrice": target, "purchaseNum": qty,
                              "needPaymentAmount": round(target * qty, 2),
                              "totalAmount": round(target * qty, 2),
                              "incrementServiceCode": [1001],
                              "priceDifference": 0, "discountAmount": 0,
                              "payConfirmFlag": False, "repeatOrderCancelFlag": False})
            okc = r.get("Code") in (0, "0", 200) or r.get("code") in (0, "0", 200)
            log(f"求购单 {c['name'][:36]} @{target}: {'OK' if okc else str(r)[:80]}")
            ok_count += okc
            time.sleep(2)
        log(f"求购单提交完成: {ok_count}/{len(verified)}")


if __name__ == "__main__":
    main()
