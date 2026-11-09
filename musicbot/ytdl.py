"""yt-dlp 共用設定：cookies、extractor 參數、YouTube「機器人驗證」錯誤偵測

.env 可選設定：
- YTDLP_COOKIES=/path/cookies.txt        Netscape 格式 cookies 檔（伺服器建議用這個）
- YTDLP_COOKIES_FROM_BROWSER=chrome      直接讀本機瀏覽器 cookies（本機執行用；可寫 firefox:設定檔名稱）
- YTDLP_PLAYER_CLIENT=default,mweb       （進階）指定 YouTube player client 的嘗試順序，不設定 = yt-dlp 預設
所有 yt-dlp 呼叫（播放解析、預先解析、搜尋、網址解析、自動推薦 Mix）都經過 build_opts()。
"""
import os
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


def cookie_opts():
    """依 .env 回傳要加進 yt-dlp 選項的 cookies 設定（每次呼叫重新讀，改 .env 重啟即可）"""
    opts = {}
    path = _env("YTDLP_COOKIES")
    if path:
        path = os.path.expanduser(path)
        if os.path.isfile(path):
            opts["cookiefile"] = path
        else:
            _warn_once("cookies_missing", f"[yt-dlp] 找不到 YTDLP_COOKIES 指定的檔案：{path}（略過 cookies）")
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
