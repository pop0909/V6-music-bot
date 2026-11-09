"""個人歌單 API（/api/ext/lists*）：用擴充功能金鑰（X-Ext-Token）辨識身分，只能看 / 改自己的歌單。
網頁面板「我的歌單」把金鑰存在瀏覽器 localStorage，走同一組 API。"""
import random

from flask import request, jsonify, g

from . import userlists, extmembers, importer
from .config import log
from .state import queue_skip_message, queue_full_message
from .webapi import flask_app
from .webcore import run_async, web_import_songs

P = "/api/ext/lists"


def user_key(ident=None):
    ident = ident or g.ext_user
    return "owner" if ident.get("owner") else ident["id"]


def _err(e, code=400):
    msg = e.args[0] if isinstance(e, (ValueError, KeyError)) and e.args else "操作失敗"
    return jsonify({"status": "error", "message": str(msg)}), code


def _guard(fn):
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except ValueError as e:
            return _err(e, 400)
        except KeyError as e:
            return _err(e, 404)
    wrap.__name__ = fn.__name__
    return wrap


def _body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


@flask_app.route(P, methods=["GET"])
@_guard
def ul_index():
    k = user_key()
    return jsonify({"status": "ok", "you": g.ext_user.get("name"), "owner": bool(g.ext_user.get("owner")),
                    "lists": userlists.list_playlists(k), "max_lists": userlists.MAX_LISTS,
                    "max_songs": userlists.MAX_SONGS, "discord_linked": bool(userlists.discord_links().get(k))})


@flask_app.route(P, methods=["POST"])
@_guard
def ul_create():
    d = _body()
    songs = d.get("songs") if isinstance(d.get("songs"), list) else None
    return jsonify({"status": "ok", "list": userlists.create(user_key(), d.get("name"), songs)}), 201


@flask_app.route(P + "/<lid>", methods=["GET"])
@_guard
def ul_get(lid):
    return jsonify({"status": "ok", "list": userlists.get_playlist(user_key(), lid)})


@flask_app.route(P + "/<lid>", methods=["POST"])
@_guard
def ul_update(lid):
    d, k = _body(), user_key()
    act = d.get("action")
    if act == "rename":
        return jsonify({"status": "ok", "list": userlists.rename(k, lid, d.get("name"))})
    if act == "add":
        if isinstance(d.get("songs"), list):
            songs = d["songs"]
        else:
            from .extapi import normalize_youtube, _lookup_title
            url = d.get("url").strip() if isinstance(d.get("url"), str) else ""
            url = normalize_youtube(url) or url          # 各種 YouTube 網址統一成 watch?v=（去重才準）
            title = d.get("title") if isinstance(d.get("title"), str) and d.get("title").strip() else None
            if not title and url.startswith("http"):
                title = _lookup_title(url)               # 只貼網址 → 查歌名（查不到就用網址）
            songs = [{"url": url, "title": title}]
        added, skipped = userlists.add_songs(k, lid, songs)
        if not added:
            return jsonify({"status": "duplicate", "added": 0, "message": "歌單裡已經有這首歌"})
        return jsonify({"status": "ok", "added": added, "skipped": skipped})
    if act == "remove":
        userlists.remove_song(k, lid, d.get("url"))
        return jsonify({"status": "ok"})
    if act == "play":
        return _play(k, lid, d)
    return jsonify({"status": "error", "message": "未知的動作"}), 400


@flask_app.route(P + "/<lid>", methods=["DELETE"])
@_guard
def ul_delete(lid):
    userlists.delete(user_key(), lid)
    return jsonify({"status": "ok"})


def _play(k, lid, d):
    from .extapi import _pick_guild
    pl = userlists.get_playlist(k, lid)
    songs = list(pl["songs"])
    if not songs:
        return jsonify({"status": "error", "message": "這份歌單是空的"}), 400
    if d.get("shuffle"):
        random.shuffle(songs)
    try:
        gid = int(d.get("guild_id") or 0) or _pick_guild()
    except (TypeError, ValueError):
        gid = _pick_guild()
    if not gid:
        return jsonify({"status": "error", "message": "機器人尚未連線到伺服器"}), 400
    stats = {}
    who = g.ext_user.get("name") or "網頁"
    n = run_async(web_import_songs(gid, songs, requester=who, stats=stats)) or 0
    skipped = stats.get("full_skipped", 0)
    if not n and skipped:
        return jsonify({"status": "error", "code": "queue_full", "message": queue_full_message()}), 409
    extmembers.record_use(g.ext_user, added=bool(n))
    log.info(f"[個人歌單] {who} 播放「{pl['name']}」：加入 {n} 首")
    msg = f"已加入 {n} 首（{pl['name']}）" if n else "歌單裡的歌都已經在佇列中"
    if skipped:
        msg += "，" + queue_skip_message(skipped)
    return jsonify({"status": "ok", "count": n, "full_skipped": skipped, "message": msg})


# ---------- 匯入 YouTube 播放清單 → 新歌單（不加入佇列） ----------
def import_into_new_list(k, url, name=None):
    """開背景工作；完成時建立歌單。回傳 job id。"""
    def on_done(title, songs):
        base = userlists._clean_name(name) or title or "匯入的歌單"
        existing = {p["name"] for p in userlists.list_playlists(k)}
        nm, i = base, 2
        while nm in existing:
            nm = f"{base[:userlists.NAME_MAX - 5]} ({i})"; i += 1
        pl = userlists.create(k, nm, songs, source=url)
        return pl["count"], 0
    return importer.start_job(url, on_done, cap=userlists.MAX_SONGS, meta={"owner_key": k, "kind": "userlist"})


@flask_app.route(P + "/import", methods=["POST"])
@_guard
def ul_import():
    d, k = _body(), user_key()
    url = d.get("url").strip() if isinstance(d.get("url"), str) else ""
    if not url.startswith("http") or not (importer.is_spotify(url) or "list=" in url):
        return jsonify({"status": "error", "message": "請貼上 YouTube 播放清單（含 list=）或 Spotify 歌單／專輯網址"}), 400
    if len(userlists.list_playlists(k)) >= userlists.MAX_LISTS:
        return jsonify({"status": "error", "message": f"每人最多 {userlists.MAX_LISTS} 份歌單"}), 400
    jid = import_into_new_list(k, url, d.get("name"))
    return jsonify({"status": "ok", "job": jid}), 202


@flask_app.route(P + "/import/<jid>", methods=["GET"])
def ul_import_status(jid):
    j = importer.get_job(jid)
    if not j or j.get("owner_key") != user_key():
        return jsonify({"status": "error", "message": "找不到這個匯入工作"}), 404
    j.pop("owner_key", None)
    return jsonify({"status": "ok", "job": j})
