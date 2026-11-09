"""本地分段代理：讓 FFmpeg 讀 YouTube 音訊時改成「固定大小的 HTTP Range 分段」

原因（2026-11 實測）：FFmpeg 對網址送 `Range: bytes=0-`（從頭到結尾）時，部分長影片會被 YouTube 限速到
約 1.9 倍即時（26 KB/s），整首斷斷續續；同一網址改用有結尾的 Range（例如 bytes=0-2097151）就是全速。

做法：FFmpeg 改讀 http://127.0.0.1:<port>/s/<token>，這裡把 FFmpeg 要的範圍切成 CHUNK 大小、
逐段向 YouTube 要，邊收邊轉給 FFmpeg（每條連線只用 64KB 緩衝，不會整首放進記憶體）。
- FFmpeg 的 -ss 跳轉 / 讀 Cues 都是 Range 請求，照樣支援（回 206 + Content-Range）
- 只代理 googlevideo 且網址帶 clen（已知檔案大小）的單檔串流；直播 / HLS（m3u8）/ 其他來源走原本路徑

2026-11-05 修正「播到後面突然沒聲音」：
- 上游某一段卡住（沒斷線、只是很慢或不動）以前不會有任何 log，FFmpeg 會一直等 → 靜音。
  現在每次讀取有逾時（READ_TIMEOUT），每段「等上游的累計時間」有上限（CHUNK_DEADLINE），卡住就從斷點重要並記 log。
  （只算等上游的時間；FFmpeg 照即時速度慢慢讀、代理寫不出去的等待不算，否則正常播放也會被誤判）
- 上游回 403 / 410（網址過期或被拒）時，用 yt-dlp 重新解析同一首歌的網址（refresh），換新網址續傳。
- 開播第一段（還沒轉出任何 byte）連續 403：可連續重新解析多次（force，略過最短間隔），
  避免「剛換上的網址又立刻 403」時被最短間隔卡住、沿用壞網址開播失敗。
- 重試 / 換網址的成功與失敗都記 log（含位元組範圍）；全部失敗就把這個串流標成失效，
  之後的請求直接回 502，讓 FFmpeg 結束 → 播放器切下一首，而不是卡在靜音。
"""
import http.server
import secrets
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

from .config import log

CHUNK = 2 * 1024 * 1024      # 每段向上游要 2MB（有結尾的 Range 不會被限速）
READ = 64 * 1024             # 轉送緩衝
RETRIES = 8                  # 每段最多向上游要幾次（含多次換網址；冷啟動 403 可能連換兩三次才通）
READ_TIMEOUT = 15            # 單次 socket 讀取逾時
CHUNK_DEADLINE = 45          # 一段 2MB 等上游的累計秒數上限（正常全速不到 1 秒；45 秒 = 卡住或被嚴重限速）
REFRESH_STATUS = (403, 410)  # 這些狀態碼代表網址失效 → 重新解析
REFRESH_MIN_GAP = 20         # 同一串流兩次重新解析至少間隔秒數（force=True 時略過）
MAX_REFRESHES_COLD = 3       # 這段還沒轉出任何 byte 時，最多重新解析幾次（開播第一段常需要）
MAX_REFRESHES_MID = 2        # 已轉出部分資料後再遇 403/410，最多再解析幾次
TTL = 8 * 3600               # 註冊的網址保留時間（YouTube 串流網址本身約 6 小時過期）
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

_lock = threading.Lock()
_streams = {}                # token -> {"url", "size", "ts", "page", "dead", "refreshed_at", "rlock"}
_server = None
_port = None
_resolver = None             # page_url -> 新串流網址（同步；由 player 設定，用 yt-dlp 重新解析）


class _Stalled(Exception):
    pass


def set_resolver(fn):
    global _resolver
    _resolver = fn


def eligible(url):
    """是否走分段代理（回傳檔案大小；不適用回傳 None）"""
    try:
        u = urlparse(url)
    except Exception:
        return None
    if u.scheme not in ("http", "https") or not (u.hostname or "").endswith("googlevideo.com"):
        return None
    if "manifest" in u.path or ".m3u8" in u.path:
        return None
    q = parse_qs(u.query)
    if "live" in q or q.get("source") == ["yt_live_broadcast"]:
        return None
    try:
        size = int(q.get("clen", [""])[0])
    except ValueError:
        return None
    return size if size > 0 else None


def _parse_range(h, size):
    """'bytes=a-b' / 'bytes=a-' → (a, b)；沒有或不合法回傳 None"""
    if not h or not h.startswith("bytes="):
        return None
    spec = h[6:].split(",")[0].strip()
    a, _, b = spec.partition("-")
    try:
        if a == "":                       # bytes=-N（最後 N bytes）
            n = int(b); return (max(0, size - n), size - 1)
        a = int(a); b = int(b) if b else size - 1
    except ValueError:
        return None
    if a >= size or b < a:
        return "bad"
    return (a, min(b, size - 1))


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"        # keep-alive：配合 FFmpeg 的 -multiple_requests 1

    def log_message(self, *a):
        pass

    def _entry(self):
        parts = self.path.split("/")
        if len(parts) != 3 or parts[1] != "s":
            return None
        with _lock:
            return _streams.get(parts[2])

    def do_HEAD(self):
        self._serve(body=False)

    def do_GET(self):
        self._serve(body=True)

    def _serve(self, body):
        e = self._entry()
        if not e:
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        if e.get("dead"):
            # 上游已確定拿不到（重試 + 換網址都失敗）：直接回錯，讓 FFmpeg 結束、播放器切下一首
            self.send_response(502); self.send_header("Content-Length", "0"); self.end_headers()
            self.close_connection = True
            return
        size = e["size"]
        r = _parse_range(self.headers.get("Range"), size)
        if r == "bad":
            self.send_response(416); self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0"); self.end_headers(); return
        start, end = r if r else (0, size - 1)
        self.send_response(206 if r else 200)
        self.send_header("Content-Type", "audio/webm")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if r:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not body:
            return
        pos = start
        out = _GuardedWriter(self.wfile)
        try:
            while pos <= end:
                stop = min(pos + CHUNK - 1, end)
                got = _fetch_into(e, pos, stop, out)
                pos += got
                if pos <= stop:
                    break
        except (BrokenPipeError, ConnectionResetError):
            return                       # FFmpeg 關了（換歌 / 跳轉 / 停止）
        if pos <= end:
            # 這段重試完還是拿不到：串流標成失效、連線收掉。FFmpeg 會 -reconnect 回來，
            # 收到 502 就放棄 → 播放結束 → 播放器切下一首（不會卡在靜音）
            e["dead"] = True
            self.close_connection = True
            log.error(f"[分段代理] 放棄串流：{pos}-{end}/{size} 重試後仍拿不到，讓播放器跳下一首"
                      f"（{e.get('page') or '未知歌曲'}）"[:300])


def _refresh(e, why, force=False):
    """網址失效（403/410）時重新解析；成功回 True。同一串流有鎖 + 最短間隔，避免多條連線同時狂解析。
    force=True：略過最短間隔（上一輪剛換上的網址又立刻 403 時一定要再解析，否則會沿用壞網址）"""
    page = e.get("page")
    if not page or _resolver is None:
        log.warning(f"[分段代理] 網址失效（{why}），但沒有可重新解析的歌曲網址")
        return False
    with e["rlock"]:
        if not force and time.time() - e.get("refreshed_at", 0) < REFRESH_MIN_GAP:
            return e.get("refresh_ok", False)    # 剛換過（可能是別條連線換的）→ 直接用目前的網址
        e["refreshed_at"] = time.time()
        try:
            new = _resolver(page)
        except Exception as ex:
            e["refresh_ok"] = False
            log.warning(f"[分段代理] 重新解析網址失敗（{why}）：{page}：{ex!r}"[:300])
            return False
        size = eligible(new) if new else None
        if size != e["size"]:
            e["refresh_ok"] = False
            log.warning(f"[分段代理] 重新解析後檔案大小不同（{e['size']} → {size}），不能續傳：{page}"[:300])
            return False
        e["url"] = new
        e["refresh_ok"] = True
        log.info(f"[分段代理] 已重新解析並換上新網址（{why}）：{page}"[:300])
        return True


def _fetch_into(e, a, b, out):
    """向上游要 a..b，寫進 out；回傳寫了多少 bytes（失敗重試 RETRIES 次；403/410 換新網址，冷啟動可多換幾次）"""
    written = 0
    failed = 0
    refreshes = 0
    for attempt in range(RETRIES):
        rng = f"{a + written}-{b}"
        url = e["url"]
        req = urllib.request.Request(url, headers={"Range": f"bytes={rng}", "User-Agent": UA})
        waited = 0.0             # 只累計等上游 read 的時間
        try:
            with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp:
                if resp.status == 200 and a + written > 0:
                    # 上游忽略 Range 回整個檔案：不能直接轉（位置會錯）
                    raise urllib.error.HTTPError(url, resp.status, "range ignored", resp.headers, None)
                if resp.status not in (200, 206):
                    raise urllib.error.HTTPError(url, resp.status, "bad status", resp.headers, None)
                need = b - a + 1
                while written < need:
                    t0 = time.monotonic()
                    buf = resp.read(min(READ, need - written))
                    waited += time.monotonic() - t0
                    if not buf:
                        break
                    out.write(buf)
                    written += len(buf)
                    if written < need and waited > CHUNK_DEADLINE:
                        raise _Stalled(f"{CHUNK_DEADLINE} 秒內沒收完")
            if written >= b - a + 1:
                if failed:
                    log.info(f"[分段代理] 上游 {a}-{b} 重試成功（失敗 {failed} 次後）")
                return written
            raise _Stalled("上游提早結束")
        except (BrokenPipeError, ConnectionResetError) as ex:
            if _is_client_error(ex, out):
                raise
            failed += 1
            log.warning(f"[分段代理] 上游 {rng} 失敗（第 {attempt + 1}/{RETRIES} 次）：{ex!r}"[:300])
        except Exception as ex:
            failed += 1
            code = getattr(ex, "code", None)
            if isinstance(ex, (socket.timeout, TimeoutError)):
                ex = _Stalled(f"讀取逾時 {READ_TIMEOUT} 秒")
            log.warning(f"[分段代理] 上游 {rng} 失敗（第 {attempt + 1}/{RETRIES} 次）：{ex!r}"[:300])
            if code in REFRESH_STATUS:
                # 第一次 403 常是暫時性的（實測重試就好）；連續兩次才當成網址失效。
                # 剛換過的網址又立刻 403（冷啟動常見）→ force 再解析，不要被 REFRESH_MIN_GAP 卡住沿用壞網址。
                max_ref = MAX_REFRESHES_COLD if written == 0 else MAX_REFRESHES_MID
                if attempt >= 1 and refreshes < max_ref:
                    force = refreshes > 0
                    if _refresh(e, f"HTTP {code} @ {rng}", force=force):
                        refreshes += 1
                        continue
        if attempt + 1 < RETRIES:
            time.sleep(0.3 * (attempt + 1))
    log.error(f"[分段代理] 上游 {a + written}-{b} 重試 {RETRIES} 次仍失敗（這段已轉 {written} bytes）")
    return written


def _is_client_error(ex, out):
    """BrokenPipe / ConnectionReset 是 FFmpeg 那端斷線（寫 out 失敗）還是上游斷線？"""
    return not isinstance(out, _GuardedWriter) or out.failed


class _GuardedWriter:
    """包住 FFmpeg 那端的 wfile：寫入失敗時記下來，好跟上游斷線區分"""
    def __init__(self, raw):
        self.raw = raw
        self.failed = False

    def write(self, b):
        try:
            return self.raw.write(b)
        except (BrokenPipeError, ConnectionResetError):
            self.failed = True
            raise


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """FFmpeg 換歌 / 跳轉 / 停止時直接斷線是正常的，不要把 Traceback 灌進 journal"""
        import sys
        ex = sys.exc_info()[1]
        if isinstance(ex, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        log.warning(f"[分段代理] 處理請求失敗：{ex!r}"[:300])


def _ensure_server():
    global _server, _port
    with _lock:
        if _server is None:
            _server = _Server(("127.0.0.1", 0), _Handler)
            _port = _server.server_address[1]
            threading.Thread(target=_server.serve_forever, name="rangeproxy", daemon=True).start()
            log.info(f"[分段代理] 已啟動 127.0.0.1:{_port}")
        return _port


def _gc(now):
    for k in [k for k, v in _streams.items() if now - v["ts"] > TTL]:
        _streams.pop(k, None)


def input_url(url, page=None):
    """給 FFmpeg 的輸入網址：適用就換成本地分段代理，否則原樣回傳。
    page = 歌曲網頁網址（網址失效時用來重新解析）"""
    size = eligible(url)
    if not size:
        return url
    try:
        port = _ensure_server()
    except Exception as ex:
        log.warning(f"[分段代理] 啟動失敗，改走原本路徑：{ex!r}")
        return url
    tok = secrets.token_urlsafe(12)
    now = time.time()
    with _lock:
        _gc(now)
        _streams[tok] = {"url": url, "size": size, "ts": now, "page": page, "dead": False,
                         "refreshed_at": 0, "rlock": threading.Lock()}
    return f"http://127.0.0.1:{port}/s/{tok}"
