"""播放引擎核心：play_next、進度更新、狀態清理、yt-dlp 解析"""
from .config import DEFAULT_VOLUME
import asyncio
import math
import random
import time
import traceback
from functools import partial

import discord
import yt_dlp

from .bot import client
from . import rangeproxy
from .config import OUTPUT_CHANNEL_ID, FFMPEG_PATH, log, ydl_opts
from .state import (
    state_lock, queues, now_playing, voice_clients, random_loop_guilds,
    volumes, volume_transformers, control_panels, now_playing_url,
    now_playing_thumbnail, song_start_times, song_durations, paused_position,
    progress_versions, progress_tasks, current_voice_channel,
    is_playing_state, is_paused_state, loop_modes, saved_queues,
    local_playlist, sleep_timers, play_failures, play_locks,
    play_notices, song_meta, note_song_meta,
    pending_seeks, now_playing_stream, remember_duration,
    is_playing_state as _is_playing_state, is_paused_state as _is_paused_state,
    notify_change, idle_leave_tasks, loop_current, manual_advance,
)

# 連續失敗幾次就停止自動接下一首（避免 YouTube 403 時無限重試洗頻）
MAX_CONSECUTIVE_FAILURES = 3
# 開播時 FFmpeg 拿不到第一格（串流網址冷啟動 403）→ 清快取重解析再開一次，通常就好
STREAM_OPEN_RETRIES = 2
from .utils import clean_str, format_time, parse_time_str
from . import autoplay
from . import prefetch
from . import audio, history, ytdl

# 分段代理遇到 403/410（串流網址失效）時，用 yt-dlp 重新解析同一首歌的網址續傳（在代理執行緒裡同步跑）
rangeproxy.set_resolver(lambda page: prefetch.extract_stream(page)["url"])


async def _get_output_channel(guild_id):
    """取得指定的 Discord 輸出頻道"""
    guild = client.get_guild(guild_id)
    if not guild:
        return None
    ch = guild.get_channel(OUTPUT_CHANNEL_ID)
    return ch if isinstance(ch, discord.TextChannel) else None


# ================== 睡眠計時器 ==================
async def _sleep_timer_fire(gid):
    """計時器到期：停止播放、清佇列、離開語音"""
    try:
        await asyncio.sleep(1)  # 讓 set 呼叫先完成
        sleep_timers.pop(gid, None)
        log.info(f"[睡眠計時器] {gid} 時間到，停止播放")
        vc = voice_clients.get(gid) or (client.get_guild(gid).voice_client if client.get_guild(gid) else None)
        await _clear_guild_state(gid)
        if vc:
            vc.stop()
            await vc.disconnect(force=True)
        await _update_panel(gid, "😴 睡眠計時器時間到，已停止播放並離開", view=None, remove=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error(f"sleep timer error: {e}")


def set_sleep_timer(gid, mode, minutes=0):
    """設定睡眠計時器。mode: 'minutes' | 'track_end' | 'off'
    回傳 (success, 訊息)
    """
    cancel_sleep_timer(gid)
    if mode == "off":
        return True, "已取消睡眠計時器"
    entry = {"mode": mode, "deadline": None, "task": None}
    if mode == "minutes":
        entry["deadline"] = time.time() + minutes * 60

        async def _timer():
            try:
                await asyncio.sleep(minutes * 60)
                # 到時間了：若還在播就整組停止
                await _sleep_timer_fire(gid)
            except asyncio.CancelledError:
                pass

        entry["task"] = asyncio.create_task(_timer())
        sleep_timers[gid] = entry
        return True, f"😴 {minutes} 分鐘後將停止播放並離開"
    elif mode == "track_end":
        # 在 play_next 取完下一首歌時檢查（旗標式，不需 task）
        sleep_timers[gid] = entry
        return True, "😴 播完目前這首後將停止"
    return False, "無效模式"


def cancel_sleep_timer(gid):
    old = sleep_timers.pop(gid, None)
    if old and old.get("task"):
        old["task"].cancel()


def check_track_end_sleep(gid):
    """play_next 開始播新歌前呼叫：若 track_end 計時器生效，回傳 True 表示應停止"""
    info = sleep_timers.get(gid)
    if info and info.get("mode") == "track_end":
        sleep_timers.pop(gid, None)

        async def _fire():
            await _sleep_timer_fire(gid)
        _keep(_fire())
        return True
    return False


async def _update_panel(gid, content, view=None, remove=False):
    msg = control_panels.get(gid)
    if msg is None: return
    try: await msg.edit(content=content, view=view)
    except: pass
    # 編輯期間別的流程（停止 / 離開 / 換歌）可能已經移除或換掉面板 → 只移除「自己這則」，不會 KeyError
    if remove and control_panels.get(gid) is msg: control_panels.pop(gid, None)


# Discord 對「建立超過 1 小時的訊息」編輯限速嚴很多（log：三次都在面板建立後約 62 分鐘開始連續 429）
# → 面板快滿 1 小時就重發一則新的、刪掉舊的
PANEL_MAX_AGE = 55 * 60


def _panel_age(msg):
    import datetime as _dt
    ca = getattr(msg, "created_at", None)
    if not isinstance(ca, _dt.datetime):
        return 0.0
    try:
        return time.time() - ca.timestamp()
    except Exception:
        return 0.0


async def _repost_panel(gid, channel, content, view=None):
    """發一則新的控制面板取代太舊的那則；失敗就保留舊的"""
    old = control_panels.get(gid)
    ch = channel or getattr(old, "channel", None)
    if ch is None:
        return None
    if view is None:
        from .views import MusicControlView
        view = MusicControlView(gid)
    try:
        msg = await ch.send(content, view=view)
    except Exception:
        return None
    if control_panels.get(gid) is not old:     # 等待期間面板被移除 / 換掉 → 新的這則也收掉，不搶回來
        try: await msg.delete()
        except Exception: pass
        return None
    control_panels[gid] = msg
    if old is not None:
        try: await old.delete()
        except Exception: pass
    return msg


async def _clear_guild_state(gid):
    if gid in queues: queues[gid] = []
    if gid in random_loop_guilds: random_loop_guilds[gid] = False
    is_playing_state[gid] = False
    is_paused_state[gid] = False
    # 循環模式是使用者的設定：停止 / 離開 / 休眠都保留（舊版會在這裡清掉，按鈕會莫名回到「關」）
    if gid in saved_queues: del saved_queues[gid]
    loop_current.pop(gid, None)
    manual_advance.pop(gid, None)
    for d in [now_playing, now_playing_url, voice_clients, volume_transformers, volumes]:
        if gid in d: del d[gid]
    # 注意：保留 current_voice_channel[gid] — 停止後點歌時需要它來自動重連語音頻道
    if gid in now_playing_thumbnail: del now_playing_thumbnail[gid]
    if gid in progress_tasks:
        progress_tasks[gid].cancel()
        del progress_tasks[gid]
    if gid in progress_versions: del progress_versions[gid]
    _drop_pending(gid)
    for d in [song_start_times, song_durations, paused_position, now_playing_stream]:
        if gid in d: del d[gid]
    prefetch.clear(gid)  # 釋放預先解析的快取、取消背景解析
    notify_change()


def _voice_connected(guild_id):
    guild = client.get_guild(guild_id)
    vc = guild.voice_client if guild else None
    return bool(vc and vc.is_connected())


async def _autoplay_pick(guild_id):
    """回傳 (url, title) 或 None。bot 不在語音（例如剛按停止）時不推薦，避免殘留佇列"""
    if not autoplay.is_enabled(guild_id) or not _voice_connected(guild_id):
        return None
    # 播放上一首時已在背景挑好並解析的推薦（種子相同才用）
    pick = prefetch.take_autoplay_pick(guild_id) if not queues.get(guild_id) else None
    if pick:
        return pick
    pick = await autoplay.find_next(guild_id, [u for u, _ in queues.get(guild_id, [])])
    # 解析期間可能被 /stop、/leave 或又有人點歌 → 重新確認
    if not pick or not _voice_connected(guild_id) or queues.get(guild_id):
        return None
    return pick


async def play_next(guild_id, channel=None):
    """序列化同一 guild 的 play_next，避免兩個請求同時 pop 佇列、第二個 vc.play 失敗而吃掉歌"""
    lock = play_locks.setdefault(guild_id, asyncio.Lock())
    async with lock:
        vc = voice_clients.get(guild_id)
        if not vc:
            guild = client.get_guild(guild_id)
            vc = guild.voice_client if guild else None
        if vc and (vc.is_playing() or vc.is_paused()):
            # 另一個呼叫已經開播了（例如連按兩次 /play），不要再 pop 佇列
            return
        await _play_next_locked(guild_id, channel)


async def _play_next_locked(guild_id, channel=None):
    # 強制使用指定輸出頻道
    out_ch = await _get_output_channel(guild_id)
    if out_ch:
        channel = out_ch
    if not channel:
        # fallback：遍歷伺服器文字頻道
        guild = client.get_guild(guild_id)
        if guild:
            for c in guild.text_channels:
                channel = c
                break
    if not channel:
        return

    # 睡眠計時器（播完這首模式）：不播下一首，直接收工
    if check_track_end_sleep(guild_id):
        if guild_id in now_playing:
            del now_playing[guild_id]
        is_playing_state[guild_id] = False
        is_paused_state[guild_id] = False
        loop_current.pop(guild_id, None)
        await _update_panel(guild_id, "😴 睡眠計時器：播完目前歌曲後已停止", view=None, remove=True)
        return

    if guild_id in progress_tasks:
        progress_tasks[guild_id].cancel()
        del progress_tasks[guild_id]
    if guild_id in progress_versions:
        del progress_versions[guild_id]

    # 單曲循環：歌曲自然播完 → 重播同一首（不放進佇列，佇列顯示的仍是真正的下一首）；
    # 使用者按跳過 / 指定播放（manual_advance）→ 照常前進
    advance = manual_advance.pop(guild_id, False)
    replay = None
    if loop_modes.get(guild_id) == "one" and not advance and loop_current.get(guild_id):
        replay = loop_current[guild_id]

    if replay is None and (guild_id not in queues or not queues[guild_id]):
        if guild_id in random_loop_guilds and random_loop_guilds[guild_id] and local_playlist:
            s = prefetch.take_random_pick(guild_id) or random.choice(local_playlist)
            queues.setdefault(guild_id, []).append((s["url"], s["title"]))
        else:
            # 自動推薦接歌：佇列空了、沒有循環/隨機會補歌時，從 YouTube Mix 找相關歌曲
            pick = await _autoplay_pick(guild_id)
            if pick:
                queues.setdefault(guild_id, []).append(pick)
                note_song_meta(guild_id, pick[0], requester=None, source="autoplay")
                log.info(f"[自動推薦] {guild_id} 接下一首：{pick[1]}")
            else:
                if guild_id in now_playing:
                    del now_playing[guild_id]
                is_playing_state[guild_id] = False
                is_paused_state[guild_id] = False
                loop_current.pop(guild_id, None)   # 單曲循環不要在之後加新歌時重播舊的那首
                song_durations.pop(guild_id, None)  # 播完了：面板不要還顯示上一首的長度
                now_playing_url.pop(guild_id, None)
                await _update_panel(guild_id, "播放完畢，佇列已空", view=None, remove=True)
                return

    vc = voice_clients.get(guild_id)
    # 安全網：如果 voice_clients 遺失但 Bot 實際上還在語音頻道中
    if not vc or not vc.is_connected():
        guild = client.get_guild(guild_id)
        if guild and guild.voice_client and guild.voice_client.is_connected():
            vc = guild.voice_client
            voice_clients[guild_id] = vc
            current_voice_channel[guild_id] = {"id": vc.channel.id, "name": vc.channel.name}
        else:
            return

    if replay is not None:
        flat_url, title = replay
    else:
        flat_url, title = queues[guild_id].pop(0)
        # 全部循環：播過的歌接回佇列尾端（新加入、排序、移除都會自然反映，不用舊的備份快照）
        if loop_modes.get(guild_id) == "all":
            queues[guild_id].append((flat_url, title))
    title = clean_str(title)
    loop_current[guild_id] = (flat_url, title)

    source = None
    try:
        last_open_err = None
        for open_try in range(STREAM_OPEN_RETRIES):
            if open_try:
                # 上一輪 FFmpeg 沒吐出音訊（常見：冷啟動分段代理連續 403）→ 丟掉快取、稍等再重解析
                prefetch.forget(guild_id, flat_url)
                if source is not None:
                    try:
                        source.cleanup()
                    except Exception:
                        pass
                    source = None
                log.warning(f"[播放] 開播重試 {open_try + 1}/{STREAM_OPEN_RETRIES}：{flat_url}（{last_open_err}）"[:300])
                await asyncio.sleep(0.8)
            # 預先解析命中就直接用快取；否則（或共用進行中的解析）照常用 yt-dlp 解析
            res = await prefetch.resolve(guild_id, flat_url)
            real_audio_url, duration, thumbnail = res["url"], res.get("duration", 0), res.get("thumbnail", "")
            song_durations[guild_id] = duration or 0
            now_playing_thumbnail[guild_id] = thumbnail

            _drop_pending(guild_id)
            _remember_stream(guild_id, flat_url, real_audio_url)
            remember_duration(flat_url, duration)
            source = discord.FFmpegPCMAudio(
                rangeproxy.input_url(real_audio_url, page=flat_url),   # 長影片避開 YouTube 對「從頭到結尾」請求的限速
                executable=FFMPEG_PATH,
                before_options=audio.before_options(),
                # 音量平衡（dynaudnorm，可每個伺服器開關）+ 開頭 0.25 秒淡入，見 audio.py
                options=audio.ffmpeg_options(guild_id)
            )
            try:
                # 先讓 FFmpeg 吐出第一格再交給播放執行緒（見 audio.PrimedSource）
                source = await audio.prime_source(source)
                last_open_err = None
                break
            except RuntimeError as e:
                last_open_err = e
                if "FFmpeg 沒有輸出音訊" not in str(e) or open_try + 1 >= STREAM_OPEN_RETRIES:
                    raise
                continue
        if last_open_err is not None:
            raise last_open_err
        if voice_clients.get(guild_id) is not vc or not vc.is_connected() or vc.is_playing() or vc.is_paused():
            # 預熱期間被按了停止 / 離開語音 / 別的路徑已開播 → 放棄這次
            source.cleanup()
            return

        vol = volumes.get(guild_id, DEFAULT_VOLUME)
        transformer = discord.PCMVolumeTransformer(source, volume=vol)
        volume_transformers[guild_id] = transformer

        def after_play(e):
            if e: log.error(f"播放錯誤: {e}")
            asyncio.run_coroutine_threadsafe(play_next(guild_id), client.loop)

        kbps = audio.target_bitrate(vc)
        audio.current_bitrate[guild_id] = kbps
        # 用語音頻道的位元率編碼（預設 128k），signal_type=music 讓 Opus 用音樂模式
        vc.play(transformer, after=after_play, bitrate=kbps, signal_type='music')
        now_playing[guild_id] = title
        now_playing_url[guild_id] = flat_url
        song_start_times[guild_id] = time.time()
        is_playing_state[guild_id] = True
        is_paused_state[guild_id] = False
        if guild_id in paused_position:
            del paused_position[guild_id]

        progress_versions[guild_id] = progress_versions.get(guild_id, 0) + 1
        version = progress_versions[guild_id]
        progress_tasks[guild_id] = asyncio.create_task(progress_updater(guild_id, channel, version))
        _start_history_watch(guild_id, version, flat_url, title, thumbnail, duration or 0)
        play_failures.pop(guild_id, None)  # 成功開播，重置失敗計數
        play_notices.pop(guild_id, None)
        autoplay.remember(guild_id, flat_url)  # 記錄播放歷史（自動推薦的種子與去重）
        prefetch.ensure(guild_id)  # 背景預先解析下一首
        notify_change()

        from .views import MusicControlView
        view = MusicControlView(guild_id)
        dur = song_durations[guild_id]
        if dur > 0:
            content = f"▶ 正在播放：**{title}**\n`0:00 / {format_time(dur)}`"
        else:
            content = f"▶ 正在播放：**{title}**"

        # 最終安全網：發送前再次清理 content
        content = clean_str(content)

        async def _send_with_fallback(content, view):
            """發送訊息，若 orjson crash 則降級為純文字（不含 view）"""
            try:
                return await channel.send(content, view=view)
            except TypeError:
                # orjson 無法序列化 → 降級為純文字
                return await channel.send(content)

        if guild_id in control_panels and _panel_age(control_panels[guild_id]) >= PANEL_MAX_AGE:
            if not await _repost_panel(guild_id, channel, content, view):
                try: await control_panels[guild_id].edit(content=content, view=view)
                except Exception: pass
        elif guild_id in control_panels:
            try:
                await control_panels[guild_id].edit(content=content, view=view)
            except:
                msg = await _send_with_fallback(content, view)
                control_panels[guild_id] = msg
        else:
            msg = await _send_with_fallback(content, view)
            control_panels[guild_id] = msg

    except Exception as e:
        # 已開好 FFmpeg 但沒交給播放器（prime / vc.play 失敗）→ 收掉，避免殘留 ffmpeg 行程
        if source is not None and not (vc and (vc.is_playing() or vc.is_paused())):
            try:
                source.cleanup()
            except Exception:
                pass
        log.exception("Queue Error: %s: %s", type(e).__name__, e)
        try:
            from . import alerts
            alerts.note_error(e, f"播放 {title}")
        except Exception:
            pass
        # 如果其實已經在播放了，表示只是顯示更新失敗，不誤報「無法播放」
        if vc and not vc.is_playing() and not vc.is_paused() and ytdl.is_bot_check(e):
            # YouTube 機器人驗證：換下一首也一樣會被擋 → 不計入失敗次數、不自動重試（避免洗版），
            # 把這首放回佇列最前面，設定好 cookies 後按播放即可續播
            prefetch.forget(guild_id, flat_url)
            if replay is None:
                q = queues.setdefault(guild_id, [])
                if loop_modes.get(guild_id) == "all" and q and q[-1][0] == flat_url:
                    q.pop()             # 剛接到尾端的那份拿掉，改放回最前面
                q.insert(0, (flat_url, title))
            loop_current.pop(guild_id, None)
            is_playing_state[guild_id] = False
            is_paused_state[guild_id] = False
            now_playing.pop(guild_id, None)
            msg = ytdl.report_bot_check(f"播放 {title} 時")
            prev = play_notices.get(guild_id)
            play_notices[guild_id] = {"kind": "bot_check", "message": msg, "ts": time.time()}
            if not prev or prev.get("kind") != "bot_check" or time.time() - prev.get("ts", 0) > 600:
                try:
                    await channel.send(f"⚠️ 無法播放 **{title}**：{msg}")
                except Exception:
                    pass
            await _update_panel(guild_id, f"⚠️ 已暫停自動播放：{msg}", view=None)
        elif vc and not vc.is_playing() and not vc.is_paused():
            prefetch.forget(guild_id, flat_url)
            autoplay.mark_failed(guild_id, flat_url)
            fails = play_failures.get(guild_id, 0) + 1
            play_failures[guild_id] = fails
            # 單曲循環時壞掉的歌不要一直重播
            loop_current.pop(guild_id, None)
            try:
                await channel.send(f"無法播放 **{title}**\n```{str(e)[:200]}```")
            except Exception:
                pass
            if fails >= MAX_CONSECUTIVE_FAILURES:
                play_failures.pop(guild_id, None)
                random_loop_guilds[guild_id] = False
                autoplay.set_enabled(guild_id, False)  # 連續失敗也關掉自動推薦，避免無限接歌重試
                is_playing_state[guild_id] = False
                is_paused_state[guild_id] = False
                now_playing.pop(guild_id, None)
                log.error(f"[播放] {guild_id} 連續 {fails} 首失敗，暫停自動播放（可能是 yt-dlp 需要更新）")
                try:
                    from . import alerts
                    alerts.playback_failed(guild_id, fails, e)
                except Exception:
                    pass
                try:
                    await channel.send(f"⚠️ 連續 {fails} 首無法播放，已暫停自動播放（隨機循環與自動推薦已關閉）。可能是 yt-dlp 需要更新（pip install -U yt-dlp）")
                except Exception:
                    pass
            else:
                async def _retry():
                    await asyncio.sleep(2)  # 小延遲，避免狂打 YouTube
                    await play_next(guild_id)
                _keep(_retry())
        else:
            # 歌確實有在播，更新控制面板即可
            from .views import MusicControlView
            view = MusicControlView(guild_id)
            dur = song_durations.get(guild_id, 0)
            if dur > 0:
                content = f"▶ 正在播放：**{title}**\n`0:00 / {format_time(dur)}`"
            else:
                content = f"▶ 正在播放：**{title}**"
            if guild_id in control_panels and _panel_age(control_panels[guild_id]) >= PANEL_MAX_AGE \
                    and await _repost_panel(guild_id, channel, content, view):
                pass
            elif guild_id in control_panels:
                try:
                    await control_panels[guild_id].edit(content=content, view=view)
                except:
                    try:
                        msg = await channel.send(content, view=view)
                        control_panels[guild_id] = msg
                    except:
                        pass
            else:
                try:
                    msg = await channel.send(content, view=view)
                    control_panels[guild_id] = msg
                except:
                    pass


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
    source = await _primed_at(gid, stream_url, pos)
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
    _cleanup_later(old)


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
        src = await _primed_at(gid, stream_url, pend["pos"])
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
_bg_tasks = set()


_long_tasks = set()


def _keep(coro):
    """長時間 / 延遲的背景工作（歷史紀錄監看、失敗重試、睡眠計時觸發）：只保留參考避免被 GC，
    不放進 _bg_tasks（wait_background 只等使用者操作）"""
    t = asyncio.create_task(coro)
    _long_tasks.add(t)
    t.add_done_callback(_long_tasks.discard)
    return t


def _spawn(coro):
    """背景執行（保留參考避免被 GC）；Web API 用它把慢操作移出 HTTP 回應路徑"""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


async def wait_background(timeout=5.0):
    """等目前所有背景操作做完（測試 / 關閉時用）"""
    end = time.monotonic() + timeout
    while _bg_tasks and time.monotonic() < end:
        await asyncio.wait(list(_bg_tasks), timeout=max(0.01, end - time.monotonic()))


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
            orig = prev["orig"] if prev and prev.get("version") == version else paused_position.get(gid, _elapsed(gid))
            _drop_pending(gid)
            pend = {"pos": pos, "orig": orig, "version": version}
            pending_seeks[gid] = pend
            pend["task"] = _spawn(_prepare_pending(gid, pend, page_url))
            paused_position[gid] = pos
            song_start_times.pop(gid, None)
            log.info(f"[跳轉] {gid} 暫停中 → {format_time(pos)}（恢復播放時生效）")
            notify_change()
            return {"status": "ok", "position": pos, "paused": True}
    if not wait:
        gen = _seek_gen[gid] = _seek_gen.get(gid, 0) + 1
        _spawn(_seek_playing(gid, vc, page_url, pos, version, gen))
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
            orig = paused_position.get(gid, _elapsed(gid))
            _drop_pending(gid)
            pending_seeks[gid] = pend = {"pos": pos, "orig": orig, "version": version}
            pend["task"] = _spawn(_prepare_pending(gid, pend, page_url))
            paused_position[gid] = pos
            song_start_times.pop(gid, None)
            notify_change()
            return {"status": "ok", "position": pos, "paused": True}
        try:
            ok = await _restart_at(gid, vc, page_url, pos, version, catch=catch)
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
        pos = paused_position.get(gid, _elapsed(gid)) if paused else _elapsed(gid) + lead
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
                ok = await _restart_at(gid, vc, now_playing_url[gid], pend["pos"], pend["version"], resume=True)
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


def _elapsed(guild_id):
    """目前歌曲實際播放秒數（暫停時停止累計）"""
    if guild_id in song_start_times:
        return time.time() - song_start_times[guild_id]
    return paused_position.get(guild_id, 0)


def _start_history_watch(guild_id, version, url, title, thumbnail, duration):
    meta = song_meta.get(guild_id, {}).get(url) or {}
    _keep(_history_watch(guild_id, version, {
        "url": url, "title": title, "thumbnail": thumbnail, "duration": int(duration or 0),
        "requester": meta.get("requester"), "source": meta.get("source", "user"),
    }))


async def _history_watch(guild_id, version, info, poll=2.0):
    """播放超過 30 秒或一半長度才寫入播放紀錄；中途換歌/停止就不記"""
    need = history.threshold_seconds(info["duration"])
    try:
        while progress_versions.get(guild_id) == version:
            if _elapsed(guild_id) >= need:
                history.record(guild_id, info["url"], info["title"], thumbnail=info["thumbnail"],
                               duration=info["duration"], requester=info["requester"], source=info["source"])
                return True
            await asyncio.sleep(poll)
    except asyncio.CancelledError:
        pass
    return False


async def progress_updater(guild_id, channel, version):
    last = None          # 上次送出的文字：沒變（暫停中 / 直播）就不再 PATCH
    try:
        while True:
            await asyncio.sleep(5)
            if progress_versions.get(guild_id) != version: break
            if guild_id not in now_playing: break
            prefetch.ensure(guild_id)  # 佇列有變動（拖曳/刪除/洗牌…）就改抓新的下一首
            if guild_id in song_start_times:
                elapsed = time.time() - song_start_times[guild_id]
            elif guild_id in paused_position:
                elapsed = paused_position[guild_id]
            else: break
            duration = song_durations.get(guild_id, 0)
            title = now_playing.get(guild_id, "未知")
            if duration > 0:
                prog = min(elapsed / duration, 1.0)
                bar_len = 15
                filled = int(prog * bar_len)
                bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
                is_paused = guild_id not in song_start_times and guild_id in paused_position
                pi = "\u23f8" if is_paused else "\u25b6"
                content = f"{pi} 正在播放：**{title}**\n{bar} `{format_time(elapsed)} / {format_time(duration)}`"
            else:
                content = f"\u25b6 正在播放：**{title}**"
            msg = control_panels.get(guild_id)
            if msg is not None:
                if _panel_age(msg) >= PANEL_MAX_AGE:
                    if await _repost_panel(guild_id, channel, content):
                        last = content
                        continue
                if content == last and msg is control_panels.get(guild_id):
                    continue
                # 只改文字；不傳 view 時 Discord 會保留原本的按鈕，不必每 5 秒重建 View
                try:
                    await msg.edit(content=content)
                    last = content
                except Exception:
                    break
    finally:
        if progress_tasks.get(guild_id) is asyncio.current_task():
            del progress_tasks[guild_id]


async def _force_cleanup_vc(vc):
    """強制拆掉殘留的 VoiceClient（已斷線但 discord.py 還掛在 guild.voice_client 上）"""
    if not vc:
        return
    try:
        await vc.disconnect(force=True)
    except Exception:
        pass
    try:
        vc.cleanup()
    except Exception:
        pass


async def connect_voice(guild, channel, attempts=3):
    """加入 / 切換語音頻道，處理殘留的 VoiceClient 並重試。回傳 VoiceClient，失敗丟出最後的例外。

    重連失敗的原因：停止 / 離開 / 網路斷線後，guild.voice_client 有時還留著一個「已斷線」的 VoiceClient
    （或 voice_clients[gid] 存著舊物件）。舊邏輯看到它存在就對它 move_to()，或直接 channel.connect()
    → discord.py 回「Already connected to a voice channel」或逾時，bot 就再也進不了語音。
    """
    gid = guild.id
    from . import idlesleep
    idlesleep.wake(gid, "加入語音")
    task = idle_leave_tasks.pop(gid, None)      # 剛加入就取消舊的「閒置自動離開」計時，避免它把新連線踢掉
    if task and not task.done():
        task.cancel()
    last = None
    for i in range(attempts):
        vc = guild.voice_client
        try:
            if vc and vc.is_connected():
                if vc.channel and vc.channel.id != channel.id:
                    await vc.move_to(channel)
            else:
                if vc:
                    log.info(f"[語音] {gid} 清掉殘留的語音連線後重新加入")
                    await _force_cleanup_vc(vc)
                stale = voice_clients.pop(gid, None)
                if stale and stale is not vc:
                    await _force_cleanup_vc(stale)
                vc = await channel.connect(timeout=15, reconnect=True)
            voice_clients[gid] = vc
            current_voice_channel[gid] = {"id": channel.id, "name": channel.name}
            notify_change()
            return vc
        except Exception as e:
            last = e
            log.warning(f"[語音] 加入 {channel.name} 失敗（第 {i + 1} 次）：{e!r}")
            await _force_cleanup_vc(guild.voice_client)
            voice_clients.pop(gid, None)
            await asyncio.sleep(1 + i)
    raise last


async def stop_playback(gid, vc=None, guild=None):
    """停止：淡出 → 清空佇列與播放狀態 → 停掉目前歌曲，但「留在語音頻道」"""
    if vc is None and guild is not None:
        vc = guild.voice_client
    if vc is None:
        vc = voice_clients.get(gid)
    if vc and vc.is_connected():
        try:
            await audio.fade_out_stop(gid, vc, stop=False)   # 只淡出；先清狀態再 stop，after_play 才不會接著播
        except Exception:
            pass
    await _clear_guild_state(gid)
    if vc:
        try:
            vc.stop()
        except Exception:
            pass
        if vc.is_connected():
            voice_clients[gid] = vc          # 仍在語音中，保留連線紀錄
    await _update_panel(gid, "⏹ 已停止播放（佇列已清空）", view=None, remove=True)


async def leave_voice(gid, guild):
    """離開語音頻道（/leave）：清狀態 + 強制斷線，確保之後一定能重新加入"""
    try:
        from . import idlesleep
        idlesleep.clear_session(gid)    # 刻意離開：重啟後不自動回去
    except Exception:
        pass
    vc = guild.voice_client or voice_clients.get(gid)
    if vc and vc.is_connected():
        try:
            await audio.fade_out_stop(gid, vc, stop=False)
        except Exception:
            pass
    await _clear_guild_state(gid)
    task = idle_leave_tasks.pop(gid, None)
    if task and not task.done():
        task.cancel()
    if vc:
        try:
            vc.stop()
        except Exception:
            pass
        await _force_cleanup_vc(vc)
    voice_clients.pop(gid, None)
    return bool(vc)


async def reply(interaction, content, **kw):
    """已 defer / 已回應過就改用 followup，避免 InteractionResponded。"""
    kw.setdefault("ephemeral", True)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, **kw)
        else:
            await interaction.response.send_message(content, **kw)
    except Exception as e:
        log.warning(f"[互動] 回覆失敗：{e!r}")


async def get_voice_client(interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await reply(interaction, "你必須先加入語音頻道！")
        return None
    channel = interaction.user.voice.channel
    cur = interaction.guild.voice_client
    # 需要實際連線 / 移動時先 defer：connect_voice 可能花上數十秒，互動 token 3 秒就過期
    if not (cur and cur.is_connected() and cur.channel == channel) and not interaction.response.is_done():
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except Exception as e:
            log.warning(f"[互動] defer 失敗：{e!r}")
    try:
        return await connect_voice(interaction.guild, channel)
    except Exception as e:
        log.error(f"[語音] 無法加入：{e!r}")
        await reply(interaction, f"無法加入語音頻道：{e}")
        return None


async def _process_and_play(interaction, query, gid, vc):
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(ytdl.build_opts(ydl_opts)) as ydl:
            info = await loop.run_in_executor(None, partial(ydl.extract_info, query, download=False))
        if not info:
            await interaction.followup.send("找不到歌曲！", ephemeral=True)
            return
        from .state import queue_room, queue_full_message
        added = full_skipped = 0
        entries = info.get("entries", [info])
        for entry in entries:
            if entry:
                url = entry.get("webpage_url") or entry.get("url")
                if url and not url.startswith("http"):
                    url = f"https://www.youtube.com/watch?v={entry['id']}"
                if url:
                    if queue_room(gid) <= 0:
                        full_skipped += 1
                        continue
                    title = clean_str(entry.get("title", "未知"))[:200]
                    queues[gid].append((url, title))
                    note_song_meta(gid, url, requester=interaction.user.display_name, source="user")
                    added += 1
        if added == 0:
            msg = f"⚠️ {queue_full_message()}" if full_skipped else "無法解析歌曲！"
            await interaction.followup.send(msg, ephemeral=True)
            return
        # 發送到指定頻道
        out_ch = await _get_output_channel(gid)
        if out_ch:
            await out_ch.send(f"**{interaction.user.display_name}** 已將 **{added}** 首歌加入佇列！")
        extra = f"（佇列已滿，另有 {full_skipped} 首沒加入）" if full_skipped else ""
        await interaction.followup.send(f"✅ 已將 **{added}** 首歌加入佇列{extra}", ephemeral=True)
        if not vc.is_playing() and not vc.is_paused():
            await play_next(gid)
    except Exception as e:
        log.error(f"_process Error: {e}")
        await interaction.followup.send(f"發生錯誤：{ytdl.friendly_error(e, '點歌解析')}", ephemeral=True)
