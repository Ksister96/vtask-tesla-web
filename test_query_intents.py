# -*- coding: utf-8 -*-
"""查询类五件套路由回归（2026-10-23 P1）：说法 -> 动作 映射正确，且不劫持原有指令。"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("OWNER_BASE", ROOT)
os.environ.setdefault("MULTI_BASE", ROOT)
sys.path.insert(0, ROOT)
import user_runner as ur  # noqa: E402

CASES = [
    ("车在哪", "location_query"),
    ("车停哪了", "location_query"),
    ("看看车在哪", "location_query"),
    ("现在胎压多少", "tire_query"),
    ("胎压正常吗", "tire_query"),
    ("上周开了多少公里", "trip_query"),
    ("这周跑了几次车", "trip_query"),
    ("昨天去哪了", "trip_query"),
    ("最近一次行程", "trip_query"),
    ("查行程", "trip_query"),
    ("看行程", "trip_query"),
    ("我的行程", "trip_query"),
    ("充电还要多久", "charge_eta_query"),
    ("多久充满", "charge_eta_query"),
    ("充电进度", "charge_eta_query"),
    ("你能做什么", "help_query"),
]
for text, want in CASES:
    acts, err = ur.resolve_actions(text)
    got = acts[0]["action"] if acts else f"(err:{err})"
    assert got == want, f"{text} -> {got}（期望 {want}）"
    scope_ok = True
    if want == "trip_query":
        expect_scope = {"上周": "last_week", "这周": "this_week", "本周": "this_week", "昨天": "yesterday"}.get(text[:2], "last_trip")
        scope_ok = acts[0]["params"].get("scope") == expect_scope
    assert scope_ok, f"{text} scope 错：{acts[0]['params']}"

# 不劫持原有指令；查电量必须仍是查车况，不能被裸「充电」规则收成开始充电
KEEP = [
    ("充到80", "set_charge_limit"),
    ("充电上限85", "set_charge_limit"),
    ("空调调到22度", "set_temps"),
    ("锁车", "door_lock"),
    ("查车况", "status"),
    ("查电量", "status"),
    ("充电", "charge_start"),
    ("充会儿电", "charge_start"),
]
for text, want in KEEP:
    acts, err = ur.resolve_actions(text)
    got = acts[0]["action"] if acts else f"(err:{err})"
    assert got == want, f"{text} -> {got}（期望 {want}）"

# 复合句不劫持（含连接词走原解析链路）
acts, _ = ur.resolve_actions("上周开了多少公里然后锁车")
assert not acts or acts[0]["action"] != "trip_query", "复合句被误劫持"

# 智谱直连与防发挥提示（不打外网）
import intent  # noqa: E402
assert intent.API_URL.startswith("https://open.bigmodel.cn/"), intent.API_URL
assert intent.MODEL == "glm-4-flash", intent.MODEL
assert intent.MODEL_FALLBACK == "glm-4.5-flash", intent.MODEL_FALLBACK
assert "准备一下" in intent.SYSTEM and "无法识别意图" in intent.SYSTEM

# 直答：胎压读档案（兼容 fl 与遥测 pressure_fl），行程读 trip_details
tmp = tempfile.mkdtemp(prefix="query_intent_")
ur.USERS_DIR = tmp
ur.get_vin = lambda uid: "VIN1"
ur._vin_archive = lambda vin: {"tire_pressure": {
    "pressure_fl": 2.4, "pressure_fr": 2.7, "pressure_rl": 2.7, "pressure_rr": 2.8,
}}
tire = ur.run_text("u", "现在胎压多少")
assert "左前 2.4bar" in tire and "低于 2.6bar" in tire, tire

ddir = os.path.join(tmp, "u", "trip_details")
os.makedirs(ddir)
with open(os.path.join(ddir, "20261116-090000.json"), "w", encoding="utf-8") as f:
    json.dump({
        "id": "20261116-090000", "date": "2026-11-16", "start_time": "09:00:00",
        "distance_km": 12.5, "duration_min": 28,
        "start_addr": "虹桥枢纽", "end_addr": "陆家嘴",
    }, f, ensure_ascii=False)
trip = ur.run_text("u", "查行程")
assert "12.5 km" in trip and "虹桥枢纽" in trip and "陆家嘴" in trip, trip

print("QUERY_INTENT_TESTS_PASS")
