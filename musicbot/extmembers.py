"""擴充功能成員（朋友各自一把金鑰）：data/ext_members.json（只有 root 可讀寫）。

只存 SHA-256 雜湊，不存明碼；金鑰只在建立 / 重設時回傳一次。
.env 的 EXT_TOKEN 永遠有效，身分是「擁有者」（可以管理成員）。
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from .config import log

FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ext_members.json")
OWNER = {"id": "owner", "name": "擁有者", "owner": True}
NAME_MAX = 32
_lock = threading.RLock()
_cache = {"mtime": None, "members": []}
_USE_WRITE_EVERY = 60          # 只查狀態時，last_used 最多每 60 秒寫一次檔


def _hash(tok):
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


def owner_token():
    return (os.getenv("EXT_TOKEN") or "").strip()


def _load():
    try:
        mt = os.path.getmtime(FILE)
    except OSError:
        _cache.update(mtime=None, members=[])
        return _cache["members"]
    if mt != _cache["mtime"]:
        try:
            with open(FILE, encoding="utf-8") as f:
                _cache["members"] = list((json.load(f) or {}).get("members") or [])
            _cache["mtime"] = mt
        except Exception as e:
            log.warning(f"[成員] 讀取 {FILE} 失敗：{e!r}")
    return _cache["members"]


def _save(members):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"members": members}, f, ensure_ascii=False, indent=1)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, FILE)
    _cache.update(mtime=os.path.getmtime(FILE), members=members)


def public(m):
    return {k: m.get(k) for k in ("id", "name", "created", "enabled", "last_used", "add_count")}


def list_members():
    with _lock:
        return [public(m) for m in _load()]


def member_record(mid):
    """成員 id → 原始資料（含 token_hash）的複本；找不到 → None（網頁登入每個請求核對用）"""
    with _lock:
        for m in _load():
            if m.get("id") == mid:
                return dict(m)
    return None


def authenticate(tok):
    """金鑰 → 身分 dict（擁有者或啟用中的成員）；無效 → None"""
    tok = (tok or "").strip()
    if not tok:
        return None
    own = owner_token()
    if own and hmac.compare_digest(tok.encode(), own.encode()):
        return dict(OWNER)
    h = _hash(tok)
    with _lock:
        for m in _load():
            if m.get("enabled", True) and hmac.compare_digest(h, m.get("token_hash", "")):
                return {"id": m["id"], "name": m["name"], "owner": False}
    return None


def record_use(ident, added=False):
    """記錄最後使用時間 / 點歌次數（擁有者不記）"""
    if not ident or ident.get("owner"):
        return
    with _lock:
        members = _load()
        for m in members:
            if m["id"] == ident["id"]:
                now = time.time()
                if not added and now - (m.get("last_used") or 0) < _USE_WRITE_EVERY:
                    return
                m["last_used"] = now
                if added:
                    m["add_count"] = int(m.get("add_count") or 0) + 1
                _save(members)
                return


def _clean_name(name):
    if not isinstance(name, str):
        return ""
    name = " ".join(name.split())[:NAME_MAX]
    return name


def create(name):
    """建立成員 → (公開資料, 明碼金鑰)；名字空白 / 重複 → ValueError"""
    name = _clean_name(name)
    if not name:
        raise ValueError("請輸入名字")
    with _lock:
        members = list(_load())
        if any(m["name"] == name for m in members):
            raise ValueError("已經有同名的成員")
        tok = secrets.token_urlsafe(32)
        m = {"id": secrets.token_hex(6), "name": name, "token_hash": _hash(tok), "created": time.time(),
             "enabled": True, "last_used": None, "add_count": 0}
        members.append(m)
        _save(members)
        log.info(f"[成員] 新增 {name}")
        return public(m), tok


def _update(mid, fn):
    with _lock:
        members = list(_load())
        for i, m in enumerate(members):
            if m["id"] == mid:
                res = fn(members, i, m)
                _save(members)
                return res
    raise KeyError(mid)


def rename(mid, name):
    name = _clean_name(name)
    if not name:
        raise ValueError("請輸入名字")
    def fn(members, i, m):
        if any(o["name"] == name and o["id"] != mid for o in members):
            raise ValueError("已經有同名的成員")
        m["name"] = name
        return public(m)
    return _update(mid, fn)


def set_enabled(mid, enabled):
    def fn(members, i, m):
        m["enabled"] = bool(enabled)
        return public(m)
    return _update(mid, fn)


def reset_token(mid):
    tok = secrets.token_urlsafe(32)
    def fn(members, i, m):
        m["token_hash"] = _hash(tok)
        return public(m)
    return _update(mid, fn), tok


def delete(mid):
    def fn(members, i, m):
        members.pop(i)
        return True
    return _update(mid, fn)
