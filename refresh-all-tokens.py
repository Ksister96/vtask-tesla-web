# -*- coding: utf-8 -*-
# 定期检查并刷新 E:/tesla-fleet-multi/users 下所有用户的 Tesla OAuth tokens。
import io, sys, os, json, time, glob, urllib.request, urllib.parse, ssl

BASE = os.environ.get("MULTI_BASE", "/opt/tesla-fleet-multi")
USERS_DIR = os.path.join(BASE, "users")
LOG_FILE = os.path.join(BASE, "token-refresh.log")
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {msg}\n"
    print(line, end="")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

def refresh_user_token(uid):
    tf = os.path.join(USERS_DIR, uid, "tokens.json")
    if not os.path.exists(tf):
        return False, "tokens.json not found"
    try:
        with open(tf, "r", encoding="utf-8-sig") as f:
            t = json.load(f)
    except Exception as e:
        return False, f"read error: {e}"

    rt = t.get("refresh_token")
    if not rt:
        return False, "no refresh_token"

    cid = t.get("client_id") or "b0708cc4-c9b7-4a66-a061-feb3ef3c58a9"
    secret = t.get("client_secret", "")
    body = (f"grant_type=refresh_token&client_id={urllib.parse.quote(cid)}"
            f"&refresh_token={urllib.parse.quote(rt)}")
    if secret:
        body += f"&client_secret={urllib.parse.quote(secret)}"

    try:
        req = urllib.request.Request(
            "https://auth.tesla.cn/oauth2/v3/token",
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            nt = json.loads(resp.read().decode("utf-8"))
        if nt.get("access_token"):
            nt["client_id"] = cid
            if secret:
                nt["client_secret"] = secret
            nt["updated_at"] = int(time.time())
            with open(tf, "w", encoding="utf-8") as f:
                json.dump(nt, f, ensure_ascii=False, indent=2)

            # 如果是 demo01，同步给 E:\tesla-fleet 和 dev 目录
            if uid == "demo01":
                try:
                    with open("/opt/tesla-fleet/tokens.json", "w", encoding="utf-8") as f:
                        json.dump(nt, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    log(f"sync to E:\\tesla-fleet error: {e}")
                try:
                    dev_tf = ""  # Linux 无 dev 副本
                    if os.path.exists(os.path.dirname(dev_tf)):
                        with open(dev_tf, "w", encoding="utf-8") as f:
                            json.dump(nt, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    log(f"sync to dev error: {e}")

            return True, f"access_len={len(nt['access_token'])} refresh_len={len(nt.get('refresh_token',''))} exp={nt.get('expires_in')}"
        return False, f"unexpected response: {nt}"
    except Exception as e:
        return False, f"request failed: {e}"

def main():
    log("=== MULTI-USER TOKEN REFRESH START ===")
    uids = [os.path.basename(p) for p in glob.glob(os.path.join(USERS_DIR, "*")) if os.path.isdir(p)]
    for uid in uids:
        ok, msg = refresh_user_token(uid)
        log(f"[{uid}] {'OK' if ok else 'FAIL'}: {msg}")
    log("=== MULTI-USER TOKEN REFRESH END ===")

if __name__ == "__main__":
    main()
