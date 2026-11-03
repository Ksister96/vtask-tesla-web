"""修复旧版 PackCurrent 符号反向造成的行程能耗与动能回收数据。"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import time

import track_split


def run(base, dates=None, apply=False):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    changed = 0
    for path in glob.glob(os.path.join(base, "users", "*", "trip_details", "*.json")):
        name = os.path.basename(path)
        date = name[:8]
        if dates and date not in dates:
            continue
        try:
            with open(path, encoding="utf-8") as source:
                detail = json.load(source)
        except (OSError, ValueError):
            continue
        if not track_split.repair_battery_power_detail(detail):
            continue
        changed += 1
        uid_tag = hashlib.sha256(os.path.basename(os.path.dirname(os.path.dirname(path))).encode()).hexdigest()[:8]
        print(uid_tag, name, "wh_per_km", detail.get("wh_per_km"),
              "regen_kwh", detail.get("regen_kwh"), "apply", apply)
        if not apply:
            continue
        backup_dir = os.path.join(base, "trip_energy_backups", stamp, uid_tag)
        os.makedirs(backup_dir, mode=0o700, exist_ok=True)
        shutil.copy2(path, os.path.join(backup_dir, name))
        tmp = path + ".energy.tmp"
        with open(tmp, "w", encoding="utf-8") as target:
            json.dump(detail, target, ensure_ascii=False)
        os.replace(tmp, path)
    print("changed", changed, "apply", apply)
    return changed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="/opt/tesla-fleet-multi")
    parser.add_argument("--date", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    dates = {value.replace("-", "") for value in args.date}
    run(args.base, dates or None, args.apply)
