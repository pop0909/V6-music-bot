"""Discord 按鈕控制面板與 Modal"""
from .config import DEFAULT_VOLUME
import random
import time

import discord

from .config import log
from .state import (
    paused_position, song_start_times, queues, now_playing, now_playing_url,
    volumes, volume_transformers, random_loop_guilds,
    is_playing_state, is_paused_state,
)
from .storage import load_favorites, add_favorite_song
from . import actionlog
from .discord_ux import md, panel_text, claim_skip, reply_error, GENERIC_ERROR


class MusicControlView(discord.ui.View):
    """timeout=None + 固定 custom_id：bot 啟動時 add_view(MusicControlView(None)) 後，
    重啟前發出的舊面板按鈕仍可使用（guild_id=None → 以互動所在伺服器為準）。"""
    def __init__(self, guild_id=None):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    def _gid(self, interaction):
        return self.guild_id if self.guild_id is not None else interaction.guild_id

    async def interaction_check(self, interaction):
        if interaction.guild_id is None or (self.guild_id is not None and interaction.guild_id != self.guild_id):
            await interaction.response.send_message("此面板不屬於這個伺服器", ephemeral=True)
            return False
        if not interaction.user.voice:
            await interaction.response.send_message("請先加入語音頻道", ephemeral=True)
            return False
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("機器人不在語音頻道中", ephemeral=True)
            return False
        if vc.channel != interaction.user.voice.channel:
            await interaction.response.send_message(f"請先加入機器人所在的語音頻道「{md(getattr(vc.channel, 'name', ''))}」", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item):
        """按鈕出錯也要回覆（舊版沒處理 → 只顯示「此互動失敗」）"""
        log.error(f"[面板] 按鈕 {getattr(item, 'custom_id', '?')} 失敗：{error!r}", exc_info=error)
        await reply_error(interaction, GENERIC_ERROR)

    @discord.ui.button(label="\u23ef\ufe0f", style=discord.ButtonStyle.primary, custom_id="mb:toggle_pause")
    async def toggle_pause(self, interaction, button):
        vc = interaction.guild.voice_client
        gid = self._gid(interaction)
        if vc.is_paused():
            from .player import resume_playback
            # 同步給 Web 面板讀的狀態；暫停中跳轉過 → 從新位置繼續
            # 面板文字馬上換成 ▶（舊版要等 5 秒的進度更新才看得出有沒有按到）
            await interaction.response.edit_message(content=panel_text(gid, paused=False), view=self)
            await resume_playback(gid, vc)
        elif vc.is_playing():
            if gid in song_start_times:
                paused_position[gid] = time.time() - song_start_times[gid]
                del song_start_times[gid]
            vc.pause()
            is_playing_state[gid] = False
            is_paused_state[gid] = True
            await interaction.response.edit_message(content=panel_text(gid, paused=True), view=self)
        else:
            await interaction.response.send_message("沒有正在播放的歌曲", ephemeral=True)

    @discord.ui.button(label="\u23ed\ufe0f", style=discord.ButtonStyle.secondary, custom_id="mb:skip_song")
    async def skip_song(self, interaction, button):
        from .audio import fade_out_stop
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            gid = self._gid(interaction)
            title = now_playing.get(gid, "")
            if not claim_skip(gid, now_playing_url.get(gid)):
                # 連點兩下 / 兩個人同時按：同一首只跳一次
                await interaction.response.send_message("⏭️ 剛剛已經跳過這首了，下一首準備中", ephemeral=True)
                return
            actionlog.record(gid, "skip", actionlog.discord_actor(interaction), title,
                             url=now_playing_url.get(gid))
            await interaction.response.send_message(f"⏭️ 已跳過：**{md(title, 120)}**" if title else "⏭️ 已跳過", ephemeral=True)
            await fade_out_stop(gid, vc)   # 0.12 秒淡出再切歌
        else:
            await interaction.response.send_message("沒有正在播放的歌曲", ephemeral=True)

    @discord.ui.button(label="\u23f9\ufe0f", style=discord.ButtonStyle.danger, custom_id="mb:stop_bot")
    async def stop_bot(self, interaction, button):
        from .player import stop_playback
        gid = self._gid(interaction)
        vc = interaction.guild.voice_client
        if not ((vc and (vc.is_playing() or vc.is_paused())) or queues.get(gid) or now_playing.get(gid)):
            await interaction.response.send_message("目前沒有在播放，佇列也是空的", ephemeral=True)
            return
        await interaction.response.send_message("⏹ 已停止並清空佇列（仍在語音頻道）", ephemeral=True)
        await stop_playback(self._gid(interaction), guild=interaction.guild)

    @discord.ui.button(label="\U0001f500", style=discord.ButtonStyle.secondary, custom_id="mb:shuffle_queue")
    async def shuffle_queue(self, interaction, button):
        gid = self._gid(interaction)
        if gid in queues and len(queues[gid]) > 1:
            random.shuffle(queues[gid])
            await interaction.response.send_message("🔀 已隨機打亂佇列", ephemeral=True)
        else:
            await interaction.response.send_message("佇列不到 2 首，不用打亂", ephemeral=True)

    @discord.ui.button(label="\u2764\ufe0f", style=discord.ButtonStyle.secondary, row=1, custom_id="mb:add_favorite")
    async def add_favorite(self, interaction, button):
        gid = self._gid(interaction)
        url = now_playing_url.get(gid)
        title = now_playing.get(gid)
        if not url or not title:
            await interaction.response.send_message("目前沒有正在播放的歌曲", ephemeral=True)
            return
        if add_favorite_song(gid, url, title):
            await interaction.response.send_message(f"❤️ 已將 **{md(title, 120)}** 加入最愛", ephemeral=True)
        else:
            await interaction.response.send_message("❤️ 這首歌已經在最愛裡了", ephemeral=True)

    @discord.ui.button(label="\U0001f3b5", style=discord.ButtonStyle.success, row=1, custom_id="mb:fav_play")
    async def fav_play(self, interaction, button):
        from .player import play_next
        gid = self._gid(interaction)
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            await interaction.response.send_message("機器人不在語音頻道中", ephemeral=True)
            return
        favs = load_favorites(gid)
        if not favs:
            await interaction.response.send_message("最愛清單是空的", ephemeral=True)
            return
        from .state import queue_room, queue_full_message
        if queue_room(gid) <= 0:
            await interaction.response.send_message(f"⚠️ {queue_full_message()}", ephemeral=True)
            return
        if gid not in queues: queues[gid] = []
        song = random.choice(favs)
        queues[gid].append((song["url"], song["title"]))
        actionlog.record(gid, "add", actionlog.discord_actor(interaction), song["title"], url=song["url"], detail="隨機最愛")
        await interaction.response.send_message(f"🎵 隨機選了一首最愛：**{md(song['title'], 120)}**", ephemeral=True)
        if not vc.is_playing() and not vc.is_paused():
            await play_next(gid, interaction.channel)

    @discord.ui.button(label="\U0001f3b2", style=discord.ButtonStyle.secondary, row=1, custom_id="mb:list_shuffle")
    async def list_shuffle(self, interaction, button):
        from .state import local_playlist
        from .player import play_next
        gid = self._gid(interaction)
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            await interaction.response.send_message("機器人不在語音頻道中", ephemeral=True)
            return
        if not local_playlist:
            await interaction.response.send_message("本地歌單是空的", ephemeral=True)
            return
        from .state import queue_room, queue_full_message
        if queue_room(gid) <= 0:
            await interaction.response.send_message(f"⚠️ {queue_full_message()}", ephemeral=True)
            return
        if gid not in queues: queues[gid] = []
        random_loop_guilds[gid] = True
        song = random.choice(local_playlist)
        queues[gid].append((song["url"], song["title"]))
        actionlog.record(gid, "add", actionlog.discord_actor(interaction), song["title"], url=song["url"], detail="隨機循環")
        await interaction.response.send_message(f"🎲 已開啟隨機循環，先播：**{md(song['title'], 120)}**", ephemeral=True)
        if not vc.is_playing() and not vc.is_paused():
            await play_next(gid, interaction.channel)

    @discord.ui.button(label="\U0001f50a", style=discord.ButtonStyle.secondary, row=2, custom_id="mb:volume_up")
    async def volume_up(self, interaction, button):
        gid = self._gid(interaction)
        cv = volumes.get(gid, DEFAULT_VOLUME)
        nv = min(round(cv * 100) + 10, 200) / 100.0
        volumes[gid] = nv
        if gid in volume_transformers:
            volume_transformers[gid].volume = nv
        await interaction.response.send_message(f"🔊 音量 {round(nv*100)}%", ephemeral=True)

    @discord.ui.button(label="\U0001f509", style=discord.ButtonStyle.secondary, row=2, custom_id="mb:volume_down")
    async def volume_down(self, interaction, button):
        gid = self._gid(interaction)
        cv = volumes.get(gid, DEFAULT_VOLUME)
        nv = max(round(cv * 100) - 10, 0) / 100.0
        volumes[gid] = nv
        if gid in volume_transformers:
            volume_transformers[gid].volume = nv
        await interaction.response.send_message(f"🔊 音量 {round(nv*100)}%", ephemeral=True)

    @discord.ui.button(label="\u2795", style=discord.ButtonStyle.primary, row=2, custom_id="mb:add_song")
    async def add_song(self, interaction, button):
        modal = MusicLinkModal()
        await interaction.response.send_modal(modal)


class MusicLinkModal(discord.ui.Modal, title="點歌"):
    # 跟 /play 一樣：關鍵字、YouTube 網址、播放清單都可以（舊版標題寫「連結」，看起來只能貼網址）
    link = discord.ui.TextInput(label="歌名或 YouTube 網址", placeholder="輸入歌名關鍵字，或貼上 YouTube 網址",
                                style=discord.TextStyle.short, required=True, max_length=400)

    async def on_error(self, interaction, error):
        log.error(f"[面板] 點歌表單失敗：{error!r}", exc_info=error)
        await reply_error(interaction, GENERIC_ERROR)

    async def on_submit(self, interaction):
        from .player import _process_and_play
        from .player import get_voice_client, reply
        query = self.link.value.strip()
        if not query:
            return await reply(interaction, "請輸入歌名或網址")
        vc = await get_voice_client(interaction)  # 走 connect_voice：喚醒、清殘留 VC、記錄頻道
        if not vc: return
        gid = interaction.guild.id
        try:
            if gid not in queues: queues[gid] = []
            if gid in random_loop_guilds: random_loop_guilds[gid] = False
            await _process_and_play(interaction, query, gid, vc)
        except Exception as e:
            await reply(interaction, "加入歌曲失敗，請稍後再試")
            log.error(f"Modal Error: {e}")
