"""面板的正式網頁伺服器（waitress），取代 Flask 自帶的開發用伺服器。

- 固定數量的工作執行緒（WEB_THREADS，預設 24）：人多時排隊，不會像開發伺服器一樣每個請求開一條新執行緒
- 即時更新（SSE /api/progress-stream）每個分頁會一直佔一條執行緒 → 同時最多 sse_max() 條，
  至少留 RESERVED 條給一般請求，超過的回 503，前端會退避後重連（見 app.js connectProgressStream）
- 不讓 waitress 清掉 X-Forwarded-* 標頭（clear_untrusted_proxy_headers=False）：
  登入 cookie 的 secure 判斷看 X-Forwarded-Proto、輸錯鎖定看 CF-Connecting-IP / X-Forwarded-For，跟以前一樣由程式自己讀
- 分頁關掉時 SSE 迴圈約 1 秒內就會結束（channel_request_lookahead + waitress.client_disconnected）
- 存取紀錄維持跟以前一樣的格式（寫到 "werkzeug" logger），本機雜訊過濾（_ForeignRequestFilter）照常有效
- 沒裝 waitress（例如部署時忘了 pip install）→ 記警告、退回 Flask 開發伺服器，面板照樣能用
"""
import logging
import os
import threading
import time

from .config import log

RESERVED = 8            # 一定留給一般請求（非 SSE）的執行緒數
CHANNEL_TIMEOUT = 120   # 閒置連線幾秒後關閉（SSE 每 15 秒有心跳，而且處理中的請求不會被關）
CONNECTION_LIMIT = 100


def threads():
    try:
        n = int((os.getenv("WEB_THREADS") or "24").strip())
    except ValueError:
        n = 24
    return max(RESERVED + 4, min(n, 64))


def sse_max():
    return threads() - RESERVED


# ---------- SSE 名額 ----------
_sse_lock = threading.Lock()
_sse_active = 0


def sse_acquire():
    """拿一個 SSE 名額；滿了回 None。回傳的 release() 可以重複呼叫（只會還一次）"""
    global _sse_active
    with _sse_lock:
        if _sse_active >= sse_max():
            return None
        _sse_active += 1
    done = []

    def release():
        global _sse_active
        with _sse_lock:
            if not done:
                done.append(1)
                _sse_active -= 1
    return release


def sse_active():
    with _sse_lock:
        return _sse_active


# ---------- 存取紀錄 ----------
_access = logging.getLogger("werkzeug")


class AccessLog:
    """WSGI 中介層：照 werkzeug 開發伺服器的格式記一行存取紀錄（送出標頭時記，跟以前一樣）"""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        def sr(status, headers, exc_info=None):
            try:
                path = environ.get("PATH_INFO", "")
                qs = environ.get("QUERY_STRING", "")
                line = f'{environ.get("REQUEST_METHOD", "-")} {path}{"?" + qs if qs else ""} {environ.get("SERVER_PROTOCOL", "HTTP/1.1")}'
                _access.info('%s - - [%s] "%s" %s -', environ.get("REMOTE_ADDR", "-"),
                             time.strftime("%d/%b/%Y %H:%M:%S"), line, status.split(" ", 1)[0])
            except Exception:
                pass
            return start_response(status, headers, exc_info) if exc_info else start_response(status, headers)
        return self.app(environ, sr)


def create(app, host, port):
    """建立 waitress 伺服器（測試也用這個）；回傳 server，呼叫 server.run() 開始服務"""
    from waitress.server import create_server
    return create_server(
        AccessLog(app), host=host, port=port,
        threads=threads(),
        channel_timeout=CHANNEL_TIMEOUT,
        connection_limit=CONNECTION_LIMIT,
        trusted_proxy=None,
        clear_untrusted_proxy_headers=False,
        asyncore_use_poll=True,
        # 處理請求時也繼續讀連線 → 客戶端關掉分頁能馬上發現（environ["waitress.client_disconnected"]），
        # SSE 迴圈每秒檢查一次就能結束並歸還執行緒，不用等下一次寫入失敗
        channel_request_lookahead=1,
        ident="musicbot",
    )


def serve(app, host, port):
    try:
        server = create(app, host, port)
    except ImportError:
        log.warning("[Web] 沒裝 waitress，改用 Flask 開發伺服器（請執行 pip install -r requirements.txt）")
        app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
        return
    log.info(f"[Web] waitress 已啟動：{threads()} 條執行緒，即時更新最多 {sse_max()} 個連線")
    server.run()
