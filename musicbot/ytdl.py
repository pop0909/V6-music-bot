"""yt-dlp 共用設定：cookies、extractor 參數、YouTube「機器人驗證」錯誤偵測

.env 可選設定：
- YTDLP_COOKIES=/path/cookies.txt        Netscape 格式 cookies 檔（伺服器建議用這個）
- YTDLP_COOKIES_FROM_BROWSER=chrome      直接讀本機瀏覽器 cookies（本機執行用；可寫 firefox:設定檔名稱）
- YTDLP_PLAYER_CLIENT=default,mweb       （進階）指定 YouTube player client 的嘗試順序，不設定 = yt-dlp 預設
所有 yt-dlp 呼叫（播放解析、預先解析、搜尋、網址解析、自動推薦 Mix）都經過 build_opts()。

cookiefile 注意：yt-dlp 會在結束時把 cookiejar 寫回 cookiefile（YoutubeDL.save_cookies）。
若直接指向主檔，YouTube 輪替／弱化後的 cookies 會覆寫原本的 LOGIN_INFO / SID。
因此 build_opts() 只把「主檔的複本」（暫存執行檔）交給 yt-dlp，永遠不寫回主檔。
主檔更新（mtime / 大小變了）時會自動再複製一份，不必重啟。
"""
import atexit
import os
import shutil
import tempfile
import threading
import time

from .config import log

BOT_CHECK_HINT = (
    "YouTube 要求「確認你不是機器人」（伺服器 IP 被擋）。"
    "請在 .env 設定 YTDLP_COOKIES=cookies.txt 路徑（用瀏覽器擴充功能 Get cookies.txt LOCALLY 匯出，"
    "建議用拋棄式 Google 帳號），並更新 yt-dlp：pip install -U yt-dlp。詳見 README。"
)
BOT_CHECK_SHORT = "YouTube 要求機器人驗證，需要設定 cookies（YTDLP_COOKIES），詳見 README"

_BOT_CHECK_MARKERS = (
    "sign in to confirm you",       # Sign in to confirm you're not a bot / you’re not a bot
    "confirm you're not a bot",
    "confirm you’re not a bot",
    "confirm that you're not a bot",
    "cookies are no longer valid",  # cookies 過期 / 被 YouTube 輪替
)
_LOG_INTERVAL = 600
_last_log = [0.0]


def _env(name):
    return (os.getenv(name) or "").strip()


# 已登入 YouTube／Google 工作階段常見的 cookie 名稱。只有 __Secure-3PSID 不夠。
_SESSION_COOKIE_NAMES = frozenset({"LOGIN_INFO", "SID", "SAPISID", "__Secure-1PSID"})

_cookie_lock = threading.Lock()
# master / src_mtime / src_size / path（runtime 複本）/ session_ok
_runtime = {"master": None, "src_mtime": None, "src_size": None, "path": None, "session_ok": None}

COOKIE_QUALITY_HINT = (
    "cookies 主檔沒有 LOGIN_INFO 或 SID（看起來不像已登入的 YouTube 工作階段，"
    "或曾被 yt-dlp 覆寫成較弱的內容）。請用已登入的拋棄式 Google 帳號重新匯出 "
    "Netscape 格式的 cookies.txt 覆寫主檔。機器人只讀取主檔，不會把 yt-dlp 寫回的內容存回去。"
)


def cookie_names(path):
    """讀 Netscape cookies 檔的名稱（不回傳、不記錄值）。"""
    names = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line or line[0] == "#":
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 6 and parts[5]:
                    names.add(parts[5])
    except OSError:
        pass
    return names


def has_session_cookies(path):
    """主檔是否含 LOGIN_INFO / SID / SAPISID / __Secure-1PSID。"""
    return bool(cookie_names(path) & _SESSION_COOKIE_NAMES)


def _unlink_runtime(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def reset_cookie_runtime():
    """測試用：清掉執行時複本。"""
    with _cookie_lock:
        _unlink_runtime(_runtime["path"])
        _runtime.update(master=None, src_mtime=None, src_size=None, path=None, session_ok=None)
        _warned.discard("cookies_quality")
        _warned.discard("cookies_quality_ok")
        _warned.discard("cookies_missing")
        _warned.discard("cookies_copy_fail")


def _copy_to_runtime(src, dest=None):
    """把主檔複製到權限 0600 的暫存檔；dest 有值就覆寫該檔（先寫 tmp 再 replace）。"""
    if dest:
        tmp = dest + ".tmp"
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        return dest
    fd, path = tempfile.mkstemp(prefix="musicbot-cookies-", suffix=".txt")
    os.close(fd)
    try:
        os.chmod(path, 0o600)
        shutil.copyfile(src, path)
        os.chmod(path, 0o600)
    except Exception:
        _unlink_runtime(path)
        raise
    return path


def _note_quality(ok):
    if ok:
        _warned.discard("cookies_quality")
        _warn_once("cookies_quality_ok", "[yt-dlp] 已載入含登入工作階段的 cookies 主檔（LOGIN_INFO / SID）。")
    else:
        _warned.discard("cookies_quality_ok")
        _warn_once("cookies_quality", "[yt-dlp] " + COOKIE_QUALITY_HINT)


def _ensure_runtime():
    """回傳要交給 yt-dlp 的執行時複本路徑；主檔不存在則 None。永不把 cookiefile 指到主檔。"""
    raw = _env("YTDLP_COOKIES")
    if not raw:
        return None
    master = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isfile(master):
        _warn_once("cookies_missing", f"[yt-dlp] 找不到 YTDLP_COOKIES 指定的檔案：{master}（略過 cookies）")
        return None
    try:
        st = os.stat(master)
    except OSError as e:
        _warn_once("cookies_missing", f"[yt-dlp] 無法讀取 YTDLP_COOKIES 指定的檔案：{master}（{e!r}）")
        return None
    with _cookie_lock:
        same = (
            _runtime["path"]
            and _runtime["master"] == master
            and _runtime["src_mtime"] == st.st_mtime
            and _runtime["src_size"] == st.st_size
            and os.path.isfile(_runtime["path"])
        )
        if same:
            return _runtime["path"]
        try:
            dest = _copy_to_runtime(master, _runtime["path"] if _runtime["path"] and _runtime["master"] == master else None)
        except OSError as e:
            _warn_once("cookies_copy_fail", f"[yt-dlp] 無法複製 cookies 主檔到執行時暫存檔：{e!r}（略過 cookies，以免覆寫主檔）")
            return None
        if _runtime["path"] and _runtime["path"] != dest:
            _unlink_runtime(_runtime["path"])
        ok = has_session_cookies(master)
        _runtime.update(master=master, src_mtime=st.st_mtime, src_size=st.st_size, path=dest, session_ok=ok)
        _note_quality(ok)
        return dest


def warn_cookie_quality():
    """啟動時呼叫：還沒播歌也先檢查主檔並寫 log。"""
    _ensure_runtime()


def cookie_status():
    """給面板用的摘要。不回傳路徑或 cookie 值。"""
    raw = _env("YTDLP_COOKIES")
    if not raw:
        return {"configured": False, "session_ok": False, "warning": None}
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isfile(path):
        return {"configured": False, "session_ok": False, "warning": None}
    _ensure_runtime()
    ok = bool(_runtime.get("session_ok"))
    return {
        "configured": True,
        "session_ok": ok,
        "warning": None if ok else COOKIE_QUALITY_HINT,
    }


def cookie_opts():
    """依 .env 回傳要加進 yt-dlp 選項的 cookies 設定（每次呼叫重新讀主檔是否更新）。
    cookiefile 指向執行時複本，不是主檔。"""
    opts = {}
    runtime = _ensure_runtime()
    if runtime:
        opts["cookiefile"] = runtime
    browser = _env("YTDLP_COOKIES_FROM_BROWSER")
    if browser and "cookiefile" not in opts:
        name, _, profile = browser.partition(":")
        opts["cookiesfrombrowser"] = (name.strip().lower(), profile.strip() or None, None, None)
    return opts


def extractor_opts():
    clients = [c.strip() for c in _env("YTDLP_PLAYER_CLIENT").split(",") if c.strip()]
    if not clients:
        return {}
    return {"extractor_args": {"youtube": {"player_client": clients}}}


def build_opts(base):
    """複製 base 並加上 cookies / extractor 參數（不修改傳入的 dict）"""
    opts = dict(base)
    opts.update(cookie_opts())
    opts.update(extractor_opts())
    return opts


def cookies_configured():
    return bool(cookie_opts())


def is_bot_check(err):
    """例外或訊息是否為 YouTube 的「確認你不是機器人」"""
    msg = str(err or "").lower()
    return any(m in msg for m in _BOT_CHECK_MARKERS)


def report_bot_check(context=""):
    """記錄清楚的中文提示（10 分鐘內只記一次，避免洗版）。回傳給使用者看的短訊息。"""
    now = time.time()
    if now - _last_log[0] >= _LOG_INTERVAL:
        _last_log[0] = now
        state = "已設定 cookies，但仍被擋（cookies 可能過期，請重新匯出）" if cookies_configured() else "尚未設定 cookies"
        log.warning(f"[yt-dlp] {context} {BOT_CHECK_HINT}（目前：{state}）")
    return BOT_CHECK_SHORT


def friendly_error(err, context=""):
    """把 yt-dlp 例外轉成給使用者看的訊息：機器人驗證 → 清楚提示；其他 → 原訊息（截短）"""
    try:
        from . import alerts
        alerts.note_error(err, context)
    except Exception:
        pass
    if is_bot_check(err):
        return report_bot_check(context)
    return str(err)[:200]


_warned = set()


def _warn_once(key, msg):
    if key not in _warned:
        _warned.add(key)
        log.warning(msg)

atexit.register(reset_cookie_runtime)
