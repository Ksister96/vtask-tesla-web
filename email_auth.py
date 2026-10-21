# -*- coding: utf-8 -*-
"""Email OTP authentication; stdlib only, durable one-use challenges, no demo bypass."""
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from email.message import EmailMessage
from contextlib import contextmanager

class AuthError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def normalize_email(value):
    email = str(value or "").strip()
    if len(email) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", email):
        raise AuthError("请输入有效的邮箱地址")
    local, domain = email.rsplit("@", 1)
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        raise AuthError("请输入有效的邮箱地址")
    labels = domain.split(".")
    if len(labels) < 2 or any(not x or len(x) > 63 or x.startswith("-") or x.endswith("-") for x in labels):
        raise AuthError("请输入有效的邮箱地址")
    return email.lower()


def mail_configured():
    return bool(os.environ.get("VTASK_SMTP_HOST") and os.environ.get("VTASK_SMTP_FROM"))


def send_mail(address, code):
    if not mail_configured():
        raise AuthError("邮箱登录尚未配置邮件服务，请联系管理员", 503)
    mode = os.environ.get("VTASK_SMTP_TLS", "starttls").lower()
    if mode not in ("ssl", "starttls"):
        raise AuthError("邮件服务必须启用 TLS", 503)
    host = os.environ["VTASK_SMTP_HOST"]
    port = int(os.environ.get("VTASK_SMTP_PORT", "465" if mode == "ssl" else "587"))
    msg = EmailMessage()
    msg["From"] = os.environ["VTASK_SMTP_FROM"]
    msg["To"] = address
    msg["Subject"] = "微特 V-TASK 登录验证码"
    msg.set_content(f"你的登录验证码是 {code}，5 分钟内有效，仅能使用一次。\n如果不是你本人操作，请忽略此邮件。请勿向他人提供验证码。")
    client = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
    options = {"context": ssl.create_default_context()} if mode == "ssl" else {}
    try:
        with client(host, port, timeout=15, **options) as smtp:
            if mode == "starttls":
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if os.environ.get("VTASK_SMTP_USER"):
                smtp.login(os.environ["VTASK_SMTP_USER"], os.environ.get("VTASK_SMTP_PASSWORD", ""))
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException, ValueError):
        raise AuthError("验证码发送失败，请稍后重试", 503) from None


class EmailAuthStore:
    def __init__(self, base, sender=None, clock=None):
        self.path = os.path.join(base, "identity.sqlite3")
        self.sender = sender or send_mail
        self.clock = clock or time.time
        self.test_codes = {}

    @contextmanager
    def _db(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS identities (email TEXT PRIMARY KEY, uid TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS challenges (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL, digest TEXT NOT NULL,
                    expires REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    consumed INTEGER NOT NULL DEFAULT 0, target_uid TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS events (kind TEXT NOT NULL, key TEXT NOT NULL, ts REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS events_lookup ON events(kind,key,ts);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('pepper', ?)", (secrets.token_hex(32),))
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _hash(self, db, value):
        pepper = db.execute("SELECT value FROM meta WHERE key='pepper'").fetchone()[0]
        return hmac.new(pepper.encode(), value.encode(), hashlib.sha256).hexdigest()

    def issue(self, email, ip, target_uid=""):
        email = normalize_email(email)
        if self.sender is send_mail and not mail_configured():
            raise AuthError("邮箱登录尚未配置邮件服务，请联系管理员", 503)
        now = self.clock()
        with self._db() as db:
            db.execute("DELETE FROM events WHERE ts < ?", (now - 86400,))
            db.execute("DELETE FROM challenges WHERE expires < ?", (now - 86400,))
            key = self._hash(db, "email:" + email)
            ipkey = self._hash(db, "ip:" + str(ip))
            last = db.execute("SELECT MAX(ts) FROM events WHERE kind='send_email' AND key=?", (key,)).fetchone()[0]
            sent = db.execute("SELECT COUNT(*) FROM events WHERE kind='send_email' AND key=? AND ts>?", (key, now-86400)).fetchone()[0]
            ipcount = db.execute("SELECT COUNT(*) FROM events WHERE kind='send_ip' AND key=? AND ts>?", (ipkey, now-3600)).fetchone()[0]
            if (last is not None and now-last < 60) or sent >= 10 or ipcount >= 20:
                raise AuthError("发送过于频繁，请稍后再试", 429)
            challenge = secrets.token_urlsafe(24)
            code = f"{secrets.randbelow(1000000):06d}"
            digest = self._hash(db, challenge + ":" + code)
            # Send before storing a usable challenge. Failed delivery cannot authenticate.
            self.sender(email, code)
            if os.environ.get("SANDBOX_ALLOW_AUTH") == "1":
                self.test_codes[challenge] = code
            db.execute("UPDATE challenges SET consumed=1 WHERE email=?", (email,))
            db.execute("INSERT INTO challenges(id,email,digest,expires,target_uid) VALUES(?,?,?,?,?)",
                       (challenge, email, digest, now+300, target_uid))
            db.executemany("INSERT INTO events VALUES(?,?,?)", [("send_email",key,now),("send_ip",ipkey,now)])
        result = {"ok": True, "challenge": challenge, "expires_in": 300, "retry_after": 60}
        if os.environ.get("SANDBOX_ALLOW_AUTH") == "1" and challenge in self.test_codes:
            result["test_code"] = self.test_codes[challenge]
        return result

    def verify(self, challenge, code, ip, target_uid=""):
        challenge, code = str(challenge or ""), str(code or "")
        if len(challenge) > 100 or not re.fullmatch(r"[0-9]{6}", code):
            raise AuthError("验证码格式错误")
        now = self.clock()
        error = None
        result = None
        with self._db() as db:
            ipkey = self._hash(db, "ip:" + str(ip))
            count = db.execute("SELECT COUNT(*) FROM events WHERE kind='verify_ip' AND key=? AND ts>?", (ipkey,now-900)).fetchone()[0]
            if count >= 30:
                raise AuthError("验证过于频繁，请稍后再试", 429)
            db.execute("INSERT INTO events VALUES('verify_ip',?,?)", (ipkey,now))
            row = db.execute("SELECT * FROM challenges WHERE id=?", (challenge,)).fetchone()
            if not row or row['consumed'] or row['expires'] <= now or row['attempts'] >= 5:
                error = AuthError("验证码无效或已过期，请重新获取")
            elif row['target_uid'] != target_uid:
                error = AuthError("账号关联会话已失效，请重新登录后获取验证码", 401)
            elif not hmac.compare_digest(row['digest'], self._hash(db, challenge + ":" + code)):
                db.execute("UPDATE challenges SET attempts=attempts+1 WHERE id=?", (challenge,))
                error = AuthError("验证码错误")
            else:
                self.test_codes.pop(challenge, None)
                identity = db.execute("SELECT uid FROM identities WHERE email=?", (row['email'],)).fetchone()
                uid = identity[0] if identity else (target_uid or "E" + secrets.token_hex(12))
                if identity and target_uid and uid != target_uid:
                    db.execute("UPDATE challenges SET consumed=1 WHERE id=?", (challenge,))
                    error = AuthError("该邮箱已关联其他账号；不会自动合并车辆或作品", 409)
                elif not identity:
                    try:
                        db.execute("INSERT INTO identities VALUES(?,?)", (row['email'], uid))
                    except sqlite3.IntegrityError:
                        error = AuthError("当前账号已经关联邮箱；更换邮箱需单独验证", 409)
                if error is None:
                    db.execute("UPDATE challenges SET consumed=1 WHERE id=?", (challenge,))
                    result = {"uid": uid, "email": row['email']}
        # Raise outside transaction so invalid-attempt counters are committed.
        if error:
            raise error
        return result
