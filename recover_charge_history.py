"""Recover *closed* charging sessions from retained telemetry logs; dry-run default.

Energy is an estimate from observed power, never a charging-station receipt.
No extrapolation across a gap exceeding two minutes. Only the selected VIN/day
is processed; records are idempotent and vehicle-scoped.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import time

from telemetry_ingest import epoch_seconds, norm
from vehicle_measurements import number


def recover(records, date, vehicle_tag):
    fields = {}
    session = None
    result = []
    for rec in records:
        vin = str(rec.get("vin") or "")
        if hashlib.sha256(vin.encode()).hexdigest()[:8] != vehicle_tag:
            continue
        ts = epoch_seconds(rec.get("time")) * 1000
        if not ts or datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") != date:
            continue
        data = rec.get("data") or {}
        items = data if isinstance(data, list) else [data]
        incoming = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            pairs = [(item["key"], item["value"])] if "key" in item and "value" in item else item.items()
            incoming.update({str(k).split(".")[-1]: norm(v) for k, v in pairs})
        fields.update(incoming)
        if not ({"ACChargingPower", "DCChargingPower"} & incoming.keys()):
            continue
        power = max(number(fields.get("ACChargingPower"), 0, 100) or 0,
                    number(fields.get("DCChargingPower"), 0, 1500) or 0)
        if session and ts > session["last"]:
            dt = ts - session["last"]
            if dt > 120000:
                # Incomplete logs cannot establish continuity or a safe end time.
                session = None
            else:
                session["kwh"] += session["last_power"] * dt / 3600000
        if power > 1:
            if session is None:
                session = {"start": int(ts), "vin": vin, "soc_start": fields.get("BatteryLevel"),
                           "kwh": 0, "max_power": power, "estimated": True,
                           "source": "telemetry_log_recovery"}
            session.update(last=ts, last_power=power,
                           max_power=max(session["max_power"], power))
        elif session:
            if ts - session["start"] >= 120000 and session["kwh"] >= 0.1:
                session.update(end=int(ts), kwh=round(session["kwh"], 2), soc_end=fields.get("BatteryLevel"))
                for key in ("last", "last_power"):
                    session.pop(key, None)
                result.append(session)
            session = None
    return result


def run(logfile, base, date, vehicle_tag, apply=False):
    def records():
        with open(logfile, encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    recovered = recover(records(), date, vehicle_tag)
    for q in recovered:
        print("closed_session", datetime.datetime.fromtimestamp(q["start"] / 1000).isoformat(),
              "minutes", round((q["end"] - q["start"]) / 60000, 1), "estimated_kwh", q["kwh"], "apply", apply)
        if not apply:
            continue
        vin = q["vin"]
        if not vin.isalnum():
            raise ValueError("invalid vehicle identifier")
        folder = os.path.join(base, "users", "vehicles", vin, "charge_sessions")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, date[:7].replace("-", "") + ".jsonl")
        previous = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                previous = [json.loads(line) for line in f if line.strip()]
        if any(abs(p["start"] - q["start"]) < 60000 for p in previous):
            print("already_present")
            continue
        if previous:
            shutil.copy2(path, path + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default="/var/log/fleet-telemetry/server.log")
    parser.add_argument("--base", default="/opt/tesla-fleet-multi")
    parser.add_argument("--date", required=True, type=lambda s: datetime.date.fromisoformat(s).isoformat())
    parser.add_argument("--vehicle-tag", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run(args.log, args.base, args.date, args.vehicle_tag, args.apply)
