"""預先解析下一首（Prefetch）

播放中就在背景（run_in_executor）用 yt-dlp 解析「下一首」的串流網址與資訊，
存進 per-guild 快取（key = 歌曲網頁網址），play_next 取到那首歌時直接用快取，
換歌幾乎不用等。

- 有效期限：取串流網址的 expire 參數提前 10 分鐘，且最多 90 分鐘（YouTube 串流網址約 6 小時失效）
- 同一網址同時只解析一次：進行中的解析共用同一個 Future（跨 guild 也共用）
- 每個 guild 同時最多 1 個 prefetch；佇列變動後 ensure() 會改瞄準新的下一首，舊的結果直接忽略
- 解析失敗只記 log，play_next 會照常重新解析（不計入連續失敗次數）
- 自動推薦開啟且佇列空時，先挑好推薦歌並預先解析；隨機循環則預先抽好下一首
- 停止 / 離開時 clear() 釋放快取
"""
import asyncio
import random
import time
from urllib.parse import urlparse, parse_qs

import yt_dlp

from .config import log, ydl_opts_single
from .state import queues, now_playing, loop_modes, saved_queues, random_loop_guilds, local_playlist, remember_duration
from . import autoplay
from . import ytdl

MAX_AGE = 90 * 60           # 快取最長保存秒數
EXPIRE_MARGIN = 10 * 60     # 距串流網址 expire 至少保留的秒數
MAX_ENTRIES = 6             # 每個 guild 最多快取幾首

_cache = {}         # gid: {page_url: {"url","duration","thumbnail","title","expires_at"}}
_inflight = {}      # page_url: asyncio.Future（共用的進行中解析）
_tasks = {}         # gid: (target_key, Task) — 每個 guild 只有一個 prefetch
_gen = {}           # gid: int — clear() 後遞增，讓舊 task 的結果被忽略
_autoplay_pick = {} # gid: {"seed": 影片ID, "pick": (url, title), "ts": time}
_random_pick = {}   # gid: {"url","title"} — 隨機循環預先抽好的下一首
_failed = {}        # gid: {page_url: 失敗時間} — 背景解析失敗後一段時間內不再重試
FAIL_RETRY_AFTER = 300


# ---------------- 解析 ----------------
def extract_stream(page_url):
    """同步（請放在 executor）：解析單曲的串流網址 + 長度 + 縮圖"""
    with yt_dlp.YoutubeDL(ytdl.build_opts(ydl_opts_single)) as ydl:
        info = ydl.extract_info(page_url, download=False)
    thumb = info.get("thumbnail", "")
    if not thumb and info.get("id"):
        thumb = f"https://i.ytimg.com/vi/{info['id']}/hqdefault.jpg"
    return {
        "url": info["url"],
        "duration": info.get("duration", 0) or 0,
        "thumbnail": thumb,
        "title": info.get("title", ""),
    }


def compute_expiry(stream_url, now=None):
    now = time.time() if now is None else now
    exp = now + MAX_AGE
    try:
        q = parse_qs(urlparse(stream_url).query)
        if "expire" in q:
            exp = min(exp, int(q["expire"][0]) - EXPIRE_MARGIN)
    except (ValueError, TypeError):
        pass
    return exp


def stream_expiry(stream_url, now=None, margin=120):
    """目前播放中的串流網址還能用到何時（跳轉用）：expire 參數提前 2 分鐘；沒有 expire 就用 MAX_AGE"""
    now = time.time() if now is None else now
    try:
        q = parse_qs(urlparse(stream_url).query)
        if "expire" in q:
            return int(q["expire"][0]) - margin
    except (ValueError, TypeError):
        pass
    return now + MAX_AGE


def _store(gid, page_url, res):
    entry = dict(res)
    remember_duration(page_url, res.get("duration"))
    entry["expires_at"] = compute_expiry(res.get("url", ""))
    if entry["expires_at"] <= time.time():
        return
    c = _cache.setdefault(gid, {})
    c.pop(page_url, None)
    c[page_url] = entry
    while len(c) > MAX_ENTRIES:
        c.pop(next(iter(c)))


def get_cached(gid, page_url):
    c = _cache.get(gid)
    e = c.get(page_url) if c else None
    if not e:
        return None
    if e["expires_at"] <= time.time():
        c.pop(page_url, None)
        return None
    return e


def _shared_resolve(page_url):
    """回傳進行中的共用 Future；沒有就開一個新的 executor 解析"""
    fut = _inflight.get(page_url)
    if fut is not None:
        return fut, True
    loop = asyncio.get_running_loop()
    fut = asyncio.ensure_future(loop.run_in_executor(None, extract_stream, page_url))

    def _done(f):
        if _inflight.get(page_url) is f:
            del _inflight[page_url]
        if not f.cancelled():
            f.exception()  # 取走例外，避免 "exception was never retrieved"
    fut.add_done_callback(_done)
    _inflight[page_url] = fut
    return fut, False


async def resolve(gid, page_url):
    """play_next 用：快取命中直接回傳，否則（共用進行中的解析或）立即解析。
    回傳 dict（url / duration / thumbnail / title）。失敗則丟出例外，交給呼叫端既有的失敗處理。"""
    e = get_cached(gid, page_url)
    if e:
        log.info(f"[預先解析] 命中 {gid}: {page_url}")
        return e
    gen = _gen.get(gid, 0)
    fut, shared = _shared_resolve(page_url)
    if shared:
        try:
            res = await asyncio.shield(fut)
            log.info(f"[預先解析] 命中（等待進行中的解析）{gid}: {page_url}")
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.info(f"[預先解析] 進行中的解析失敗，改為重新解析 {gid}: {ex}")
            fut, _ = _shared_resolve(page_url)
            res = await asyncio.shield(fut)
    else:
        log.info(f"[預先解析] 未命中 {gid}: {page_url}")
        res = await asyncio.shield(fut)
    if _gen.get(gid, 0) == gen:
        _store(gid, page_url, res)
    return res


# ---------------- 預測下一首 ----------------
def predict_next(gid):
    """回傳 play_next 下一次會播的 (page_url, title)、"autoplay"（需先挑推薦）或 None（無法預測）"""
    from .state import loop_current
    if loop_modes.get(gid) == "one" and loop_current.get(gid):
        return loop_current[gid]       # 單曲循環：下一首就是目前這首（快取保留）
    q = queues.get(gid)
    if q:
        return q[0]
    if random_loop_guilds.get(gid) and local_playlist:
        rp = _random_pick.get(gid)
        if rp is None or not any(s.get("url") == rp["url"] for s in local_playlist):
            s = random.choice(local_playlist)
            rp = _random_pick[gid] = {"url": s["url"], "title": s["title"]}
        return (rp["url"], rp["title"])
    if autoplay.is_enabled(gid):
        return "autoplay"
    return None


def take_random_pick(gid):
    """play_next 的隨機循環分支用：取走預先抽好的歌（仍在歌單中才算），否則 None"""
    rp = _random_pick.pop(gid, None)
    if rp and any(s.get("url") == rp["url"] for s in local_playlist):
        return rp
    return None


def take_autoplay_pick(gid):
    """play_next 的自動推薦分支用：種子（目前歌曲）沒變且未過期才使用預先挑好的推薦"""
    p = _autoplay_pick.pop(gid, None)
    if not p or not p.get("pick"):
        return None
    if p["seed"] != autoplay.last_seed(gid) or time.time() - p["ts"] > MAX_AGE:
        return None
    vid = autoplay.extract_video_id(p["pick"][0])
    if vid and vid in autoplay.failed_ids.get(gid, ()):
        return None
    log.info(f"[預先解析] 自動推薦命中 {gid}: {p['pick'][1]}")
    return p["pick"]


# ---------------- 背景 prefetch ----------------
async def _prefetch_url(gid, page_url, gen):
    if get_cached(gid, page_url):
        return
    fut, _ = _shared_resolve(page_url)
    try:
        res = await asyncio.shield(fut)
    except asyncio.CancelledError:
        raise
    except Exception as ex:
        if ytdl.is_bot_check(ex):
            ytdl.report_bot_check("預先解析時")
        log.info(f"[預先解析] 失敗（播放時會重新解析）{gid}: {str(ex)[:120]}")
        if _gen.get(gid, 0) == gen:
            _failed.setdefault(gid, {})[page_url] = time.time()
        return
    if _gen.get(gid, 0) == gen:
        _store(gid, page_url, res)
        log.info(f"[預先解析] 已備妥下一首 {gid}: {res.get('title') or page_url}")


async def _prefetch_autoplay(gid, seed, gen):
    pick = await autoplay.find_next(gid, [])
    if _gen.get(gid, 0) != gen:
        return
    # 找不到也記下來（pick=None），同一個種子不再重複讀 Mix
    _autoplay_pick[gid] = {"seed": seed, "pick": pick, "ts": time.time()}
    if not pick:
        return
    await _prefetch_url(gid, pick[0], gen)


def ensure(gid):
    """確認背景 prefetch 瞄準的是目前的下一首；不是就取消舊的、改抓新的。可隨時重複呼叫（同步、很便宜）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    if not now_playing.get(gid):
        _cancel(gid)  # 沒在播放（停止/閒置）就不預先解析
        return
    target = predict_next(gid)
    if target is None:
        _cancel(gid)
        return
    gen = _gen.get(gid, 0)
    if target == "autoplay":
        seed = autoplay.last_seed(gid)
        if not seed:
            return
        key = ("autoplay", seed)
        ap = _autoplay_pick.get(gid)
        if ap and ap["seed"] == seed:
            if ap["pick"] is None:
                return
            if get_cached(gid, ap["pick"][0]) or ap["pick"][0] in _failed.get(gid, {}):
                return
            key = ("url", ap["pick"][0])
            coro_factory = lambda: _prefetch_url(gid, ap["pick"][0], gen)
        else:
            coro_factory = lambda: _prefetch_autoplay(gid, seed, gen)
    else:
        url = target[0]
        if get_cached(gid, url):
            return
        failed_at = _failed.get(gid, {}).get(url)
        if failed_at and time.time() - failed_at < FAIL_RETRY_AFTER:
            return
        key = ("url", url)
        coro_factory = lambda: _prefetch_url(gid, url, gen)
    cur = _tasks.get(gid)
    if cur and cur[0] == key and not cur[1].done():
        return
    _cancel(gid)
    task = asyncio.create_task(coro_factory())
    _tasks[gid] = (key, task)

    def _fin(t, gid=gid):
        if _tasks.get(gid, (None, None))[1] is t:
            _tasks.pop(gid, None)
    task.add_done_callback(_fin)


def _cancel(gid):
    cur = _tasks.pop(gid, None)
    if cur and not cur[1].done():
        cur[1].cancel()  # 共用的解析被 shield 保護，不會中斷別人在等的結果


def forget(gid, page_url):
    """播放失敗時呼叫：丟掉這首的快取，下次重新解析"""
    c = _cache.get(gid)
    if c:
        c.pop(page_url, None)


def clear(gid):
    """停止 / 離開：取消 prefetch、釋放快取"""
    _gen[gid] = _gen.get(gid, 0) + 1
    _cancel(gid)
    _cache.pop(gid, None)
    _autoplay_pick.pop(gid, None)
    _random_pick.pop(gid, None)
    _failed.pop(gid, None)
