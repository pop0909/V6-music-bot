"""語音連線（加入 / 停止 / 離開）、Discord 互動回覆、點歌解析。從 player.py 拆出，行為不變。

留在 player.py 的函式與測試會替換的名字（player.X）一律經 `_p.X` 在呼叫時取用：
測試 mock.patch.object(player, ...) 照樣生效，也避免循環 import（_p 在檔案最後才 import）。
"""
import asyncio
from functools import partial

import yt_dlp

from .config import log, ydl_opts
from .state import (
    queues, voice_clients, current_voice_channel, note_song_meta, notify_change, idle_leave_tasks,
)
from .utils import clean_str
from . import audio, ytdl


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
    await _p._clear_guild_state(gid)
    if vc:
        try:
            vc.stop()
        except Exception:
            pass
        if vc.is_connected():
            voice_clients[gid] = vc          # 仍在語音中，保留連線紀錄
    await _p._update_panel(gid, "⏹ 已停止播放（佇列已清空）", view=None, remove=True)


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
    await _p._clear_guild_state(gid)
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
        return await _p.connect_voice(interaction.guild, channel)
    except Exception as e:
        log.error(f"[語音] 無法加入：{e!r}")
        await reply(interaction, f"無法加入語音頻道：{e}")
        return None


def _search_entries(info):
    """yt-dlp 文字搜尋的結果 → 只留可以暫存的欄位（扁平結果，沒有會過期的串流網址）；不是搜尋結果回傳 None"""
    if not info or info.get("extractor_key") != "YoutubeSearch":
        return None
    out = []
    for e in ytdl.video_entries(info):
        out.append({"id": e["id"], "url": e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
                    "title": e.get("title") or "未知", "duration": e.get("duration") or 0})
    return out


async def _lookup(query):
    """/play 的查詢：打字搜尋先看暫存（searchcache），貼網址照常每次解析。
    打字搜尋只取第一首「影片」：YouTube 有時把頻道 / 播放清單排在第一筆（例如「tuki.」→ 頻道 tuki.(17)），
    以前用 default_search 的 ytsearch1 會把頻道網址塞進隊列、播不出來。"""
    from . import searchcache
    cacheable = not searchcache.is_url(query)
    if cacheable:
        hit = searchcache.get("play1v", query)
        if hit:
            log.info(f"[搜尋暫存] 命中 /play：{query[:80]}")
            return {"entries": hit}
    loop = asyncio.get_running_loop()
    q = f"ytsearch3:{query}" if cacheable else query
    with yt_dlp.YoutubeDL(ytdl.build_opts(ydl_opts)) as ydl:
        info = await loop.run_in_executor(None, partial(ydl.extract_info, q, download=False))
    if not cacheable:
        return info
    entries = _search_entries(info)
    if entries is None:          # 不是 YouTube 搜尋結果（不會發生，保險）：照原樣
        return info
    entries = entries[:1]
    searchcache.put("play1v", query, entries)
    return {"entries": entries} if entries else None


async def _process_and_play(interaction, query, gid, vc):
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        info = await _lookup(query)
        if not info:
            await interaction.followup.send("找不到歌曲！", ephemeral=True)
            return
        from .state import queue_room, queue_full_message
        added = full_skipped = 0
        added_songs = []
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
                    added_songs.append((url, title))
                    added += 1
        if added == 0:
            msg = f"⚠️ {queue_full_message()}" if full_skipped else "無法解析歌曲！"
            await interaction.followup.send(msg, ephemeral=True)
            return
        from . import actionlog       # 操作紀錄：一首記歌名；YouTube 播放清單只記一筆「N 首」
        actionlog.record_add(gid, actionlog.discord_actor(interaction), added_songs,
                             label=(f"清單：{clean_str(info.get('title') or '')[:80]}" if len(added_songs) > 1 and info.get("title") else None))
        # 發送到指定頻道
        out_ch = await _p._get_output_channel(gid)
        if out_ch:
            await out_ch.send(f"**{interaction.user.display_name}** 已將 **{added}** 首歌加入佇列！")
        extra = f"（佇列已滿，另有 {full_skipped} 首沒加入）" if full_skipped else ""
        await interaction.followup.send(f"✅ 已將 **{added}** 首歌加入佇列{extra}", ephemeral=True)
        if not vc.is_playing() and not vc.is_paused():
            await _p.play_next(gid)
    except Exception as e:
        log.error(f"_process Error: {e}")
        await interaction.followup.send(f"發生錯誤：{ytdl.friendly_error(e, '點歌解析')}", ephemeral=True)


from . import player as _p  # noqa: E402  放最後：player 載入完才取用（見上方說明）
