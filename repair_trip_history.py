"""Offline repair for the ShiftStateD enum bug. Dry-run by default.

Only rebuild a selected vehicle/day with raw enum points. Original details are
copied to a timestamped backup before overlapping records are replaced.
No network requests, notifications, or fabricated power samples.
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import shutil
import time

import track_split


def corrected(points):
    out = {}
    for point in points:
        p = dict(point)
        gear = str(p.get("gear") or "")
        if gear.startswith("ShiftState"):
            p["gear"] = gear[len("ShiftState"):]
            if p.get("spd") is not None:
                p["spd"] = round(float(p["spd"]) * 1.609344, 2)
        if p["ts"] not in out or gear.startswith("ShiftState"):
            out[p["ts"]] = p
    return sorted(out.values(), key=lambda p: p["ts"])


def run(base, date, vehicle_tag, apply=False):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for account in glob.glob(os.path.join(base, "users", "*", "account.json")):
        with open(account, encoding="utf-8-sig") as f:
            vehicles = json.load(f).get("vehicles") or []
        if len(vehicles) != 1 or hashlib.sha256(str(vehicles[0].get("vin", "")).encode()).hexdigest()[:8] != vehicle_tag:
            continue
        folder = os.path.dirname(account)
        uid_tag = hashlib.sha256(os.path.basename(folder).encode()).hexdigest()[:8]
        points = []
        for path in glob.glob(os.path.join(folder, "tracks", date[:7].replace("-", "") + ".jsonl")):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        p = json.loads(line)
                        if datetime.datetime.fromtimestamp(p["ts"] / 1000).strftime("%Y-%m-%d") == date:
                            points.append(p)
                    except (ValueError, KeyError):
                        continue
        if not any(str(p.get("gear", "")).startswith("ShiftState") for p in points):
            continue
        rebuilt = []
        for trip in track_split.split_trips(corrected(points)):
            if (trip.get("km") or 0) >= 0.3 and trip["t1"] - trip["t0"] >= 120000:
                rebuilt.append((trip, track_split.build_detail(trip["pts"])))
        if not rebuilt:
            continue
        old = []
        for path in glob.glob(os.path.join(folder, "trip_details", date.replace("-", "") + "-*.json")):
            t0 = datetime.datetime.strptime(os.path.basename(path)[:-5], "%Y%m%d-%H%M%S").timestamp() * 1000
            if any(t["t0"] <= t0 <= t["t1"] for t, _ in rebuilt):
                with open(path, encoding="utf-8") as f:
                    old.append((path, json.load(f)))
        print(uid_tag, "points", len(points), "replace", len(old), "rebuilt", len(rebuilt), "apply", apply)
        if not apply:
            continue
        backup = os.path.join(base, "trip_repair_backups", stamp, os.path.basename(folder))
        os.makedirs(backup, mode=0o700, exist_ok=True)
        for path, _ in old:
            shutil.copy2(path, backup)
        for _, detail in rebuilt:
            for _, previous in old:
                if previous.get("start_time") == detail["start_time"] and previous.get("start_addr"):
                    detail["start_addr"] = previous["start_addr"]
                if previous.get("end_time") == detail["end_time"] and previous.get("end_addr"):
                    detail["end_addr"] = previous["end_addr"]
            detail["data_repaired"] = "shift_enum_v1"
            path = os.path.join(folder, "trip_details", detail["id"] + ".json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(detail, f, ensure_ascii=False)
            os.replace(path + ".tmp", path)
        new_names = {d["id"] + ".json" for _, d in rebuilt}
        for path, _ in old:
            if os.path.basename(path) not in new_names:
                os.remove(path)  # exact files backed up above; no track data removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="/opt/tesla-fleet-multi")
    parser.add_argument("--date", required=True, type=lambda s: datetime.date.fromisoformat(s).isoformat())
    parser.add_argument("--vehicle-tag", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run(args.base, args.date, args.vehicle_tag, args.apply)
