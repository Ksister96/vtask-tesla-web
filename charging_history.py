"""Read charge history for an authenticated vehicle, including old account layouts."""
import glob
import json
import os


def read_sessions(users, uid, vin, cutoff):
    vin = str(vin or "").strip().upper()
    folders = [os.path.join(users, uid)]
    if vin and vin.isalnum():
        folders.append(os.path.join(users, "vehicles", vin))
    if vin:
        for path in glob.glob(os.path.join(users, "*", "account.json")):
            try:
                with open(path, encoding="utf-8-sig") as f:
                    vehicles = json.load(f).get("vehicles") or []
                # Unlabelled legacy records can only be attributed unambiguously
                # when the old account has exactly one vehicle.
                if len(vehicles) == 1 and str(vehicles[0].get("vin") or "").upper() == vin:
                    folders.append(os.path.dirname(path))
            except (OSError, ValueError):
                continue
    result = []
    for folder in sorted(set(folders)):
        for path in glob.glob(os.path.join(folder, "charge_sessions", "*.jsonl")):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        q = json.loads(line)
                        if q.get("vin") and str(q["vin"]).upper() != vin:
                            continue
                        if q.get("start", 0) < cutoff:
                            continue
                        result.append(q)
                    except (ValueError, TypeError):
                        continue
    out = []
    for q in sorted(result, key=lambda q: q["start"]):
        duplicate = next((p for p in out if abs(p["start"] - q["start"]) < 60000
                          and abs(p.get("end", 0) - q.get("end", 0)) < 120000), None)
        if duplicate is None:
            out.append(q)
        elif q.get("kwh", 0) > duplicate.get("kwh", 0):
            duplicate.update(q)
    return out
