"""個人歌單（每個人多份、具名）：data/user_playlists.json（原子寫入、0600）

跟「本地歌單 list.txt」和「伺服器最愛」完全分開，不互相搬移。

身分（user key）：
- 擁有者金鑰（.env EXT_TOKEN）→ "owner"
- 成員金鑰（extmembers）       → 成員 id
- Discord 帳號（網頁用 Discord 登入、Discord /mylist）→ discord_key()：
    有連結（擁有者在 /members 設定）→ 連結的成員 id / "owner"（擴充功能、網頁、/mylist 共用同一份）
    沒連結的擁有者 → "owner"；其他人 → "d:<Discord id>"（自動有自己的歌單，不用連結）
- 連結 Discord 時，那個帳號原本 "d:<id>" 的歌單會合併到成員名下；刪除有連結的成員時，成員的歌單搬到 "d:<id>"
  （都不會丟資料；重做一次也不會重複搬）
"""
import json
import os
import secrets
import threading
import time

from .jsonfile import write_atomic
from .config import log
from .utils import allowed_song_url, safe_title

FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "user_playlists.json")
MAX_LISTS = 20          # 每人最多幾份歌單
MAX_SONGS = 500         # 每份歌單最多幾首
NAME_MAX = 40

_lock = threading.RLock()
_rev = [0]              # 任何人的歌單一變就 +1：網頁 SSE 帶出去，前端看到變了才重抓自己的（不洩漏內容）
_cache = {"data": None, "path": None}


def _empty():
    return {"users": {}, "links": {}}


def _load():
    if _cache["data"] is not None and _cache["path"] == FILE:
        return _cache["data"]
    data = _empty()
    try:
        with open(FILE, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            if isinstance(raw.get("users"), dict):
                data["users"] = raw["users"]
            if isinstance(raw.get("links"), dict):
                data["links"] = {str(k): str(v) for k, v in raw["links"].items()}
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error(f"[個人歌單] 讀取失敗（原檔已改名為 user_playlists.json.corrupt，改用空白資料）：{e!r}")
        bad = FILE + ".corrupt"
        try:
            os.replace(FILE, bad)
        except OSError:
            pass
    _cache.update(data=data, path=FILE)
    return data


def _save():
    data = _cache["data"]
    write_atomic(FILE, data, private=True)
    _rev[0] += 1
    try:
        from .state import notify_change
        notify_change()         # 喚醒 SSE（擴充功能 / Discord /mylist / 其他分頁的變更也會即時推到面板）
    except Exception:
        pass


def rev():
    return _rev[0]


def _reset_cache():
    _cache.update(data=None, path=None)


def _clean_name(name):
    if not isinstance(name, str):
        return ""
    return " ".join(name.split())[:NAME_MAX]


def _clean_songs(songs, limit=MAX_SONGS):
    out, seen = [], set()
    for s in songs or []:
        if not isinstance(s, dict):
            continue
        url = s.get("url").strip() if isinstance(s.get("url"), str) else ""
        if not allowed_song_url(url) or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "title": safe_title(s.get("title"), url)})
        if len(out) >= limit:
            break
    return out


def _lists(key):
    return _load()["users"].setdefault(key, {"lists": []})["lists"]


def _summary(pl):
    return {"id": pl["id"], "name": pl["name"], "count": len(pl["songs"]),
            "created": pl.get("created"), "updated": pl.get("updated"), "source": pl.get("source")}


def _find(key, lid):
    for pl in _lists(key):
        if pl["id"] == lid:
            return pl
    raise KeyError("找不到這份歌單")


# ---------- 查詢 ----------
def list_playlists(key):
    with _lock:
        return [_summary(p) for p in _lists(key)]


def get_playlist(key, lid):
    with _lock:
        pl = _find(key, lid)
        return dict(_summary(pl), songs=[dict(s) for s in pl["songs"]])


def find_by_name(key, name):
    """Discord 指令用：名稱完全相同優先，其次唯一的前綴 / 包含"""
    name = _clean_name(name).lower()
    with _lock:
        ls = _lists(key)
        for p in ls:
            if p["name"].lower() == name:
                return p["id"]
        hits = [p["id"] for p in ls if name and name in p["name"].lower()]
        return hits[0] if len(hits) == 1 else None


# ---------- 修改 ----------
def create(key, name, songs=None, source=None):
    name = _clean_name(name)
    if not name:
        raise ValueError("請輸入歌單名稱")
    with _lock:
        ls = _lists(key)
        if len(ls) >= MAX_LISTS:
            raise ValueError(f"每人最多 {MAX_LISTS} 份歌單")
        if any(p["name"] == name for p in ls):
            raise ValueError("已經有同名的歌單")
        now = time.time()
        pl = {"id": secrets.token_hex(5), "name": name, "created": now, "updated": now,
              "songs": _clean_songs(songs)}
        if source:
            pl["source"] = str(source)[:300]
        ls.append(pl)
        _save()
        return _summary(pl)


def rename(key, lid, name):
    name = _clean_name(name)
    if not name:
        raise ValueError("請輸入歌單名稱")
    with _lock:
        pl = _find(key, lid)
        if any(p["name"] == name and p["id"] != lid for p in _lists(key)):
            raise ValueError("已經有同名的歌單")
        pl["name"], pl["updated"] = name, time.time()
        _save()
        return _summary(pl)


def delete(key, lid):
    with _lock:
        ls = _lists(key)
        pl = _find(key, lid)
        ls.remove(pl)
        _save()
        return True


def add_songs(key, lid, songs):
    """回傳 (加入數, 略過數)；重複的網址不重加，滿 MAX_SONGS 就停"""
    with _lock:
        pl = _find(key, lid)
        have = {s["url"] for s in pl["songs"]}
        added = skipped = 0
        for s in _clean_songs(songs, limit=10 ** 6):
            if s["url"] in have:
                continue
            if len(pl["songs"]) >= MAX_SONGS:
                skipped += 1
                continue
            pl["songs"].append(s)
            have.add(s["url"])
            added += 1
        if not added and skipped:
            raise ValueError(f"歌單已滿（最多 {MAX_SONGS} 首）")
        if added:
            pl["updated"] = time.time()
            _save()
        return added, skipped


def remove_song(key, lid, url):
    with _lock:
        pl = _find(key, lid)
        n = len(pl["songs"])
        pl["songs"] = [s for s in pl["songs"] if s["url"] != url]
        if len(pl["songs"]) == n:
            raise KeyError("歌單裡沒有這首歌")
        pl["updated"] = time.time()
        _save()
        return True


# ---------- Discord 連結 ----------
DISCORD_PREFIX = "d:"


def discord_key(discord_id, owner=False):
    """Discord 使用者 → 個人歌單的 key（網頁 Discord 登入和 /mylist 都用這個，兩邊看到同一份）"""
    did = str(discord_id or "").strip()
    with _lock:
        k = _load()["links"].get(did)
    if k:
        return k
    return "owner" if owner else DISCORD_PREFIX + did


def _merge_locked(src, dst, enforce_cap=True):
    """把 src 的歌單全部搬到 dst（同名的加「（2）」…）；src 沒有歌單 → 什麼都不做。回傳搬了幾份"""
    users = _load()["users"]
    moving = (users.get(src) or {}).get("lists") or []
    if src == dst:
        return 0
    if not moving:
        users.pop(src, None)        # 空的也清掉
        return 0
    have = _lists(dst)
    if enforce_cap and len(have) + len(moving) > MAX_LISTS:
        raise ValueError(f"這個 Discord 帳號已經有 {len(moving)} 份自己的歌單，合併後會超過 {MAX_LISTS} 份，"
                         f"請對方先刪掉一些再連結")
    names = {p["name"] for p in have}
    ids = {p["id"] for p in have}
    for pl in moving:
        pl = dict(pl)
        base, n = pl["name"], 2
        while pl["name"] in names:
            suffix = f"（{n}）"
            pl["name"] = base[:NAME_MAX - len(suffix)] + suffix
            n += 1
        while pl["id"] in ids:
            pl["id"] = secrets.token_hex(5)
        names.add(pl["name"]); ids.add(pl["id"])
        have.append(pl)
    users.pop(src, None)
    return len(moving)


def link_discord(key, discord_id):
    """把 Discord 使用者 id 對應到 key；discord_id 空 → 解除這個 key 的連結。
    連結時：那個 Discord 帳號自己的歌單（"d:<id>"）合併到 key 名下"""
    with _lock:
        links = _load()["links"]
        did = str(discord_id or "").strip()
        if did:
            if not did.isdigit() or not 17 <= len(did) <= 20:
                raise ValueError("Discord 使用者 ID 應為 17～20 位數字")
            if links.get(did) not in (None, key):
                raise ValueError("這個 Discord ID 已經連結到另一位成員，請先解除那邊的連結")
            moved = _merge_locked(DISCORD_PREFIX + did, key)       # 超過上限會丟 ValueError，什麼都沒改
            if moved:
                log.info(f"[個人歌單] Discord 帳號的 {moved} 份歌單已合併到連結的成員")
        for d in [d for d, k in links.items() if k == key]:
            links.pop(d)
        if did:
            links[did] = key
        _save()
        return True


def discord_links():
    with _lock:
        return {k: d for d, k in _load()["links"].items()}


def key_for_discord(discord_id):
    """只看連結（沒連結 → None）"""
    with _lock:
        return _load()["links"].get(str(discord_id))


def drop_user(key):
    """成員被刪除時：有連結 Discord → 歌單搬到那個 Discord 帳號名下（不丟資料）；沒連結 → 歌單一起刪。連結一律移除"""
    with _lock:
        data = _load()
        dids = [d for d, k in data["links"].items() if k == key]
        if dids and (data["users"].get(key) or {}).get("lists"):
            n = _merge_locked(key, DISCORD_PREFIX + dids[0], enforce_cap=False)
            log.info(f"[個人歌單] 刪除成員：{n} 份歌單移到他的 Discord 帳號名下")
            changed = True
        else:
            changed = data["users"].pop(key, None) is not None
        for d in dids:
            data["links"].pop(d); changed = True
        if changed:
            _save()
