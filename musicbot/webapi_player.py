"""網頁 API：伺服器 / 狀態、播放控制（播放 / 跳過 / 停止 / 音量 / 循環 / 洗牌 / 自動推薦 / 音量平衡 / 跳轉）、語音頻道、睡眠計時器、系統狀態與更新、即時狀態串流（SSE）。

從 webapi.py 拆出，路由與行為不變；由 webapi.py 最後載入（flask_app、登入檢查等 hook 都在 webapi.py）。

webapi.py 的函式與測試會替換的名字（webapi.X，例如 run_async）一律經 `_w.X` 在呼叫時取用，
mock.patch.object(webapi, ...) 照樣生效。
"""
import json
import time

from flask import request, jsonify, Response, stream_with_context

from .bot import client
from .config import log
from .state import (
    state_lock, volumes, volume_transformers, random_loop_guilds, guild_names, sleep_timers,
    wait_change, change_version,
)
from .webcore import (
    web_toggle_play, web_skip, web_stop, web_set_loop, web_join_voice, web_set_autoplay,
    _set_active_guild, get_active_guild, get_guild_list, get_guild_status, web_set_normalize,
)
from . import webauth
from .webapi import flask_app
from . import webapi as _w


@flask_app.route("/api/guilds")
def api_guilds():
    return jsonify({"guilds": get_guild_list()})

@flask_app.route("/api/active-guild", methods=["GET"])
def api_active_guild_get():
    with state_lock:
        # 傳字串避免 JavaScript 精度丟失（Discord Snowflake ID 超過 JS 安全整數）
        return jsonify({"active_guild_id": str(get_active_guild()) if get_active_guild() is not None else None})

@flask_app.route("/api/active-guild", methods=["POST"])
def api_active_guild_set():
    data = request.get_json() or {}
    raw = data.get("guild_id")
    with state_lock:
        if raw is not None:
            _set_active_guild(int(raw))
        else:
            _set_active_guild(None)
        return jsonify({"active_guild_id": str(get_active_guild()) if get_active_guild() is not None else None})

@flask_app.route("/api/status")
def api_status():
    gid = request.args.get("guild_id", 0, type=int)
    if gid == 0:
        with state_lock:
            if get_active_guild():
                gid = get_active_guild()
            else:
                guilds = get_guild_list()
                if guilds:
                    gid = guilds[0]["id"]
                else:
                    return jsonify({"error": "no_guilds", "message": "Bot 尚未連線到任何伺服器"})
    return jsonify(get_guild_status(gid))

@flask_app.route("/api/play", methods=["POST"])
def api_play():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    result = _w.run_async(web_toggle_play(gid))
    return jsonify(result or {"status": "error"})

@flask_app.route("/api/skip", methods=["POST"])
def api_skip():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    result = _w.run_async(web_skip(gid, actor=_w._actor()))
    return _w._ack(result)

@flask_app.route("/api/stop", methods=["POST"])
def api_stop():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    result = _w.run_async(web_stop(gid))
    return _w._ack(result)

@flask_app.route("/api/volume", methods=["POST"])
def api_volume():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    level = int(data.get("level", 100))
    with state_lock:
        vol = max(0, min(200, level)) / 100.0
        volumes[gid] = vol
        if gid in volume_transformers:
            volume_transformers[gid].volume = vol
    return jsonify({"status": "ok", "volume": round(vol * 100)})

@flask_app.route("/api/loop", methods=["POST"])
def api_loop():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    mode = data.get("mode", "none")
    result = _w.run_async(web_set_loop(gid, mode))
    return jsonify(result or {"status": "error"})

@flask_app.route("/api/shuffle", methods=["POST"])
def api_shuffle():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    enabled = bool(data.get("enabled", False))
    with state_lock:
        random_loop_guilds[gid] = enabled
    return jsonify({"status": "ok", "shuffle_mode": enabled})

@flask_app.route("/api/autoplay", methods=["POST"])
def api_autoplay():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    if not gid:
        return jsonify({"status": "error", "message": "缺少 guild_id"}), 400
    enabled = bool(data.get("enabled", False))
    result = _w.run_async(web_set_autoplay(gid, enabled))
    return jsonify(result or {"status": "error"})

@flask_app.route("/api/normalize", methods=["POST"])
def api_normalize():
    """切換音量平衡（播放中從目前位置立即生效）。body: {guild_id, enabled}"""
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    if not gid:
        return jsonify({"status": "error", "message": "缺少 guild_id"}), 400
    result = _w.run_async(web_set_normalize(gid, bool(data.get("enabled", True))))
    return jsonify(result or {"status": "error"})


@flask_app.route("/api/seek", methods=["POST"])
def api_seek():
    """跳到目前歌曲的指定秒數。body: {guild_id, position}（秒，可含小數）
    暫停中跳轉會維持暫停，繼續播放時從新位置開始。"""
    from .player import seek, SEEK_HTTP
    data = request.get_json(silent=True) or {}
    try:
        gid = int(data.get("guild_id", 0) or 0)
    except (TypeError, ValueError):
        gid = 0
    if not gid:
        return jsonify({"status": "error", "code": "invalid", "message": "缺少 guild_id"}), 400
    # wait=False：驗證完立刻回應，FFmpeg 重開 + 預熱在背景（舊位置繼續播到新位置就緒為止）
    result = _w.run_async(seek(gid, data.get("position"), wait=False))
    if not result:
        return jsonify({"status": "error", "code": "failed", "message": "跳轉失敗"}), 500
    if result.get("status") != "ok":
        return jsonify(result), SEEK_HTTP.get(result.get("code"), 400)
    return _w._ack(result)

@flask_app.route("/api/system")
def api_system():
    from . import sysinfo
    return jsonify(sysinfo.status(request.args.get("guild_id", 0, type=int) or None))


@flask_app.route("/api/system/update", methods=["POST"])
def api_system_update():
    """一鍵更新 yt-dlp + 重啟（需確認字串、10 分鐘最多一次）"""
    from . import sysinfo
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != "UPDATE":
        return jsonify({"status": "error", "message": "需要確認"}), 400
    ok, msg = sysinfo.start_update()
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (202 if ok else 429)

@flask_app.route("/api/guilds/<int:gid>/voice-channels")
def api_voice_channels(gid):
    """取得伺服器的語音頻道列表"""
    guild = client.get_guild(gid)
    if not guild:
        return jsonify({"status": "error", "message": "找不到伺服器"}), 404
    channels = []
    for c in guild.voice_channels:
        channels.append({"id": str(c.id), "name": c.name, "members": len(c.members)})
    return jsonify({"channels": channels})


@flask_app.route("/api/join", methods=["POST"])
def api_join():
    """從 Web 加入語音頻道"""
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    channel_id = int(data.get("channel_id", 0))
    if not gid or not channel_id:
        return jsonify({"status": "error", "message": "缺少 guild_id 或 channel_id"}), 400
    result = _w.run_async(web_join_voice(gid, channel_id))
    return jsonify(result or {"status": "error"})


# ================== 睡眠計時器 ==================
@flask_app.route("/api/sleep-timer", methods=["POST"])
def api_sleep_timer():
    """設定/取消睡眠計時器。body: {guild_id, mode: 'minutes'|'track_end'|'off', minutes?: int}"""
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    mode = data.get("mode", "off")
    minutes = int(data.get("minutes", 0))
    if gid == 0:
        with state_lock:
            gid = get_active_guild() or (next(iter(guild_names)) if guild_names else 0)
    if gid == 0:
        return jsonify({"status": "error", "message": "Bot 尚未連線到任何伺服器"}), 400
    if mode not in ("minutes", "track_end", "off"):
        return jsonify({"status": "error", "message": "無效模式"}), 400
    if mode == "minutes" and not (1 <= minutes <= 480):
        return jsonify({"status": "error", "message": "分鐘數需在 1~480 之間"}), 400
    result = _w.run_async(_set_sleep_timer_async(gid, mode, minutes))
    return jsonify(result or {"status": "error"})


@flask_app.route("/api/sleep-timer", methods=["GET"])
def api_sleep_timer_get():
    """查詢目前睡眠計時器狀態"""
    gid = request.args.get("guild_id", 0, type=int)
    with state_lock:
        info = sleep_timers.get(gid)
        if not info:
            return jsonify({"active": False})
        remaining = None
        if info.get("deadline"):
            remaining = max(0, int(info["deadline"] - time.time()))
        return jsonify({"active": True, "mode": info.get("mode"), "remaining_seconds": remaining})


# ================== SSE 即時進度串流 ==================
@flask_app.route("/api/progress-stream")
def api_progress_stream():
    """
    Server-Sent Events 即時推送完整應用狀態：狀態一變（notify_change）就推，否則最多每秒一次
    取代前端所有輪詢 API，單一通道驅動所有 UI 更新
    """
    gid = request.args.get("guild_id", 0, type=int)
    check_auth = not flask_app.config.get("LOGIN_DISABLED")
    cookie = request.cookies.get(webauth.COOKIE)
    # waitress 提供：客戶端已斷線就回 True（見 webserver.py）；開發伺服器沒有 → 等寫入失敗才知道
    disconnected = request.environ.get("waitress.client_disconnected")
    # 每個 SSE 連線會一直佔一條伺服器執行緒：名額滿了回 503（前端退避後重連），留執行緒給一般請求
    from . import webserver
    release = webserver.sse_acquire()
    if release is None:
        log.warning(f"[Web] 即時更新連線已達上限（{webserver.sse_max()}），請稍後重連")
        return jsonify({"error": "busy", "message": "即時更新連線太多，稍後自動重連"}), 503
    # stream_with_context：讓 generator 內仍能存取 request context。
    # 沒有它，generator 一進入就脫離 request context，任何 request 存取都會炸。
    def generate():
        # 只在狀態變更時推送：比對完整狀態 JSON，相同就跳過。
        # 這樣多數 tick 只是輕量字串比較，且前端不會收到無意義的重複事件。
        last_payload = None
        last_sent = time.monotonic()
        ver = change_version()
        while True:
            if disconnected is not None and disconnected():
                break                    # 分頁已關：結束串流、歸還執行緒與名額
            # 長連線也要隨時核對登入：金鑰被停用 / 刪除 / 重設 → 通知前端回登入頁並結束
            # （停用成員是 POST → notify_change 會立刻喚醒這裡，所以幾乎是即時的）
            if check_auth and not webauth.ident_from_cookie(cookie):
                yield "event: auth\ndata: logout\n\n"
                break
            try:
                data = _w.get_full_app_state(gid)
                payload = f"data: {json.dumps(data)}\n\n"
                if payload != last_payload:
                    yield payload
                    last_payload = payload
                    last_sent = time.monotonic()
                elif time.monotonic() - last_sent >= 30:
                    # 每 30 秒強制推送一次心跳，避免中間設備（Nginx/Tunnel）把閒置連線掐掉
                    yield ": keepalive\n\n"
                    last_sent = time.monotonic()
            except GeneratorExit:
                raise  # 客戶端斷線，正常結束
            except Exception as e:
                log.error(f"SSE error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
            # 休眠中沒有東西會動：只等狀態變更（醒來）或 15 秒一次的心跳，不再每秒重算
            from . import idlesleep
            timeout = 15.0 if idlesleep.all_asleep() else 1.0
            waited = 0.0
            while True:                  # 分段等，每秒看一次分頁是否已關（不重算狀態，休眠時一樣省）
                step = min(1.0, timeout - waited)
                new_ver = wait_change(ver, step)
                waited += step
                if new_ver != ver or waited >= timeout or (disconnected is not None and disconnected()):
                    break
            if new_ver != ver:
                ver = new_ver
                time.sleep(0.015)   # 合併同一波連續變更（例如清狀態 + 更新面板），少推幾次
                ver = change_version()
    resp = Response(stream_with_context(generate()), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    resp.call_on_close(release)   # 客戶端斷線 / 串流結束時 WSGI 伺服器會呼叫 close() → 歸還名額
    return resp

async def _set_sleep_timer_async(gid, mode, minutes):
    """在 bot event loop 中設定睡眠計時器"""
    from .player import set_sleep_timer
    ok, msg = set_sleep_timer(gid, mode, minutes)
    return {"status": "ok" if ok else "error", "message": msg}
