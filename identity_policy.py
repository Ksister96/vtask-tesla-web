# -*- coding: utf-8 -*-
"""Fail-closed identity eligibility and OAuth-scope helpers, shared by workers."""
import base64
import json
import os
import hashlib
import sqlite3
import time

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default

def _email_uid_known(base, uid):
    """邮箱账号（E 前缀 uid）以 identity.sqlite3 的 identities 表为准（2026-10-17 新登录体系）。"""
    if not str(uid or "").startswith("E"):
        return False
    dbpath = os.path.join(base, "identity.sqlite3")
    if not os.path.isfile(dbpath):
        return False
    try:
        with sqlite3.connect(dbpath, timeout=10) as db:
            row = db.execute("SELECT 1 FROM identities WHERE uid=? LIMIT 1", (uid,)).fetchone()
        return row is not None
    except (OSError, sqlite3.Error):
        return False

def enabled_user(base, uid):
    if os.environ.get("SANDBOX_ALLOW_AUTH") == "1":
        return True
    if _email_uid_known(base, uid):
        return True
    codes = read_json(os.path.join(base, "friends-codes.json"), {})
    if not isinstance(codes, dict):
        return False
    return any(isinstance(rec, dict) and (code == uid or rec.get("uid") == uid)
               and rec.get("enabled", True) for code, rec in codes.items())

def token_scopes(tokens):
    # This is a hint to avoid invalid requests, not JWT signature authentication.
    try:
        token = tokens.get("access_token", "")
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        scope = claims.get("scp", claims.get("scope", ""))
        return set(scope.split() if isinstance(scope, str) else scope or [])
    except (ValueError, IndexError, TypeError):
        scope = tokens.get("scope", "") if isinstance(tokens, dict) else ""
        return set(scope.split() if isinstance(scope, str) else scope or [])

def token_fingerprint(tokens):
    return hashlib.sha256(str((tokens or {}).get("access_token", "")).encode()).hexdigest()

def authorization_health(tokens):
    scopes = token_scopes(tokens or {})
    return {"location": "vehicle_location" in scopes,
            "needs_location_authorization": "vehicle_location" not in scopes,
            "message": "" if "vehicle_location" in scopes else "缺少车辆定位权限，行程采集已暂停，请重新授权；基础车况仍可使用。"}

def refresh_candidates(base):
    users = os.path.join(base, "users")
    if not os.path.isdir(users):
        return []
    return [uid for uid in sorted(os.listdir(users))
            if enabled_user(base, uid)
            and os.path.isfile(os.path.join(users, uid, "tokens.json"))
            and os.path.isfile(os.path.join(users, uid, "account.json"))]

def tracking_groups(base):
    """One request per VIN. Include only enabled, bound, scope-eligible identities."""
    groups = {}
    for uid in refresh_candidates(base):
        root = os.path.join(base, "users", uid)
        account = read_json(os.path.join(root, "account.json"), {}) or {}
        tokens = read_json(os.path.join(root, "tokens.json"), {}) or {}
        vehicles = account.get("vehicles") or []
        if not vehicles or "vehicle_location" not in token_scopes(tokens):
            continue
        vin = str(vehicles[0].get("vin") or "").strip().upper()
        if vin and vehicles[0].get("id") and tokens.get("access_token"):
            groups.setdefault(vin, []).append(uid)
    return list(groups.values())
