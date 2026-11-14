"""網頁 API：播放紀錄 / 排行、操作紀錄、佇列（加入 / 批次 / 匯入 / 恢復 / 刪除 / 排序 / 指定播放 / 清空）。

從 webapi.py 拆出，路由與行為不變；由 webapi.py 最後載入（flask_app、登入檢查等 hook 都在 webapi.py）。

webapi.py 的函式與測試會替換的名字（webapi.X，例如 run_async）一律經 `_w.X` 在呼叫時取用，
mock.patch.object(webapi, ...) 照樣生效。
"""

from flask import request, jsonify

from .bot import client
from .state import state_lock, queues, voice_clients, guild_names, remember_duration
from .utils import valid_song_url
from . import history
from .state import queue_full_message, queue_skip_message
from .webcore import (
    web_add_to_queue, web_play_index, web_add_to_queue_batch, get_active_guild, web_import_songs,
)
from . import importer
from . import actionlog
from .webapi import flask_app
from . import webapi as _w


def _history_gid():
    gid = request.args.get("guild_id", 0, type=int)
    if not gid:
        gid = get_active_guild() or (next(iter(guild_names)) if guild_names else 0)
    return gid


def _history_item(r):
    return {
        "url": r.get("url", ""), "title": r.get("title", ""), "thumbnail": r.get("thumbnail") or "",
        "duration": r.get("duration") or 0,
    }


@flask_app.route("/api/history")
def api_history():
    """最近播放紀錄（不走 SSE，切到分頁時才抓）"""
    gid = _history_gid()
    if not gid:
        return jsonify({"status": "ok", "items": []})
    limit = request.args.get("limit", 50, type=int)
    items = []
    for r in history.recent(gid, limit):
        remember_duration(r.get("url"), r.get("duration"))
        it = _history_item(r)
        it.update({"played_at": r["played_at"], "requester": r.get("requester") or "",
                   "source": r.get("source") or "user"})
        items.append(it)
    return jsonify({"status": "ok", "items": items})


@flask_app.route("/api/history/top")
def api_history_top():
    """播放排行：range=7d|30d|all"""
    gid = _history_gid()
    rng = request.args.get("range", "all")
    if rng not in history.RANGES:
        rng = "all"
    if not gid:
        return jsonify({"status": "ok", "range": rng, "items": []})
    limit = request.args.get("limit", 20, type=int)
    items = []
    for r in history.top(gid, rng, limit):
        it = _history_item(r)
        it.update({"plays": r["plays"], "last_played": r["last_played"]})
        items.append(it)
    return jsonify({"status": "ok", "range": rng, "items": items})


@flask_app.route("/api/actions")
def api_actions():
    """操作紀錄（點歌 / 跳過 / 刪歌，最近 7 天，新的在前）：有金鑰登入的人都能看"""
    gid = _history_gid()
    if not gid:
        return jsonify({"status": "ok", "items": [], "keep_days": actionlog.KEEP_DAYS})
    limit = request.args.get("limit", 200, type=int)
    return jsonify({"status": "ok", "items": actionlog.recent(gid, limit), "keep_days": actionlog.KEEP_DAYS})


@flask_app.route("/api/queue/add", methods=["POST"])
def api_queue_add():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    # 沒指定伺服器時自動 fallback：active guild → 第一個有語音連線的 → 第一個 guild
    # 讓新開分頁的使用者不必選伺服器就能直接點歌
    if gid == 0:
        with state_lock:
            if get_active_guild():
                gid = get_active_guild()
            else:
                connected = [g for g, vc in voice_clients.items() if vc and vc.is_connected()]
                if connected:
                    gid = connected[0]
                elif guild_names:
                    gid = next(iter(guild_names))
    if gid == 0:
        return jsonify({"status": "error", "message": "Bot 尚未連線到任何伺服器"}), 400
    url = data.get("url", "").strip()
    title = data.get("title", url)
    no_play = data.get("no_play", False)
    if not url:
        return jsonify({"status": "error", "message": "請提供網址"}), 400
    if not valid_song_url(url):
        return jsonify({"status": "error", "message": "網址格式不正確（需要 http/https 連結）"}), 400
    result = _w.run_async(web_add_to_queue(gid, url, title, auto_play=not no_play, duration=data.get("duration"),
                                        actor=_w._actor()))
    if not result:
        return jsonify({"status": "error", "message": "加入失敗"})
    if result.get("invalid"):
        return jsonify({"status": "error", "message": result.get("message")}), 400
    if result.get("full"):
        return jsonify({"status": "error", "code": "queue_full", "message": result.get("message")}), 409
    if result.get("duplicate"):
        return jsonify({"status": "duplicate", "title": title, "position": result.get("position"),
                        "message": result.get("message", "")})
    # position（佇列第幾首）/ starts_now（馬上開播）是新增欄位，舊前端忽略即可
    return jsonify({"status": "added", "title": title, "position": result.get("position"),
                    "starts_now": bool(result.get("starts_now"))})

@flask_app.route("/api/queue/add-batch", methods=["POST"])
def api_queue_add_batch():
    """批次加入多首歌曲到佇列（不自動播放）"""
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    # 與 /api/queue/add 相同的 fallback 邏輯
    if gid == 0:
        with state_lock:
            if get_active_guild():
                gid = get_active_guild()
            else:
                connected = [g for g, vc in voice_clients.items() if vc and vc.is_connected()]
                if connected:
                    gid = connected[0]
                elif guild_names:
                    gid = next(iter(guild_names))
        if gid == 0:
            return jsonify({"status": "error", "message": "Bot 尚未連線到任何伺服器"}), 400
    songs = data.get("songs", [])
    if not songs:
        return jsonify({"status": "error", "message": "請提供歌曲列表"}), 400
    with state_lock:
        before = len(queues.get(gid, []))
    stats = {}
    count = _w.run_async(web_add_to_queue_batch(gid, songs, stats=stats, actor=_w._actor()))
    skipped = stats.get("full_skipped", 0)
    if not count and skipped:
        return jsonify({"status": "error", "code": "queue_full", "message": queue_full_message(),
                        "count": 0, "full_skipped": skipped}), 409
    # position = 第一首加入後排在佇列第幾首（新增欄位）
    return jsonify({"status": "ok", "count": count or 0, "position": (before + 1) if count else None,
                    "full_skipped": skipped, "message": queue_skip_message(skipped)})


@flask_app.route("/api/import", methods=["POST"])
def api_import():
    """匯入 YouTube / Spotify 歌單（背景解析，前端用 /api/import/<id> 輪詢進度）"""
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify({"status": "error", "message": "請貼上 YouTube 或 Spotify 歌單網址"}), 400
    gid = int(data.get("guild_id", 0) or 0)
    if gid == 0:
        with state_lock:
            gid = get_active_guild() or next(
                (g for g, vc in voice_clients.items() if vc and vc.is_connected()),
                next(iter(guild_names), 0))
    if not gid:
        return jsonify({"status": "error", "message": "Bot 尚未連線到任何伺服器"}), 400
    actor = _w._actor()        # 背景執行緒裡沒有 flask.g，先取好
    def _done(name, songs):
        stats = {}
        added = _w.run_async(web_import_songs(gid, songs, stats=stats, actor=actor, label=f"匯入：{name}" if name else "匯入歌單"))
        return added, stats.get("full_skipped", 0)
    jid = importer.start_job(url, _done)
    return jsonify({"status": "ok", "job": jid}), 202


@flask_app.route("/api/restore", methods=["POST"])
def api_restore():
    """恢復休眠前的佇列（play=true 從上次位置繼續播）"""
    from . import idlesleep
    data = request.get_json(silent=True) or {}
    gid = int(data.get("guild_id") or 0) or (get_active_guild() or 0)
    if not gid:
        return jsonify({"status": "error", "message": "請先選擇伺服器"}), 400
    r = _w.run_async(idlesleep.restore(gid, play=bool(data.get("play", True))))
    return jsonify(r or {"status": "error", "message": "恢復失敗"})


@flask_app.route("/api/import/<jid>")
def api_import_status(jid):
    job = importer.get_job(jid)
    if not job or job.get("owner_key") is not None:     # 個人歌單的匯入工作只能用本人金鑰查（/api/ext/lists/import/<jid>）
        return jsonify({"status": "error", "message": "找不到匯入工作"}), 404
    return jsonify(job)


@flask_app.route("/api/queue/remove", methods=["POST"])
def api_queue_remove():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    index = int(data.get("index", -1))
    removed = None
    with state_lock:
        if gid in queues and 0 <= index < len(queues[gid]):
            removed = queues[gid].pop(index)
    if removed:
        actionlog.record(gid, "remove", _w._actor(), removed[1], url=removed[0])
    return jsonify({"status": "ok"})

@flask_app.route("/api/queue/reorder", methods=["POST"])
def api_queue_reorder():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    frm = int(data.get("from", -1))
    to = int(data.get("to", -1))
    expect = (data.get("url") or "").strip()
    with state_lock:
        q = queues.get(gid)
        if not q or not (0 <= frm < len(q)) or not (0 <= to < len(q)):
            return jsonify({"status": "error", "message": "佇列已變動，請重試"}), 409
        if expect and q[frm][0] != expect:   # 拖曳期間佇列被別人改過 → 不要搬錯歌
            return jsonify({"status": "error", "message": "佇列已變動，請重試"}), 409
        q.insert(to, q.pop(frm))
    try:
        from . import prefetch
        if client.loop and not client.loop.is_closed():
            client.loop.call_soon_threadsafe(prefetch.ensure, gid)
    except Exception:
        pass
    return jsonify({"status": "ok"})

@flask_app.route("/api/queue/play", methods=["POST"])
def api_queue_play():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    index = int(data.get("index", -1))
    result = _w.run_async(web_play_index(gid, index, actor=_w._actor()))
    return _w._ack(result)

@flask_app.route("/api/queue/clear", methods=["POST"])
def api_queue_clear():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    with state_lock:
        if gid in queues: queues[gid] = []
    return jsonify({"status": "cleared"})
