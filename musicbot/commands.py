"""全部 Discord slash 指令"""
import time  # noqa: F401  舊測試 / 外部模組可能取用

import discord
from discord import app_commands

from .bot import tree
from .config import PUBLIC_URL, log
from .state import (
    queues, now_playing, voice_clients, volumes,
    volume_transformers, control_panels, song_start_times, song_durations,  # noqa: F401
    paused_position, random_loop_guilds, now_playing_url,  # noqa: F401
)
from . import audio, ytdl
from .utils import format_time  # noqa: F401
from .discord_ux import md, panel_text, claim_skip, announce, queue_embed, queue_pages, QueuePager, reply_error, voice_gate, GENERIC_ERROR


from .player import (  # noqa: F401  play_next / _update_panel：測試用 mock.patch.object(commands, ...) 替換
    _get_output_channel, _update_panel, play_next,
    get_voice_client, _process_and_play,
)
from .views import MusicControlView


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """指令出錯時一定回覆使用者（舊版沒處理 → Discord 只顯示「應用程式沒有回應」）"""
    if isinstance(error, app_commands.NoPrivateMessage):
        text = "這個指令只能在伺服器裡使用"
    elif isinstance(error, app_commands.CheckFailure):
        text = "你現在不能使用這個指令"
    else:
        orig = getattr(error, "original", error)
        name = getattr(getattr(interaction, "command", None), "qualified_name", "?")
        log.error(f"[指令] /{name} 失敗：{orig!r}", exc_info=orig)
        text = GENERIC_ERROR
    await reply_error(interaction, text)


@tree.command(name="play", description="播放音樂（支援單曲、播放清單、關鍵字或網址）")
@app_commands.describe(
    query="歌曲名稱、網址或播放清單",
    source="關鍵字要搜哪個網站，預設 YouTube；貼網址時不用選",
)
@app_commands.rename(source="來源")
@app_commands.choices(source=[
    app_commands.Choice(name="YouTube", value="youtube"),
    app_commands.Choice(name="Bilibili", value="bilibili"),
])
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str, source: str | None = None):
    if not (query or "").strip():
        await interaction.response.send_message("請輸入歌曲名稱或網址", ephemeral=True)
        return
    vc = await get_voice_client(interaction)
    if not vc: return
    gid = interaction.guild.id
    if gid not in queues: queues[gid] = []
    if gid in random_loop_guilds: random_loop_guilds[gid] = False
    await _process_and_play(interaction, query.strip(), gid, vc, source=source)

@tree.command(name="import", description="匯入整個 YouTube / Spotify 歌單到佇列（最多 200 首）")
@app_commands.describe(url="YouTube 播放清單或 Spotify 歌單／專輯網址")
@app_commands.guild_only()
async def import_cmd(interaction: discord.Interaction, url: str):
    import asyncio
    from . import importer
    from .webcore import web_import_songs
    url = (url or "").strip()
    from .utils import song_host_allowed
    if not url.startswith("http") or not song_host_allowed(url):
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
        await interaction.followup.send(f"⚠️ 匯入失敗：{ytdl.friendly_error(e, '匯入歌單')}", ephemeral=True)
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
    if not added:
        await interaction.followup.send(f"清單「{md(name, 80)}」的歌都已經在佇列裡了", ephemeral=True)
        return
    extra = f"（{skipped} 首在 YouTube 找不到）" if skipped else ""
    if stats.get("full_skipped"):
        extra += f"（佇列已滿，另有 {stats['full_skipped']} 首沒加入）"
    await interaction.followup.send(f"✅ 已從「{md(name, 80)}」加入 **{added}** 首歌{extra}", ephemeral=True)
    await announce(gid, f"📥 **{md(interaction.user.display_name)}** 匯入了歌單「{md(name, 80)}」，共 **{added}** 首")


@tree.command(name="skip", description="跳過目前歌曲")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    gid = interaction.guild_id
    msg = voice_gate(interaction, vc)    # 只有跟機器人在同一個語音頻道的人能跳（擁有者不限）
    if msg:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    if vc and (vc.is_playing() or vc.is_paused()):
        title = now_playing.get(gid, "")
        if not claim_skip(gid, now_playing_url.get(gid)):
            # 連點 / 兩個人同時按：同一首只跳一次（不然會連下一首一起跳掉）
            await interaction.response.send_message("⏭️ 剛剛已經跳過這首了，下一首準備中", ephemeral=True)
            return
        from . import actionlog
        actionlog.record(gid, "skip", actionlog.discord_actor(interaction), title, url=now_playing_url.get(gid))
        from . import prewarm
        nxt = (queues.get(gid) or [None])[0]
        prewarm.arm_upcoming(gid, nxt[0] if nxt else None)
        await interaction.response.send_message(f"⏭️ 已跳過：**{md(title, 120)}**" if title else "⏭️ 已跳過", ephemeral=True)
        await audio.fade_out_stop(gid, vc)   # 0.12 秒淡出再切歌；預熱同時進行
        who = md(interaction.user.display_name)
        await announce(gid, f"⏭️ **{who}** 跳過了 **{md(title, 120)}**" if title else f"⏭️ **{who}** 跳過了這首歌")
    else:
        await interaction.response.send_message("沒有正在播放的歌曲", ephemeral=True)

@tree.command(name="stop", description="停止播放並清空佇列（留在語音頻道；離開請用 /leave）")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    from .player import stop_playback
    gid = interaction.guild.id
    vc = interaction.guild.voice_client or voice_clients.get(gid)
    msg = voice_gate(interaction, vc)
    if msg:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    busy = bool(vc and (vc.is_playing() or vc.is_paused())) or bool(queues.get(gid)) or bool(now_playing.get(gid))
    if not busy:
        # 沒東西可停：不要回「已停止並清空」，也不要在頻道公告
        await interaction.response.send_message("目前沒有在播放，佇列也是空的", ephemeral=True)
        return
    await interaction.response.send_message("⏹ 已停止並清空佇列（仍在語音頻道，離開請用 /leave）", ephemeral=True)
    await stop_playback(gid, guild=interaction.guild)
    await announce(gid, f"⏹ **{md(interaction.user.display_name)}** 已停止播放並清空佇列")

@tree.command(name="volume", description="調整播放音量（0～200%）")
@app_commands.describe(level="音量：0～200（%）")
@app_commands.guild_only()
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 200]):
    gid = interaction.guild.id
    if not isinstance(level, int) or level < 0 or level > 200:   # Discord 端已限制範圍；直接呼叫時的保險
        await interaction.response.send_message("音量請輸入 0 到 200", ephemeral=True)
        return
    vol = level / 100.0
    volumes[gid] = vol
    if gid in volume_transformers:
        volume_transformers[gid].volume = vol
    # 先回覆（3 秒內）再公告：公告慢或失敗都不會讓指令顯示失敗
    await interaction.response.send_message(f"🔊 音量已設為 **{level}**%", ephemeral=True)
    await announce(gid, f"🔊 **{md(interaction.user.display_name)}** 將音量設為 **{level}**%")

@tree.command(name="queue", description="查看目前的播放佇列（只有你看得到）")
@app_commands.guild_only()
async def queue(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid not in queues or not queues[gid]:
        msg = "佇列是空的"
        if now_playing.get(gid):
            msg = f"正在播放：**{md(now_playing[gid], 150)}**\n佇列是空的"
        await interaction.response.send_message(msg, ephemeral=True)
        return
    # 只給問的人看（不洗音樂頻道）；翻頁按鈕在這則私人訊息上，只有自己按得到，5 分鐘後收起
    view = QueuePager(gid) if queue_pages(gid) > 1 else None
    kw = {"embed": queue_embed(gid, 0), "ephemeral": True}
    if view is not None:
        kw["view"] = view
    await interaction.response.send_message(**kw)
    if view is not None:
        try:
            view.message = await interaction.original_response()
        except Exception:
            view.message = None

@tree.command(name="join", description="讓機器人加入你所在的語音頻道")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    me = interaction.user.voice.channel if interaction.user.voice else None
    cur = interaction.guild.voice_client
    already = bool(me and cur and cur.is_connected() and cur.channel == me)
    vc = await get_voice_client(interaction)
    if vc:
        from .player import reply
        name = md(getattr(vc.channel, "name", ""))
        await reply(interaction, f"✅ 已經在 {name} 了" if already else f"✅ 已加入 {name}")

@tree.command(name="panel", description="重新顯示音樂控制面板")
@app_commands.guild_only()
async def panel(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    gid = interaction.guild.id
    if not vc or not vc.is_connected():
        await interaction.response.send_message("機器人不在語音頻道中（用 /join 或 /play 讓它加入）", ephemeral=True)
        return
    # 先 defer：收掉舊面板的編輯可能被 Discord 限速好幾秒（舊版會超過 3 秒 → 顯示「應用程式沒有回應」）
    await interaction.response.defer(ephemeral=True, thinking=True)
    view = MusicControlView(gid)
    content = panel_text(gid)
    await _update_panel(gid, "⬇️ 控制面板已重新發送，請用最新的那則", view=None, remove=True)
    msg = await announce(gid, content, fallback=interaction.channel, view=view)
    if msg is None:
        await interaction.followup.send("⚠️ 控制面板發不出去（機器人可能沒有在這個頻道發言的權限）", ephemeral=True)
        return
    control_panels[gid] = msg
    where = getattr(getattr(msg, "channel", None), "mention", None)
    await interaction.followup.send(f"✅ 控制面板已發到 {where}" if isinstance(where, str) else "✅ 控制面板已發出", ephemeral=True)

@tree.command(name="leave", description="讓機器人離開語音頻道")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    from .player import leave_voice
    gid = interaction.guild.id
    vc = interaction.guild.voice_client or voice_clients.get(gid)
    if not vc:
        await interaction.response.send_message("機器人不在語音頻道中", ephemeral=True)
        return
    msg = voice_gate(interaction, vc)
    if msg:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    await interaction.response.send_message("✅ 已離開語音頻道", ephemeral=True)
    await leave_voice(gid, interaction.guild)
    await _update_panel(gid, "已離開", view=None, remove=True)
    await announce(gid, f"👋 **{md(interaction.user.display_name)}** 讓機器人離開了語音頻道")

def _web_status_line(gid):
    """/web 狀態行：正在播放 / 已暫停 / 待機 / 休眠 · 佇列 N 首 · 語音頻道"""
    from . import idlesleep
    vc = voice_clients.get(gid) if gid else None
    connected = bool(vc and vc.is_connected())
    title = now_playing.get(gid) if gid else None
    if connected and title and (vc.is_playing() or vc.is_paused()):
        head = ("⏸️ 已暫停：" if vc.is_paused() else "🎶 正在播放：") + f"**{md(title, 80)}**"
    elif connected:
        head = "⏹️ 待機中"
    else:
        head = "💤 休眠中" if (gid and idlesleep.is_asleep(gid)) else "💤 未在語音"
    parts = [head, f"佇列 {len(queues.get(gid, [])) if gid else 0} 首"]
    if connected and getattr(vc, "channel", None):
        parts.append(f"🔊 {md(vc.channel.name)}")
    return " · ".join(parts)


@tree.command(name="web", description="取得網頁控制面板連結")
@app_commands.guild_only()
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


# /help 的內容：每個指令一行。測試會比對實際註冊的指令與面板按鈕，新增 / 改名沒更新這裡就會失敗
HELP_COMMANDS = [
    ("play", "點歌：輸入歌名或網址；關鍵字預設搜 YouTube，可選來源改搜 B 站"),
    ("import", "把整個 YouTube / Spotify 歌單加進佇列（最多 200 首）"),
    ("skip", "跳過目前這首"),
    ("stop", "停止播放並清空佇列（機器人留在語音頻道）"),
    ("volume", "調整音量，0～200%"),
    ("queue", "查看播放佇列（只有你看得到，可翻頁）"),
    ("join", "讓機器人加入你所在的語音頻道"),
    ("leave", "讓機器人離開語音頻道"),
    ("panel", "重新發送控制面板"),
    ("web", "取得網頁控制面板連結"),
    ("mylist show", "列出我的個人歌單，或看某份歌單的內容"),
    ("mylist play", "把整份個人歌單加進佇列（可選隨機順序）"),
    ("mylist add", "把歌加進個人歌單（不填網址＝目前播放的歌）"),
    ("mylist import", "匯入 YouTube / Spotify 歌單，存成新的個人歌單"),
    ("help", "顯示這份說明"),
]
HELP_BUTTONS = [
    ("\u23ef\ufe0f", "播放 / 暫停"),
    ("\u23ed\ufe0f", "跳過目前這首"),
    ("\u23f9\ufe0f", "停止並清空佇列（會先問你確定）"),
    ("\U0001f500", "把佇列順序打亂一次"),
    ("\U0001f3b2", "開啟隨機循環（從本地歌單一直隨機播）"),
    ("\U0001f50a", "音量 +10%"),
    ("\U0001f509", "音量 −10%"),
    ("\u2795", "點歌（輸入歌名或網址）"),
]


def help_embed():
    url = PUBLIC_URL.rstrip("/") + "/"
    embed = discord.Embed(title="🎵 聽歌機器人 使用說明", color=discord.Color.from_rgb(108, 92, 231),
                          description="大部分指令要先進語音頻道；回覆大多只有你看得到。")
    embed.add_field(name="指令", value="\n".join(f"`/{n}` {d}" for n, d in HELP_COMMANDS), inline=False)
    embed.add_field(name="控制面板按鈕（要在機器人所在的語音頻道）",
                    value="\n".join(f"{e} {d}" for e, d in HELP_BUTTONS), inline=False)
    embed.add_field(name="網頁控制面板", value=f"搜尋、排序佇列、我的歌單、播放紀錄等更多功能：{url}（或用 `/web`）", inline=False)
    return embed


@tree.command(name="help", description="列出所有指令與控制面板按鈕的用法（只有你看得到）")
@app_commands.guild_only()
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=help_embed(), ephemeral=True)
