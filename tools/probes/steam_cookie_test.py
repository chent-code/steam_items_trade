#!/usr/bin/env python3
"""Steam cookie 有效性测试:调一次 pricehistory,打印状态码与结果"""
import json
import os
import sys

import requests
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    txt_p = os.path.join(HERE, "steam_cookie.txt")
    if not os.path.exists(txt_p):
        print("未找到 steam_cookie.txt")
        sys.exit(1)
    cookie = open(txt_p, encoding="utf-8").read().strip()
    print(f"cookie 长度: {len(cookie)} | 前缀: {cookie[:20]}...")

    name = "AK-47 | Redline (Field-Tested)"
    url = (f"https://steamcommunity.com/market/pricehistory/"
           f"?appid=730&market_hash_name={quote(name)}")
    try:
        r = requests.get(url, timeout=30, headers={
            "Cookie": f"steamLoginSecure={cookie}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    except Exception as e:  # noqa
        print(f"网络异常: {str(e)[:100]}")
        sys.exit(1)
    print(f"HTTP 状态码: {r.status_code}")
    if r.status_code != 200:
        print("失败:非 200,cookie 可能失效或未登录 Steam 社区市场")
        sys.exit(1)
    data = r.json()
    if not data.get("success"):
        print("失败:接口返回 success=false,cookie 可能需要重新复制")
        sys.exit(1)
    prices = data.get("prices") or []
    print(f"成功! 共 {len(prices)} 条历史成交记录")
    # 打印最近3条
    for entry in prices[-3:]:
        print(f"  {entry[0]}  价格 {entry[1]}  成交量 {entry[2] if len(entry) > 2 else '?'}")
    print("\ncookie 有效,可以跑 UUYP_buy_scan.bat 了")


if __name__ == "__main__":
    main()
