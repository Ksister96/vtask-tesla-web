import os
import sys
import json
import logging

# Ensure user-installed packages are importable
user_pkg = os.path.expanduser(r'~\AppData\Roaming\Python\Python314\site-packages')
if user_pkg not in sys.path:
    sys.path.append(user_pkg)

try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None
    WebPushException = Exception

try:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
except ImportError:
    load_pem_private_key = None

BASE = os.environ.get("MULTI_BASE", "/opt/tesla-fleet-multi")
VAPID_FILE = os.path.join(BASE, "vapid.json")


def get_vapid_info():
    if not os.path.exists(VAPID_FILE):
        return None
    try:
        with open(VAPID_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def get_public_key():
    info = get_vapid_info()
    return info.get("public_key") if info else None


def _private_key_b64url(vinfo):
    """把 PEM 格式私钥转成 pywebpush 需要的 base64url 裸密钥。"""
    pk = vinfo.get("private_key", "")
    if not pk:
        return None
    if "BEGIN" in pk:
        # PEM 格式：解析出原始 32 字节私钥再转 base64url
        if load_pem_private_key is None:
            return None
        try:
            key = load_pem_private_key(pk.encode(), password=None)
            raw = key.private_numbers().private_value.to_bytes(32, "big")
        except Exception:
            return None
    else:
        try:
            import base64
            raw = base64.urlsafe_b64decode(pk + "=" * (-len(pk) % 4))
        except Exception:
            return None
    import base64
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def send_web_push(subscription_info, payload_dict):
    if not webpush:
        logging.error("[Push] pywebpush not available")
        return False
    vinfo = get_vapid_info()
    if not vinfo:
        logging.error("[Push] vapid.json not found")
        return False

    priv_b64 = _private_key_b64url(vinfo)
    if not priv_b64:
        logging.error("[Push] vapid private key conversion failed")
        return False

    payload_data = json.dumps(payload_dict, ensure_ascii=False)
    try:
        response = webpush(
            subscription_info=subscription_info,
            data=payload_data,
            vapid_private_key=priv_b64,
            vapid_claims={"sub": vinfo.get("claim_email", "mailto:admin@vtask-tesla.cpolar.cn"), "iat": int(__import__("time").time())}
        )
        return True
    except WebPushException as ex:
        logging.warning(f"[Push] WebPushException: {ex}")
        if getattr(ex, "response", None) and ex.response.status_code in (404, 410):
            return "expired"
        return False
    except Exception as e:
        logging.error(f"[Push] Unexpected error: {e}")
        return False

def push_to_user(user_dir_path, payload_dict):
    sub_file = os.path.join(user_dir_path, "push_subs.json")
    if not os.path.exists(sub_file):
        return 0
    try:
        with open(sub_file, "r", encoding="utf-8") as f:
            subs = json.load(f)
    except Exception:
        return 0

    if not isinstance(subs, list) or not subs:
        return 0

    valid_subs = []
    sent_count = 0
    for sub in subs:
        res = send_web_push(sub, payload_dict)
        if res == "expired":
            logging.info("[Push] Removing expired subscription")
            continue
        elif res:
            sent_count += 1
            valid_subs.append(sub)
        else:
            valid_subs.append(sub)

    if len(valid_subs) != len(subs):
        try:
            with open(sub_file, "w", encoding="utf-8") as f:
                json.dump(valid_subs, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    return sent_count
