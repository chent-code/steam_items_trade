"""观察模式(notify_only)单测:零网络、零资产写、验证推送与状态回滚"""
import os
import sys
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
import watcher_inv as wi

cap, writes = [], []
wi.notify = lambda cfg, t, c: cap.append((t, c))
wi.save_state = lambda *a, **k: None
wi.steam_real_ask = lambda name: 30.0        # 免网络
wi.yyyp_bid = lambda cfg, gid: 8.0           # 免网络
wi.exit_check = lambda *a, **k: []           # 出口评估本身只提醒,单测里跳过
wi.http_json = lambda *a, **k: {"data": {}}  # 免网络

cfg = {"accept_ratio": 0.75, "delist_ratio": 0.70, "satisfy_ratio": 0.70,
       "sell_fee": 0.01, "net_multiplier": 0.8696, "undercut_pct": 1.0,
       "notify_only": True, "wxpusher": {"app_token": "x", "uid": "y"}}
wi.load_cfg = lambda: cfg
wi.load_state = lambda cfg: ({}, "fake_state.json")

class FakeUU:
    user_id = 7011181
    def get_sell_list(self):
        # 一件已上架、一件未上架
        return [{"steam_asset_id": "S1", "price": 12.0, "commodity_id": 111,
                 "name": "Item A"}]
    def call(self, method, path, data=None):
        # 实时挂单簿:他人最低 10.0(触发改价/追价)
        if "GetCsGoPagedList" in path:
            return {"Data": {"CommodityList": [
                {"Price": "12.0", "UserId": 7011181},
                {"Price": "10.0", "UserId": 9999999}]}}
        return {"Data": {}}
    def off_shelf(self, ids): writes.append(("off_shelf", ids))
    def change_price(self, d): writes.append(("change_price", d))
    def sell_items(self, d): writes.append(("sell_items", d)); return (0, [])

rows = [
    {"name": "Item A", "cn": "物品甲", "asset_id": "S1", "ratio": 0.80,
     "yyyp": 10.0, "steam": 30.0, "template_id": 47016, "good_id": 1,
     "tradable": True, "shot": ""},
    {"name": "Item B", "cn": "物品乙", "asset_id": "S2", "ratio": 0.80,
     "yyyp": 10.0, "steam": 30.0, "template_id": 47017, "good_id": 2,
     "tradable": True, "shot": ""},
]

actions, notes = wi.cmd_auto(cfg, FakeUU(), rows, dry=False)
print()
print("=== 资产写接口调用 ===", writes if writes else "(零调用 OK)")
print("=== 推送标题 ===", [t for t, _ in cap])
for t, c in cap:
    if t == "调度建议(仅通知)":
        print("--- 推送内容 ---")
        print(c[:700])
assert not writes, "观察模式不应调用任何资产写接口"
assert any(t == "调度建议(仅通知)" for t, _ in cap), "应推送建议"
assert any("[建议改价]" in c or "[建议上架]" in c or "[建议下架" in c for t, c in cap if t == "调度建议(仅通知)"), "文案应标注建议/未执行"
print("=== PASS ===")
