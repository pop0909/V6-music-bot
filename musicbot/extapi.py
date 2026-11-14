"""Chrome 擴充功能專用 API（/api/ext/*）：需要金鑰（X-Ext-Token 標頭）。
擁有者 = .env 的 EXT_TOKEN；朋友 = 成員金鑰（extmembers.py，可在網頁「成員管理」建立 / 停用）。

- GET  /api/ext/status            目前播放 + 佇列數量 + 語音頻道
- POST /api/ext/add {url, title?, next?}
        加入佇列；next=true → 插到下一首（已在佇列裡的也會移到下一首）
允許 chrome-extension:// 來源的 CORS；沒設 EXT_TOKEN 時整組停用（503）。
"""
import re
from urllib.parse import urlparse, parse_qs

from flask import request, jsonify, g, render_template

from .config import log
from . import extmembers
from .state import (
    state_lock, queues, now_playing, now_playing_url, voice_clients, guild_names,
    current_voice_channel, notify_change,
)
from .utils import clean_str
from .webapi import flask_app
from .webcore import run_async, web_add_to_queue, get_active_guild

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
        ident = extmembers.authenticate(request.headers.get("X-Ext-Token"))
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


def ext_actor():
    """操作紀錄用：(金鑰成員名稱, 來源)。帶 X-Ext-Token 標頭 = Chrome 擴充功能；用登入 cookie = 網頁面板"""
    who = (getattr(g, "ext_user", None) or {}).get("name") or "未知"
    return (who, "ext" if "X-Ext-Token" in request.headers else "web")


def _pick_guild():
    with state_lock:
        gid = get_active_guild()
        if gid:
            return gid
        connected = [g for g, vc in voice_clients.items() if vc and vc.is_connected()]
        if connected:
            return connected[0]
        return next(iter(guild_names), 0)


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


@flask_app.route(PREFIX + "status", methods=["GET"])
def ext_status():
    extmembers.record_use(g.ext_user)
    gid = request.args.get("guild_id", 0, type=int) or _pick_guild()
    vc = voice_clients.get(gid)
    connected = bool(vc and vc.is_connected())
    ch = current_voice_channel.get(gid) if connected else None
    return jsonify({
        "status": "ok",
        "guild_name": guild_names.get(gid, ""),
        "connected": connected,
        "is_playing": bool(connected and vc.is_playing()),
        "is_paused": bool(connected and vc.is_paused()),
        "current_title": now_playing.get(gid, "") if connected else "",
        "current_url": now_playing_url.get(gid, "") if connected else "",
        "queue_length": len(queues.get(gid, [])),
        "voice_channel": (ch or {}).get("name"),
        "you": g.ext_user.get("name"),
        "owner": bool(g.ext_user.get("owner")),
    })


@flask_app.route(PREFIX + "add", methods=["POST"])
def ext_add():
    data = request.get_json(silent=True) or {}
    url = normalize_youtube(data.get("url"))
    if not url:
        return jsonify({"status": "error", "message": "不是 YouTube 影片網址"}), 400
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
    members = [dict(m, discord_id=links.get(m["id"])) for m in extmembers.list_members()]
    return jsonify({"status": "ok", "members": members, "owner_discord_id": links.get("owner")})


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
