"""面板載入速度：靜態檔快取標頭 + 預先 gzip

- /static/...?v=<版本>：一年 + immutable（瀏覽器 / Cloudflare 邊緣直接用快取，不再回源驗證）。
  改了 app.js / style.css 一定要改 templates/index.html 的 ?v=（tests/test_webstatic.py 會檢查兩個檔的 ?v= 一致）
- /static/... 沒帶 v：5 分鐘
- HTML 頁面：no-cache（每次都向伺服器確認，換版馬上生效；只多一個小請求）
- 文字類靜態檔（js / css / svg / json / txt）用戶端支援 gzip 就回 gzip（依檔案修改時間快取壓縮結果，不重複壓）。
  Cloudflare 對瀏覽器本來就會壓縮；這裡省的是本機 → Cloudflare 隧道那段的傳輸量（app.js 183 KB → 約 55 KB）
"""
import gzip
import os
import threading

from flask import request

from .webapi import flask_app

LONG = "public, max-age=31536000, immutable"
SHORT = "public, max-age=300"
HTML = "no-cache"
GZIP_TYPES = ("text/css", "text/javascript", "application/javascript", "image/svg+xml",
              "application/json", "text/plain")
MIN_GZIP = 1024

_gz_cache = {}          # 檔案路徑 -> (mtime_ns, size, gz bytes)
_gz_lock = threading.Lock()


def _static_file(path):
    rel = path[len("/static/"):]
    root = os.path.abspath(flask_app.static_folder)
    full = os.path.abspath(os.path.join(root, rel))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        return None
    return full


def gzipped(full):
    st = os.stat(full)
    key = (st.st_mtime_ns, st.st_size)
    with _gz_lock:
        hit = _gz_cache.get(full)
        if hit and hit[:2] == key:
            return hit[2]
    with open(full, "rb") as f:
        data = gzip.compress(f.read(), compresslevel=9, mtime=0)
    with _gz_lock:
        _gz_cache[full] = key + (data,)
    return data


def _accepts_gzip():
    return request.accept_encodings["gzip"] > 0


def _close_body(resp):
    body = resp.response
    close = getattr(body, "close", None)
    if close:
        try:
            close()
        except Exception:
            pass


@flask_app.after_request
def _static_cache(resp):
    path = request.path
    if path.startswith("/static/"):
        if resp.status_code in (200, 304):
            resp.headers["Cache-Control"] = LONG if request.args.get("v") else SHORT
        if resp.status_code == 200 and resp.mimetype in GZIP_TYPES:
            resp.vary.add("Accept-Encoding")
            full = _static_file(path)
            if full and _accepts_gzip() and os.path.getsize(full) >= MIN_GZIP and "Content-Range" not in resp.headers:
                etag, weak = resp.get_etag()
                gz_etag = etag + "-gz" if etag else None
                _close_body(resp)                # 換掉原本的檔案串流前先關檔（避免 fd 外洩）
                resp.direct_passthrough = False
                resp.headers.pop("Accept-Ranges", None)
                if gz_etag and request.if_none_match.contains(gz_etag):
                    resp.set_data(b"")           # 用戶端已有同一版的 gzip → 304
                    resp.status_code = 304
                    resp.headers.pop("Content-Length", None)
                    resp.set_etag(gz_etag, weak=weak)
                    return resp
                resp.set_data(gzipped(full))     # 也會更新 Content-Length
                resp.headers["Content-Encoding"] = "gzip"
                if gz_etag:
                    resp.set_etag(gz_etag, weak=weak)
    elif resp.mimetype == "text/html" and "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = HTML
    return resp
