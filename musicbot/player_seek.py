"""跳轉（seek）、暫停中跳轉的預熱、播放中換濾鏡（音量平衡）重開、恢復播放。從 player.py 拆出，行為不變。

留在 player.py 的函式與測試會替換的名字（player.X）一律經 `_p.X` 在呼叫時取用：
測試 mock.patch.object(player, ...) 照樣生效，也避免循環 import（_p 在檔案最後才 import）。
"""
from .config import DEFAULT_VOLUME
import asyncio
import math
import time

import discord

from .bot import client
from . import rangeproxy
from .config import FFMPEG_PATH, log
from .state import (
    now_playing, voice_clients, volumes, volume_transformers, now_playing_url, song_start_times,
    song_durations, paused_position, progress_versions, play_locks, pending_seeks,
    now_playing_stream, is_playing_state as _is_playing_state, is_paused_state as _is_paused_state,
    notify_change,
)
from .utils import format_time, parse_time_str
from . import prefetch
from . import audio, ytdl


# ================== 跳轉（seek） ==================
SEEK_HTTP = {"invalid": 400, "out_of_range": 400, "not_playing": 409, "unseekable": 409,
             "busy": 409, "failed": 502}
_seek_locks = {}


def _remember_stream(gid, page_url, stream_url):
    now_playing_stream[gid] = {"page": page_url, "url": stream_url,
                               "expires_at": prefetch.stream_expiry(stream_url)}


def parse_seek_position(value):
    """網頁傳數字（秒）、Discord 傳字串（1:23 / 83）；回傳 float 秒數，不合法回傳 None"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        v = parse_time_str(value)
        if v is None:
            return None
    if not math.isfinite(v) or v < 0:
        return None
    return v


def _seek_err(code, message):
    return {"status": "error", "code": code, "message": message}


def _current_vc(gid):
    vc = voice_clients.get(gid)
    if not vc:
        guild = client.get_guild(gid)
        vc = guild.voice_client if guild else None
    return vc


async def _stream_url_for(gid, page_url):
    """目前歌曲的串流網址：還沒過期就直接用；過期（或不知道）就重新解析"""
    info = now_playing_stream.get(gid)
    if info and info.get("page") == page_url and info.get("expires_at", 0) > time.time():
        return info["url"]
    log.info(f"[跳轉] 串流網址已過期，重新解析 {gid}: {page_url}")
    prefetch.forget(gid, page_url)
    res = await prefetch.resolve(gid, page_url)
    _remember_stream(gid, page_url, res["url"])
    return res["url"]


def _cleanup_later(src, delay=1.0):
    """舊的 FFmpeg 晚一點才收掉：播放執行緒可能正在讀舊來源的最後一格，立刻 kill 會被當成「播完」而跳下一首"""
    if src is None:
        return
    loop = asyncio.get_running_loop()

    def _do():
        try:
            src.cleanup()
        except Exception as e:
            log.debug(f"[跳轉] 清理舊來源失敗: {e}")
    loop.call_later(delay, lambda: loop.run_in_executor(None, _do))


async def _restart_at(gid, vc, page_url, pos, version, resume=False, catch=None):
    """用 -ss 重開 FFmpeg 並直接替換播放中的來源（不 stop → 不會觸發 after_play 切歌）。
    濾鏡沿用目前的音量平衡設定，只做極短淡入。回傳 False = 期間已換歌 / 停止。
    catch={"t0": monotonic, "lead": 秒}（播放中換濾鏡用）：重開實際花的時間超過 lead 時，新來源先快轉同樣長度，
    換上時接在舊來源正在播的位置；快轉秒數寫回 catch["dropped"]"""
    stream_url = await _stream_url_for(gid, page_url)
    if (progress_versions.get(gid) != version or now_playing_url.get(gid) != page_url
            or not (vc.is_playing() or vc.is_paused())):
        return False
    source = await _p._primed_at(gid, stream_url, pos)
    if catch is not None and vc.is_playing():
        catch["dropped"] = 0.0
        for _ in range(2):    # 快轉本身也要一點時間 → 最多補兩次
            behind = (time.monotonic() - catch["t0"]) - catch["lead"] - catch["dropped"]
            if behind < 2 * audio.FRAME_SECONDS:
                break
            catch["dropped"] += await audio.drop_frames(source, behind)
    # FFmpeg 啟動期間舊來源繼續播；預熱完再確認一次沒換歌
    if (progress_versions.get(gid) != version or now_playing_url.get(gid) != page_url
            or not (vc.is_playing() or vc.is_paused())):
        source.cleanup()
        return False
    if vc.is_paused() and not resume:
        # 預熱期間被按了暫停：set_source 會順便 resume，所以先別換；當成「暫停中跳轉」留到繼續時換上
        _drop_pending(gid)
        pending_seeks[gid] = {"pos": pos, "orig": pos, "version": version, "src": source}
        return "paused"
    _swap_source(gid, vc, source)
    return True


async def _primed_at(gid, stream_url, pos):
    source = discord.FFmpegPCMAudio(
        rangeproxy.input_url(stream_url, page=now_playing_url.get(gid)),   # 網址失效時代理會用歌曲網址重新解析
        executable=FFMPEG_PATH,
        before_options=audio.before_options(pos),
        options=audio.ffmpeg_options(gid, fade_in=audio.SEEK_FADE_IN_SECONDS),
    )
    return await audio.prime_source(source)


def _swap_source(gid, vc, source):
    """換掉播放中的來源（AudioPlayer.set_source：同一個播放執行緒、同一個 after callback；注意它會順便 resume）"""
    transformer = discord.PCMVolumeTransformer(source, volume=volumes.get(gid, DEFAULT_VOLUME))
    old = vc.source
    vc.source = transformer
    volume_transformers[gid] = transformer
    _p._cleanup_later(old)


def _drop_pending(gid):
    """丟掉暫停中跳轉的預備來源（換歌 / 停止 / 又跳到別處）"""
    pend = pending_seeks.pop(gid, None)
    if pend:
        _discard_prepared(pend)
    return pend


def _discard_prepared(pend):
    pend["dead"] = True
    task = pend.get("task")
    if task and not task.done():
        task.cancel()
    src = pend.pop("src", None)
    if src is not None:
        try:
            src.cleanup()
        except Exception:
            pass


async def _prepare_pending(gid, pend, page_url):
    """暫停中跳轉：先在背景把新位置的 FFmpeg 開好並預熱，按「繼續」時直接換上，不必再等 FFmpeg 啟動"""
    try:
        stream_url = await _stream_url_for(gid, page_url)
        src = await _p._primed_at(gid, stream_url, pend["pos"])
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.info(f"[跳轉] {gid} 預備暫停中的跳轉失敗，恢復時再試: {e}")
        return
    # 只看 dead：resume_playback 會先把 pend 從 pending_seeks 取出再等這個工作，那時不能把預熱好的來源丟掉
    if pend.get("dead"):
        src.cleanup()
        return
    pend["src"] = src

_seek_gen = {}
async def seek(gid, position, wait=True, catch=None):
    """跳到目前歌曲的 position 秒。回傳 {"status": "ok", "position", "paused"} 或
    {"status": "error", "code", "message"}（code 對應 SEEK_HTTP）。
    - 暫停中：只更新位置，並在背景預熱新位置的 FFmpeg；恢復播放時直接換上（維持暫停、不出聲）
    - wait=False（Web）：驗證完就回傳 accepted，重開 FFmpeg 在背景做（連續拖曳只執行最後一次）
    - 長度未知（直播）不能跳；超過長度回錯誤；接近結尾會夾到最後 1 秒"""
    pos = parse_seek_position(position)
    if pos is None:
        return _seek_err("invalid", "時間格式不正確（例：1:23 或 83）")
    vc = _current_vc(gid)
    page_url = now_playing_url.get(gid)
    if not vc or not now_playing.get(gid) or not page_url or not (vc.is_playing() or vc.is_paused()):
        return _seek_err("not_playing", "目前沒有播放中的歌曲")
    dur = song_durations.get(gid, 0) or 0
    if dur <= 0:
        return _seek_err("unseekable", "這首歌長度未知（可能是直播），無法跳轉")
    if pos > dur:
        return _seek_err("out_of_range", f"超出歌曲長度（{format_time(dur)}）")
    pos = min(pos, max(0.0, dur - 1.0))
    lock = play_locks.get(gid)
    if (lock and lock.locked()) or audio.is_fading(gid):
        return _seek_err("busy", "正在切換歌曲，請稍後再試")
    slock = _seek_locks.setdefault(gid, asyncio.Lock())
    async with slock:
        version = progress_versions.get(gid)
        if vc.is_paused():
            prev = pending_seeks.get(gid)
            orig = prev["orig"] if prev and prev.get("version") == version else paused_position.get(gid, _p._elapsed(gid))
            _drop_pending(gid)
            pend = {"pos": pos, "orig": orig, "version": version}
            pending_seeks[gid] = pend
            pend["task"] = _p._spawn(_prepare_pending(gid, pend, page_url))
            paused_position[gid] = pos
            song_start_times.pop(gid, None)
            log.info(f"[跳轉] {gid} 暫停中 → {format_time(pos)}（恢復播放時生效）")
            notify_change()
            return {"status": "ok", "position": pos, "paused": True}
    if not wait:
        gen = _seek_gen[gid] = _seek_gen.get(gid, 0) + 1
        _p._spawn(_seek_playing(gid, vc, page_url, pos, version, gen))
        return {"status": "ok", "position": pos, "paused": False, "accepted": True}
    return await _seek_playing(gid, vc, page_url, pos, version, None, catch)


async def _seek_playing(gid, vc, page_url, pos, version, gen, catch=None):
    slock = _seek_locks.setdefault(gid, asyncio.Lock())
    async with slock:
        if gen is not None and _seek_gen.get(gid) != gen:
            return {"status": "ok", "position": pos, "paused": False, "superseded": True}
        if progress_versions.get(gid) != version:
            return _seek_err("busy", "歌曲已切換")
        if vc.is_paused():
            # 背景排隊期間被暫停 → 改成暫停中跳轉
            orig = paused_position.get(gid, _p._elapsed(gid))
            _drop_pending(gid)
            pending_seeks[gid] = pend = {"pos": pos, "orig": orig, "version": version}
            pend["task"] = _p._spawn(_prepare_pending(gid, pend, page_url))
            paused_position[gid] = pos
            song_start_times.pop(gid, None)
            notify_change()
            return {"status": "ok", "position": pos, "paused": True}
        try:
            ok = await _p._restart_at(gid, vc, page_url, pos, version, catch=catch)
        except Exception as e:
            log.error(f"[跳轉] {gid} 失敗: {e}")
            return _seek_err("failed", ytdl.friendly_error(e, "跳轉時"))
        if not ok:
            return _seek_err("busy", "歌曲已切換")
        if ok == "paused":
            paused_position[gid] = pos
            song_start_times.pop(gid, None)
            notify_change()
            return {"status": "ok", "position": pos, "paused": True}
        if catch and catch.get("dropped"):
            pos += catch["dropped"]
        song_start_times[gid] = time.time() - pos
        paused_position.pop(gid, None)
        log.info(f"[跳轉] {gid} → {format_time(pos)}")
        notify_change()
        return {"status": "ok", "position": pos, "paused": False}


# ---------- 播放中換濾鏡（音量平衡）：沿用跳轉機制，從目前位置用新濾鏡重開 FFmpeg ----------
REAPPLY_LEAD_MIN, REAPPLY_LEAD_MAX = 0.05, 1.5
_reapply_lead = 0.35          # FFmpeg 重開預估秒數（新來源從「現在 + 這段時間」開始 → 換上時剛好接上）；
                              # 實際比預估久 → _restart_at 會把新來源快轉補齊（不重複）；比預估快 → 只會跳過 < 預估的一小段
reapply_stats = {}            # guild_id: {"ms": 實測毫秒, "pos": 起點秒, "ok": bool}（日誌 / 測試用）
_reapply_gen = {}


async def reapply_audio(gid, retries=10):
    """音量平衡設定改了：播放中 → 從目前位置重開 FFmpeg 立即生效；暫停中 → 備好新位置的來源，按繼續時換上；
    沒在播 / 長度未知（直播）→ 下一首生效。連按只做最後一次。回傳 "now" / "paused" / "next" / "superseded"。"""
    global _reapply_lead
    gen = _reapply_gen[gid] = _reapply_gen.get(gid, 0) + 1
    for attempt in range(retries + 1):
        if _reapply_gen.get(gid) != gen:
            return "superseded"
        vc = _current_vc(gid)
        if not vc or not now_playing.get(gid) or not now_playing_url.get(gid) or not (vc.is_playing() or vc.is_paused()):
            lock = play_locks.get(gid)
            if lock and lock.locked() and attempt < retries:
                await asyncio.sleep(0.3)      # 正在開下一首：等它開好再重開（開播時可能已讀到舊設定）
                continue
            return "next"
        if (song_durations.get(gid, 0) or 0) <= 0:
            return "next"
        paused = vc.is_paused()
        if audio.filters_current(gid) and not pending_seeks.get(gid):
            return "now"                      # 目前的 FFmpeg 已經是新設定（例如等待期間剛開播的下一首）
        # 預估往低估（×0.7）：估少了由快轉精準補齊（20 ms 一格）；估多了才會跳過一小段
        lead = 0.0 if paused else _reapply_lead * 0.7
        pos = paused_position.get(gid, _p._elapsed(gid)) if paused else _p._elapsed(gid) + lead
        t0 = time.monotonic()
        catch = None if paused else {"t0": t0, "lead": lead}
        res = await seek(gid, pos, wait=True, catch=catch)
        ms = (time.monotonic() - t0) * 1000
        if res.get("status") == "ok":
            if not res.get("paused"):
                # 下次的預估往實測值靠（EWMA），夾在合理範圍
                _reapply_lead = min(REAPPLY_LEAD_MAX, max(REAPPLY_LEAD_MIN, 0.7 * _reapply_lead + 0.3 * ms / 1000))
            reapply_stats[gid] = {"ms": round(ms), "pos": round(pos, 2), "ok": True, "paused": bool(res.get("paused")),
                                  "lead_ms": round(lead * 1000), "dropped_ms": round((catch or {}).get("dropped", 0) * 1000)}
            log.info(f"[音量平衡] {gid} 已從 {format_time(pos)} 套用新設定（{'暫停中，繼續時生效' if res.get('paused') else f'{ms:.0f} ms'}）")
            return "paused" if res.get("paused") else "now"
        if res.get("code") == "busy" and attempt < retries:
            await asyncio.sleep(0.3)          # 正在換歌 / 淡出 / 別的跳轉：稍後再試
            continue
        reapply_stats[gid] = {"ms": round(ms), "pos": round(pos, 2), "ok": False, "code": res.get("code")}
        if res.get("code") in ("not_playing", "unseekable", "busy"):
            return "next"
        log.warning(f"[音量平衡] {gid} 播放中套用失敗，下一首生效：{res.get('message')}")
        return "next"
    return "next"


async def resume_playback(gid, vc):
    """恢復播放（Web / Discord 按鈕 / 指令共用）：暫停中有跳轉過就先用新位置重開 FFmpeg"""
    pend = pending_seeks.pop(gid, None)
    if pend and pend.get("version") == progress_versions.get(gid) and now_playing_url.get(gid):
        ok = False
        try:
            task = pend.get("task")
            if task and not task.done() and "src" not in pend:
                await asyncio.shield(task)        # 預熱進行中 → 等它（比重開一次快）
            src = pend.pop("src", None)
            if src is not None and progress_versions.get(gid) == pend["version"] and vc.is_paused():
                _swap_source(gid, vc, src)        # 已預熱好：直接換上（set_source 會順便 resume）
                ok = True
            elif src is not None:
                src.cleanup()
            if not ok:
                ok = await _p._restart_at(gid, vc, now_playing_url[gid], pend["pos"], pend["version"], resume=True)
        except Exception as e:
            log.error(f"[跳轉] {gid} 恢復播放時重開失敗，從原位置繼續: {e}")
            ok = False
        paused_position[gid] = pend["pos"] if ok else pend["orig"]
    elif pend:
        _discard_prepared(pend)
    if gid in paused_position:
        song_start_times[gid] = time.time() - paused_position.pop(gid)
    vc.resume()
    _is_playing_state[gid] = True
    _is_paused_state[gid] = False
    notify_change()


from . import player as _p  # noqa: E402  放最後：player 載入完才取用（見上方說明）
