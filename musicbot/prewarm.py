"""換歌預熱：目前這首快播完時（剩 LEAD 秒），先把下一首的 FFmpeg 開好並讀到第一格。

預先解析（prefetch.py）只省掉 yt-dlp 解析；實測串流網址已解析好之後，FFmpeg 開啟 + 經分段代理拿到
第一格音訊仍要約 1–2 秒（box 上 10 次中位數約 2 秒）→ 換歌時那段空白。這裡把那段搬到上一首結尾前做。

- 預熱「下一首真正會播的歌」：佇列第一首；佇列空時是隨機循環預先抽好的那首、或自動推薦預先挑好的那首
  （跟 play_next 取的是同一份預選，所以預熱的就是實際會播的）。單曲循環 / 直播 / 暫停中不預熱
- 預熱好之後繼續盯著：使用者加歌 / 刪歌 / 換模式讓下一首變了 → 立刻收掉（還來得及就改熱新的下一首）
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

GAP_MAX = 30.0      # 上一首結束到下一首開播超過這麼久就不算換歌（例如停止後過一陣子才又點歌）

_warm = {}          # gid: {"url", "src", "opts", "res", "ts"}
_ended = {}         # gid: 上一首結束（after 回呼）的 monotonic 時間
gaps = []           # 最近幾次換歌間隔：{"ms", "warm", "manual"}
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


def mark_end(gid):
    """播放執行緒的 after 回呼：記下上一首結束的時間（量換歌間隔用）"""
    _ended[gid] = time.monotonic()


def note_start(gid, warmed, manual=False):
    """下一首 vc.play 之後呼叫：記一行「上一首結束到下一首開播」的實際間隔到日誌"""
    t = _ended.pop(gid, None)
    if t is None:
        return None
    ms = (time.monotonic() - t) * 1000
    if ms > GAP_MAX * 1000:
        return None
    gaps.append({"ms": round(ms), "warm": bool(warmed), "manual": bool(manual)})
    del gaps[:-50]
    kind = "跳過" if manual else "播完"
    log.info(f"[換歌] {gid} {kind}→下一首開播 {ms:.0f} ms（{'已預熱' if warmed else '未預熱'}）")
    return ms


def discard(gid):
    """收掉預熱好的來源並停止監看（停止 / 離開 / 睡眠計時器 / 狀態清除時呼叫）"""
    _ended.pop(gid, None)
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
    """play_next 下一次會播的歌（網址）；預測不到 → None"""
    if loop_modes.get(gid) == "one":
        return None
    q = queues.get(gid) or []
    if q:
        return q[0][0]
    from . import prefetch
    try:
        t = prefetch.predict_next(gid)     # 隨機循環：取（必要時先抽）預選的那首，之後 play_next 會取走同一首
    except Exception:
        return None
    if t == "autoplay":
        p = prefetch.peek_autoplay_pick(gid)   # 推薦還沒挑好 → 先不熱，下一秒再看
        return p[0] if p else None
    return t[0] if t else None


def _drop_stale(gid, url):
    """預熱好的不是下一首了（加歌 / 換模式 / 刪歌）→ 收掉，不要一直佔著 FFmpeg 和上游連線"""
    w = _warm.get(gid)
    if w and w["url"] != url:
        _warm.pop(gid, None)
        stats["discarded"] += 1
        _cleanup(w["src"])
        log.info(f"[預熱] {gid} 下一首變了，收掉預熱的：{w['url']}")


def open_source(gid, page_url, stream_url, headers=None, size=None, mime=None):
    """建立跟 play_next 一樣的 FFmpeg 來源（還沒預熱）"""
    import discord
    from . import audio, prefetch, rangeproxy
    from .config import FFMPEG_PATH
    cached = prefetch.get_cached(gid, page_url) or {}
    headers = headers or cached.get("headers") or None
    if size is None:
        size = cached.get("filesize") or 0
    mime = mime or cached.get("mime")
    proxied = rangeproxy.input_url(stream_url, page=page_url, headers=headers, size=size, mime=mime)
    # 走本機代理時 FFmpeg 只連 127.0.0.1，標頭由代理轉給 CDN；直連時要把 Referer 給 FFmpeg
    use_headers = None if proxied != stream_url else headers
    return discord.FFmpegPCMAudio(
        proxied,
        executable=FFMPEG_PATH,
        before_options=audio.before_options(headers=use_headers),
        options=audio.ffmpeg_options(gid),
    )


async def _watch_song(gid, version):
    """監看這首：剩 LEAD 秒內預熱下一首；之後到換歌前每秒確認下一首沒變（變了就收掉、來得及就改熱新的）"""
    from . import audio, prefetch
    try:
        while progress_versions.get(gid) == version:
            rem = _remaining(gid)
            if rem is not None and rem <= LEAD:
                url = _next_url(gid)
                _drop_stale(gid, url)
                if url and gid not in _warm and rem >= 1.5:     # 剩不到 1.5 秒就來不及了，照原本流程
                    t0 = time.monotonic()
                    res = await prefetch.resolve(gid, url)
                    opts = audio.ffmpeg_options(gid)
                    src = open_source(gid, url, res["url"])
                    ps = await audio.prime_source(src)
                    if progress_versions.get(gid) != version or _next_url(gid) != url or gid in _warm:
                        _cleanup(ps)             # 期間換歌 / 佇列變了 → 丟掉（下一輪再看要不要熱新的）
                    else:
                        _warm[gid] = {"url": url, "src": ps, "opts": opts, "res": res, "ts": time.time()}
                        stats["warmed"] += 1
                        log.info(f"[預熱] {gid} 下一首已就緒（{(time.monotonic() - t0) * 1000:.0f} ms）：{url}")
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
