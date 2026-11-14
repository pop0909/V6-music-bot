"""網頁 API：共用歌單、最愛、網址解析、搜尋。

從 webapi.py 拆出，路由與行為不變；由 webapi.py 最後載入（flask_app、登入檢查等 hook 都在 webapi.py）。

webapi.py 的函式與測試會替換的名字（webapi.X，例如 run_async）一律經 `_w.X` 在呼叫時取用，
mock.patch.object(webapi, ...) 照樣生效。
"""
import random

import yt_dlp
from flask import request, jsonify

from .config import log
from .state import state_lock
from .storage import load_favorites, add_favorite_song, favorites_cache
from .utils import clean_str, valid_song_url, safe_title
from . import ytdl
from .state import queue_skip_message
from .webcore import web_add_to_queue, web_play_all
from .webapi import flask_app
from . import webapi as _w


def _resolve_index(items, index, expect_url=None):
    """以索引操作清單，但有給 expect_url 時要確認是同一首（防連點 / 多分頁改到別首）。
    索引已偏移 → 改找同網址的第一首；找不到 → -1。"""
    if not isinstance(expect_url, str) or not expect_url:
        return index if 0 <= index < len(items) else -1
    if 0 <= index < len(items) and items[index].get("url") == expect_url:
        return index
    for i, it in enumerate(items):
        if it.get("url") == expect_url:
            return i
    return -1


@flask_app.route("/api/playlist", methods=["GET"])
def api_playlist():
    with state_lock:
        pl = _w.local_playlist.copy()
    return jsonify({"playlist": pl, "count": len(pl)})

@flask_app.route("/api/playlist/add", methods=["POST"])
def api_playlist_add():
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"status": "error", "message": "請提供網址"}), 400
    if not valid_song_url(url):
        return jsonify({"status": "error", "message": "網址格式不正確"}), 400
    title = safe_title(data.get("title"), url)
    with state_lock:
        _w.local_playlist.append({"url": url, "title": title})
        _w.save_local_playlist()
    return jsonify({"status": "ok", "count": len(_w.local_playlist)})

@flask_app.route("/api/playlist/edit", methods=["POST"])
def api_playlist_edit():
    data = request.get_json() or {}
    index = int(data.get("index", -1))
    url = (data.get("url") or "").strip()
    title = (data.get("title") or "").strip()
    if not url or not title:
        return jsonify({"status": "error", "message": "網址和標題不能為空"}), 400
    if not valid_song_url(url):
        return jsonify({"status": "error", "message": "網址格式不正確"}), 400
    with state_lock:
        index = _resolve_index(_w.local_playlist, index, data.get("expect_url"))
        if index < 0:
            return (jsonify({"status": "error", "code": "stale", "message": "歌單已變動，請重新整理後再試"}), 409) if data.get("expect_url") else (jsonify({"status": "error", "message": "無效索引"}), 400)
        _w.local_playlist[index] = {"url": url, "title": safe_title(title, url)}
        _w.save_local_playlist()
    return jsonify({"status": "ok"})

@flask_app.route("/api/playlist/delete", methods=["POST"])
def api_playlist_delete():
    data = request.get_json() or {}
    index = int(data.get("index", -1))
    with state_lock:
        index = _resolve_index(_w.local_playlist, index, data.get("expect_url"))
        if index < 0:
            return (jsonify({"status": "error", "code": "stale", "message": "歌單已變動（可能已刪除），請重新整理"}), 409) if data.get("expect_url") else (jsonify({"status": "error", "message": "無效索引"}), 400)
        _w.local_playlist.pop(index)
        _w.save_local_playlist()
    return jsonify({"status": "ok"})

@flask_app.route("/api/playlist/play", methods=["POST"])
def api_playlist_play():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    index = int(data.get("index", -1))
    if index < 0 or index >= len(_w.local_playlist):
        return jsonify({"status": "error", "message": "無效索引"}), 400
    song = _w.local_playlist[index]
    result = _w.run_async(web_add_to_queue(gid, song["url"], song["title"], actor=_w._actor()))
    if not result:
        return jsonify({"status": "error", "message": "加入失敗"})
    if result.get("full") or result.get("invalid"):
        return jsonify({"status": "error", "code": "queue_full" if result.get("full") else "invalid",
                        "message": result.get("message")}), 409 if result.get("full") else 400
    if result.get("duplicate"):
        return jsonify({"status": "duplicate", "title": song["title"],
                        "message": result.get("message", "")})
    return jsonify({"status": "ok", "title": song["title"], "position": result.get("position"),
                    "starts_now": bool(result.get("starts_now"))})

@flask_app.route("/api/playlist/play-all", methods=["POST"])
def api_playlist_play_all():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    shuffle = data.get("shuffle", False)
    with state_lock:
        songs = _w.local_playlist.copy()
    if shuffle: random.shuffle(songs)
    stats = {}
    count = _w.run_async(web_play_all(gid, songs, stats=stats, actor=_w._actor(), label="本地歌單"))
    skipped = stats.get("full_skipped", 0)
    return jsonify({"status": "ok", "count": count or 0, "full_skipped": skipped, "message": queue_skip_message(skipped)})

@flask_app.route("/api/favorites", methods=["GET"])
def api_favorites():
    gid = request.args.get("guild_id", 0, type=int)
    with state_lock:
        favs = load_favorites(gid)
    return jsonify({"favorites": favs, "count": len(favs)})

@flask_app.route("/api/favorites/add", methods=["POST"])
def api_favorites_add():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    url = (data.get("url") or "").strip()
    if not url: return jsonify({"status": "error", "message": "請提供網址"}), 400
    if not valid_song_url(url): return jsonify({"status": "error", "message": "網址格式不正確"}), 400
    title = safe_title(data.get("title"), url)
    with state_lock:
        added = add_favorite_song(gid, url, title)
    return jsonify({"status": "added" if added else "exists", "title": title})

@flask_app.route("/api/favorites/remove", methods=["POST"])
def api_favorites_remove():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    index = int(data.get("index", -1))
    with state_lock:
        favs = load_favorites(gid)
        index = _resolve_index(favs, index, data.get("expect_url"))
        if index < 0:
            return (jsonify({"status": "error", "code": "stale", "message": "最愛已變動（可能已移除），請重新整理"}), 409) if data.get("expect_url") else (jsonify({"status": "error", "message": "無效索引"}), 400)
        favs.pop(index)
        favorites_cache[gid] = favs
        _w.save_favorites(gid)
    return jsonify({"status": "ok"})

@flask_app.route("/api/resolve-url", methods=["POST"])
def api_resolve_url():
    """解析 YouTube URL，傳回歌曲資訊（支援播放清單）"""
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"status": "error", "message": "請提供網址"}), 400
    if not valid_song_url(url):
        return jsonify({"status": "error", "message": "網址格式不正確（需要 http/https 連結）"}), 400
    try:
        with yt_dlp.YoutubeDL(ytdl.build_opts({
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "noplaylist": False,
            "playlistend": 100,
        })) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return jsonify({"status": "error", "message": "無法解析網址"}), 400

            if "entries" in info:
                playlist_title = clean_str(info.get("title", "未知播放清單"))
                songs = []
                for entry in info["entries"]:
                    if entry:
                        song_url = entry.get("webpage_url") or entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"
                        songs.append({
                            "url": song_url,
                            "title": clean_str(entry.get("title", "未知")),
                            "duration": entry.get("duration", 0),
                            "thumbnail": f"https://i.ytimg.com/vi/{entry['id']}/hqdefault.jpg" if entry.get("id") else ""
                        })
                return jsonify({
                    "status": "ok",
                    "is_playlist": True,
                    "playlist_title": playlist_title,
                    "songs": songs,
                    "count": len(songs)
                })
            else:
                song_url = info.get("webpage_url") or info.get("url") or url
                return jsonify({
                    "status": "ok",
                    "is_playlist": False,
                    "songs": [{
                        "url": song_url,
                        "title": clean_str(info.get("title", "未知")),
                        "duration": info.get("duration", 0),
                        "thumbnail": f"https://i.ytimg.com/vi/{info['id']}/hqdefault.jpg" if info.get("id") else ""
                    }],
                    "count": 1
                })
    except Exception as e:
        log.error(f"Resolve URL error: {str(e)[:300]}")
        return jsonify({"status": "error", "message": ytdl.friendly_error(e, "網址解析時"),
                        "code": "bot_check" if ytdl.is_bot_check(e) else "error"}), 500


@flask_app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json() or {}
    query = data.get("query", "").strip()
    if not query: return jsonify({"status": "error", "message": "請輸入關鍵字"}), 400
    try:
        with yt_dlp.YoutubeDL(ytdl.build_opts({
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "default_search": "ytsearch5",
            "extract_flat": True,
            "noplaylist": True,
        })) as ydl:
            info = ydl.extract_info(f"ytsearch5:{query}", download=False)
            results = []
            if "entries" in info:
                for entry in info["entries"]:
                    if entry:
                        results.append({
                            "url": f"https://www.youtube.com/watch?v={entry['id']}",
                            "title": clean_str(entry.get("title", "未知")),
                            "duration": entry.get("duration", 0),
                            "thumbnail": f"https://i.ytimg.com/vi/{entry['id']}/hqdefault.jpg"
                        })
            return jsonify({"status": "ok", "results": results, "count": len(results)})
    except Exception as e:
        log.error(f"搜尋錯誤: {str(e)[:300]}")
        return jsonify({"status": "error", "message": ytdl.friendly_error(e, "搜尋時"),
                        "code": "bot_check" if ytdl.is_bot_check(e) else "error"}), 500
