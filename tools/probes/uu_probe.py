#!/usr/bin/env python3
"""SendSignInSmsCode 指纹变体探针:失败不发短信,安全迭代;成功即停并保存会话。"""
import json
import os

import requests

from uu_auth import generate_headers, rand

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://api.youpin898.com"
PHONE = "15527209138"


def variant(name, ver, apptype="4", platform="android", dev_info=True, ua=None):
    dt = rand(32)
    h = generate_headers(dt)
    h["App-Version"] = ver
    h["AppType"] = apptype
    h["platform"] = platform
    if not dev_info:
        h.pop("Device-Info", None)
    if ua:
        h["user-agent"] = ua
    return name, dt, h


VARIANTS = [
    variant("V0 基线 5.28.3(复现)", "5.28.3"),
    variant("V1 5.49.1", "5.49.1"),
    variant("V2 5.52.0", "5.52.0"),
    variant("V3 6.0.0", "6.0.0"),
    variant("V4 5.49.1 iOS(AppType3)", "5.49.1", apptype="3", platform="ios"),
    variant("V5 5.49.1 无Device-Info", "5.49.1", dev_info=False),
    variant("V6 5.49.1 浏览器UA", "5.49.1",
            ua="Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36"),
]

PAYLOADS = [
    ("P1 Area+Code空", lambda dt: {"Area": 86, "Mobile": PHONE, "Sessionid": dt, "Code": ""}),
    ("P2 +DeviceToken", lambda dt: {"Area": 86, "Mobile": PHONE, "Sessionid": dt,
                                    "Code": "", "DeviceToken": dt, "DeviceId": dt}),
]


def main():
    results = []
    for name, dt, headers in VARIANTS:
        for pname, payload_fn in PAYLOADS:
            try:
                r = requests.post(BASE + "/api/user/Auth/SendSignInSmsCode",
                                  json=payload_fn(dt), headers=headers, timeout=20)
                try:
                    body = r.json()
                except Exception:  # noqa
                    body = r.text[:150]
                msg = body.get("Msg") or body.get("msg") or body if isinstance(body, dict) else body
                code = body.get("Code", body.get("code", "?")) if isinstance(body, dict) else "?"
                sent = ("成功" in str(msg)) or (code in (200, "200", 0, "0"))
                print(f"[{name} / {pname}] HTTP {r.status_code} Code={code} → {str(msg)[:80]}"
                      + ("   ★★★ 短信已发出!" if sent else ""))
                results.append((name, pname, sent, dt, headers))
                if sent:
                    with open(os.path.join(HERE, "uu_login_session.json"), "w", encoding="utf-8") as f:
                        json.dump({"device_token": dt, "headers": headers}, f, ensure_ascii=False, indent=1)
                    print(f"\n== 会话已保存到 uu_login_session.json,用同款头完成 SmsSignIn ==")
                    return
            except Exception as e:  # noqa
                print(f"[{name} / {pname}] 异常: {e}")
            import time
            time.sleep(1.5)
    print("\n== 全部变体被拦。下一步:抓包取 token(HttpCanary/Reqable 抓 api.youpin898.com 的 authorization 头),或换 SmsUpSignIn 上行短信模式 ==")


if __name__ == "__main__":
    main()
