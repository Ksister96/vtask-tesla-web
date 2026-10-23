# -*- coding: utf-8 -*-
"""一键场景存储回归：清洗（截断/去空/限量）、精确命中、不劫持真实指令。"""
import importlib.util
import os
import tempfile

spec = importlib.util.spec_from_file_location("cwm", "/opt/tesla-fleet-multi/car-web-multi.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

tmp = tempfile.mkdtemp(prefix="scen_test_")
os.makedirs(os.path.join(tmp, "users", "u9"), exist_ok=True)
# 接管用户目录（不碰生产 users/）
m.user_dir = lambda uid: os.path.join(tmp, "users", uid)

m.save_scenarios("u9", [
    {"name": "离家模式", "items": ["锁车", "关空调", "闪灯", "", "多余步骤"]},
    {"name": "", "items": ["x"]},                     # 空名丢弃
    {"name": "冬季上车", "items": ["开空调", "座椅加热", "方向盘加热"]},
])
sc = m.load_scenarios("u9")
assert sc == [
    {"name": "离家模式", "items": ["锁车", "关空调", "闪灯", "多余步骤"]},   # 空串滤掉，最多留 4 步
    {"name": "冬季上车", "items": ["开空调", "座椅加热", "方向盘加热"]},
], sc
assert m.match_scenario("u9", "离家模式")["name"] == "离家模式"
assert m.match_scenario("u9", " 离家模式 ")["name"] == "离家模式"   # 首尾空白容错
assert m.match_scenario("u9", "锁车") is None                       # 真实指令不被劫持
assert m.match_scenario("u9", "") is None
assert m.match_scenario("u9", "不存在的场景") is None

# 超 8 个只留前 8
m.save_scenarios("u9", [{"name": f"场景{i}", "items": ["锁车"]} for i in range(12)])
assert len(m.load_scenarios("u9")) == 8
print("SCENARIO_STORE_TESTS_PASS")
