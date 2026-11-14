"""播放引擎核心：play_next、進度更新、狀態清理、控制面板。

拆出去的部分（名字照舊可從 player 取用 / 替換）：
- player_seek.py：跳轉、暫停中跳轉預熱、播放中換濾鏡、恢復播放
- player_voice.py：語音連線 / 停止 / 離開、互動回覆、點歌解析
- player_sleep.py：睡眠計時器
"""
from .config import DEFAULT_VOLUME
import asyncio
import random
import time

import discord
import yt_dlp  # noqa: F401  測試用 player.yt_dlp 替換解析

from .bot import client
from . import rangeproxy
from .config import OUTPUT_CHANNEL_ID, log
from .state import (
    queues, now_playing, voice_clients, random_loop_guilds, volumes, volume_transformers,
    control_panels, now_playing_url, now_playing_thumbnail, song_start_times, song_durations,
    paused_position, progress_versions, progress_tasks, current_voice_channel, is_playing_state,
    is_paused_state, loop_modes, saved_queues, local_playlist, play_failures, play_locks,
    play_notices, song_meta, note_song_meta, now_playing_stream, remember_duration, notify_change,
    loop_current, manual_advance,
)

# 連續失敗幾次就停止自動接下一首（避免 YouTube 403 時無限重試洗頻）
MAX_CONSECUTIVE_FAILURES = 3
# 開播時 FFmpeg 拿不到第一格（串流網址冷啟動 403）→ 清快取重解析再開一次，通常就好
STREAM_OPEN_RETRIES = 2
from .utils import clean_str, format_time
from . import autoplay
from . import prefetch
from . import audio, history, ytdl, prewarm

# 分段代理遇到 403/410（串流網址失效）時，用 yt-dlp 重新解析同一首歌的網址續傳（在代理執行緒裡同步跑）
rangeproxy.set_resolver(lambda page: prefetch.extract_stream(page)["url"])


async def _get_output_channel(guild_id):
    """取得指定的 Discord 輸出頻道"""
    guild = client.get_guild(guild_id)
    if not guild:
        return None
    ch = guild.get_channel(OUTPUT_CHANNEL_ID)
    return ch if isinstance(ch, discord.TextChannel) else None


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
    prewarm.discard(gid)  # 收掉預熱好的下一首
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
        prewarm.discard(guild_id)
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
                prewarm.discard(guild_id)
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
            # 上一首結尾前已預熱好這首（FFmpeg 已讀到第一格）→ 直接用，換歌不用再等 FFmpeg 開啟
            warm = prewarm.take(guild_id, flat_url) if open_try == 0 else None
            # 預先解析命中就直接用快取；否則（或共用進行中的解析）照常用 yt-dlp 解析
            res = warm[1] if warm else await prefetch.resolve(guild_id, flat_url)
            real_audio_url, duration, thumbnail = res["url"], res.get("duration", 0), res.get("thumbnail", "")
            song_durations[guild_id] = duration or 0
            now_playing_thumbnail[guild_id] = thumbnail

            _drop_pending(guild_id)
            _remember_stream(guild_id, flat_url, real_audio_url)
            remember_duration(flat_url, duration)
            if warm:
                source = warm[0]
                last_open_err = None
                break
            # 長影片經分段代理避開 YouTube 對「從頭到結尾」請求的限速；音量平衡 + 開頭 0.25 秒淡入，見 audio.py
            source = prewarm.open_source(guild_id, flat_url, real_audio_url)
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
            prewarm.mark_end(guild_id)     # 量換歌間隔：上一首結束的時間
            asyncio.run_coroutine_threadsafe(play_next(guild_id), client.loop)

        kbps = audio.target_bitrate(vc)
        audio.current_bitrate[guild_id] = kbps
        # 用語音頻道的位元率編碼（預設 128k），signal_type=music 讓 Opus 用音樂模式
        vc.play(transformer, after=after_play, bitrate=kbps, signal_type='music')
        now_playing[guild_id] = title
        now_playing_url[guild_id] = flat_url
        song_start_times[guild_id] = time.time()
        prewarm.note_start(guild_id, bool(warm), advance)   # 日誌記一行實際換歌間隔
        is_playing_state[guild_id] = True
        is_paused_state[guild_id] = False
        if guild_id in paused_position:
            del paused_position[guild_id]

        progress_versions[guild_id] = progress_versions.get(guild_id, 0) + 1
        version = progress_versions[guild_id]
        progress_tasks[guild_id] = asyncio.create_task(progress_updater(guild_id, channel, version))
        _start_history_watch(guild_id, version, flat_url, title, thumbnail, duration or 0)
        prewarm.start(guild_id, version)   # 快播完時預熱下一首的 FFmpeg
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


# 拆出去的模組（行為不變）；名字照舊可從 player 取用 / 替換
from .player_sleep import (  # noqa: E402,F401
    _sleep_timer_fire, cancel_sleep_timer, check_track_end_sleep, set_sleep_timer,
)
from .player_seek import (  # noqa: E402,F401
    REAPPLY_LEAD_MAX, REAPPLY_LEAD_MIN, SEEK_HTTP, _cleanup_later,
    _current_vc, _discard_prepared, _drop_pending, _prepare_pending,
    _primed_at, _reapply_gen, _remember_stream, _restart_at,
    _seek_err, _seek_gen, _seek_locks, _seek_playing,
    _stream_url_for, _swap_source, parse_seek_position, reapply_audio,
    reapply_stats, resume_playback, seek,
)
from .player_voice import (  # noqa: E402,F401
    _force_cleanup_vc, _process_and_play, connect_voice, get_voice_client,
    leave_voice, reply, stop_playback,
)
