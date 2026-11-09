# -*- coding: utf-8 -*-
"""行程一次性订阅：额度 +1/-1、43101 清零、四字段 payload、旧 title/value 兼容。"""
import importlib.util
import json
import os
import tempfile

TMP = tempfile.mkdtemp(prefix="wxmsg_test_")
os.environ["MULTI_BASE"] = TMP
os.makedirs(os.path.join(TMP, "users", "t1"), exist_ok=True)

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("cwm_wxmsg", os.path.join(ROOT, "car-web-multi.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

assert m.WXMSG_QUOTA_FILE.startswith(TMP), m.WXMSG_QUOTA_FILE
assert m.WXMSG_TRIP_TMPL and "tesla" not in m.WXMSG_TRIP_TMPL.lower()
assert m.WXMSG_LAYOUT == "trip4"
assert m.WXMSG_TRIP_TMPL == "Qd_2YRBEOPkeUYG8pSwXi8H2dscjrhqqdebRonPVWTw"

LOGS = []
m.log_line = lambda kind, msg: LOGS.append((kind, msg))

TMPL = m.WXMSG_TRIP_TMPL
F_START = m.WXMSG_FIELD_START
F_END = m.WXMSG_FIELD_END
F_DUR = m.WXMSG_FIELD_DURATION
F_MIL = m.WXMSG_FIELD_MILEAGE


def logs_text():
    return "\n".join(msg for _, msg in LOGS)


# ---- 四字段 payload（thing ≤20、short_thing ≤5、character_string 无汉字）----
assert F_START == "thing1" and F_END == "thing2"
assert F_DUR == "short_thing3" and F_MIL == "character_string4"
assert m.WXMSG_START_MAX == 20 and m.WXMSG_END_MAX == 20
assert m.WXMSG_DURATION_MAX == 5 and m.WXMSG_MILEAGE_MAX == 32
assert m.WXMSG_START_DEFAULT == "出发地" and m.WXMSG_END_DEFAULT == "目的地"
assert m.WXMSG_DURATION_DEFAULT == "已结束" and m.WXMSG_MILEAGE_DEFAULT == "0km"

p = m.wxmsg_trip_payload(
    start="上海虹桥枢纽", end="陆家嘴环路", duration="35分钟", mileage="35.2km")
assert set(p.keys()) == {F_START, F_END, F_DUR, F_MIL}
assert p[F_START]["value"] == "上海虹桥枢纽"
assert p[F_END]["value"] == "陆家嘴环路"
assert p[F_DUR]["value"] == "35分钟"
assert p[F_MIL]["value"] == "35.2km"
assert "特斯拉" not in json.dumps(p, ensure_ascii=False)

# character_string：公里→km，去汉字
assert m.wxmsg_sanitize_character_string("35.2公里", 32, "0km") == "35.2km"
assert m.wxmsg_sanitize_character_string("约 12.3 千米", 32, "0km") == "12.3km"
import re as _re
mil = m.wxmsg_trip_payload(mileage="35.2公里")
assert mil[F_MIL]["value"] == "35.2km"
assert _re.search(r"[\u4e00-\u9fff]", mil[F_MIL]["value"]) is None

# short_thing ≤5；超长压短
assert len(m.wxmsg_format_duration_min(35)) <= 5
assert m.wxmsg_format_duration_min(35) == "35分钟"
assert m.wxmsg_format_duration_min(80) == "1时20分"
assert len(m.wxmsg_format_duration_min(80)) <= 5
long_dur = m.wxmsg_trip_payload(duration="1小时20分")
assert len(long_dur[F_DUR]["value"]) <= 5

# thing ≤20
long_addr = "上海市浦东新区陆家嘴环路一百零一号超级长地址尾巴"
assert len(long_addr) > 20
assert len(m.wxmsg_trip_payload(start=long_addr)[F_START]["value"]) == 20

# 空值回落默认；商标回落
empty = m.wxmsg_trip_payload()
assert empty[F_START]["value"] == "出发地"
assert empty[F_END]["value"] == "目的地"
assert empty[F_DUR]["value"] == "已结束"
assert empty[F_MIL]["value"] == "0km"
brand = m.wxmsg_trip_payload(start="特斯拉工厂", end="Tesla Store", duration="Tesla", mileage="Tesla35km")
assert brand[F_START]["value"] == "出发地"
assert brand[F_END]["value"] == "目的地"
assert brand[F_DUR]["value"] == "已结束"
assert brand[F_MIL]["value"] == "0km"

# 旧 title/value 兼容：不报错，能拆出里程/时长
legacy = m.wxmsg_trip_payload(title="行程结束", value="12.3 km · 25 分钟")
assert legacy[F_DUR]["value"] == "25分钟"
assert legacy[F_MIL]["value"] == "12.3km"
assert legacy[F_START]["value"] == "出发地"
assert legacy[F_END]["value"] == "目的地"
legacy2 = m.wxmsg_trip_payload("行程结束", "12.3 km · 25 分钟")  # 位置参数走 start/end；仍不炸
assert isinstance(legacy2, dict) and F_START in legacy2

# env/layout 切回旧 status2 模板可用
m.WXMSG_LAYOUT = "status2"
m.WXMSG_TRIP_TMPL = "zErBRh7BVm9q7SEJe8FpINXm0qg8Af5oDNHCsMrSDxM"
m.WXMSG_FIELD_PHRASE = "phrase1"
m.WXMSG_FIELD_THING = "thing12"
m.WXMSG_PHRASE_MAX = 5
m.WXMSG_THING_MAX = 20
m.WXMSG_PHRASE_DEFAULT = "行程结束"
m.WXMSG_THING_DEFAULT = "已到达"
old = m.wxmsg_trip_payload(title="行程结束", value="12.3 km · 25 分钟")
assert set(old.keys()) == {"phrase1", "thing12"}
assert old["phrase1"]["value"] == "行程结束"
assert old["thing12"]["value"] == "12.3 km · 25 分钟"
old_brand = m.wxmsg_trip_payload(title="特斯拉行程", value="Tesla 到站")
assert old_brand["phrase1"]["value"] == "行程结束"
assert old_brand["thing12"]["value"] == "已到达"
# 恢复 trip4 默认
m.WXMSG_LAYOUT = "trip4"
m.WXMSG_TRIP_TMPL = TMPL
m.WXMSG_FIELD_START = F_START
m.WXMSG_FIELD_END = F_END
m.WXMSG_FIELD_DURATION = F_DUR
m.WXMSG_FIELD_MILEAGE = F_MIL

# accept +1；同一 trip 不重复加；无 trip_id 的另一次再 +1
a = m.wxmsg_note_accept("u1", TMPL, "tripA", "accept")
assert a["added"] and a["remain"] == 1, a
a = m.wxmsg_note_accept("u1", TMPL, "tripA", "accept")
assert (not a["added"]) and a["remain"] == 1, a
a = m.wxmsg_note_accept("u1", TMPL, "", "accept")
assert a["added"] and a["remain"] == 2, a

# 拒绝不加次数
r = m.wxmsg_note_accept("u1", TMPL, "tripB", "reject")
assert (not r["added"]) and r["remain"] == 2, r
assert "授权 reject" in logs_text()

# 有额度才发；成功 -1；同一行程不再发
prep = m.wxmsg_prepare_send("u1", TMPL, "20261109-181000")
assert prep["action"] == "send" and prep["tracked"] and prep["remain"] == 2, prep
done = m.wxmsg_apply_send_result("u1", TMPL, "20261109-181000", ok=True, errcode=0)
assert done["decremented"] and done["remain"] == 1, done
assert "额度 -1" in logs_text()
again = m.wxmsg_prepare_send("u1", TMPL, "20261109-181000")
assert again["action"] == "skip" and again["reason"] == "already_sent", again
# 重放成功结果不重复扣
done2 = m.wxmsg_apply_send_result("u1", TMPL, "20261109-181000", ok=True, errcode=0)
assert (not done2["decremented"]) and done2["remain"] == 1, done2

# 其他错误不扣
kept = m.wxmsg_apply_send_result("u1", TMPL, "20261109-190000", ok=False, errcode=47003)
assert kept["remain"] == 1 and not kept["cleared"], kept

# 43101 清零，之后跳过
cleared = m.wxmsg_apply_send_result("u1", TMPL, "20261109-190000", ok=False, errcode=43101)
assert cleared["cleared"] and cleared["remain"] == 0, cleared
assert "errcode=43101 本地一次性额度清零" in logs_text()
skip = m.wxmsg_prepare_send("u1", TMPL, "20261109-200000")
assert skip["action"] == "skip" and skip["reason"] == "no_quota", skip

# 再接受一次，恢复可发
a = m.wxmsg_note_accept("u1", TMPL, "tripC", "accept")
assert a["remain"] == 1, a
assert m.wxmsg_prepare_send("u1", TMPL, "20261109-210000")["action"] == "send"

# 从未上报 accept：照样发，成功不写成 remain=0
assert m.wxmsg_prepare_send("u2", TMPL, "20261109-181100")["action"] == "send"
ghost = m.wxmsg_apply_send_result("u2", TMPL, "20261109-181100", ok=True, errcode=0)
assert ghost["tracked"] is False and ghost["remain"] is None and not ghost["decremented"], ghost
assert "按微信一次性语义" in logs_text()
assert m.wxmsg_prepare_send("u2", TMPL, "20261109-181100")["reason"] == "already_sent"
assert m.wxmsg_prepare_send("u2", TMPL, "20261109-181200")["action"] == "send"
# 未记账时 43101 不制造 remain=0 把门关上
z = m.wxmsg_apply_send_result("u9", TMPL, "t-9", ok=False, errcode=43101)
assert z["tracked"] is False and z["remain"] is None, z
assert m.wxmsg_prepare_send("u9", TMPL, "t-other")["action"] == "send"

# 上限
for i in range(m.WXMSG_QUOTA_CAP + 3):
    m.wxmsg_note_accept("u3", TMPL, "n%d" % i, "accept")
snap = m.wxmsg_prepare_send("u3", TMPL, "fresh")
assert snap["remain"] == m.WXMSG_QUOTA_CAP, snap

# 发送日志：成功 / 43101（含 HTTP 错误体）
class Resp:
    def __init__(self, obj):
        self._b = json.dumps(obj).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SEEN = {}


def fake_open(req, timeout=8):
    SEEN["url"] = req.full_url
    SEEN["body"] = json.loads(req.data.decode("utf-8"))
    return Resp(SEEN["resp"])


class _SendStub:
    _wxmsg_access_token = m.Handler._wxmsg_access_token


m.urllib.request.urlopen = fake_open
m._WXMSG_TOKEN["tok"] = "tok"
m._WXMSG_TOKEN["ts"] = 10**12
os.environ["WXMSG_MINIPROGRAM_STATE"] = "developer"
SEEN["resp"] = {"errcode": 0, "errmsg": "ok"}
card = m.wxmsg_trip_payload(
    start="虹桥", end="陆家嘴", duration="25分钟", mileage="12.3km")
ok = m.Handler.send_subscribe_msg(_SendStub(), "openid-ABCDEFGH", TMPL, card, "pages/home/home")
assert ok["ok"] and ok["errcode"] == 0, ok
assert "cgi-bin/message/subscribe/send" in SEEN["url"]
assert SEEN["body"]["miniprogram_state"] == "developer"
assert SEEN["body"]["template_id"] == TMPL
assert set(SEEN["body"]["data"].keys()) == {F_START, F_END, F_DUR, F_MIL}
assert SEEN["body"]["data"][F_MIL]["value"] == "12.3km"
assert "发送成功 errcode=0" in logs_text()

SEEN["resp"] = {"errcode": 43101, "errmsg": "user refuse to accept the msg"}
bad = m.Handler.send_subscribe_msg(_SendStub(), "openid-ABCDEFGH", TMPL, card)
assert bad["ok"] is False and bad["errcode"] == 43101, bad
assert "发送失败 errcode=43101" in logs_text()
assert "一次性额度已用完" in logs_text()

class Boom(Exception):
    def read(self):
        return b'{"errcode":43101,"errmsg":"user refuse to accept the msg"}'


def boom_open(req, timeout=8):
    raise Boom("http 412")


m.urllib.request.urlopen = boom_open
bad2 = m.Handler.send_subscribe_msg(_SendStub(), "openid-ABCDEFGH", TMPL, card)
assert bad2["errcode"] == 43101, bad2

m._WXMSG_TOKEN["tok"] = ""
m._WXMSG_TOKEN["ts"] = 0
m.MP_WX_APPID = ""
m.MP_WX_SECRET = ""
miss = m.Handler.send_subscribe_msg(_SendStub(), "openid-ABCDEFGH", TMPL, card)
assert miss["ok"] is False and miss["errmsg"] == "no access_token", miss
assert "无 access_token" in logs_text()

# settle_trip 带 start/end/duration/mileage + trip_id
import track_watcher_multi as tw

posted = {}


class TwResp:
    def read(self):
        return b'{"ok":true,"sent":false,"reason":"no_quota","errcode":null}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def tw_open(req, timeout=5):
    posted["url"] = req.full_url
    posted["body"] = json.loads(req.data.decode("utf-8"))
    return TwResp()


tw.urllib.request.urlopen = tw_open
# 避免 regeo 打外网
tw.regeo = lambda lat, lon: "测试起点" if lat < 31.25 else "测试终点"
t0 = 1700000000000
tw.settle_trip("t1", [
    {"ts": t0, "lat": 31.2, "lon": 121.5, "gear": "D", "spd": 30, "odo": 10.0},
    {"ts": t0 + 180000, "lat": 31.3, "lon": 121.6, "gear": "D", "spd": 40, "odo": 12.5},
    {"ts": t0 + 181000, "lat": 31.3, "lon": 121.6, "gear": "P", "spd": 0, "odo": 12.5},
])
assert posted["url"].endswith("/internal/wxmsg"), posted
assert posted["body"]["uid"] == "t1"
assert posted["body"]["trip_id"]
assert posted["body"]["start"] == "测试起点"
assert posted["body"]["end"] == "测试终点"
assert posted["body"]["duration"] == "3分钟"
assert posted["body"]["mileage"] == "2.5km"
assert "特斯拉" not in json.dumps(posted["body"], ensure_ascii=False)

print("WXMSG_QUOTA_TESTS_PASS")
