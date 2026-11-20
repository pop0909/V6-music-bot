"""網頁面板用 Discord 帳號登入（OAuth2，只要 identify 權限）。

流程：/auth/discord → Discord 授權頁 → /auth/discord/callback?code&state
- state：隨機值放在簽章過的短效 cookie（10 分鐘、httponly），回來時比對，擋 CSRF / 登入劫持
- 用 code 換 access token（伺服器對 Discord，client secret 只在這裡用），拿 /users/@me 的 id 後就丟掉 token，不保存
- 必須是機器人所在任一伺服器的成員（由機器人自己查，不需要 guilds 權限）
- 擁有者：.env 的 OWNER_ID；沒設就用 Discord 應用程式擁有者（跟 alerts.py 一樣）；或在 /members 連結為「我的 Discord」的帳號
- 個人歌單：userlists.discord_key()（每個 Discord 帳號自動有自己的；有連結成員 → 用成員那份）
- 設定好之後，網頁只能用 Discord 登入；舊的金鑰登入 cookie 失效（擴充功能的 X-Ext-Token 不受影響）
- 伺服器端 session：data/web_sessions.json（0600），只存 session id 的雜湊 + Discord id / 顯示名稱 / 頭像
  cookie 沿用 v6_session（webauth 簽章），內容只有隨機 session id
- 沒設 DISCORD_CLIENT_SECRET / OAUTH_REDIRECT_URI → 整個功能關閉，登入頁跟以前一樣只有金鑰
.env：
    DISCORD_CLIENT_SECRET=…（Discord 開發者後台 OAuth2 頁的 Client Secret）
    OAUTH_REDIRECT_URI=https://musbot.kdns.fr/auth/discord/callback
    DISCORD_CLIENT_ID=…（選填；不填就用機器人自己的應用程式 ID）
"""
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import log

API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = API + "/oauth2/token"
ME_URL = API + "/users/@me"
CDN = "https://cdn.discordapp.com/"
CALLBACK_PATH = "/auth/discord/callback"
USER_AGENT = "DiscordBot (https://musbot.kdns.fr, 1.0) V6MusicBot"

STATE_COOKIE = "v6_oauth"
STATE_MAX_AGE = 600             # 授權要在 10 分鐘內完成
SESSION_IDLE = 90 * 86400       # 90 天沒用就失效（有在用會一直續）
SEEN_WRITE_EVERY = 3600         # 「最後使用」最多每小時寫一次檔
RECHECK_EVERY = 3600            # 每小時用機器人的成員快取確認一次還在伺服器裡
MAX_PER_USER = 10               # 每人最多幾個登入中的裝置（多了踢掉最舊的）
MAX_SESSIONS = 5000
NAME_MAX = 32
CB_LIMIT, CB_WINDOW = 20, 600   # callback：同一 IP 10 分鐘最多 20 次

SESSIONS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "web_sessions.json")

_lock = threading.RLock()
_sessions = {"data": None, "path": None}
_cb_hits = {}
_warned = set()


# ---------- 設定 ----------
def _warn_once(key, msg):
    if key not in _warned:
        _warned.add(key)
        log.warning(msg)


def _bot():
    from .bot import client
    return client


def client_id():
    env = (os.getenv("DISCORD_CLIENT_ID") or "").strip()
    if env.isdigit():
        return env
    try:
        aid = _bot().application_id
        return str(aid) if aid else ""
    except Exception:
        return ""


def client_secret():
    return (os.getenv("DISCORD_CLIENT_SECRET") or "").strip()


def redirect_uri():
    """.env 的 OAUTH_REDIRECT_URI；只接受本站（ALLOWED_HOSTS / 本機）的 /auth/discord/callback，https（本機可 http）"""
    raw = (os.getenv("OAUTH_REDIRECT_URI") or "").strip()
    if not raw:
        return ""
    try:
        u = urllib.parse.urlsplit(raw)
        host = (u.hostname or "").lower()
    except ValueError:
        host, u = "", None
    from .webapi import _ALLOWED_HOSTS, _LOCAL_HOSTS
    ok = (u is not None and u.path == CALLBACK_PATH and not u.query and not u.fragment and not u.username
          and ((u.scheme == "https" and (host in _ALLOWED_HOSTS or host in _LOCAL_HOSTS))
               or (u.scheme == "http" and host in _LOCAL_HOSTS)))
    if not ok:
        _warn_once("redirect", f"[Discord 登入] OAUTH_REDIRECT_URI 不正確（要是 https://<本站網域>{CALLBACK_PATH}），Discord 登入已停用")
        return ""
    return raw


def enabled():
    return bool(client_secret() and redirect_uri() and client_id())


def authorize_url(state):
    q = urllib.parse.urlencode({"client_id": client_id(), "response_type": "code", "scope": "identify",
                                "redirect_uri": redirect_uri(), "state": state, "prompt": "none"})
    return f"{AUTHORIZE_URL}?{q}"


# ---------- state（CSRF） ----------
def _state_serializer():
    from itsdangerous import URLSafeTimedSerializer
    from . import webauth
    return URLSafeTimedSerializer(webauth._load_secret(), salt="v6-discord-oauth")


def new_state(nxt):
    """→ (state, 要放進 cookie 的值)"""
    st = secrets.token_urlsafe(24)
    return st, _state_serializer().dumps({"s": st, "n": nxt})


def check_state(cookie_value, state):
    """cookie 裡的 state 跟網址的 state 一致且沒過期 → 登入後要回去的頁面；否則 None"""
    import hmac
    if not cookie_value or not isinstance(state, str) or not state or len(cookie_value) > 1024:
        return None
    try:
        data = _state_serializer().loads(cookie_value, max_age=STATE_MAX_AGE)
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("s"), str):
        return None
    if not hmac.compare_digest(data["s"].encode(), state.encode()):
        return None
    from . import webauth
    return webauth.safe_next(data.get("n"))


# ---------- callback 頻率限制 ----------
def callback_allowed(ip):
    now = time.time()
    with _lock:
        if len(_cb_hits) > 5000:
            for k in [k for k, v in _cb_hits.items() if not v or now - v[-1] > CB_WINDOW]:
                _cb_hits.pop(k, None)
        hits = [t for t in _cb_hits.get(ip, []) if now - t < CB_WINDOW]
        if len(hits) >= CB_LIMIT:
            _cb_hits[ip] = hits
            return False
        hits.append(now)
        _cb_hits[ip] = hits
        return True


# ---------- 跟 Discord 交換（測試會替換 _http） ----------
class OAuthError(Exception):
    """code = 給使用者看的錯誤代號（webapi 對應成中文）"""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _http(method, url, data=None, headers=None, timeout=10):
    """→ (狀態碼, JSON)；任何錯誤 → (0 或狀態碼, None)。不記錄內容（裡面有 token）"""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read(1 << 20).decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def exchange_code(code):
    """授權碼 → Discord 使用者 dict（id / username / global_name / avatar）；token 用完即丟"""
    if not isinstance(code, str) or not code or len(code) > 200:
        raise OAuthError("bad_code")
    st, tok = _http("POST", TOKEN_URL, data={
        "client_id": client_id(), "client_secret": client_secret(), "grant_type": "authorization_code",
        "code": code, "redirect_uri": redirect_uri()})
    access = (tok or {}).get("access_token") if isinstance(tok, dict) else None
    if st != 200 or not isinstance(access, str) or not access:
        log.warning(f"[Discord 登入] 換 token 失敗（HTTP {st}）")
        raise OAuthError("exchange")
    st, me = _http("GET", ME_URL, headers={"Authorization": f"Bearer {access}"})
    if st != 200 or not isinstance(me, dict) or not str(me.get("id") or "").isdigit():
        log.warning(f"[Discord 登入] 讀取使用者失敗（HTTP {st}）")
        raise OAuthError("exchange")
    return me


# ---------- 伺服器成員 / 擁有者 ----------
def _clean_name(s):
    return " ".join(str(s or "").split())[:NAME_MAX]


def _avatar_ok(url):
    return isinstance(url, str) and url.startswith(CDN) and len(url) < 300 and '"' not in url and "<" not in url


def user_avatar(me):
    did, h = str(me.get("id")), me.get("avatar")
    if isinstance(h, str) and h.replace("_", "").isalnum():
        return f"{CDN}avatars/{did}/{h}.png?size=64"
    return f"{CDN}embed/avatars/{(int(did) >> 22) % 6}.png"


async def _find_member(did):
    """機器人所在的伺服器裡找這個人（先找目前在用的伺服器）；→ Member 或 None"""
    import discord
    client = _bot()
    try:
        from .webcore import get_active_guild
        active = get_active_guild()
    except Exception:
        active = None
    guilds = sorted(client.guilds, key=lambda g: g.id != active)
    for gd in guilds:
        m = gd.get_member(did)
        if m:
            return m
    for gd in guilds[:10]:
        try:
            return await gd.fetch_member(did)
        except (discord.NotFound, discord.Forbidden):
            continue
        except discord.HTTPException:
            continue
    return None


def bot_ready():
    try:
        c = _bot()
        return bool(c.is_ready() and c.loop and not c.loop.is_closed())
    except Exception:
        return False


def lookup_member(did):
    """→ Member / None（不是成員）；機器人沒連上 → OAuthError("notready")"""
    if not bot_ready():
        raise OAuthError("notready")
    from .webcore import run_async
    return run_async(_find_member(int(did)))


def in_any_guild_cached(did):
    """只看機器人的成員快取（開了 members intent）；機器人沒連上就當作還在（不誤踢）"""
    if not bot_ready():
        return True
    try:
        return any(gd.get_member(int(did)) for gd in _bot().guilds)
    except Exception:
        return True


def owner_ids():
    ids = set()
    raw = (os.getenv("OWNER_ID") or "").strip()
    if raw.isdigit():
        ids.add(raw)
    else:
        try:
            app = _bot().application
            if app:
                owner = app.team.owner if getattr(app, "team", None) and app.team.owner else app.owner
                if owner:
                    ids.add(str(owner.id))
        except Exception:
            pass
    try:
        from . import userlists
        did = userlists.discord_links().get("owner")
        if did:
            ids.add(str(did))
    except Exception:
        pass
    return ids


def is_owner(did):
    return str(did) in owner_ids()


# ---------- session ----------
def _hash(sid):
    return hashlib.sha256(sid.encode()).hexdigest()


def _load():
    if _sessions["data"] is not None and _sessions["path"] == SESSIONS_FILE:
        return _sessions["data"]
    data = {}
    try:
        with open(SESSIONS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and isinstance(raw.get("sessions"), dict):
            data = {k: v for k, v in raw["sessions"].items() if isinstance(v, dict) and str(v.get("did", "")).isdigit()}
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"[Discord 登入] 讀取登入紀錄失敗，大家要重新登入：{e!r}")
    _sessions.update(data=data, path=SESSIONS_FILE)
    return data


def _save():
    data = _sessions["data"] or {}
    try:
        os.makedirs(os.path.dirname(SESSIONS_FILE), exist_ok=True)
        tmp = SESSIONS_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "sessions": data}, f, ensure_ascii=False, separators=(",", ":"))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, SESSIONS_FILE)
    except Exception as e:
        log.warning(f"[Discord 登入] 寫入登入紀錄失敗：{e!r}")


def _prune_locked(now):
    data = _load()
    dead = [k for k, v in data.items() if now - float(v.get("seen") or 0) > SESSION_IDLE]
    for k in dead:
        data.pop(k, None)
    return bool(dead)


def create_session(member_or_user):
    """登入成功 → 新 session id（明碼只在 cookie 裡）"""
    did = str(member_or_user["id"])
    now = time.time()
    rec = {"did": did, "name": _clean_name(member_or_user.get("name")) or "Discord 使用者",
           "avatar": member_or_user.get("avatar") if _avatar_ok(member_or_user.get("avatar")) else "",
           "created": now, "seen": now, "chk": now}
    sid = secrets.token_urlsafe(32)
    with _lock:
        data = _load()
        _prune_locked(now)
        mine = sorted((v.get("seen", 0), k) for k, v in data.items() if v.get("did") == did)
        for _, k in mine[:max(0, len(mine) - MAX_PER_USER + 1)]:
            data.pop(k, None)
        if len(data) >= MAX_SESSIONS:
            for _, k in sorted((v.get("seen", 0), k) for k, v in data.items())[:len(data) - MAX_SESSIONS + 1]:
                data.pop(k, None)
        data[_hash(sid)] = rec
        _save()
    return sid


def delete_session(sid):
    if not isinstance(sid, str) or not sid:
        return
    with _lock:
        if _load().pop(_hash(sid), None) is not None:
            _save()


def delete_user_sessions(did):
    with _lock:
        data = _load()
        ks = [k for k, v in data.items() if v.get("did") == str(did)]
        for k in ks:
            data.pop(k, None)
        if ks:
            _save()
        return len(ks)


def ident_for_session(sid):
    """session id → 身分 dict；過期 / 被踢 / 連結的成員被停用 → None"""
    if not isinstance(sid, str) or not 20 <= len(sid) <= 100:
        return None
    now = time.time()
    k = _hash(sid)
    with _lock:
        rec = _load().get(k)
        if not rec:
            return None
        if now - float(rec.get("seen") or 0) > SESSION_IDLE:
            _load().pop(k, None)
            _save()
            return None
        did = rec["did"]
        if now - float(rec.get("chk") or 0) > RECHECK_EVERY:
            if not in_any_guild_cached(did):
                log.info("[Discord 登入] 使用者已不在機器人的伺服器裡，登出")
                _load().pop(k, None)
                _save()
                return None
            rec["chk"] = now
        if now - float(rec.get("seen") or 0) > SEEN_WRITE_EVERY:
            rec["seen"] = now
            _save()
        name, avatar = rec.get("name") or "Discord 使用者", rec.get("avatar") or ""
    from . import userlists, extmembers
    owner = is_owner(did)
    list_key = userlists.discord_key(did, owner=owner)     # 跟 Discord /mylist 同一份
    if list_key not in ("owner",) and not list_key.startswith(userlists.DISCORD_PREFIX):
        m = extmembers.member_record(list_key)
        if m and not m.get("enabled", True):
            return None             # 擁有者停用了連結的成員 → 網頁也一起登出
    uid = "owner" if owner else list_key
    return {"id": uid, "name": name, "owner": owner, "discord_id": did, "avatar": avatar,
            "via": "discord", "list_key": list_key, "_sid": sid}


def login_from_code(code):
    """callback 的主體：code → (session id, 顯示名稱)；失敗丟 OAuthError"""
    me = exchange_code(code)
    did = str(me["id"])
    member = lookup_member(did)
    if member is None:
        log.info("[Discord 登入] 不是機器人所在伺服器的成員，拒絕")
        raise OAuthError("notmember")
    name = getattr(member, "display_name", None) or me.get("global_name") or me.get("username")
    avatar = ""
    try:
        avatar = member.display_avatar.with_size(64).url
    except Exception:
        pass
    if not _avatar_ok(avatar):
        avatar = user_avatar(me)
    sid = create_session({"id": did, "name": name, "avatar": avatar})
    return sid, _clean_name(name)


def reset_for_tests(path=None):
    with _lock:
        _sessions.update(data=None, path=None)
        _cb_hits.clear()
        _warned.clear()
    global SESSIONS_FILE
    if path:
        SESSIONS_FILE = path
