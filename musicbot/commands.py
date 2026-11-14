"""全部 Discord slash 指令"""
import time

import discord
from discord import app_commands

from .bot import tree
from .config import PUBLIC_URL
from .state import (
    queues, now_playing, voice_clients, volumes,
    volume_transformers, control_panels, song_start_times, song_durations,
    paused_position, random_loop_guilds, now_playing_url,
)
from . import audio, ytdl
from .utils import format_time


def _field_lines(lines, limit=1024):
    """Embed field value 上限 1024 字元，超過 Discord 會回 400 導致整個指令失敗"""
    out, size = [], 0
    for line in lines:
        if size + len(line) + 1 > limit - 4:
            out.append("…")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)
from .player import (  # noqa: F401  play_next / _update_panel：測試用 mock.patch.object(commands, ...) 替換
    _get_output_channel, _update_panel, play_next,
    get_voice_client, _process_and_play,
)
from .views import MusicControlView


@tree.command(name="play", description="播放音樂（支援單曲、播放清單、關鍵字或網址）")
@app_commands.describe(query="歌曲名稱、網址或播放清單")
async def play(interaction: discord.Interaction, query: str):
    if not query:
        await interaction.response.send_message("請提供歌曲名稱或網址！", ephemeral=True)
        return
    vc = await get_voice_client(interaction)
    if not vc: return
    gid = interaction.guild.id
    if gid not in queues: queues[gid] = []
    if gid in random_loop_guilds: random_loop_guilds[gid] = False
    await _process_and_play(interaction, query, gid, vc)

@tree.command(name="import", description="匯入整個 YouTube / Spotify 歌單到佇列（最多 200 首）")
@app_commands.describe(url="YouTube 播放清單或 Spotify 歌單／專輯網址")
async def import_cmd(interaction: discord.Interaction, url: str):
    import asyncio
    from . import importer
    from .webcore import web_import_songs
    url = (url or "").strip()
    if not url.startswith("http"):
        await interaction.response.send_message("請貼上 YouTube 或 Spotify 歌單網址", ephemeral=True)
        return
    vc = await get_voice_client(interaction)
    if not vc: return
    gid = interaction.guild.id
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)
    loop = asyncio.get_running_loop()
    prog = {"done": 0, "total": 0}
    spotify = importer.is_spotify(url)

    async def _ticker():
        last = None
        while True:
            await asyncio.sleep(3)
            cur = (prog["done"], prog["total"])
            if cur != last and cur[1]:
                last = cur
                try:
                    await interaction.edit_original_response(content=f"🔎 正在從 YouTube 搜尋 Spotify 歌曲… {cur[0]}/{cur[1]}")
                except Exception:
                    pass

    ticker = asyncio.create_task(_ticker()) if spotify else None
    try:
        name, songs, skipped = await loop.run_in_executor(
            None, lambda: importer.resolve(url, progress=lambda d, t: prog.update(done=d, total=t)))
    except Exception as e:
        await interaction.followup.send(f"匯入失敗：{ytdl.friendly_error(e, '匯入歌單')}", ephemeral=True)
        return
    finally:
        if ticker: ticker.cancel()
    if not songs:
        await interaction.followup.send("清單裡沒有可加入的歌曲", ephemeral=True)
        return
    stats = {}
    from . import actionlog
    added = await web_import_songs(gid, songs, requester=interaction.user.display_name, stats=stats,
                                   actor=actionlog.discord_actor(interaction), label=f"匯入：{name}" if name else "匯入歌單")
    if not added and stats.get("full_skipped"):
        from .state import queue_full_message
        await interaction.followup.send(f"⚠️ {queue_full_message()}", ephemeral=True)
        return
    extra = f"（{skipped} 首在 YouTube 找不到）" if skipped else ""
    if stats.get("full_skipped"):
        extra += f"（佇列已滿，另有 {stats['full_skipped']} 首沒加入）"
    await interaction.followup.send(f"✅ 已從「{name}」加入 **{added}** 首歌{extra}", ephemeral=True)
    out_ch = await _get_output_channel(gid)
    if out_ch:
        try:
            await out_ch.send(f"**{interaction.user.display_name}** 匯入了歌單「{name}」，共 **{added}** 首")
        except Exception:
            pass


@tree.command(name="skip", description="跳過目前歌曲")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        from . import actionlog
        actionlog.record(interaction.guild_id, "skip", actionlog.discord_actor(interaction),
                         now_playing.get(interaction.guild_id, ""), url=now_playing_url.get(interaction.guild_id))
        await interaction.response.send_message("✅ 已跳過", ephemeral=True)
        await audio.fade_out_stop(interaction.guild_id, vc)   # 0.12 秒淡出再切歌
        out_ch = await _get_output_channel(interaction.guild_id)
        if out_ch:
            await out_ch.send(f"**{interaction.user.display_name}** 已跳過歌曲")
    else:
        await interaction.response.send_message("沒有正在播放的歌曲", ephemeral=True)

@tree.command(name="stop", description="停止播放並清空佇列（留在語音頻道；離開請用 /leave）")
async def stop(interaction: discord.Interaction):
    from .player import stop_playback
    gid = interaction.guild.id
    await interaction.response.send_message("⏹ 已停止並清空佇列（仍在語音頻道，離開請用 /leave）", ephemeral=True)
    await stop_playback(gid, guild=interaction.guild)
    out_ch = await _get_output_channel(gid)
    if out_ch:
        await out_ch.send(f"⏹ **{interaction.user.display_name}** 已停止播放並清空佇列")

@tree.command(name="volume", description="調整播放音量")
@app_commands.describe(level="音量大小：0~200")
async def volume(interaction: discord.Interaction, level: int):
    gid = interaction.guild.id
    if level < 0 or level > 200:
        await interaction.response.send_message("音量範圍為 0~200", ephemeral=True)
        return
    vol = level / 100.0
    volumes[gid] = vol
    if gid in volume_transformers:
        volume_transformers[gid].volume = vol
    out_ch = await _get_output_channel(gid)
    if out_ch:
        await out_ch.send(f"🔊 **{interaction.user.display_name}** 將音量設為 **{level}**%")
    await interaction.response.send_message(f"✅ 音量已設為 **{level}**%", ephemeral=True)

@tree.command(name="queue", description="顯示目前的播放佇列")
async def queue(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid not in queues or not queues[gid]:
        msg = "佇列為空"
        if gid in now_playing and now_playing.get(gid):
            msg = f"正在播放：**{now_playing[gid]}**\n佇列為空"
        await interaction.response.send_message(msg, ephemeral=True)
        return
    current = now_playing.get(gid, "無")
    total = len(queues[gid])
    lines = []
    for i, (_, t) in enumerate(queues[gid][:20]):
        lines.append(f"`{i+1}.` **{t}**")
    embed = discord.Embed(title="播放佇列", description=f"正在播放：**{current}**", color=discord.Color.blue())
    embed.add_field(name=f"即將播放（共 {total} 首）", value=_field_lines(lines), inline=False)
    if total > 20:
        embed.set_footer(text=f"... 還有 {total - 20} 首未顯示")
    out_ch = await _get_output_channel(gid)
    if out_ch:
        await out_ch.send(embed=embed)
    await interaction.response.send_message("✅ 播放佇列已輸出", ephemeral=True)

@tree.command(name="join", description="讓機器人加入你所在的語音頻道")
async def join(interaction: discord.Interaction):
    vc = await get_voice_client(interaction)
    if vc:
        from .player import reply
        await reply(interaction, f"✅ 已加入 {vc.channel.name}")

@tree.command(name="panel", description="重新顯示音樂控制面板")
async def panel(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    gid = interaction.guild.id
    if not vc or not vc.is_connected():
        await interaction.response.send_message("機器人不在語音頻道中", ephemeral=True)
        return
    current = now_playing.get(gid, "未知")
    view = MusicControlView(gid)
    if gid in song_start_times:
        elapsed = time.time() - song_start_times[gid]
    elif gid in paused_position:
        elapsed = paused_position[gid]
    else: elapsed = 0
    dur = song_durations.get(gid, 0)
    if dur > 0 and current != "未知":
        prog = min(elapsed / dur, 1.0)
        filled = int(prog * 15)
        bar = "\u2588" * filled + "\u2591" * (15 - filled)
        indicator = "\u23f8" if gid not in song_start_times and gid in paused_position else "\u25b6"
        content = f"{indicator} 正在播放：**{current}**\n{bar} `{format_time(elapsed)} / {format_time(dur)}`"
    else:
        content = f"\u25b6 正在播放：**{current}**"
    await _update_panel(gid, "", view=None, remove=True)
    # 發送到指定頻道
    out_ch = await _get_output_channel(gid)
    if out_ch:
        msg = await out_ch.send(content, view=view)
        control_panels[gid] = msg
    await interaction.response.send_message("✅ 控制面板已輸出", ephemeral=True)

@tree.command(name="leave", description="讓機器人離開語音頻道")
async def leave(interaction: discord.Interaction):
    from .player import leave_voice
    gid = interaction.guild.id
    if not (interaction.guild.voice_client or voice_clients.get(gid)):
        await interaction.response.send_message("我不在語音頻道中", ephemeral=True)
        return
    await interaction.response.send_message("✅ 已離開語音頻道", ephemeral=True)
    await leave_voice(gid, interaction.guild)
    await _update_panel(gid, "已離開", view=None, remove=True)
    out_ch = await _get_output_channel(gid)
    if out_ch:
        await out_ch.send(f"👋 **{interaction.user.display_name}** 機器人已離開語音頻道")

def _web_status_line(gid):
    """/web 狀態行：正在播放 / 已暫停 / 待機 / 休眠 · 佇列 N 首 · 語音頻道"""
    from . import idlesleep
    vc = voice_clients.get(gid) if gid else None
    connected = bool(vc and vc.is_connected())
    title = now_playing.get(gid) if gid else None
    if connected and title and (vc.is_playing() or vc.is_paused()):
        head = ("⏸️ 已暫停：" if vc.is_paused() else "🎶 正在播放：") + f"**{title[:80]}**"
    elif connected:
        head = "⏹️ 待機中"
    else:
        head = "💤 休眠中" if (gid and idlesleep.is_asleep(gid)) else "💤 未在語音"
    parts = [head, f"佇列 {len(queues.get(gid, [])) if gid else 0} 首"]
    if connected and getattr(vc, "channel", None):
        parts.append(f"🔊 {vc.channel.name}")
    return " · ".join(parts)


@tree.command(name="web", description="取得網頁控制面板連結")
async def web_panel(interaction: discord.Interaction):
    """只給公開網址（不顯示主機 IP 與埠號），附開啟按鈕與目前狀態，僅自己可見"""
    url = PUBLIC_URL.rstrip("/") + "/"
    embed = discord.Embed(
        title="🎛️ 網頁控制面板",
        description=f"{url}\n{_web_status_line(interaction.guild_id)}",
        color=discord.Color.from_rgb(108, 92, 231),
    )
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="開啟網頁面板", url=url, emoji="🌐"))
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

from . import mylist_cmd as _mylist_cmd  # noqa: E402,F401  /mylist 指令群組
