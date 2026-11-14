"""Flask 網頁伺服器：API 路由 + SSE"""
import json
import os
import random
import time

import yt_dlp
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, redirect, g

from .bot import client
from .config import log
from .state import (
    state_lock, queues, voice_clients, volumes, volume_transformers,
    random_loop_guilds, guild_names,
    local_playlist, sleep_timers, remember_duration,
    notify_change, wait_change, change_version,
)
from .storage import (
    load_favorites, save_favorites, add_favorite_song, save_local_playlist,
    favorites_cache,
)
from .utils import clean_str, valid_song_url, safe_title
from . import ytdl, history
from .state import queue_full_message, queue_skip_message
from .webcore import (
    run_async, web_add_to_queue, web_toggle_play, web_skip, web_stop,
    web_set_loop, web_join_voice, web_play_index,
    web_add_to_queue_batch, web_play_all, web_set_autoplay, _set_active_guild, get_active_guild,
    get_guild_list, get_guild_status, get_full_app_state, web_set_normalize, web_import_songs,
)
from . import importer
from . import webauth, extmembers, actionlog


def _actor():
    """操作紀錄用：網頁面板登入的金鑰成員名稱（擁有者 = 「擁有者」），來源 = 網頁面板"""
    u = getattr(g, "panel_user", None) or {}
    return (u.get("name") or "網頁", "web")

# 模組化後 Flask 位於套件內，需明確指向專案根目錄的 templates/ 與 static/
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
flask_app = Flask(
    __name__,
    template_folder=os.path.join(_PROJECT_ROOT, "templates"),
    static_folder=os.path.join(_PROJECT_ROOT, "static"),
)


# 只允許透過公開網域（Cloudflare Tunnel）或本機存取；直接用 IP:5000 連線一律拒絕
_ALLOWED_HOSTS = {h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "musbot.kdns.fr").split(",") if h.strip()}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


@flask_app.before_request
def _check_host():
    host = (request.host or "").lower()
    if host.startswith("["):
        name = host.split("]")[0] + "]"
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if name in _ALLOWED_HOSTS or name in _LOCAL_HOSTS or "*" in _ALLOWED_HOSTS:
        return None
    return Response("Forbidden", status=403, mimetype="text/plain")


# ================== 登入（每個人用自己的擴充功能金鑰） ==================
# 不用登入：登入頁、登出、靜態檔；/api/ext/* 交給 extapi（X-Ext-Token 金鑰，或網頁登入的 cookie）
_PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico"}


def _auth_fail(path):
    if path.startswith("/api/") or request.method not in ("GET", "HEAD"):
        return jsonify({"error": "auth_required", "message": "請先登入"}), 401
    from urllib.parse import quote
    nxt = request.full_path.rstrip("?") if request.query_string else path
    return redirect("/login" + ("" if nxt == "/" else "?next=" + quote(nxt, safe="/")))


@flask_app.before_request
def _require_login():
    path = request.path
    if flask_app.config.get("LOGIN_DISABLED"):      # 只有測試程式會設（舊測試直接打 API）
        g.panel_user = dict(extmembers.OWNER)
        return None
    if path in _PUBLIC_PATHS or path.startswith("/static/") or path.startswith("/api/ext/"):
        return None
    ident, issued = webauth.parse_cookie(request.cookies.get(webauth.COOKIE))
    if not ident:
        return _auth_fail(path)
    g.panel_user = ident
    if webauth.needs_renew(issued):         # 有在用就自動續期（等於只要登入一次）
        g.renew_login = ident
    if request.method not in ("GET", "HEAD", "OPTIONS") and not webauth.same_origin(request):
        return jsonify({"error": "forbidden", "message": "來源不符"}), 403
    if webauth.is_owner_only(path) and not ident.get("owner"):
        if path.startswith("/api/"):
            return jsonify({"status": "error", "error": "forbidden", "message": "只有擁有者可以使用"}), 403
        return Response("只有擁有者可以使用這個頁面", status=403, mimetype="text/plain; charset=utf-8")
    return None


@flask_app.route("/login", methods=["GET", "POST"])
def login():
    ip = webauth.client_ip(request)
    nxt = webauth.safe_next(request.values.get("next"))
    if request.method == "GET":
        if webauth.current_user():
            return redirect(nxt)
        wait = webauth.locked_for(ip)
        return render_template("login.html", next=nxt, error=_lock_msg(wait) if wait else None, locked=bool(wait))
    if not extmembers.owner_token():
        return render_template("login.html", next=nxt, error="伺服器還沒設定 EXT_TOKEN，暫時無法登入", locked=True), 503
    wait = webauth.locked_for(ip)
    if wait:
        return render_template("login.html", next=nxt, error=_lock_msg(wait), locked=True), 429
    if not webauth.same_origin(request):
        return render_template("login.html", next=nxt, error="來源不符，請重新整理再試", locked=False), 403
    ident = extmembers.authenticate(request.form.get("key"))
    value = webauth.make_cookie_value(ident) if ident else None
    if not value:
        left, lock = webauth.record_fail(ip)
        log.info(f"[登入] 金鑰錯誤（{ip}，剩 {left} 次）")
        if lock:
            return render_template("login.html", next=nxt, error=_lock_msg(lock), locked=True), 429
        return render_template("login.html", next=nxt, error=f"金鑰錯誤或已停用，還可以再試 {left} 次", locked=False), 401
    webauth.clear_fails(ip)
    extmembers.record_use(ident)
    log.info(f"[登入] {ident.get('name')} 登入面板（{ip}）")
    resp = redirect(nxt)
    webauth.set_login_cookie(resp, request, value)
    return resp


def _lock_msg(secs):
    return f"輸錯太多次，請 {max(1, (secs + 59) // 60)} 分鐘後再試"


@flask_app.route("/logout", methods=["POST"])
def logout():
    resp = redirect("/login")
    webauth.clear_login_cookie(resp, request)
    return resp


@flask_app.route("/api/me")
def api_me():
    u = g.panel_user
    return jsonify({"status": "ok", "name": u.get("name"), "owner": bool(u.get("owner"))})


@flask_app.before_request
def _wake_on_action():
    """網頁上任何操作（POST）都會把該伺服器從休眠叫醒"""
    if request.method != "POST" or not request.path.startswith("/api/"):
        return None
    if not g.get("panel_user") and not (request.path.startswith("/api/ext/") and (
            extmembers.authenticate(request.headers.get("X-Ext-Token")) or webauth.current_user())):
        return None     # 沒登入 / 金鑰錯誤的請求不叫醒
    try:
        from . import idlesleep
        data = request.get_json(silent=True) or {}
        gid = int(data.get("guild_id") or 0) or (get_active_guild() or 0)
        if gid and gid in idlesleep.asleep:
            idlesleep.wake(gid, "網頁操作")
    except Exception:
        pass
    return None


@flask_app.after_request
def _renew_login(resp):
    """登入 cookie 簽發超過 1 天 → 重發一張新的（再 400 天）；串流回應不動"""
    ident = g.get("renew_login")
    if ident and not resp.is_streamed:
        value = webauth.make_cookie_value(ident)
        if value:
            webauth.set_login_cookie(resp, request, value)
    return resp


@flask_app.after_request
def _no_store_api(resp):
    """API 回應一律不快取（瀏覽器 / Cloudflare 都不會拿到舊清單）；SSE 自己設 no-cache 就不動"""
    if request.path.startswith("/api/") and "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-store"
    return resp


@flask_app.after_request
def _notify_sse(resp):
    """任何 POST（控制操作）完成後立刻喚醒 SSE，前端不必等下一個 1 秒 tick 才看到伺服器狀態"""
    if request.method == "POST":
        notify_change()
    return resp


def _ack(result):
    """背景執行的慢操作（跳過 / 停止 / 指定播放 / 跳轉）回 202 Accepted"""
    if not result:
        return jsonify({"status": "error"})
    return jsonify(result), (202 if result.get("accepted") else 200)


@flask_app.errorhandler(Exception)
def api_error_handler(e):
    """所有未捕捉例外統一回 JSON，避免 HTML 錯誤頁打壞前端解析"""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({"error": e.name.lower().replace(" ", "_"), "message": e.description}), e.code
    if isinstance(e, (ValueError, TypeError, AttributeError)) and request.path.startswith("/api/"):
        # 使用者送了格式錯誤的參數（例如 guild_id="abc"、JSON 是陣列）：回 400，不當成伺服器錯誤
        log.warning(f"API bad request {request.path}: {e!r}")
        return jsonify({"error": "bad_request", "message": "參數格式不正確"}), 400
    log.error(f"API error: {e}", exc_info=True)
    return jsonify({"error": "internal", "message": str(e)}), 500


@flask_app.route("/")
def index():
    return render_template("index.html", me=g.panel_user)

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
    result = run_async(web_toggle_play(gid))
    return jsonify(result or {"status": "error"})

@flask_app.route("/api/skip", methods=["POST"])
def api_skip():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    result = run_async(web_skip(gid, actor=_actor()))
    return _ack(result)

@flask_app.route("/api/stop", methods=["POST"])
def api_stop():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    result = run_async(web_stop(gid))
    return _ack(result)

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
    result = run_async(web_set_loop(gid, mode))
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
    result = run_async(web_set_autoplay(gid, enabled))
    return jsonify(result or {"status": "error"})

@flask_app.route("/api/normalize", methods=["POST"])
def api_normalize():
    """切換音量平衡（播放中從目前位置立即生效）。body: {guild_id, enabled}"""
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    if not gid:
        return jsonify({"status": "error", "message": "缺少 guild_id"}), 400
    result = run_async(web_set_normalize(gid, bool(data.get("enabled", True))))
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
    result = run_async(seek(gid, data.get("position"), wait=False))
    if not result:
        return jsonify({"status": "error", "code": "failed", "message": "跳轉失敗"}), 500
    if result.get("status") != "ok":
        return jsonify(result), SEEK_HTTP.get(result.get("code"), 400)
    return _ack(result)


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
    result = run_async(web_add_to_queue(gid, url, title, auto_play=not no_play, duration=data.get("duration"),
                                        actor=_actor()))
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
    count = run_async(web_add_to_queue_batch(gid, songs, stats=stats, actor=_actor()))
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
    actor = _actor()        # 背景執行緒裡沒有 flask.g，先取好
    def _done(name, songs):
        stats = {}
        added = run_async(web_import_songs(gid, songs, stats=stats, actor=actor, label=f"匯入：{name}" if name else "匯入歌單"))
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
    r = run_async(idlesleep.restore(gid, play=bool(data.get("play", True))))
    return jsonify(r or {"status": "error", "message": "恢復失敗"})


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
        actionlog.record(gid, "remove", _actor(), removed[1], url=removed[0])
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
    result = run_async(web_play_index(gid, index, actor=_actor()))
    return _ack(result)

@flask_app.route("/api/queue/clear", methods=["POST"])
def api_queue_clear():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    with state_lock:
        if gid in queues: queues[gid] = []
    return jsonify({"status": "cleared"})

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
        pl = local_playlist.copy()
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
        local_playlist.append({"url": url, "title": title})
        save_local_playlist()
    return jsonify({"status": "ok", "count": len(local_playlist)})

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
        index = _resolve_index(local_playlist, index, data.get("expect_url"))
        if index < 0:
            return (jsonify({"status": "error", "code": "stale", "message": "歌單已變動，請重新整理後再試"}), 409) if data.get("expect_url") else (jsonify({"status": "error", "message": "無效索引"}), 400)
        local_playlist[index] = {"url": url, "title": safe_title(title, url)}
        save_local_playlist()
    return jsonify({"status": "ok"})

@flask_app.route("/api/playlist/delete", methods=["POST"])
def api_playlist_delete():
    data = request.get_json() or {}
    index = int(data.get("index", -1))
    with state_lock:
        index = _resolve_index(local_playlist, index, data.get("expect_url"))
        if index < 0:
            return (jsonify({"status": "error", "code": "stale", "message": "歌單已變動（可能已刪除），請重新整理"}), 409) if data.get("expect_url") else (jsonify({"status": "error", "message": "無效索引"}), 400)
        local_playlist.pop(index)
        save_local_playlist()
    return jsonify({"status": "ok"})

@flask_app.route("/api/playlist/play", methods=["POST"])
def api_playlist_play():
    data = request.get_json() or {}
    gid = int(data.get("guild_id", 0))
    index = int(data.get("index", -1))
    if index < 0 or index >= len(local_playlist):
        return jsonify({"status": "error", "message": "無效索引"}), 400
    song = local_playlist[index]
    result = run_async(web_add_to_queue(gid, song["url"], song["title"], actor=_actor()))
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
        songs = local_playlist.copy()
    if shuffle: random.shuffle(songs)
    stats = {}
    count = run_async(web_play_all(gid, songs, stats=stats, actor=_actor(), label="本地歌單"))
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
        save_favorites(gid)
    return jsonify({"status": "ok"})

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
    result = run_async(web_join_voice(gid, channel_id))
    return jsonify(result or {"status": "error"})


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
    result = run_async(_set_sleep_timer_async(gid, mode, minutes))
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
    # stream_with_context：讓 generator 內仍能存取 request context。
    # 沒有它，generator 一進入就脫離 request context，任何 request 存取都會炸。
    def generate():
        # 只在狀態變更時推送：比對完整狀態 JSON，相同就跳過。
        # 這樣多數 tick 只是輕量字串比較，且前端不會收到無意義的重複事件。
        last_payload = None
        last_sent = time.monotonic()
        ver = change_version()
        while True:
            # 長連線也要隨時核對登入：金鑰被停用 / 刪除 / 重設 → 通知前端回登入頁並結束
            # （停用成員是 POST → notify_change 會立刻喚醒這裡，所以幾乎是即時的）
            if check_auth and not webauth.ident_from_cookie(cookie):
                yield "event: auth\ndata: logout\n\n"
                break
            try:
                data = get_full_app_state(gid)
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
            new_ver = wait_change(ver, 15.0 if idlesleep.all_asleep() else 1.0)
            if new_ver != ver:
                ver = new_ver
                time.sleep(0.015)   # 合併同一波連續變更（例如清狀態 + 更新面板），少推幾次
                ver = change_version()
    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


import logging as _logging
import re as _re
import threading as _threading


class _ForeignRequestFilter(_logging.Filter):
    """本機其他程式（例如 Steam）一直打 127.0.0.1:<埠>/ 的 POST，會在終端機洗出大量
    「"POST / HTTP/1.1" 405」。這些不是面板發的請求：不印每一行，改成每 10 分鐘彙總提示一次。
    只過濾「不是本面板路徑 + 404/405」的存取紀錄，其他照常顯示。"""
    _pat = _re.compile(r'"([A-Z{}\[\]":,\w-]*?)(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) (\S+) HTTP/[\d.]+" (404|405) ')
    _interval = 600.0

    def __init__(self, port):
        super().__init__()
        self.port = port
        self.lock = _threading.Lock()
        self.count = 0
        self.last_hint = 0.0
        self.sample = ""

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        m = self._pat.search(msg)
        if not m:
            return True
        path = m.group(3).split("?", 1)[0]
        if path.startswith("/api/") or path.startswith("/static/"):
            return True   # 面板自己的路徑出錯要看得到
        now = time.monotonic()
        with self.lock:
            self.count += 1
            self.sample = f"{m.group(2)} {path} → {m.group(4)}"
            if now - self.last_hint < self._interval:
                return False
            n, self.count, self.last_hint = self.count, 0, now
        log.warning(f"[Web] 有非本面板的程式在對 127.0.0.1:{self.port} 送請求（例如 {self.sample}，"
                    f"{n} 次）；已忽略且不再逐行顯示。若是 Steam 等軟體佔用這個埠，"
                    f"可在 .env 設 WEB_PORT=5050 之類換埠。")
        return False


def start_flask():
    """啟動 Flask（在背景執行緒中呼叫）"""
    from .config import PUBLIC_URL, WEB_HOST, WEB_PORT
    _logging.getLogger("werkzeug").addFilter(_ForeignRequestFilter(WEB_PORT))
    log.info(f"控制面板: http://127.0.0.1:{WEB_PORT}（監聽 {WEB_HOST}）")
    log.info(f"公開網址: {PUBLIC_URL}")
    flask_app.run(host=WEB_HOST, port=WEB_PORT, threaded=True, debug=False, use_reloader=False)


async def _set_sleep_timer_async(gid, mode, minutes):
    """在 bot event loop 中設定睡眠計時器"""
    from .player import set_sleep_timer
    ok, msg = set_sleep_timer(gid, mode, minutes)
    return {"status": "ok" if ok else "error", "message": msg}


# Chrome 擴充功能 API（/api/ext/*，需 EXT_TOKEN）：最後才載入，它會用到上面的 flask_app
from . import extapi as _extapi  # noqa: E402,F401
from . import userlistapi as _userlistapi  # noqa: E402,F401
from . import webstatic as _webstatic  # noqa: E402,F401
