# -*- coding: utf-8 -*-
"""场景整合执行回归（2026-10-23 定稿：一个场景只回一条整合消息）。"""
import importlib.util
import os
import tempfile

spec = importlib.util.spec_from_file_location("cwm", "/opt/tesla-fleet-multi/car-web-multi.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

tmp = tempfile.mkdtemp(prefix="scenrun_test_")
os.makedirs(os.path.join(tmp, "users", "u8"), exist_ok=True)
m.user_dir = lambda uid: os.path.join(tmp, "users", uid)
m.save_scenarios("u8", [{"name": "离家模式", "items": ["锁车", "关空调", "闪灯"]}])
m.run_runner = lambda uid, text: (
    ("✅ 已锁车", None) if text == "锁车" else
    (("❄️ 空调已关闭", None) if text == "关空调" else (None, "车辆无响应"))
)

out, err = m._run_scenario("u8", m.match_scenario("u8", "离家模式"))
assert err is None, err
assert out.startswith("🎬 场景「离家模式」执行完成（2/3 成功）"), out
assert "✅ 锁车：已锁车" in out, out
assert "✅ 关空调：❄️ 空调已关闭" in out, out
assert "❌ 闪灯：车辆无响应" in out, out
print(out)
print("SCENARIO_RUN_TESTS_PASS")
