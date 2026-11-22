"""一次性：把舊的「伺服器最愛」（favorites/<伺服器 id>.txt）搬進擁有者的「我的歌單」。

最愛功能已拿掉（網頁、Discord 都沒有了），只留「我的歌單」。啟動時（第一次 on_ready）跑一次：
- 每個伺服器的最愛 → 擁有者名下一份「最愛（伺服器名）」；機器人已不在那個伺服器 → 「最愛（伺服器 id）」
- 空的最愛不建歌單；同一首（同網址）只放一次；不支援的網址（白名單外）略過並記在日誌
- 一份歌單最多 userlists.MAX_SONGS 首：超過就接著放「最愛（伺服器名）2」「…3」
- 歌單份數到上限（userlists.MAX_LISTS）放不下 → 那個伺服器這次不算完成，日誌說明，下次啟動再試
- 搬好的伺服器記在 data/fav_migrated.json（0600）；之後重啟不會再搬（就算擁有者把那份歌單刪了也不會再冒出來）。
  記號檔不見了再跑一次也不會重複：同名歌單已在 → 只補沒有的歌
- 原本的 favorites/*.txt 一律不刪也不改名：留著當備份（自動備份也照樣打包），要還原隨時可以
"""
import os
import re
import time

from .config import FAVORITES_DIR, PROJECT_ROOT, log
from .jsonfile import write_atomic
from .utils import allowed_song_url, safe_title
from . import userlists

MARKER = os.path.join(PROJECT_ROOT, "data", "fav_migrated.json")
_FILE_RE = re.compile(r"^(\d+)\.txt$")


def read_favorites(path):
    """舊格式：一行「網址 標題」（# 開頭是註解）"""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(" ", 1)
            out.append({"url": parts[0], "title": parts[1] if len(parts) > 1 else parts[0]})
    return out


def list_name(guild_label, part=1):
    suffix = "" if part <= 1 else str(part)
    room = userlists.NAME_MAX - len("最愛（）") - len(suffix)
    label = " ".join(str(guild_label).split())
    if len(label) > room:
        label = label[:max(room - 1, 1)] + "…"
    return f"最愛（{label}）{suffix}"


def _load_marker(path):
    import json
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and isinstance(raw.get("guilds"), dict):
            return raw
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"[最愛搬移] 讀不到 {os.path.basename(path)}（當作還沒搬過；重搬也不會重複）：{e!r}")
    return {"guilds": {}}


def _migrate_one(key, gid, label, songs):
    """回傳結果 dict；放不下（歌單份數到上限）→ 丟 ValueError"""
    res = {"found": len(songs), "added": 0, "dupes": 0, "invalid": 0, "lists": []}
    clean, seen = [], set()
    for s in songs:
        url = s["url"].strip()
        if not allowed_song_url(url):
            res["invalid"] += 1
            continue
        if url in seen:
            res["dupes"] += 1
            continue
        seen.add(url)
        clean.append({"url": url, "title": safe_title(s.get("title"), url)})
    if not clean:
        return res
    # 已經有的「最愛（X）」「最愛（X）2」…（上次搬到一半 / 記號檔不見）：裡面有的歌不再加
    mine = {p["name"]: p for p in userlists.list_playlists(key)}
    have = set()
    part = 1
    while list_name(label, part) in mine:
        have |= {s["url"] for s in userlists.get_playlist(key, mine[list_name(label, part)]["id"])["songs"]}
        part += 1
    todo = [s for s in clean if s["url"] not in have]
    res["dupes"] += len(clean) - len(todo)
    part = 1
    while todo:
        name = list_name(label, part)
        pl = mine.get(name)
        if pl is None:
            pl = userlists.create(key, name, source="最愛搬移")
            mine[name] = pl
        room = userlists.MAX_SONGS - userlists.get_playlist(key, pl["id"])["count"]
        if room > 0:
            chunk, todo = todo[:room], todo[room:]
            added, _ = userlists.add_songs(key, pl["id"], chunk)
            res["added"] += added
            res["lists"].append(name)
        part += 1
    return res


def migrate(owner_key, guild_names, fav_dir=None, marker=None):
    """把還沒搬過的最愛檔搬到 owner_key 名下。回傳 {gid: 結果}（這次有處理的伺服器）"""
    fav_dir = fav_dir or FAVORITES_DIR
    marker = marker or MARKER
    try:
        files = sorted(n for n in os.listdir(fav_dir) if _FILE_RE.match(n))
    except FileNotFoundError:
        return {}
    state = _load_marker(marker)
    done = state["guilds"]
    results = {}
    for fn in files:
        gid = _FILE_RE.match(fn).group(1)
        if gid in done:
            continue
        name = guild_names.get(int(gid)) if gid.isdigit() else None
        label = name or gid
        try:
            songs = read_favorites(os.path.join(fav_dir, fn))
            res = _migrate_one(owner_key, gid, label, songs)
        except ValueError as e:      # 歌單份數到上限之類：這次不算完成，下次啟動再試
            log.warning(f"[最愛搬移] 伺服器 {label}：放不進「我的歌單」（{e}），下次啟動再試")
            results[gid] = {"error": str(e)}
            continue
        except Exception as e:
            log.error(f"[最愛搬移] 伺服器 {label}：搬移失敗 {e!r}，下次啟動再試")
            results[gid] = {"error": repr(e)}
            continue
        res.update(at=time.time(), key=owner_key, guild=label)
        done[gid] = res
        results[gid] = res
        write_atomic(marker, state, private=True)       # 每搬好一個就記下來（中途當機也不會重搬）
        if not res["found"]:
            log.info(f"[最愛搬移] 伺服器 {label}：最愛是空的，不建歌單")
        else:
            extra = "".join([f"、重複略過 {res['dupes']} 首" if res["dupes"] else "",
                             f"、網址不支援略過 {res['invalid']} 首" if res["invalid"] else ""])
            lists = "、".join(f"「{n}」" for n in dict.fromkeys(res["lists"])) or "（都已經在歌單裡）"
            log.info(f"[最愛搬移] 伺服器 {label}：最愛 {res['found']} 首 → {lists} 新增 {res['added']} 首{extra}"
                     f"（原檔 favorites/{fn} 保留）")
    return results


def owner_key():
    """擁有者看到的「我的歌單」：跟網頁用 Discord 登入、/mylist 一樣的 key（通常是 "owner"）"""
    try:
        from . import discordauth
        did = discordauth.owner_discord_id()
    except Exception:
        did = None
    return userlists.discord_key(did, owner=True) if did else "owner"


def run(guild_names):
    """啟動時呼叫；不會丟例外"""
    try:
        res = migrate(owner_key(), dict(guild_names))
        if res:
            ok = [r for r in res.values() if "error" not in r]
            log.info(f"[最愛搬移] 完成 {len(ok)}/{len(res)} 個伺服器，共新增 {sum(r['added'] for r in ok)} 首到擁有者的「我的歌單」")
        return res
    except Exception as e:
        log.error(f"[最愛搬移] 失敗：{e!r}")
        return {}
