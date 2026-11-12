"""網頁面板登入：用每個人自己的擴充功能金鑰（extmembers）登入，記住 30 天。

- Cookie 只存「身分 id + 金鑰指紋」並簽章（itsdangerous），不存金鑰本身；httponly、SameSite=Lax、HTTPS 時加 Secure。
- 每個請求都重新核對：成員被停用 / 刪除 / 重設金鑰、或 .env 的 EXT_TOKEN 換掉 → 立刻失效（等於被登出）。
- 同一個 IP（優先看 Cloudflare 的 CF-Connecting-IP）連續輸錯 5 次 → 鎖 10 分鐘。
- 擁有者（.env 的 EXT_TOKEN）才能用：更新 / 重啟、成員管理。
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from urllib.parse import urlparse

from itsdangerous import URLSafeTimedSerializer, BadSignature

from .config import log
from . import extmembers

COOKIE = "v6_session"
MAX_AGE = 30 * 86400            # 記住 30 天
MAX_FAILS = 5                   # 連續輸錯幾次就鎖
LOCK_SECS = 10 * 60             # 鎖多久
FAIL_WINDOW = 10 * 60           # 輸錯次數在這段時間內累計（之後歸零）
SECRET_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "panel_secret")

# 只有擁有者能用的路徑（前綴比對）；/api/ext/members* 由 extapi 的金鑰檢查擋
OWNER_ONLY = ("/members", "/api/system/update")

_lock = threading.Lock()
_fails = {}                     # ip → {"n": 次數, "first": 第一次輸錯時間, "until": 鎖到何時}
_secret = [None]


# ---------- 簽章金鑰 ----------
def _load_secret():
    """PANEL_SECRET（.env）優先；沒有就用 data/panel_secret（第一次自動產生，0600）"""
    env = (os.getenv("PANEL_SECRET") or "").strip()
    if env:
        return env
    if _secret[0]:
        return _secret[0]
    try:
        with open(SECRET_FILE, encoding="utf-8") as f:
            s = f.read().strip()
        if len(s) >= 32:
            _secret[0] = s
            return s
    except OSError:
        pass
    s = secrets.token_hex(32)
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(s)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    _secret[0] = s
    log.info("[登入] 已產生面板簽章金鑰 data/panel_secret")
    return s


def _serializer():
    return URLSafeTimedSerializer(_load_secret(), salt="v6-panel-login")


def _fingerprint(user_key, token_hash):
    """金鑰指紋：金鑰換掉指紋就不同 → 舊 cookie 失效。用簽章金鑰再做一次 HMAC，cookie 內容看不出雜湊"""
    return hmac.new(_load_secret().encode(), f"{user_key}:{token_hash}".encode(), hashlib.sha256).hexdigest()[:24]


def _token_hash_for(user_key):
    """目前有效的金鑰雜湊；擁有者 = EXT_TOKEN 的雜湊；成員不存在或停用 → None"""
    if user_key == "owner":
        own = extmembers.owner_token()
        return extmembers._hash(own) if own else None
    m = extmembers.member_record(user_key)
    if not m or not m.get("enabled", True):
        return None
    return m.get("token_hash") or None


# ---------- 登入 / 驗證 ----------
def make_cookie_value(ident):
    """身分（authenticate 的結果）→ 簽章後的 cookie 值"""
    uk = "owner" if ident.get("owner") else ident["id"]
    th = _token_hash_for(uk)
    if not th:
        return None
    return _serializer().dumps({"u": uk, "f": _fingerprint(uk, th)})


def ident_from_cookie(value):
    """cookie 值 → 身分 dict；過期 / 偽造 / 金鑰已失效 → None"""
    if not value or len(value) > 512:
        return None
    try:
        data = _serializer().loads(value, max_age=MAX_AGE)
    except BadSignature:            # SignatureExpired 也是 BadSignature 的子類別
        return None
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    uk, fp = data.get("u"), data.get("f")
    if not isinstance(uk, str) or not isinstance(fp, str):
        return None
    th = _token_hash_for(uk)
    if not th or not hmac.compare_digest(fp, _fingerprint(uk, th)):
        return None
    if uk == "owner":
        return dict(extmembers.OWNER)
    m = extmembers.member_record(uk)
    return {"id": m["id"], "name": m["name"], "owner": False} if m else None


def current_user(req=None):
    from flask import request
    req = req or request
    return ident_from_cookie(req.cookies.get(COOKIE))


def is_https(req):
    if req.is_secure:
        return True
    return (req.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower() == "https"


def set_login_cookie(resp, req, value):
    resp.set_cookie(COOKIE, value, max_age=MAX_AGE, httponly=True, secure=is_https(req), samesite="Lax", path="/")


def clear_login_cookie(resp, req):
    resp.delete_cookie(COOKIE, path="/", httponly=True, secure=is_https(req), samesite="Lax")


def is_owner_only(path):
    return any(path == p or path.startswith(p + "/") for p in OWNER_ONLY)


def same_origin(req):
    """會改東西的請求：有 Origin（或 Referer）就必須跟本站同一個主機，擋跨站送出"""
    src = req.headers.get("Origin") or req.headers.get("Referer")
    if not src or src == "null":
        return src is None      # 沒帶 → 放行（同站 fetch / 舊瀏覽器）；"null" → 拒絕
    try:
        host = (urlparse(src).netloc or "").lower()
    except Exception:
        return False
    return host == (req.host or "").lower()


def safe_next(nxt):
    """登入後要回去的頁面：只接受站內路徑，擋 //evil.com、/\\evil.com、http://… 之類"""
    if not isinstance(nxt, str) or not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        return "/"
    if nxt.startswith("/login") or nxt.startswith("/logout") or len(nxt) > 300:
        return "/"
    return nxt


# ---------- 輸錯鎖定 ----------
def client_ip(req):
    ip = (req.headers.get("CF-Connecting-IP") or "").strip()
    if not ip:
        xff = (req.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        ip = xff or (req.remote_addr or "")
    return ip[:64] or "?"


def locked_for(ip):
    """還要鎖幾秒（0 = 沒鎖）"""
    now = time.time()
    with _lock:
        f = _fails.get(ip)
        if not f:
            return 0
        if f["until"] > now:
            return int(f["until"] - now) + 1
        if f["until"] or now - f["first"] > FAIL_WINDOW:     # 鎖過期 / 累計時間過了 → 歸零
            _fails.pop(ip, None)
        return 0


def record_fail(ip):
    """記一次輸錯；回傳 (剩幾次, 鎖幾秒)"""
    now = time.time()
    with _lock:
        if len(_fails) > 5000:          # 防止被灌爆記憶體：清掉過期的
            for k in [k for k, v in _fails.items() if v["until"] < now and now - v["first"] > FAIL_WINDOW]:
                _fails.pop(k, None)
        f = _fails.get(ip)
        if not f or (f["until"] and f["until"] <= now) or now - f["first"] > FAIL_WINDOW:
            f = _fails[ip] = {"n": 0, "first": now, "until": 0}
        f["n"] += 1
        if f["n"] >= MAX_FAILS:
            f["until"] = now + LOCK_SECS
            log.warning(f"[登入] {ip} 連續輸錯 {f['n']} 次，鎖 {LOCK_SECS // 60} 分鐘")
            return 0, LOCK_SECS
        return MAX_FAILS - f["n"], 0


def clear_fails(ip):
    with _lock:
        _fails.pop(ip, None)


def reset_all():
    """測試用"""
    with _lock:
        _fails.clear()
    _secret[0] = None
