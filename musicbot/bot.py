"""Discord client 與事件處理"""
import asyncio

import discord
from discord import app_commands

from .config import DISCORD_TOKEN, SYNC_GUILD_ID, log
from .state import (
    state_lock, guild_names, idle_leave_tasks, progress_tasks,
    voice_clients, current_voice_channel, is_playing_state, is_paused_state,
    now_playing,
)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

# 歌名來自 YouTube / 網頁 / 擴充功能成員，可能夾帶 @everyone、@here 或身分組提及 → 一律不觸發提及
client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
tree = app_commands.CommandTree(client)
_synced_once = False


@client.event
async def on_ready():
    log.info(f"[Discord] 已登入：{client.user}")
    # 延遲 import 避免循環依賴（storage 不依賴 bot，但 on_ready 用到）
    from .storage import load_local_playlist
    load_local_playlist()

    with state_lock:
        for g in client.guilds:
            guild_names[g.id] = g.name

    from . import sysinfo, idlesleep
    sysinfo.start_presence()
    idlesleep.start()

    global _synced_once
    if _synced_once:
        # on_ready 在斷線重連後會再次觸發；指令同步只需做一次，避免撞 rate limit
        return
    _synced_once = True
    try:  # 持久化面板按鈕：重啟前發出的面板仍可按
        from .views import MusicControlView
        client.add_view(MusicControlView(None))
    except Exception as e:
        log.warning(f"[面板] 註冊持久化按鈕失敗：{e!r}")
    from . import history
    history.preload_durations()   # 佇列總長度用（背景執行緒，不阻塞；只在第一次 on_ready）
    await _sync_commands()
    try:
        from . import idlesleep
        await idlesleep.resume_after_restart()
    except Exception as e:
        log.warning(f"[語音續播] {e!r}")
    from . import alerts
    alerts.startup_notice()
    from . import ytdl
    ytdl.warn_cookie_quality()
    from . import backup
    backup.start(client)

    log.info(f"[Discord] 已在 {len(client.guilds)} 個伺服器中")


async def _sync_commands():
    """啟動時同步 slash 指令。
    - bot 在 SYNC_GUILD_ID 伺服器且有權限：照舊同步到該伺服器（立即生效）
    - 不在該伺服器 / 沒有 applications.commands 權限（403 / 50001 Missing Access）：不噴 ERROR，
      改為：已有全域指令就沿用（不重複註冊，避免同一個指令出現兩次）；完全沒有全域指令才同步一次全域"""
    gid = SYNC_GUILD_ID
    reason = None
    if not gid:
        reason = "未設定 SYNC_GUILD_ID"
    elif client.get_guild(gid) is None:
        reason = f"bot 不在 SYNC_GUILD_ID 指定的伺服器（{gid}）"
    else:
        try:
            guild = discord.Object(id=gid)
            tree.clear_commands(guild=guild)
            await asyncio.sleep(1.5)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            log.info(f"[Discord] 已同步 {len(synced)} 個指令到測試伺服器")
            # 指令已經以伺服器指令註冊；Discord 上殘留的全域指令（舊版或其他專案留下的 /voice、舊 /stop…）
            # 會讓同名指令出現兩次 → 清掉全域指令（只有真的有殘留時才呼叫，避免多打 API）
            try:
                if await tree.fetch_commands():
                    tree.clear_commands(guild=None)
                    await tree.sync()
                    log.info("[Discord] 已清除殘留的全域指令（避免重複）")
            except Exception as e:
                log.warning(f"[Discord] 清除全域指令失敗：{e}")
            return
        except discord.Forbidden:
            tree.clear_commands(guild=discord.Object(id=gid))
            reason = f"bot 在伺服器 {gid} 沒有 applications.commands 權限（403 / 50001 Missing Access）"
        except Exception as e:
            log.warning(f"[Discord] 測試伺服器指令同步失敗：{e}（不影響播放與網頁面板）")
            return
    try:
        existing = await tree.fetch_commands()
        if existing:
            log.info(f"[Discord] {reason}，略過測試伺服器同步；沿用已註冊的 {len(existing)} 個全域指令"
                     f"（要指定伺服器可在 .env 設 SYNC_GUILD_ID）")
        else:
            synced = await tree.sync()
            log.info(f"[Discord] {reason}，已改為同步 {len(synced)} 個全域指令（全部伺服器可用，Discord 端可能需數分鐘生效）")
    except Exception as e:
        log.warning(f"[Discord] {reason}；全域指令同步也失敗：{e}（不影響播放與網頁面板）")


@client.event
async def on_guild_join(guild):
    with state_lock:
        guild_names[guild.id] = guild.name


@client.event
async def on_voice_state_update(member, before, after):
    """語音頻道只剩 bot 自己時，等 5 分鐘仍無人則自動離開"""
    if member.bot:
        return
    vc = member.guild.voice_client
    if not vc or not vc.is_connected() or not vc.channel:
        return
    channel = vc.channel
    # 只在事件與 bot 所在頻道相關時觸發檢查
    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None
    if before_id != channel.id and after_id != channel.id:
        return
    gid = member.guild.id
    humans = [m for m in channel.members if not m.bot]
    if humans:
        # 頻道有人：取消排程的離開
        task = idle_leave_tasks.pop(gid, None)
        if task and not task.done():
            task.cancel()
        return
    # 頻道空了（或只剩 bot）：排程 5 分鐘後離開
    if gid not in idle_leave_tasks or idle_leave_tasks[gid].done():
        async def _idle_leave():
            try:
                await asyncio.sleep(300)
                v = member.guild.voice_client
                if v and v.is_connected() and v.channel and not [m for m in v.channel.members if not m.bot]:
                    log.info(f"[語音] {v.channel.name} 閒置 5 分鐘，自動離開")
                    if gid in progress_tasks:
                        progress_tasks[gid].cancel()
                        del progress_tasks[gid]
                    v.stop()
                    await v.disconnect(force=True)
                    voice_clients.pop(gid, None)
                    current_voice_channel.pop(gid, None)
                    is_playing_state[gid] = False
                    is_paused_state[gid] = False
                    now_playing.pop(gid, None)
                    from . import prefetch
                    prefetch.clear(gid)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error(f"idle leave error: {e}")
            finally:
                # 只移除自己；避免被取消的舊 task 把剛建立的新 task 從 dict 刪掉
                if idle_leave_tasks.get(gid) is asyncio.current_task():
                    idle_leave_tasks.pop(gid, None)
        idle_leave_tasks[gid] = asyncio.create_task(_idle_leave())


def run():
    client.run(DISCORD_TOKEN)
