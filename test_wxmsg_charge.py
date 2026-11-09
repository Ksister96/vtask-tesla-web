# -*- coding: utf-8 -*-
"""充电结束订阅：payload 格式、按模板分账、同会话只发一次。"""
import importlib.util
import json
import os
import re
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="wxmsg_charge_")
os.environ["MULTI_BASE"] = TMP
os.makedirs(os.path.join(TMP, "users"), exist_ok=True)

SPEC = importlib.util.spec_from_file_location(
    "car_web_multi_charge", os.path.join(os.path.dirname(os.path.abspath(__file__)), "car-web-multi.py"))
CAR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAR)

TMPL = "gWvIgcvnYLgjvkrNt9wtVrdR4L-j3M18nylRyv8IEPg"
assert CAR.WXMSG_CHARGE_TMPL == TMPL
assert CAR.WXMSG_CHARGE_QUOTA_CAP >= 9999
assert CAR.WXMSG_CHARGE_FIELD_DURATION == "time3"
assert CAR.WXMSG_CHARGE_FIELD_ENERGY == "character_string4"
assert CAR.WXMSG_CHARGE_FIELD_END == "time2"

START = time.mktime(time.strptime("2026-11-10 01:00", "%Y-%m-%d %H:%M"))
END = time.mktime(time.strptime("2026-11-10 03:30", "%Y-%m-%d %H:%M"))
RANGE = "2026-11-10 01:00~2026-11-10 03:30"


def card(**kw):
    return CAR.wxmsg_charge_payload(kw.pop("start_ts", START), kw.pop("end_ts", END), **kw)


# kWh、半角 ~ 时段、结束时间；禁止把「35分钟」填进 time
p = card(kwh=25.8)
assert p["time3"]["value"] == RANGE, p
assert p["time2"]["value"] == "2026-11-10 03:30", p
assert p["character_string4"]["value"] == "25.8kWh", p
assert "～" not in p["time3"]["value"] and "~" in p["time3"]["value"]
assert "分钟" not in json.dumps(p, ensure_ascii=False)
assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}~\d{4}-\d{2}-\d{2} \d{2}:\d{2}", p["time3"]["value"])

# 剥汉字：度 / 分钟都不能留在 character_string
p = card(kwh="25.8度")
assert p["character_string4"]["value"] == "25.8kWh", p
assert not re.search(r"[\u4e00-\u9fff]", p["character_string4"]["value"])
p = card(kwh="35分钟")
assert p["character_string4"]["value"] == "35.0kWh", p
assert p["time3"]["value"] == RANGE
assert "分钟" not in p["time3"]["value"] and "分钟" not in p["character_string4"]["value"]

# 缺能量用 SOC 差；都没有用 0kWh；显式 0 仍是 kWh 不是百分比
p = card(kwh=None, soc_delta=38)
assert p["character_string4"]["value"] == "+38%", p
p = card(kwh=None, soc_delta=37.6)
assert p["character_string4"]["value"] == "+38%", p
p = card()
assert p["character_string4"]["value"] == "0kWh", p
p = card(kwh=0)
assert p["character_string4"]["value"] == "0.0kWh", p
p = card(start_ts=None)
assert p["time3"]["value"] == "2026-11-10 03:30", p
assert "~" not in p["time3"]["value"]

# 毫秒时间戳同样能拼时段；超长电量截到 32 且无汉字
p = CAR.wxmsg_charge_payload(START * 1000, END * 1000, kwh=25.8)
assert p["time3"]["value"] == RANGE, p
p = card(kwh="汉" * 10 + "123456789012345678901234567890999kWh多余")
assert len(p["character_string4"]["value"]) <= 32
assert not re.search(r"[\u4e00-\u9fff]", p["character_string4"]["value"])
assert p["character_string4"]["value"]

# 按模板分账：充电与另一张模板互不挪用；同一 accept_id 不重复 +1；上限可调但默认很大
noted = CAR.wxmsg_charge_note_accept("u1", TMPL, "accept", accept_id="boot")
assert noted["added"] is True and noted["remain"] == 1, noted
again = CAR.wxmsg_charge_note_accept("u1", TMPL, "accept", accept_id="boot")
assert again["added"] is False and again["remain"] == 1, again
other = CAR.wxmsg_charge_note_accept("u1", "Qd_2YRBEOPkeUYG8pSwXi8H2dscjrhqqdebRonPVWTw", "accept", accept_id="boot")
assert other["added"] is True and other["remain"] == 1, other
reject = CAR.wxmsg_charge_note_accept("u1", TMPL, "reject")
assert reject["added"] is False and reject["remain"] == 1, reject

raw = json.load(open(CAR.WXMSG_QUOTA_FILE, encoding="utf-8"))
assert set(raw["u1"]["tmpls"]) == {TMPL, "Qd_2YRBEOPkeUYG8pSwXi8H2dscjrhqqdebRonPVWTw"}

old_cap = CAR.WXMSG_CHARGE_QUOTA_CAP
CAR.WXMSG_CHARGE_QUOTA_CAP = 2
try:
    one = CAR.wxmsg_charge_note_accept("u-cap", TMPL, "accept", accept_id="a")
    two = CAR.wxmsg_charge_note_accept("u-cap", TMPL, "accept", accept_id="b")
    three = CAR.wxmsg_charge_note_accept("u-cap", TMPL, "accept", accept_id="c")
    assert one["remain"] == 1 and two["remain"] == 2 and three["added"] is False and three["remain"] == 2
    assert three.get("capped") is True
finally:
    CAR.WXMSG_CHARGE_QUOTA_CAP = old_cap

# 同会话只发一次；另一会话仍可发；43101 清零后不再轰炸
prep = CAR.wxmsg_charge_prepare("u2", TMPL, "1762729200000")
assert prep["action"] == "send", prep
applied = CAR.wxmsg_charge_apply_send("u2", TMPL, "1762729200000", ok=True, errcode=0)
assert applied["decremented"] is False  # 从未 accept，不扣，但要去重
dup = CAR.wxmsg_charge_prepare("u2", TMPL, "1762729200000")
assert dup["action"] == "skip" and dup["reason"] == "duplicate", dup
nxt = CAR.wxmsg_charge_prepare("u2", TMPL, "1762730000000")
assert nxt["action"] == "send", nxt
# start_ts 兜底键与显式 session_id 对齐
assert CAR.wxmsg_charge_session_key("", 1762729200) == "1762729200"
assert CAR.wxmsg_charge_session_key("1762729200000", 1) == "1762729200000"
assert CAR.wxmsg_charge_session_key("有汉字", 1762729200.9) == "1762729200"

CAR.wxmsg_charge_note_accept("u3", TMPL, "accept", accept_id="once")
assert CAR.wxmsg_charge_prepare("u3", TMPL, "s1")["action"] == "send"
done = CAR.wxmsg_charge_apply_send("u3", TMPL, "s1", ok=True, errcode=0)
assert done["decremented"] is True and done["remain"] == 0, done
blocked = CAR.wxmsg_charge_prepare("u3", TMPL, "s2")
assert blocked["action"] == "skip" and blocked["reason"] == "no_quota", blocked

CAR.wxmsg_charge_apply_send("u4", TMPL, "s9", ok=False, errcode=43101)
cleared = CAR.wxmsg_charge_prepare("u4", TMPL, "s10")
assert cleared["action"] == "skip" and cleared["reason"] == "no_quota", cleared
# 其他错误不扣、不占用会话
CAR.wxmsg_charge_note_accept("u5", TMPL, "accept", accept_id="e1")
CAR.wxmsg_charge_apply_send("u5", TMPL, "sx", ok=False, errcode=47003)
retry = CAR.wxmsg_charge_prepare("u5", TMPL, "sx")
assert retry["action"] == "send" and retry["remain"] == 1, retry


class Fake(object):
    def __init__(self, body=None, ip="127.0.0.1"):
        self.body = body or {}
        self.client_address = (ip, 9)
        self.headers = {}
        self.resp = None
        self.sent = []

    def _json(self, code, obj):
        self.resp = (code, obj)

    def _parse_json(self):
        return self.body

    def _subscribe_send_result(self, openid, template_id, data, page=None):
        self.sent.append({"openid": openid, "tmpl": template_id, "data": data, "page": page})
        if self.body.get("_errcode") == 43101:
            return {"ok": False, "errcode": 43101, "errmsg": "user refuse"}
        return {"ok": True, "errcode": 0, "errmsg": "ok"}

    def send_subscribe_msg(self, openid, template_id, data, page=None):
        self.sent.append({"via": "trip", "openid": openid, "tmpl": template_id, "data": data})
        return True

    def _rate_ok(self, code):
        return True

    _internal_wxmsg_charge = CAR.Handler._internal_wxmsg_charge


with open(os.path.join(TMP, "friends-codes.json"), "w", encoding="utf-8") as f:
    json.dump({"u9": {"openid": "oChargeTestOpenid", "enabled": True}}, f)

# 非本机拒绝
far = Fake(ip="8.8.8.8")
CAR.Handler._internal_wxmsg(far)
assert far.resp[0] == 403 and far.sent == [], far.resp

# 缺省 kind 仍走行程模板，不碰充电模板
trip = Fake({"uid": "u9", "title": "行程完成", "value": "12.3 km · 25 分钟"})
CAR.Handler._internal_wxmsg(trip)
assert trip.resp[1]["sent"] is True, trip.resp
assert trip.sent[0]["tmpl"] == CAR.WXMSG_TRIP_TMPL, trip.sent
assert trip.sent[0]["via"] == "trip"
assert "thing1" in trip.sent[0]["data"]

# Complete 路径：发一次，重复 session 不双发；字段就是充电模板
body = {
    "uid": "u9", "kind": "charge",
    "start_ts": START, "end_ts": END,
    "kwh": "25.8度", "soc_start": 41.6, "soc_end": 80,
    "end_reason": "complete", "session_id": "charge-sess-1",
}
first = Fake(body)
CAR.Handler._internal_wxmsg(first)
assert first.resp[1]["sent"] is True, first.resp
assert len(first.sent) == 1
sent = first.sent[0]
assert sent["tmpl"] == TMPL, sent
assert sent["data"]["time3"]["value"] == RANGE
assert sent["data"]["character_string4"]["value"] == "25.8kWh"
assert sent["data"]["time2"]["value"] == "2026-11-10 03:30"
assert "分钟" not in json.dumps(sent["data"], ensure_ascii=False)
assert set(sent["data"]) == {"time3", "character_string4", "time2"}

second = Fake(dict(body))
CAR.Handler._internal_wxmsg(second)
assert second.resp[1]["sent"] is False and second.resp[1]["reason"] == "duplicate", second.resp
assert second.sent == []

# 没 openid：不发
ghost = Fake({"uid": "nobody", "kind": "charge", "kwh": 1, "session_id": "x", "end_ts": END})
CAR.Handler._internal_wxmsg(ghost)
assert ghost.resp[1]["sent"] is False and ghost.resp[1]["reason"] == "no openid", ghost.resp
assert ghost.sent == []

# 43101 后换一个会话也不再发
bad = Fake({
    "uid": "u9", "kind": "charge", "start_ts": START, "end_ts": END,
    "kwh": 1, "session_id": "charge-sess-2", "_errcode": 43101,
})
CAR.Handler._internal_wxmsg(bad)
assert bad.resp[1]["sent"] is False and bad.resp[1]["reason"] == "43101", bad.resp
assert len(bad.sent) == 1
later = Fake({
    "uid": "u9", "kind": "charge", "start_ts": START, "end_ts": END,
    "kwh": 2, "session_id": "charge-sess-3",
})
CAR.Handler._internal_wxmsg(later)
assert later.resp[1]["sent"] is False and later.resp[1]["reason"] == "no_quota", later.resp
assert later.sent == []

# accept：登录 + 已绑 openid 才 +1；同一 accept_id 不重复；未登录 401
token = CAR.make_session("u9", "tester", uid="u9")
acc = Fake({"token": token, "result": "accept", "accept_id": "boot-click"})
CAR.Handler._api_wxmsg_accept(acc)
assert acc.resp[0] == 200 and acc.resp[1]["added"] is True and acc.resp[1]["tmpl"] == TMPL, acc.resp
acc2 = Fake({"token": token, "result": "accept", "accept_id": "boot-click"})
CAR.Handler._api_wxmsg_accept(acc2)
assert acc2.resp[1]["added"] is False and acc2.resp[1]["remain"] == acc.resp[1]["remain"], acc2.resp
anon = Fake({"result": "accept"})
CAR.Handler._api_wxmsg_accept(anon)
assert anon.resp[0] == 401, anon.resp

print("WXMSG_CHARGE_TESTS_PASS")
