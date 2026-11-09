# -*- coding: utf-8 -*-
"""charge_watch 状态机回归（2026-10-23 通知中心）：完成推送 / 低电一天一次 / 不重复打扰。
另：充电结束微信只在 Charging→Complete 发一次；停留、Stopped、低电量不发。"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="cwatch_test_")
os.environ["MULTI_BASE"] = TMP
os.makedirs(os.path.join(TMP, "users", "t1"))
sys.path.insert(0, "/opt/tesla-fleet-multi")
import track_watcher_multi as tw  # noqa: E402

wx_posts = []


def _record_wx(uid, payload):
    wx_posts.append((uid, payload))


tw.post_charge_wxmsg = _record_wx


def chat(uid="t1"):
    try:
        return json.load(open(os.path.join(TMP, "users", uid, "chat.json"), encoding="utf-8"))
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

# 8) 微信：两段完成各发一次；完成态停留、低电量都不发
assert len(wx_posts) == 2, wx_posts
assert all(p[0] == "t1" and p[1].get("kind") == "charge" and p[1].get("end_reason") == "complete"
           and p[1].get("session_id") for p in wx_posts), wx_posts

# 9) 充入电量走本会话增量；重复 Complete 不双发；Stopped/Disconnected/低电不发微信
os.makedirs(os.path.join(TMP, "users", "t2"))
n = len(wx_posts)
tw.charge_watch("t2", {"charging_state": "Charging", "battery_level": 41.6, "charge_energy_added": 1.2})
tw.charge_watch("t2", {
    "charging_state": "Complete", "battery_level": 80, "charge_energy_added": 27.0,
    "sample_ts": 1790000000000,
})
assert len(wx_posts) == n + 1, wx_posts
body = wx_posts[-1][1]
assert body["kind"] == "charge" and body["end_reason"] == "complete", body
assert body["kwh"] == 25.8, body
assert body["soc_start"] == 41.6 and body["soc_end"] == 80, body
assert body["end_ts"] == 1790000000, body
assert body["session_id"] and "分钟" not in json.dumps(body, ensure_ascii=False)
tw.charge_watch("t2", {"charging_state": "Complete", "battery_level": 80, "charge_energy_added": 27.0})
assert len(wx_posts) == n + 1, wx_posts
tw.charge_watch("t2", {"charging_state": "Charging", "battery_level": 50, "charge_energy_added": 0})
tw.charge_watch("t2", {"charging_state": "Stopped", "battery_level": 55, "charge_energy_added": 3.2})
tw.charge_watch("t2", {"charging_state": "Charging", "battery_level": 55, "charge_energy_added": 3.2})
tw.charge_watch("t2", {"charging_state": "Disconnected", "battery_level": 56, "charge_energy_added": 3.4})
tw.charge_watch("t2", {"charging_state": "", "battery_level": 12})
assert len(wx_posts) == n + 1, wx_posts
done = [m for m in chat("t2") if "充电完成" in m.get("text", "")]
assert len(done) == 1, chat("t2")
assert any("12%" in m.get("text", "") for m in chat("t2"))

# 10) 微信发送失败不打断「充电完成」聊天
os.makedirs(os.path.join(TMP, "users", "t3"))


def _boom(uid, payload):
    raise RuntimeError("wx down")


tw.post_charge_wxmsg = _boom
tw.charge_watch("t3", {"charging_state": "Charging", "battery_level": 30, "charge_energy_added": 0})
tw.charge_watch("t3", {"charging_state": "Complete", "battery_level": 70, "charge_energy_added": 10, "battery_range": 100})
assert any("充电完成" in m.get("text", "") and "70%" in m.get("text", "") for m in chat("t3")), chat("t3")

print("CHARGE_WATCH_TESTS_PASS")
