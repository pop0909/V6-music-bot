"""換歌預熱：目前這首快播完時（剩 LEAD 秒），先把下一首的 FFmpeg 開好並讀到第一格。

預先解析（prefetch.py）只省掉 yt-dlp 解析；實測串流網址已解析好之後，FFmpeg 開啟 + 經分段代理拿到
第一格音訊仍要約 1–2 秒（box 上 10 次中位數約 2 秒）→ 換歌時那段空白。這裡把那段搬到上一首結尾前做。

- 只預熱「佇列第一首」（單曲循環 / 直播 / 暫停中 / 佇列空（自動推薦、隨機循環）都不預熱，照原本流程）
- play_next 取到同一首、而且 FFmpeg 選項（音量平衡）沒變、預熱不超過 MAX_AGE 秒才用；否則收掉照常開
- 開播失敗重試（403 重新解析）、睡眠計時器「播完這首」、停止 / 離開 / 休眠都會收掉預熱的來源
"""
import asyncio
import time

from .config import log
from .state import queues, loop_modes, song_durations, progress_versions, song_start_times

LEAD = 8.0          # 剩幾秒時開始預熱
MAX_AGE = 45.0      # 預熱好的來源最多保留幾秒（避免上游連線閒置太久）
POLL = 1.0

_warm = {}          # gid: {"url", "src", "opts", "res", "ts"}
_watch = {}         # gid: Task
stats = {"warmed": 0, "used": 0, "discarded": 0}


def _cleanup(src):
    if src is None:
        return
    try:
        from .audio import _in_thread
        _in_thread(src.cleanup)
    except RuntimeError:            # 沒有事件迴圈（測試 / 關閉中）
        try:
            src.cleanup()
        except Exception:
            pass
    except Exception:
        pass


def discard(gid):
    """收掉預熱好的來源並停止監看（停止 / 離開 / 睡眠計時器 / 狀態清除時呼叫）"""
    t = _watch.pop(gid, None)
    if t and not t.done() and t is not _current_task():
        t.cancel()
    w = _warm.pop(gid, None)
    if w:
        stats["discarded"] += 1
        _cleanup(w["src"])


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def take(gid, page_url):
    """play_next 用：同一首、選項沒變、沒過期 → 回傳 (source, res)；否則收掉並回傳 None"""
    w = _warm.pop(gid, None)
    if not w:
        return None
    from . import audio
    if w["url"] != page_url or w["opts"] != audio.ffmpeg_options(gid) or time.time() - w["ts"] > MAX_AGE:
        stats["discarded"] += 1
        _cleanup(w["src"])
        return None
    stats["used"] += 1
    return w["src"], w["res"]


def has(gid):
    return gid in _warm


def _remaining(gid):
    dur = song_durations.get(gid) or 0
    if dur <= 0 or gid not in song_start_times:      # 直播 / 暫停中
        return None
    return dur - (time.time() - song_start_times[gid])


def _next_url(gid):
    if loop_modes.get(gid) == "one":
        return None
    q = queues.get(gid) or []
    return q[0][0] if q else None


def open_source(gid, page_url, stream_url):
    """建立跟 play_next 一樣的 FFmpeg 來源（還沒預熱）"""
    import discord
    from . import audio, rangeproxy
    from .config import FFMPEG_PATH
    return discord.FFmpegPCMAudio(
        rangeproxy.input_url(stream_url, page=page_url),
        executable=FFMPEG_PATH,
        before_options=audio.before_options(),
        options=audio.ffmpeg_options(gid),
    )


async def _watch_song(gid, version):
    from . import audio, prefetch
    try:
        while progress_versions.get(gid) == version:
            rem = _remaining(gid)
            if rem is not None and rem <= LEAD and gid not in _warm:
                url = _next_url(gid)
                if not url:
                    return
                if rem < 1.5:
                    return                   # 太晚了，來不及，照原本流程
                t0 = time.monotonic()
                res = await prefetch.resolve(gid, url)
                opts = audio.ffmpeg_options(gid)
                src = open_source(gid, url, res["url"])
                ps = await audio.prime_source(src)
                if progress_versions.get(gid) != version or _next_url(gid) != url or gid in _warm:
                    _cleanup(ps)             # 期間換歌 / 佇列變了 → 丟掉
                    return
                _warm[gid] = {"url": url, "src": ps, "opts": opts, "res": res, "ts": time.time()}
                stats["warmed"] += 1
                log.info(f"[預熱] {gid} 下一首已就緒（{(time.monotonic() - t0) * 1000:.0f} ms）：{url}")
                return
            await asyncio.sleep(POLL)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.info(f"[預熱] {gid} 預熱失敗（換歌時照常開啟）：{str(e)[:160]}")
    finally:
        if _watch.get(gid) is _current_task():
            _watch.pop(gid, None)


def start(gid, version):
    """開播成功後呼叫：監看這首，快結束時預熱下一首"""
    old = _watch.pop(gid, None)
    if old and not old.done():
        old.cancel()
    _watch[gid] = asyncio.get_running_loop().create_task(_watch_song(gid, version))
