# -*- coding: utf-8 -*-
"""charge_watch 状态机回归（2026-10-23 通知中心）：完成推送 / 低电一天一次 / 不重复打扰。"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="cwatch_test_")
os.environ["MULTI_BASE"] = TMP
os.makedirs(os.path.join(TMP, "users", "t1"))
sys.path.insert(0, "/opt/tesla-fleet-multi")
import track_watcher_multi as tw  # noqa: E402


def chat():
    try:
        return json.load(open(os.path.join(TMP, "users", "t1", "chat.json"), encoding="utf-8"))
    except Exception:
        return []


# 1) 开始充电：无推送，记录开始时间
tw.charge_watch("t1", {"charging_state": "Charging", "battery_level": 40})
assert len(chat()) == 0, chat()
# 2) 充电完成：推一条 sys 消息
tw.charge_watch("t1", {"charging_state": "Complete", "battery_level": 80, "battery_range": 160})
msgs = chat()
assert len(msgs) == 1 and "充电完成" in msgs[0]["text"] and "80%" in msgs[0]["text"], msgs
assert msgs[0]["who"] == "sys"
# 3) 完成态停留：不重复推送
tw.charge_watch("t1", {"charging_state": "Complete", "battery_level": 80})
assert len(chat()) == 1, chat()
# 4) 低电量：推一次
tw.charge_watch("t1", {"charging_state": "", "battery_level": 15})
msgs = chat()
assert len(msgs) == 2 and "15%" in msgs[1]["text"], msgs
# 5) 低电量同一天不重复
tw.charge_watch("t1", {"charging_state": "", "battery_level": 14})
assert len(chat()) == 2, chat()
# 6) 再充再完成：再推一条
tw.charge_watch("t1", {"charging_state": "Charging", "battery_level": 16})
tw.charge_watch("t1", {"charging_state": "Complete", "battery_level": 55})
assert len(chat()) == 3 and "充电完成" in chat()[-1]["text"], chat()
# 7) sys_msg 直写
tw.sys_msg("t1", "🧪 直写测试")
assert chat()[-1]["text"] == "🧪 直写测试" and chat()[-1]["who"] == "sys"

print("CHARGE_WATCH_TESTS_PASS")
