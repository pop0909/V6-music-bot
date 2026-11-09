"""自動推薦接歌（Autoplay）

佇列播完時，用 YouTube Mix（radio 播放清單 list=RD<影片ID>）找「相關歌曲」接著播。
不需要任何 API key，只用 yt-dlp 的 extract_flat 讀清單（不下載、很快）。

- 每個伺服器各自開關（預設由 .env 的 AUTOPLAY_DEFAULT 決定，未設定 = 關閉）
- 記錄最近播放過的影片 ID，避免一直推薦同幾首
- 跳過超過 10 分鐘的影片（合輯、直播、一小時版）
"""
import os
import random
from collections import deque
from urllib.parse import urlparse, parse_qs

import yt_dlp

from .config import log
from .utils import clean_str
from . import ytdl

MAX_DURATION = 10 * 60      # 秒；超過就跳過（duration 未知則放行）
HISTORY_SIZE = 50           # 每個伺服器記住最近幾首
MIX_FETCH_COUNT = 25        # 從 Mix 讀取幾首候選
AUTOPLAY_DEFAULT = os.getenv("AUTOPLAY_DEFAULT", "0").strip().lower() in ("1", "true", "on", "yes")

autoplay_guilds = {}        # guild_id: bool
recent_history = {}         # guild_id: deque[影片ID]
failed_ids = {}             # guild_id: set[影片ID] — 播放失敗過的推薦，不再挑

_TITLE_BLOCKLIST = ("1 hour", "1hour", "10 hours", "合輯", "playlist", "full album", "live stream", "直播")


def is_enabled(gid):
    return autoplay_guilds.get(gid, AUTOPLAY_DEFAULT)


def set_enabled(gid, enabled):
    autoplay_guilds[gid] = bool(enabled)
    return autoplay_guilds[gid]


def extract_video_id(url):
    """從各種 YouTube 網址取出 11 碼影片 ID；不是 YouTube 就回 None"""
    if not url:
        return None
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    vid = None
    if host.endswith("youtu.be"):
        vid = u.path.lstrip("/").split("/")[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        if u.path == "/watch":
            vid = parse_qs(u.query).get("v", [None])[0]
        else:
            parts = [p for p in u.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
                vid = parts[1]
    if vid and len(vid) == 11:
        return vid
    return None


def remember(gid, url):
    """成功開播時呼叫：記錄到最近播放歷史"""
    vid = extract_video_id(url)
    if not vid:
        return
    hist = recent_history.setdefault(gid, deque(maxlen=HISTORY_SIZE))
    if vid in hist:
        hist.remove(vid)
    hist.append(vid)


def mark_failed(gid, url):
    vid = extract_video_id(url)
    if vid:
        s = failed_ids.setdefault(gid, set())
        if len(s) > 200:
            s.clear()
        s.add(vid)


def last_seed(gid):
    hist = recent_history.get(gid)
    return hist[-1] if hist else None


def pick_recommendation(entries, recent_ids, seed_id=None, queued_ids=(), max_duration=MAX_DURATION, rng=None):
    """從 Mix 清單的 entries 挑一首（純函式，方便測試）

    規則：跳過種子本身、最近播過、佇列中已有、超過 max_duration、標題像合輯/直播的。
    為了不要每次都挑 Mix 第 2 首造成固定路線，從前幾個合格的候選中隨機挑。
    回傳 (url, title) 或 None。
    """
    rng = rng or random
    recent = set(recent_ids or ())
    queued = set(queued_ids or ())
    candidates = []
    for e in entries or []:
        if not e:
            continue
        vid = e.get("id")
        if not vid or len(vid) != 11:
            continue
        if vid == seed_id or vid in recent or vid in queued:
            continue
        dur = e.get("duration")
        if dur and max_duration and dur > max_duration:
            continue
        if e.get("live_status") in ("is_live", "is_upcoming"):
            continue
        title = clean_str(e.get("title") or "")
        if not title or title in ("[Deleted video]", "[Private video]"):
            continue
        if any(b in title.lower() for b in _TITLE_BLOCKLIST):
            continue
        candidates.append((f"https://www.youtube.com/watch?v={vid}", title))
    if not candidates:
        return None
    return rng.choice(candidates[:5])


def fetch_mix_entries(seed_id):
    """同步呼叫（請放在 executor）：讀取 YouTube Mix 清單的扁平 entries"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "playlistend": MIX_FETCH_COUNT,
        "skip_download": True,
    }
    url = f"https://www.youtube.com/watch?v={seed_id}&list=RD{seed_id}"
    with yt_dlp.YoutubeDL(ytdl.build_opts(opts)) as ydl:
        info = ydl.extract_info(url, download=False)
    return list((info or {}).get("entries") or [])


async def find_next(gid, queued_urls=()):
    """非同步：依最近播放的歌找下一首推薦；找不到回 None（不丟例外）"""
    import asyncio
    seed = last_seed(gid)
    if not seed:
        return None
    try:
        loop = asyncio.get_running_loop()
        entries = await loop.run_in_executor(None, fetch_mix_entries, seed)
    except Exception as e:
        if ytdl.is_bot_check(e):
            ytdl.report_bot_check("自動推薦讀取 Mix 時")
        log.warning(f"[自動推薦] 讀取 Mix 失敗（seed={seed}）: {str(e)[:200]}")
        return None
    queued_ids = {extract_video_id(u) for u in queued_urls}
    excluded = list(recent_history.get(gid, ())) + list(failed_ids.get(gid, ()))
    pick = pick_recommendation(entries, excluded, seed, queued_ids)
    if not pick:
        log.info(f"[自動推薦] seed={seed} 沒有合適的候選（共 {len(entries)} 筆）")
    return pick
