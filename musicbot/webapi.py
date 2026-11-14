"""Flask 網頁伺服器：建立 flask_app、主機 / 登入檢查、共用 hook、錯誤處理、首頁、啟動。

API 路由拆在 webapi_player.py（播放控制 / 狀態 / SSE）、webapi_queue.py（佇列 / 紀錄）、
webapi_library.py（歌單 / 最愛 / 搜尋），由本檔最後載入。
"""
import os
import time

import yt_dlp  # noqa: F401  測試用 webapi.yt_dlp 替換
from flask import Flask, render_template, request, jsonify, Response, redirect, g

from .config import log
from .state import local_playlist, notify_change  # noqa: F401  local_playlist：路由經 _w. 取用（測試會替換）
from .storage import save_favorites, save_local_playlist  # noqa: F401  同上
from .webcore import run_async, get_active_guild, get_full_app_state  # noqa: F401  同上
from . import importer  # noqa: F401  測試用 webapi.importer 替換
from . import webauth, extmembers


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


# 拆出去的 API 路由（行為不變）：最後才載入，它們用到上面的 flask_app 與 hook
from . import webapi_player as _webapi_player  # noqa: E402,F401
from . import webapi_queue as _webapi_queue  # noqa: E402,F401
from . import webapi_library as _webapi_library  # noqa: E402,F401

# Chrome 擴充功能 API（/api/ext/*，需 EXT_TOKEN）：最後才載入，它會用到上面的 flask_app
from . import extapi as _extapi  # noqa: E402,F401
from . import userlistapi as _userlistapi  # noqa: E402,F401
from . import webstatic as _webstatic  # noqa: E402,F401
