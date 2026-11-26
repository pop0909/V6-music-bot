"""Chrome 擴充功能專用 API（/api/ext/*）：需要金鑰（X-Ext-Token 標頭）。
擁有者 = .env 的 EXT_TOKEN；朋友 = 成員金鑰（extmembers.py，可在網頁「成員管理」建立 / 停用）。

- GET  /api/ext/status            目前播放 + 進度 / 音量 + 佇列數量 + 語音頻道（擁有者金鑰另外回 voice_guilds）
- POST /api/ext/add {url, title?, next?}
        加入佇列（YouTube 或 B 站單一影片）；next=true → 插到下一首
- POST /api/ext/control {action: toggle|skip|volume, level?, guild_id?}
        播放／暫停、跳過、調音量（0–200，100 為原音量）
- POST /api/ext/import {url, guild_id?}   加入整個播放清單／B 站合集（背景工作）
- GET  /api/ext/import/<id>               查匯入進度（只能查自己的工作）
允許 chrome-extension:// 來源的 CORS；沒設 EXT_TOKEN 時整組停用（503）。
"""
import re
import time
from urllib.parse import urlparse, parse_qs

from flask import request, jsonify, g, render_template

from .config import log, DEFAULT_VOLUME
from . import extmembers
from .state import (
    state_lock, queues, now_playing, now_playing_url, voice_clients, guild_names,
    current_voice_channel, notify_change, song_start_times, song_durations,
    paused_position, now_playing_thumbnail, volumes, volume_transformers,
)
from .utils import clean_str, format_time, song_url_error
from .webapi import flask_app
from .webcore import run_async, web_add_to_queue, web_toggle_play, web_skip, web_import_songs

PREFIX = "/api/ext/"
TITLE_MAX = 200       # 擴充功能送來的標題（不可信）上限，避免 Discord 訊息超過 2000 字而送不出去
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@flask_app.before_request
def _ext_auth():
    if not request.path.startswith(PREFIX) or request.method == "OPTIONS":
        return None
    if not extmembers.owner_token():
        return jsonify({"status": "error", "message": "伺服器未設定 EXT_TOKEN，擴充功能 API 已停用"}), 503
    if "X-Ext-Token" in request.headers:        # Chrome 擴充功能：金鑰標頭（跟以前一樣）
        from . import webauth
        ip = webauth.client_ip(request)
        wait = webauth.locked_for(ip, "ext")
        if wait:                                # 同一個 IP 一直用錯的金鑰 → 先鎖 10 分鐘（防猜金鑰）
            return jsonify({"status": "error",
                            "message": f"金鑰錯太多次，請 {max(1, (wait + 59) // 60)} 分鐘後再試（請確認擴充功能選項裡的金鑰）"}), 429
        ident = with_discord_name(extmembers.authenticate(request.headers.get("X-Ext-Token")))
        if not ident:
            webauth.record_fail(ip, "ext")
    else:                                       # 網頁面板：登入的 cookie（每次請求重新核對金鑰是否還有效）
        from . import webauth
        ident = webauth.current_user()
        if ident and request.method not in ("GET", "HEAD") and not webauth.same_origin(request):
            return jsonify({"status": "error", "message": "來源不符"}), 403
    if not ident:
        return jsonify({"status": "error", "message": "金鑰錯誤或已停用（請向擁有者索取新金鑰）"}), 401
    g.ext_user = ident
    if request.path.startswith(PREFIX + "members") and not ident.get("owner"):
        return jsonify({"status": "error", "message": "只有擁有者可以管理成員"}), 403
    return None


@flask_app.after_request
def _ext_cors(resp):
    if request.path.startswith(PREFIX):
        origin = request.headers.get("Origin") or ""
        if origin.startswith("chrome-extension://"):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Ext-Token"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            resp.headers["Access-Control-Max-Age"] = "600"
    return resp


def normalize_youtube(url):
    """各種 YouTube 網址 → https://www.youtube.com/watch?v=ID；不是單一影片 → None"""
    try:
        u = urlparse((url or "").strip())
    except Exception:
        return None
    host = (u.hostname or "").lower()
    vid = None
    if host in ("youtu.be", "www.youtu.be"):
        vid = u.path.strip("/").split("/")[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if u.path == "/watch":
            vid = (parse_qs(u.query).get("v") or [None])[0]
        else:
            m = re.match(r"^/(?:shorts|live|embed)/([^/?#]+)", u.path)
            vid = m.group(1) if m else None
    if vid and _ID_RE.match(vid):
        return f"https://www.youtube.com/watch?v={vid}"
    return None


def normalize_media_url(url, *, resolve_short=True):
    """單一影片網址 → 正規化的 YouTube 或 B 站網址；不是單一影片 → None。"""
    yt = normalize_youtube(url)
    if yt:
        return yt
    from . import bilibili
    bili = bilibili.normalize(url)
    if bili:
        return bili
    if not resolve_short or not url:
        return None
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlparse(raw).hostname or "").lower().rstrip(".")
    except Exception:
        return None
    if not any(host == x or host.endswith("." + x) for x in ("b23.tv", "b23.wtf", "bili2233.cn")):
        return None
    got = bilibili.resolve_short(url)
    return normalize_media_url(got, resolve_short=False) if got else None


def _lookup_title(url):
    """擴充功能沒給標題（右鍵連結）→ 用 yt-dlp 快速查（只取資訊、不解析串流）"""
    try:
        import yt_dlp
        from . import ytdl
        with yt_dlp.YoutubeDL(ytdl.build_opts({"quiet": True, "no_warnings": True, "noplaylist": True,
                                               "extract_flat": True, "skip_download": True})) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        return clean_str(info.get("title") or "") or None
    except Exception as e:
        log.info(f"[擴充功能] 查標題失敗：{str(e)[:120]}")
        return None


def with_discord_name(ident):
    """擴充功能的金鑰 → 有連結 Discord 帳號就改用 Discord 名字（操作紀錄 / 佇列「誰點的」/ 擴充功能顯示的「我是」）。
    擁有者金鑰 = 擁有者的 Discord（「我的 Discord」連結 > OWNER_ID > 應用程式擁有者）；成員金鑰 = 成員管理裡連結的 Discord。
    沒連結或查不到名字 → 照舊用金鑰名稱。身分 id（歌單、點歌次數）不變。"""
    if not ident:
        return ident
    try:
        from . import discordauth, userlists
        if ident.get("owner"):
            did = discordauth.owner_discord_id()
        else:
            did = userlists.discord_links().get(ident.get("id"))
        if not did:
            return ident
        out = dict(ident, discord_id=str(did), key_name=ident.get("name"))
        name = discordauth.display_name(did)
        if name:
            out["name"] = name
        return out
    except Exception as e:
        log.info(f"[擴充功能] 查 Discord 名字失敗：{e!r}")
        return ident


def ext_actor():
    """操作紀錄用：(名稱, 來源, Discord id)。帶 X-Ext-Token 標頭 = Chrome 擴充功能（有連結 Discord → Discord 名字，否則金鑰名稱）；
    用登入 cookie = 網頁面板（Discord 登入 → Discord 名稱與 id）"""
    u = getattr(g, "ext_user", None) or {}
    who = u.get("name") or "未知"
    if "X-Ext-Token" in request.headers:
        return (who, "ext", u.get("discord_id"))
    return (who, "web", u.get("discord_id"))


def _pick_guild():
    """沒指定伺服器：這把金鑰 / 這個人的目前伺服器 → 在語音中的 → 第一個（只在自己有加入的伺服器裡挑，見 guildscope.py）"""
    from . import guildscope
    return guildscope.default_guild(g.get("ext_user"))


async def _move_to_next(gid, url):
    q = queues.get(gid) or []
    for i, (u, _t) in enumerate(q):
        if u == url:
            if i > 0:
                q.insert(0, q.pop(i))
                try:
                    from . import prefetch
                    prefetch.ensure(gid)
                except Exception:
                    pass
            return True
    return False


def _is_asleep(gid):
    try:
        from . import idlesleep
        return bool(gid) and gid in idlesleep.asleep       # 真的因沒人聽而休眠（不是單純沒進語音）
    except Exception:
        return False


@flask_app.route(PREFIX + "status", methods=["GET"])
def ext_status():
    extmembers.record_use(g.ext_user)
    gid = request.args.get("guild_id", 0, type=int) or _pick_guild()
    vc = voice_clients.get(gid)
    connected = bool(vc and vc.is_connected())
    ch = current_voice_channel.get(gid) if connected else None
    elapsed = 0.0
    duration = 0
    thumb = ""
    if connected and gid:
        if gid in song_start_times:
            elapsed = max(0.0, time.time() - song_start_times[gid])
        else:
            elapsed = float(paused_position.get(gid, 0) or 0)
        elapsed = round(elapsed, 3)
        duration = int(song_durations.get(gid) or 0)
        thumb = now_playing_thumbnail.get(gid) or ""
    vol = float(volumes.get(gid, DEFAULT_VOLUME) if gid else DEFAULT_VOLUME)
    volume = int(round(max(0.0, min(2.0, vol)) * 100))
    return jsonify({
        "status": "ok",
        "guild_name": guild_names.get(gid, ""),
        "connected": connected,
        "is_playing": bool(connected and vc.is_playing()),
        "is_paused": bool(connected and vc.is_paused()),
        "current_title": now_playing.get(gid, "") if connected else "",
        "current_url": now_playing_url.get(gid, "") if connected else "",
        "thumbnail": thumb,
        "elapsed": elapsed,
        "elapsed_str": format_time(elapsed),
        "duration": duration,
        "duration_str": format_time(duration) if duration else "",
        "volume": volume,
        "queue_length": len(queues.get(gid, [])),
        "voice_channel": (ch or {}).get("name"),
        "you": g.ext_user.get("name"),
        "you_discord": bool(g.ext_user.get("discord_id")),
        "asleep": _is_asleep(gid),
        "owner": bool(g.ext_user.get("owner")),
        **({"voice_guilds": _voice_guild_count()} if g.ext_user.get("owner") else {}),
    })


def _voice_guild_count():
    """機器人目前在幾個伺服器的語音頻道裡（只給擁有者金鑰：deploy/update-ytdlp.sh 判斷「有沒有人在聽」用）"""
    with state_lock:
        vcs = list(voice_clients.values())
    n = 0
    for vc in vcs:
        try:
            if vc and vc.is_connected():
                n += 1
        except Exception:
            n += 1          # 判斷不出來 → 當作在用（寧可不重啟）
    return n


@flask_app.route(PREFIX + "add", methods=["POST"])
def ext_add():
    data = request.get_json(silent=True) or {}
    url = normalize_media_url(data.get("url"))
    if not url:
        return jsonify({"status": "error", "message": "不是 YouTube 或 B 站影片網址（播放清單請用「加入整個清單」）"}), 400
    gid = int(data.get("guild_id") or 0) or _pick_guild()
    if not gid:
        return jsonify({"status": "error", "message": "機器人尚未連線到伺服器"}), 400
    title = (clean_str(data.get("title") or "") or "").strip()[:TITLE_MAX] or _lookup_title(url) or url
    nxt = bool(data.get("next"))
    who = g.ext_user.get("name")
    result = run_async(web_add_to_queue(gid, url, title, auto_play=True, requester=who, actor=ext_actor()))
    if not result:
        return jsonify({"status": "error", "message": "加入失敗"}), 500
    if result.get("full"):
        return jsonify({"status": "error", "code": "queue_full", "message": result.get("message")}), 409
    if result.get("duplicate") and result.get("position") == 0:
        return jsonify({"status": "duplicate", "title": title, "message": "這首歌正在播放"})
    if not result.get("duplicate"):
        extmembers.record_use(g.ext_user, added=True)
        log.info(f"[擴充功能] {who} 點歌：{title}")
    if nxt and not result.get("starts_now"):
        run_async(_move_to_next(gid, url))
        notify_change()
        return jsonify({"status": "next", "title": title, "position": 1,
                        "message": f"下一首播：{title}"})
    if result.get("duplicate"):
        return jsonify({"status": "duplicate", "title": title, "position": result.get("position"),
                        "message": result.get("message", "已在佇列中")})
    msg = f"開始播放：{title}" if result.get("starts_now") else f"已加入佇列第 {result.get('position')} 首：{title}"
    return jsonify({"status": "added", "title": title, "position": result.get("position"),
                    "starts_now": bool(result.get("starts_now")), "message": msg})


def _ext_gid(data=None):
    data = data or {}
    return int(data.get("guild_id") or 0) or _pick_guild()


def _import_owner_key(ident=None):
    ident = ident or g.ext_user or {}
    if ident.get("owner"):
        return "owner"
    return ident.get("id") or ident.get("list_key") or ""


def _is_importable_list(url):
    from . import importer, bilibili
    if importer.is_spotify(url) or "list=" in (url or "").lower() or "/playlist" in (url or "").lower():
        return True
    raw = (url or "").strip()
    info = bilibili.parse_resource(raw)
    if info is None:
        host = ""
        try:
            u = raw if "://" in raw else "https://" + raw
            host = (urlparse(u).hostname or "").lower().rstrip(".")
        except Exception:
            host = ""
        if any(host == x or host.endswith("." + x) for x in ("b23.tv", "b23.wtf", "bili2233.cn")):
            got = bilibili.resolve_short(url)
            info = bilibili.parse_resource(got) if got else None
    return bool(info and info.get("kind") in ("season", "favlist"))


@flask_app.route(PREFIX + "control", methods=["POST"])
def ext_control():
    """播放／暫停、跳過、音量。權限與 /api/ext/add 相同（金鑰 + 自己能用的伺服器）。"""
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip().lower()
    gid = _ext_gid(data)
    if not gid:
        return jsonify({"status": "error", "message": "機器人尚未連線到伺服器"}), 400
    if action in ("toggle", "play", "pause"):
        result = run_async(web_toggle_play(gid))
        return jsonify(result or {"status": "error", "message": "操作失敗"})
    if action == "skip":
        result = run_async(web_skip(gid, actor=ext_actor()))
        return jsonify(result or {"status": "error", "message": "操作失敗"})
    if action == "volume":
        try:
            level = int(data.get("level", 100))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "音量格式不正確"}), 400
        vol = max(0, min(200, level)) / 100.0
        with state_lock:
            volumes[gid] = vol
            if gid in volume_transformers:
                try:
                    volume_transformers[gid].volume = vol
                except Exception:
                    pass
        notify_change()
        return jsonify({"status": "ok", "volume": int(round(vol * 100))})
    return jsonify({"status": "error", "message": "未知的操作"}), 400


@flask_app.route(PREFIX + "import", methods=["POST"])
def ext_import():
    """把 YouTube／Spotify 播放清單或 B 站合集／公開收藏夾整份加入佇列（背景解析，用 /api/ext/import/<id> 查進度）。"""
    from . import importer
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip() if isinstance(data.get("url"), str) else ""
    err = song_url_error(url) if url else "請提供播放清單或合集網址"
    if err:
        return jsonify({"status": "error", "message": err}), 400
    if not _is_importable_list(url):
        return jsonify({"status": "error", "message": "這不是播放清單或合集網址（單一影片請用「加入機器人」）"}), 400
    gid = _ext_gid(data)
    if not gid:
        return jsonify({"status": "error", "message": "機器人尚未連線到伺服器"}), 400
    actor = ext_actor()
    who = g.ext_user.get("name")
    key = _import_owner_key()

    def _done(name, songs):
        stats = {}
        added = run_async(web_import_songs(
            gid, songs, stats=stats, actor=actor, requester=who,
            label=f"匯入：{name}" if name else "匯入歌單"))
        if added is None:
            added = stats.get("added", 0)
        return added, stats.get("full_skipped", 0)

    jid = importer.start_job(url, _done, meta={"owner_key": key, "kind": "ext-queue"})
    return jsonify({"status": "ok", "job": jid}), 202


@flask_app.route(PREFIX + "import/<jid>", methods=["GET"])
def ext_import_status(jid):
    from . import importer
    job = importer.get_job(jid)
    if not job or job.get("owner_key") != _import_owner_key():
        return jsonify({"status": "error", "message": "找不到這個匯入工作"}), 404
    job.pop("owner_key", None)
    return jsonify({"status": "ok", "job": job})


# ==================== 成員管理（只有擁有者金鑰可用；網頁 /members） ====================
@flask_app.route("/members")
def members_page():
    # 登入檢查與「只有擁有者」在 webapi._require_login 處理
    return render_template("members.html")


def _err(e, code=400):
    return jsonify({"status": "error", "message": str(e) if isinstance(e, ValueError) else "找不到這個成員"}), code


@flask_app.route(PREFIX + "members", methods=["GET"])
def ext_members_list():
    from . import userlists
    links = userlists.discord_links()
    from . import discordauth
    members = [dict(m, discord_id=links.get(m["id"]), discord_name=discordauth.display_name(links.get(m["id"])) if links.get(m["id"]) else None)
               for m in extmembers.list_members()]
    own = discordauth.owner_discord_id()
    return jsonify({"status": "ok", "members": members, "owner_discord_id": links.get("owner"),
                    "owner_key_discord_id": own, "owner_key_discord_name": discordauth.display_name(own) if own else None})


@flask_app.route(PREFIX + "members", methods=["POST"])
def ext_members_create():
    data = request.get_json(silent=True) or {}
    try:
        m, tok = extmembers.create(data.get("name"))
    except ValueError as e:
        return _err(e)
    return jsonify({"status": "ok", "member": m, "token": tok}), 201


@flask_app.route(PREFIX + "members/<mid>", methods=["POST"])
def ext_members_update(mid):
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    try:
        if action == "rename":
            return jsonify({"status": "ok", "member": extmembers.rename(mid, data.get("name"))})
        if action in ("enable", "disable"):
            return jsonify({"status": "ok", "member": extmembers.set_enabled(mid, action == "enable")})
        if action == "link_discord":   # mid = 成員 id 或 "owner"；discord_id 空字串 = 解除
            from . import userlists
            if mid != "owner" and not any(m["id"] == mid for m in extmembers.list_members()):
                raise KeyError(mid)
            userlists.link_discord(mid, data.get("discord_id") or "")
            return jsonify({"status": "ok"})
        if action == "reset":
            m, tok = extmembers.reset_token(mid)
            return jsonify({"status": "ok", "member": m, "token": tok})
    except ValueError as e:
        return _err(e)
    except KeyError as e:
        return _err(e, 404)
    return jsonify({"status": "error", "message": "未知的動作"}), 400


@flask_app.route(PREFIX + "members/<mid>", methods=["DELETE"])
def ext_members_delete(mid):
    try:
        extmembers.delete(mid)
    except KeyError as e:
        return _err(e, 404)
    from . import userlists
    userlists.drop_user(mid)
    return jsonify({"status": "ok"})
