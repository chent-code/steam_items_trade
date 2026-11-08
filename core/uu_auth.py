#!/usr/bin/env python3
"""UUYP 认证与最小客户端 —— 逐字段复刻 Steamauto 当前可用实现。

关键点(相对 PyPI 精简版的修复):
  1. 所有请求(含登录)携带完整客户端指纹头(App-Version 5.28.3 / uk / DeviceToken / Device-Info 等)
  2. SendSignInSmsCode 请求体含 Area:86 与 Code:""
  3. deviceToken 全程一致(DeviceToken=DeviceId=Sessionid)
"""
import json
import random
import time

import json
import os
import requests

BASE = "https://api.youpin898.com"


def rand(n, upper=False):
    pool = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    s = "".join(random.choice(pool) for _ in range(n))
    return s.upper() if upper else s


def generate_headers(device_token, token=""):
    """与 Steamauto uuyoupinapi 当前版本逐字段一致"""
    return {
        "uk": rand(65),
        "authorization": "Bearer " + token,
        "content-type": "application/json; charset=utf-8",
        "user-agent": "okhttp/3.14.9",
        "App-Version": "5.28.3",
        "AppType": "4",
        "deviceType": "1",
        "package-type": "uuyp",
        "DeviceToken": device_token,
        "DeviceId": device_token,
        "platform": "android",
        "accept-encoding": "gzip",
        "Gameid": "730",
        "Device-Info": json.dumps({
            "deviceId": device_token,
            "deviceType": device_token,
            "hasSteamApp": 1,
            "requestTag": rand(32, upper=True),
            "systemName ": "Android",
            "systemVersion": "15",
        }, ensure_ascii=False),
    }


class UUClient:
    """已登录会话:库存 / 购入订单"""

    def __init__(self, token, device_token):
        self.token = token
        self.dt = device_token
        self.s = requests.Session()
        self.s.headers.update(generate_headers(device_token, token))
        info = self.call("GET", "/api/user/Account/getUserInfo")
        d = info.get("Data") or info.get("data") or {}
        self.nickname = d.get("NickName", "?")
        self.user_id = d.get("UserId")
        if not self.user_id:
            raise RuntimeError(f"token 校验失败: {info}")

    def call(self, method, path, data=None):
        url = BASE + path
        for attempt in range(3):
            r = self.s.request(method, url, json=data, timeout=30)
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"[uu] 429 限频,等待 {wait}s 后重试({attempt + 1}/2): {path}")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"429 持续,重试 2 次仍未成功: {path}")

    def inventory(self, refresh=False):
        data = {"pageIndex": 1, "pageSize": 100, "AppType": 4, "IsMerge": 0,
                "Sessionid": self.dt}
        if refresh:
            data["IsRefresh"] = True
            data["RefreshType"] = 2
        rsp = self.call("POST", "/api/commodity/Inventory/GetUserInventoryDataListV3", data)
        body = rsp.get("Data") or rsp.get("data") or {}
        return body.get("ItemsInfos") or []

    def wait_deliver_list(self, debug_dir=None):
        """待发货订单,双通道取并集(按 orderNo 去重),互为兜底:
        A: 专用端点 waitDeliverList(结构最准)
        B: 全量 sell/list 按 orderStatusName 文本客户端过滤
           (外层 orderStatus=140 过滤不可靠:实测 pending 时也可能返回空)
        → (pending, cancels)
        pending: [{order_no, name, price, offer_type, offer_id, status, buyer_uid}]
        cancels: {buyerUserId: 已取消单数}(供惯犯标记)"""
        out, seen = {}, set()
        cancels = {}
        # 通道 A: 专用端点(pageSize 上限 20,超限返回空!)
        try:
            r = self.call("POST", "/api/youpin/bff/trade/sell/page/v1/waitDeliver/waitDeliverList",
                          data={"gameId": 730, "pageIndex": 1, "pageSize": 20, "Sessionid": self.dt})
            d = r.get("Data") or r.get("data") or {}
            for it in (d.get("waitDeliverList") or []):
                ov, cv = it.get("orderInfoVO") or {}, it.get("commodityInfoVO") or {}
                no = str(ov.get("orderNo") or "")
                if no:
                    seen.add(no)
                    out[no] = {"order_no": no,
                               "name": cv.get("commodityName") or cv.get("commodityHashName") or "?",
                               "price": ov.get("amount") or ov.get("price"),
                               "offer_type": ov.get("offerType"),
                               "offer_id": ov.get("tradeOfferId") or ov.get("steamTradeOfferId"),
                               "status": "待发货",
                               "buyer_uid": None}
        except Exception as e:  # noqa
            print(f"[uu] 待发货通道A失败: {str(e)[:60]}")
        # 通道 B: 全量订单客户端过滤(pageSize 上限 20,超限返回空!)
        try:
            r = self.call("POST", "/api/youpin/bff/trade/sale/v1/sell/list",
                          data={"keys": "", "orderStatus": "", "pageIndex": 1,
                                "pageSize": 20, "Sessionid": self.dt})
            d = r.get("Data") or r.get("data") or {}
            for o in (d.get("orderList") or []):
                st_txt = o.get("orderStatusName") or ""
                # 取消买家统计(惯犯标记用,须在 pending 过滤之前)
                if o.get("orderStatusName") == "已取消" and o.get("buyerUserId"):
                    uid = str(o["buyerUserId"])
                    cancels[uid] = cancels.get(uid, 0) + 1
                if o.get("orderStatus") != 140 and "待发货" not in st_txt:
                    continue
                no = str(o.get("orderNo") or "")
                if no and no not in seen:
                    seen.add(no)
                    pd = o.get("productDetail") or {}
                    out[no] = {"order_no": no,
                               "name": pd.get("commodityName") or "?",
                               "price": o.get("amount") or o.get("price") or o.get("orderAmount"),
                               "offer_type": o.get("offerType"),
                               "offer_id": o.get("tradeOfferId"),
                               "status": st_txt or "待发货",
                               "buyer_uid": str(o.get("buyerUserId") or "")}
        except Exception as e:  # noqa
            print(f"[uu] 待发货通道B失败: {str(e)[:60]}")
        if debug_dir:
            os.makedirs(debug_dir, exist_ok=True)
            with open(os.path.join(debug_dir, "wait_deliver_raw.json"), "w", encoding="utf-8") as f:
                json.dump({"pending": list(out.values()), "cancels": cancels},
                          f, ensure_ascii=False, indent=1)
        return list(out.values()), cancels

    def get_sell_list(self, game_id=730, debug_dir=None):
        """当前在售列表 → [{commodity_id, steam_asset_id, price, name}](字段防御性提取)"""
        r = self.call("POST", "/api/youpin/bff/new/commodity/v1/commodity/list/sell",
                      data={"gameId": game_id, "pageIndex": 1, "pageSize": 100,
                            "Sessionid": self.dt})
        if debug_dir:
            os.makedirs(debug_dir, exist_ok=True)
            with open(os.path.join(debug_dir, "sell_list_raw.json"), "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=1)
        data = r.get("Data") or r.get("data") or {}
        items = (data.get("commodityInfoList") or data.get("Items")
                 or data.get("items") or data.get("ItemList") or [])
        out = []
        for it in items:
            cid = it.get("Id") or it.get("id") or it.get("CommodityId")
            sid = str(it.get("SteamAssetId") or it.get("steamAssetId") or "")
            raw_price = it.get("Price") or it.get("price") or it.get("sellAmount")
            try:
                price = float(str(raw_price).replace("¥", "")) if raw_price else None
            except (TypeError, ValueError):
                price = None
            name = (it.get("CommodityName") or it.get("CommodityHashName")
                    or it.get("name") or "")
            if cid:
                out.append({"commodity_id": cid, "steam_asset_id": sid,
                            "price": price, "name": name})
        return out

    def change_price(self, assets):
        """批量改价。assets: {UUYP商品Id: 新价格(元)}"""
        item_infos = [{"CommodityId": int(cid), "Price": str(price),
                       "Remark": None, "IsCanSold": True}
                      for cid, price in assets.items()]
        return self.call("PUT", "/api/commodity/Commodity/PriceChangeWithLeaseV2",
                         data={"Commoditys": item_infos})

    def off_shelf(self, commodity_ids):
        ids = ",".join(str(i) for i in commodity_ids)
        return self.call("PUT", "/api/commodity/Commodity/OffShelf",
                         data={"Ids": ids, "IsDeleteCommodityCache": 1,
                               "IsForceOffline": True})

    def sell_items(self, assets):
        """批量上架出售。assets: {SteamAssetId: 价格(元)} → 成功上架数量"""
        item_infos = [{"AssetId": asset, "Price": assets[asset], "Remark": None}
                      for asset in assets]
        rsp = self.call("POST", "/api/commodity/Inventory/SellInventoryWithLeaseV2",
                        data={"GameID": 730, "ItemInfos": item_infos})
        success, failed = 0, []
        for c in rsp.get("Data") or []:
            if c.get("Status") == 1:
                success += 1
            else:
                failed.append(str(c.get("AssetId")))
                print(f"  上架失败 AssetId={c.get('AssetId')}: {c.get('Remark')}")
        return success, failed

    def buy_costs(self, max_pages=6):
        """已完成购入订单 → (costs_name, costs_asset)。price 分→元。"""
        costs_name, costs_asset = {}, {}
        page = 1
        while page <= max_pages:
            r = self.call("POST", "/api/youpin/bff/trade/sale/v1/buy/list", data={
                "keys": "", "orderStatus": 340, "pageIndex": page, "pageSize": 20,
                "presenterId": 0, "sceneType": 0, "Sessionid": self.dt})
            data = r.get("Data") or r.get("data") or {}
            orders = data.get("orderList") or []
            if not orders:
                break
            for od in orders:
                if od.get("orderStatusName") != "已完成":
                    continue
                plist = od.get("productDetailList") or []
                if len(plist) <= 3:
                    for p in plist:
                        name, price = p.get("commodityName"), p.get("price")
                        aid = p.get("assertId") or p.get("commodityId")
                        if name and price:
                            costs_name[name] = float(price) / 100.0
                        if aid and price:
                            costs_asset[str(aid)] = float(price) / 100.0
                else:  # 大宗订单走批量详情
                    d = self.call("POST", "/api/youpin/bff/trade/v1/order/query/detail",
                                  data={"orderNo": str(od.get("orderId")),
                                        "userId": self.user_id,
                                        "Sessionid": self.dt})
                    for vo in (d.get("Data") or {}).get("userCommodityVOList") or []:
                        for c in vo.get("commodityVOList") or []:
                            name, price = c.get("name"), c.get("price")
                            aid = c.get("id")
                            if name and price:
                                costs_name[name] = float(price)
                            if aid and price:
                                costs_asset[str(aid)] = float(price)
                    time.sleep(2)
            if len(orders) < 20:
                break
            page += 1
            time.sleep(1)
        return costs_name, costs_asset


def sms_up_config(device_token):
    """上行短信验证配置 → {"SmsUpNumber": 号码, "SmsUpContent": 内容, ...}(预登录可调)"""
    headers = generate_headers(device_token)
    r = requests.get(BASE + "/api/user/Auth/GetSmsUpSignInConfig",
                     headers=headers, timeout=20).json()
    return r


def sms_up_sign_in(phone, device_token):
    """上行短信校验:用户发出短信后轮询此接口 → 成功时 Data.Token"""
    headers = generate_headers(device_token)
    r = requests.post(BASE + "/api/user/Auth/SmsUpSignIn",
                      json={"Area": 86, "Code": "", "DeviceName": device_token,
                            "Sessionid": device_token, "Mobile": phone},
                      headers=headers, timeout=20).json()
    d = r.get("Data") or r.get("data") or {}
    return d.get("Token"), r


def sms_login(phone):
    """短信验证码登录 → (token, device_token)。deviceToken 需与后续请求一致。"""
    device_token = rand(32)
    headers = generate_headers(device_token)
    r = requests.post(BASE + "/api/user/Auth/SendSignInSmsCode",
                      json={"Area": 86, "Mobile": phone, "Sessionid": device_token,
                            "Code": ""},
                      headers=headers, timeout=20).json()
    print("发送验证码:", r.get("Msg") or r.get("msg") or r)
    code = input("输入验证码: ").strip()
    r2 = requests.post(BASE + "/api/user/Auth/SmsSignIn",
                       json={"Area": 86, "Code": code, "DeviceName": device_token,
                             "Sessionid": device_token, "Mobile": phone},
                       headers=headers, timeout=20).json()
    d = r2.get("Data") or r2.get("data") or {}
    token = d.get("Token")
    if not token:
        raise RuntimeError(f"登录失败,原始返回: {r2}")
    return token, device_token
