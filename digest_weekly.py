# -*- coding: utf-8 -*-
"""每周用车周报 → 聊天流系统消息（2026-10-23 通知中心）。
触发：tesla-weekly-report.timer（周一 08:30）。数据源：users/<uid>/trip_details/*.json
（track_watcher 结算产物，id=YYYYMMDD-HHMMSS 可排序）。零行程用户不发，不打扰。"""
import glob
import json
import os
import sys
import time

BASE = os.environ.get("MULTI_BASE", "/opt/tesla-fleet-multi")
USERS = os.path.join(BASE, "users")
sys.path.insert(0, BASE)
from track_watcher_multi import sys_msg, log, user_ids  # noqa: E402


def week_bounds():
    """上一周一 00:00 ~ 本周一 00:00（本地时区）。"""
    lt = time.localtime()
    this_mon = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, 0)) - lt.tm_wday * 86400
    return this_mon - 7 * 86400, this_mon


def main():
    lo, hi = week_bounds()
    sent = 0
    for uid in user_ids():
        ddir = os.path.join(USERS, uid, "trip_details")
        if not os.path.isdir(ddir):
            continue
        trips = []
        for fp in glob.glob(os.path.join(ddir, "*.json")):
            try:
                d = json.load(open(fp, encoding="utf-8"))
            except Exception:
                continue
            tid = str(d.get("id") or "")
            try:
                ts = time.mktime(time.strptime(tid[:8], "%Y%m%d"))
            except Exception:
                continue
            if lo <= ts < hi and d.get("distance_km"):
                trips.append(d)
        if not trips:
            continue
        km = sum(t["distance_km"] for t in trips)
        mins = sum(t.get("duration_min") or 0 for t in trips)
        best = max(trips, key=lambda t: t["distance_km"])
        wd = "周" + "一二三四五六日"[time.strptime(best["id"][:8], "%Y%m%d").tm_wday]
        hours, rem = divmod(mins, 60)
        dur = f"{hours}小时{rem}分" if hours else f"{rem} 分钟"
        msg = (f"📊 上周用车周报\n"
               f"🚗 {len(trips)} 次行程 · {km:.1f} km · {dur}"
               f"\n🏆 最长一次 {best['distance_km']} km（{wd}）")
        sys_msg(uid, msg)
        log(f"{uid} 周报已推送（{len(trips)} 次行程 {km:.1f}km）")
        sent += 1
    log(f"周报完成：推送 {sent} 位用户")


if __name__ == "__main__":
    main()
