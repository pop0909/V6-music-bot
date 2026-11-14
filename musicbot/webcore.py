"""Web 控制輔助函數 + Flask API + SSE"""
from .config import DEFAULT_VOLUME
import asyncio
import inspect
import time
import math

import discord

from . import state as _state  # active_guild_id 要透過模組屬性讀寫，不能 from-import（會變成複本）
from .bot import client
from .config import log
from .state import (
    state_lock, queues, now_playing, voice_clients, volumes,
    volume_transformers, control_panels, now_playing_url,
    now_playing_thumbnail, song_start_times, song_durations, paused_position,
    current_voice_channel, guild_names,
    is_playing_state, is_paused_state, loop_modes,
    saved_queues, local_playlist, random_loop_guilds,
)
from .storage import load_favorites
from . import autoplay as _autoplay
from . import prefetch as _prefetch
from . import audio as _audio
from .state import manual_advance, song_meta, queue_room, queue_max, queue_full_message
from .state import play_notices, note_song_meta, known_durations, remember_duration, notify_change
from .state import pending_seeks as _pending_seeks, sleep_timers
from .utils import format_time, valid_song_url, safe_title
from .player import _update_panel, play_next, resume_playback, _spawn  # noqa: F401  _update_panel：測試會替換
from . import actionlog


# ================== 從 Web UI 控制 Discord 的輔助函數 ==================

def _vc_json(vc):
    """語音頻道資訊給前端用：id 一律轉字串（大整數在 JS 會失真）"""
    if not vc:
        return vc
    d = dict(vc)
    if d.get("id") is not None:
        d["id"] = str(d["id"])
    return d

def run_async(coro):
    """從任何執行緒安全地排程 Discord 協程"""
    def _close():
        if inspect.iscoroutine(coro):
            coro.close()
    if not client.loop or client.loop.is_closed():
        _close()
        log.warning("run_async: Discord 事件迴圈尚未就緒，已取消操作")
        return None
    try:
        future = asyncio.run_coroutine_threadsafe(coro, client.loop)
        return future.result(timeout=15)
    except Exception as e:
        log.error(f"run_async error: {e}")
        return None


async def ensure_voice_connected(gid):
    """確保 bot 在語音頻道中（vc 紀錄遺失就重新註冊；已離開就用記憶的頻道重連，會清掉殘留連線並重試）"""
    from .player import connect_voice
    guild = client.get_guild(gid)
    if not guild:
        return None
    vc = guild.voice_client
    if vc and vc.is_connected():
        voice_clients[gid] = vc
        return vc
    saved_ch = current_voice_channel.get(gid)
    if not saved_ch or not saved_ch.get("id"):
        return None
    ch = guild.get_channel(saved_ch["id"])
    if not ch or not isinstance(ch, discord.VoiceChannel):
        return None
    try:
        log.info(f"[自動重連] bot 不在語音，自動加入 {ch.name}")
        return await connect_voice(guild, ch)
    except Exception as e:
        log.error(f"自動重連失敗: {e!r}")
        return None


async def web_add_to_queue(gid, url, title, auto_play=True, duration=None, requester="網頁", actor=None):
    """從 Web 加入歌曲到 Discord 佇列

    auto_play=False 時不自動播放（讓 Web UI 使用者手動按播放）
    回傳 dict: {"ok": bool, "duplicate": bool, "position": int|None, "starts_now": bool}
    position = 加入後在佇列中的位置（1 = 下一首）；starts_now = 佇列原本閒置，馬上開播
    """
    url = (url or "").strip() if isinstance(url, str) else ""
    if not valid_song_url(url):
        return {"ok": False, "invalid": True, "message": "網址格式不正確（需要 http/https 連結）"}
    if gid not in queues: queues[gid] = []
    title = safe_title(title, url)
    # 去重檢查：同一首歌已在佇列（或正在播放）就不重複加入
    for i, (u, t) in enumerate(queues[gid]):
        if u == url:
            return {"ok": False, "duplicate": True, "position": i + 1,
                    "message": f"這首歌已在佇列第 {i + 1} 位"}
    if now_playing.get(gid) and now_playing_url.get(gid) == url:
        return {"ok": False, "duplicate": True, "position": 0,
                "message": "這首歌正在播放中"}
    if queue_room(gid) <= 0:
        return {"ok": False, "full": True, "message": queue_full_message()}
    queues[gid].append((url, title))
    position = len(queues[gid])
    remember_duration(url, duration)
    note_song_meta(gid, url, requester=requester, source="user")
    actionlog.record(gid, "add", actor, title, url=url)     # 操作紀錄（actor=None 不記）
    if gid in random_loop_guilds: random_loop_guilds[gid] = False
    if auto_play:
        # 確保 bot 在語音中（不在的話自動重連上次的頻道）
        vc = await ensure_voice_connected(gid)
        if vc and not vc.is_playing() and not vc.is_paused():
            channel = None
            if gid in control_panels:
                channel = control_panels[gid].channel
            if not channel:
                guild = client.get_guild(gid)
                if guild:
                    for c in guild.text_channels:
                        channel = c
                        break
            if channel:
                # 不 await：yt-dlp 解析可能超過 run_async 的 15 秒 timeout，
                # 會讓 API 誤報「加入失敗」；改成背景開播，狀態由 SSE 推送
                _spawn(play_next(gid, channel))
                return {"ok": True, "duplicate": False, "position": position, "starts_now": position == 1}
            else:
                return {"ok": True, "duplicate": False, "position": position, "starts_now": False,
                        "message": "已加入佇列（bot 不在可通知的文字頻道）"}
    return {"ok": True, "duplicate": False, "position": position, "starts_now": False}


async def web_toggle_play(gid):
    """從 Web 切換播放/暫停"""
    vc = await ensure_voice_connected(gid)
    if not vc: return {"status": "no_voice", "message": "bot 不在語音頻道且無法自動重連"}
    if vc.is_paused():
        if _pending_seeks.get(gid):
            # 暫停中跳轉過：新位置的 FFmpeg 可能還在預熱 → 在背景等它好了再換上並恢復，HTTP 先回應
            is_playing_state[gid] = True
            is_paused_state[gid] = False
            _spawn(resume_playback(gid, vc))
            return {"status": "resumed", "title": now_playing.get(gid, ""), "accepted": True}
        await resume_playback(gid, vc)
        return {"status": "resumed", "title": now_playing.get(gid, "")}
    elif vc.is_playing():
        if gid in song_start_times:
            paused_position[gid] = time.time() - song_start_times[gid]
            del song_start_times[gid]
        vc.pause()
        is_playing_state[gid] = False
        is_paused_state[gid] = True
        return {"status": "paused", "title": now_playing.get(gid, "")}
    elif queues.get(gid):
        guild = client.get_guild(gid)
        channel = None
        if gid in control_panels:
            channel = control_panels[gid].channel
        if not channel and guild:
            for c in guild.text_channels:
                channel = c
                break
        if channel:
            _spawn(play_next(gid, channel))
        return {"status": "playing", "title": queues[gid][0][1] if queues.get(gid) else ""}
    return {"status": "error", "message": "佇列為空"}


async def web_skip(gid, actor=None):
    """從 Web 跳過歌曲：淡出（0.12 秒）+ 停止在背景執行，立刻回應（accepted）。
    停止後 after_play → play_next 照常接下一首；下一首通常已被預先解析。"""
    vc = await ensure_voice_connected(gid)
    if vc:
        if vc.is_playing() or vc.is_paused():
            actionlog.record(gid, "skip", actor, now_playing.get(gid, ""), url=now_playing_url.get(gid))
            _spawn(_audio.fade_out_stop(gid, vc))
        elif queues.get(gid):
            manual_advance[gid] = True      # 閒置但佇列有歌：跳過 = 直接播下一首
            _spawn(play_next(gid))
        nxt = queues.get(gid) or []
        return {"status": "skipped", "accepted": True, "next": nxt[0][1] if nxt else None}
    return {"status": "no_voice"}


async def web_stop(gid):
    """從 Web 停止：清空佇列並停掉目前歌曲，bot 留在語音頻道（背景執行，立刻回應）"""
    from .player import stop_playback
    guild = client.get_guild(gid)
    is_playing_state[gid] = False
    is_paused_state[gid] = False
    _spawn(stop_playback(gid, guild=guild))
    return {"status": "stopped", "accepted": True}


async def web_set_volume(gid, level):
    """從 Web 設定音量"""
    vol = max(0, min(200, level)) / 100.0
    volumes[gid] = vol
    if gid in volume_transformers:
        volume_transformers[gid].volume = vol
    return {"status": "ok", "volume": level}


async def web_set_shuffle(gid, enabled):
    """從 Web 切換隨機循環"""
    random_loop_guilds[gid] = enabled
    _prefetch.ensure(gid)  # 下一首可能變了 → 重新瞄準預先解析
    return {"status": "ok", "shuffle_mode": enabled}


async def web_set_loop(gid, mode):
    """從 Web 設定循環模式: none, one, all"""
    if mode not in ("none", "one", "all"):
        return {"status": "error", "message": "無效模式"}
    from .state import loop_current
    with state_lock:
        prev = loop_modes.get(gid, "none")
        loop_modes[gid] = mode
        saved_queues.pop(gid, None)
        q = queues.setdefault(gid, [])
        cur = loop_current.get(gid) if now_playing_url.get(gid) else None
        if mode == "all" and prev != "all" and cur and not (q and q[-1][0] == cur[0]):
            q.append(cur)          # 開啟全部循環時，目前這首也算在循環裡
        elif prev == "all" and mode != "all" and cur and q and q[-1][0] == cur[0]:
            q.pop()                # 關掉時拿掉剛接到尾端的目前歌曲
    _prefetch.ensure(gid)  # 下一首可能變了 → 重新瞄準預先解析
    return {"status": "ok", "loop_mode": mode}


def _applies_now(gid):
    """播放中換濾鏡會在什麼時候生效：now / paused / live（直播不能跳轉）/ next（沒在播）"""
    vc = voice_clients.get(gid)
    if vc and now_playing.get(gid) and (vc.is_playing() or vc.is_paused()):
        return "paused" if vc.is_paused() else ("live" if (song_durations.get(gid, 0) or 0) <= 0 else "now")
    return "next"


async def web_set_normalize(gid, enabled):
    """從 Web 切換音量平衡：播放中從目前位置立即生效（暫停中繼續時、直播下一首）"""
    from .player import reapply_audio
    prev = _audio.normalize_enabled(gid)
    now = _audio.set_normalize(gid, enabled)
    applies = _applies_now(gid)
    if now != prev:
        log.info(f"[音量平衡] {gid} → {'開' if now else '關'}")
        notify_change()
        if voice_clients.get(gid) and applies != "live":
            _spawn(reapply_audio(gid))
    return {"status": "ok", "normalize": now, "applies": applies}


async def web_join_voice(gid, channel_id):
    """從 Web 加入指定的語音頻道（若已在某頻道則切換過去）"""
    import discord
    guild = client.get_guild(gid)
    if not guild:
        return {"status": "error", "message": "找不到伺服器"}
    channel = guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.VoiceChannel):
        return {"status": "error", "message": "找不到語音頻道"}
    try:
        from .player import connect_voice
        await connect_voice(guild, channel)
        _set_active_guild(gid)
        return {"status": "joined", "channel_name": channel.name}
    except Exception as e:
        log.error(f"Join voice error: {e}")
        return {"status": "error", "message": str(e)}


async def web_remove_queue(gid, index, actor=None):
    """從 Web 移除佇列歌曲"""
    if gid in queues and 0 <= index < len(queues[gid]):
        url, title = queues[gid].pop(index)
        actionlog.record(gid, "remove", actor, title, url=url)
    _prefetch.ensure(gid)  # 下一首可能變了 → 重新瞄準預先解析
    return {"status": "ok"}


async def web_reorder_queue(gid, frm, to):
    """從 Web 重新排序佇列"""
    if gid in queues and 0 <= frm < len(queues[gid]) and 0 <= to < len(queues[gid]):
        song = queues[gid].pop(frm)
        queues[gid].insert(to, song)
    _prefetch.ensure(gid)  # 下一首可能變了 → 重新瞄準預先解析
    return {"status": "ok"}


async def web_play_index(gid, index, actor=None):
    """從 Web 播放指定索引（將目標歌曲移到佇列最前，靠 after_play 驅動 play_next）"""
    if gid not in queues or index < 0 or index >= len(queues[gid]):
        return {"status": "error", "message": "無效索引"}

    # 將目標歌曲移到佇列最前面
    song = queues[gid].pop(index)
    queues[gid].insert(0, song)

    vc = await ensure_voice_connected(gid)
    if vc:
        _prefetch.ensure(gid)   # 目標歌曲立刻開始解析（與淡出同時進行）
        if vc.is_playing() or vc.is_paused():
            # 操作紀錄：目前這首被跳過、改播佇列裡的另一首
            actionlog.record(gid, "skip", actor, now_playing.get(gid, ""), url=now_playing_url.get(gid),
                             detail=f"改播：{song[1]}")
            # 淡出 + 停止在背景；after_play 會自動 call play_next，從位置 0 播放
            _spawn(_audio.fade_out_stop(gid, vc))
        else:
            # 閒置在語音中（例如機器人驗證後暫停自動播放）：沒有 after_play 可觸發 → 直接開播
            manual_advance[gid] = True
            _spawn(play_next(gid))
        return {"status": "playing", "title": song[1], "accepted": True}

    # 無法連上語音：把歌塞回原位並回報
    queues[gid].insert(index, song)
    return {"status": "error", "message": "bot 不在語音頻道且無法自動重連"}


async def web_add_to_queue_batch(gid, songs, stats=None, actor=None, label=None):
    """批次加入多首歌曲（單次 run_async 呼叫，不自動播放）。
    佇列上限：加到滿為止，stats["full_skipped"] = 因為佇列滿而沒加入的首數"""
    if gid not in queues: queues[gid] = []
    count = skipped = 0
    added = []
    for song in songs:
        if not isinstance(song, dict):
            continue
        url = (song.get("url") or "").strip() if isinstance(song.get("url"), str) else ""
        title = safe_title(song.get("title"), url)
        if valid_song_url(url):
            if queue_room(gid) <= 0:
                skipped += 1
                continue
            queues[gid].append((url, title))
            remember_duration(url, song.get("duration"))
            note_song_meta(gid, url, requester="網頁", source="user")
            added.append((url, title))
            count += 1
    actionlog.record_add(gid, actor, added, label)     # 多首只記一筆
    if stats is not None:
        stats["full_skipped"] = skipped
    if gid in random_loop_guilds: random_loop_guilds[gid] = False
    _prefetch.ensure(gid)  # 下一首可能變了 → 重新瞄準預先解析
    return count


async def web_set_autoplay(gid, enabled):
    """從 Web 切換自動推薦接歌；開啟時若閒置在語音中且佇列空，立刻開始接歌"""
    enabled = _autoplay.set_enabled(gid, enabled)
    if enabled and not queues.get(gid):
        guild = client.get_guild(gid)
        vc = guild.voice_client if guild else None
        if vc and vc.is_connected() and not vc.is_playing() and not vc.is_paused():
            _spawn(play_next(gid))
    return {"status": "ok", "autoplay": enabled}


def get_active_guild():
    return _state.active_guild_id


async def web_play_all(gid, songs, stats=None, actor=None, label=None):
    """清空佇列、一次加入整份歌單（依網址去重，最多 QUEUE_MAX 首），閒置時開播。取代逐首 run_async。"""
    seen = set()
    new_q = []
    for s in songs:
        if not isinstance(s, dict):
            continue
        url = (s.get("url") or "").strip() if isinstance(s.get("url"), str) else ""
        if valid_song_url(url) and url not in seen:
            seen.add(url)
            new_q.append((url, safe_title(s.get("title"), url)))
    cap = queue_max()
    if stats is not None:
        stats["full_skipped"] = max(0, len(new_q) - cap)
    new_q = new_q[:cap]
    queues[gid] = new_q
    actionlog.record_add(gid, actor, new_q, label)     # 整份歌單：只記一筆
    if gid in random_loop_guilds:
        random_loop_guilds[gid] = False
    vc = await ensure_voice_connected(gid)
    if vc and new_q and not vc.is_playing() and not vc.is_paused():
        channel = control_panels[gid].channel if gid in control_panels else None
        _spawn(play_next(gid, channel))
    return len(new_q)


def _set_active_guild(gid):
    """設定當前活躍的伺服器，所有 Web 使用者會自動跟隨
    傳入 None 可清除活躍狀態
    """
    with state_lock:
        if _state.active_guild_id != gid:
            _state.active_guild_id = gid
            if gid is not None:
                log.info(f"[Active Guild] 已切換至 {guild_names.get(gid, gid)}")
            else:
                log.info("[Active Guild] 已清除")


def get_guild_list():
    """取得伺服器列表（不讀取 VoiceClient）"""
    result = []
    with state_lock:
        for gid, name in guild_names.items():
            is_active = gid in voice_clients  # 只檢查是否有連線記錄
            result.append({
                "id": str(gid),
                "name": name,
                "active": is_active,
                "now_playing": now_playing.get(gid, ""),
                "queue_length": len(queues.get(gid, []))
            })
    return result


def get_guild_status(gid):
    """取得指定伺服器的播放狀態（只讀取純 Python 狀態，不碰 VoiceClient）"""
    gid = int(gid)
    with state_lock:
        is_play = is_playing_state.get(gid, False)
        is_paus = is_paused_state.get(gid, False)
        _g = client.get_guild(gid)
        _vc = (_g.voice_client if _g else None) or voice_clients.get(gid)
        is_connected = bool(_vc and _vc.is_connected())   # 以實際連線為準（不看殘留的 voice_clients 紀錄）

        if is_play and not is_paus and gid in song_start_times:
            elapsed = time.time() - song_start_times[gid]
        elif is_paus and gid in paused_position:
            elapsed = paused_position[gid]
        else:
            elapsed = 0

        queue_list = []
        if gid in queues:
            for i, (u, t) in enumerate(queues[gid]):
                # duration：已知長度（0 = 未知），網頁佇列標頭算總長度用
                item = {"index": i, "url": u, "title": t, "duration": known_durations.get(u, 0)}
                who = (song_meta.get(gid) or {}).get(u, {}).get("requester")
                if who and who != "網頁":
                    item["by"] = who        # 誰點的（擴充功能成員 / Discord 使用者）
                queue_list.append(item)

        return {
            "is_connected": is_connected,
            "is_playing": is_play,
            "is_paused": is_paus,
            "current_title": now_playing.get(gid, ""),
            "current_url": now_playing_url.get(gid, ""),
            "thumbnail": now_playing_thumbnail.get(gid, ""),
            "queue": queue_list,
            "queue_length": len(queues.get(gid, [])),
            "elapsed": elapsed,
            "duration": song_durations.get(gid, 0),
            "elapsed_str": format_time(elapsed),
            "duration_str": format_time(song_durations.get(gid, 0)),
            "volume": round(volumes.get(gid, DEFAULT_VOLUME) * 100),
            "shuffle_mode": random_loop_guilds.get(gid, False),
            "loop_mode": loop_modes.get(gid, "none"),
            "autoplay": _autoplay.is_enabled(gid),
            "normalize": _audio.normalize_enabled(gid),
            "bitrate": _audio.current_bitrate.get(gid) if is_connected else None,
            "notice": play_notices.get(gid),
            "guild_name": guild_names.get(gid, f"伺服器 {gid}"),
            # 只有真的在語音中才回報頻道；斷線後回 None，前端選單才會回到預設、能再選同一個頻道
            # 頻道 id 轉字串：Discord id 超過 JS 安全整數（2^53），用數字傳到前端會被四捨五入，對不上選單
            "voice_channel": _vc_json(current_voice_channel.get(gid)) if is_connected else None,
            "last_voice_channel": _vc_json(current_voice_channel.get(gid)),
            "active_guild_id": str(_state.active_guild_id) if _state.active_guild_id is not None else None,
        }


def get_full_app_state(gid):
    """
    統一的全狀態函式 — 一次打包所有前端需要的資料。
    取代前端所有輪詢，透過 SSE 每秒推送。
    """
    gid = int(gid)
    state_data = get_guild_status(gid)
    # 加入最愛清單（讓前端可以直接快取）
    with state_lock:
        state_data["favorites"] = load_favorites(gid)
        state_data["favorites_count"] = len(state_data["favorites"])
        state_data["playlist"] = local_playlist.copy()
        state_data["playlist_count"] = len(local_playlist)
        state_data["sleep"] = _sleep_info(gid)
    from . import idlesleep
    state_data["asleep"] = idlesleep.is_asleep(gid)
    state_data["restore"] = idlesleep.saved_info(gid)
    from . import userlists
    state_data["ul_rev"] = userlists.rev()
    state_data["act_rev"] = actionlog.rev()        # 操作紀錄有新的 → 正在看的面板重新抓
    return state_data


def _sleep_info(gid):
    """睡眠計時器給前端顯示：剩餘分鐘無條件進位（每分鐘才變一次，SSE 不會因此每秒重推）"""
    info = sleep_timers.get(gid)
    if not info:
        return None
    if info.get("mode") == "minutes" and info.get("deadline"):
        return {"mode": "minutes", "minutes": max(1, math.ceil((info["deadline"] - time.time()) / 60))}
    return {"mode": info.get("mode")}


async def web_import_songs(gid, songs, requester="網頁", stats=None, actor=None, label=None):
    """匯入歌單：附加到佇列尾端（依網址去重），閒置在語音中就開播。回傳加入數量。
    佇列上限：加到滿為止，stats["full_skipped"] = 因為佇列滿而沒加入的首數"""
    q = queues.setdefault(gid, [])
    existing = {u for u, _ in q}
    if now_playing_url.get(gid):
        existing.add(now_playing_url[gid])      # 正在播的那首不算「不在佇列」：連點播放歌單不會又把它加回去
    count = skipped = 0
    added = []
    for s in songs:
        if not isinstance(s, dict) or not isinstance(s.get("url"), str):
            continue
        url = (s.get("url") or "").strip()
        if not url or url in existing:
            continue
        existing.add(url)
        if not valid_song_url(url):
            continue
        if queue_room(gid) <= 0:
            skipped += 1
            continue
        q.append((url, safe_title(s.get("title"), url)))
        remember_duration(url, s.get("duration"))
        note_song_meta(gid, url, requester=requester, source="user")
        added.append(q[-1])
        count += 1
    actionlog.record_add(gid, actor, added, label)     # 匯入 / 整份歌單：只記一筆
    if stats is not None:
        stats["full_skipped"] = skipped
    if gid in random_loop_guilds:
        random_loop_guilds[gid] = False
    _prefetch.ensure(gid)
    vc = await ensure_voice_connected(gid)
    if vc and count and not vc.is_playing() and not vc.is_paused():
        channel = control_panels[gid].channel if gid in control_panels else None
        _spawn(play_next(gid, channel))
    notify_change()
    return count
