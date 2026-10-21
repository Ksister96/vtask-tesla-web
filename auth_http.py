# -*- coding: utf-8 -*-
"""HTTP email endpoints. Backend supplies store/session callbacks on the handler."""
import ipaddress
import os
from email_auth import AuthError, mail_configured

class EmailAuthEndpoints:
    def _client_ip(self):
        peer = self.client_address[0] if self.client_address else "?"
        trusted = {p.strip() for p in os.environ.get("VTASK_TRUSTED_PROXIES", "127.0.0.1,::1").split(",")}
        forwarded = self.headers.get("X-Real-IP", "").strip()
        if peer in trusted and forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return peer

    def _email_target(self, data):
        if data.get("link_current"):
            session = self._auth()
            if not session:
                raise AuthError("请先登录原账号，再关联邮箱", 401)
            return session["uid"]
        legacy = str(data.get("legacy_code") or "").strip()
        if legacy:
            codes = self._load_email_codes()
            key = next((k for k in codes if str(k).strip().lower() == legacy.lower()), None)
            rec = codes.get(key) if key is not None else None
            if not rec or not rec.get("enabled", True):
                raise AuthError("旧账号凭据无效或已停用")
            return rec.get("uid") or key
        return ""

    def _api_email_send(self):
        data = self._parse_json()
        if not isinstance(data, dict):
            self._json(400, {"ok": False, "error": "参数错误"})
            return
        try:
            target = self._email_target(data)
            result = self.email_auth.issue(data.get("email"), self._client_ip(), target)
            result["test_mode"] = os.environ.get("SANDBOX_ALLOW_AUTH") == "1"
            result["email"] = mail_configured() or result["test_mode"]
            self._json(200, result)
        except AuthError as e:
            self._json(e.status, {"ok": False, "error": str(e)})
        except Exception:
            self._json(503, {"ok": False, "error": "登录服务暂不可用"})

    def _api_email_verify(self):
        data = self._parse_json()
        if not isinstance(data, dict):
            self._json(400, {"ok": False, "error": "参数错误"})
            return
        try:
            identity = self.email_auth.verify(data.get("challenge"), data.get("code"),
                                              self._client_ip(), self._email_target(data))
            result = self.finish_email_login(identity, bool(data.get("link_current")))
            self._json(200, result)
        except AuthError as e:
            self._json(e.status, {"ok": False, "error": str(e)})
        except Exception:
            self._json(503, {"ok": False, "error": "登录服务暂不可用"})
