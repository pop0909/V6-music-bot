"""/mylist 指令群組：Discord 上用自己的個人歌單（Discord 使用者 id → 擁有者在 /members 連結的身分）"""
import asyncio
import random

import discord
from discord import app_commands

from .bot import tree
from . import userlists, importer, ytdl

NOT_LINKED = ("你的 Discord 帳號還沒連結到個人歌單。請擁有者到網頁「成員管理」(/members) 幫你填上 Discord 使用者 ID"
              "（Discord 設定 → 進階 → 開發者模式，再對自己按右鍵「複製使用者 ID」）。")

mylist = app_commands.Group(name="mylist", description="我的個人歌單（看 / 播放 / 加歌 / 匯入 YouTube 播放清單）")


def _key(interaction):
    return userlists.key_for_discord(interaction.user.id)


async def _say(interaction, msg):
    from .player import reply
    await reply(interaction, msg)


async def _name_ac(interaction, current):
    k = _key(interaction)
    if not k:
        return []
    cur = (current or "").lower()
    return [app_commands.Choice(name=f"{p['name']}（{p['count']} 首）"[:100], value=p["name"])
            for p in userlists.list_playlists(k) if cur in p["name"].lower()][:25]


def _resolve(k, name):
    lid = userlists.find_by_name(k, name)
    if not lid:
        names = [p["name"] for p in userlists.list_playlists(k)]
        hint = ("，你的歌單：" + "、".join(names[:10])) if names else "（你還沒有歌單）"
        raise KeyError(f"找不到歌單「{str(name)[:40]}」{hint}")
    return lid


@mylist.command(name="show", description="列出我的歌單，或看某份歌單的內容")
@app_commands.describe(name="歌單名稱（不填 = 列出全部）")
@app_commands.autocomplete(name=_name_ac)
async def show(interaction: discord.Interaction, name: str = None):
    k = _key(interaction)
    if not k:
        return await _say(interaction, NOT_LINKED)
    if not name:
        ls = userlists.list_playlists(k)
        if not ls:
            return await _say(interaction, "你還沒有歌單。用 `/mylist import` 匯入 YouTube 播放清單，或到網頁面板「我的歌單」建立。")
        lines = [f"• **{p['name']}**（{p['count']} 首）" for p in ls]
        return await _say(interaction, "\n".join(lines)[:1900])
    try:
        pl = userlists.get_playlist(k, _resolve(k, name))
    except KeyError as e:
        return await _say(interaction, str(e.args[0]))
    lines = [f"{i}. {s['title']}" for i, s in enumerate(pl["songs"][:20], 1)]
    more = f"\n…還有 {pl['count'] - 20} 首" if pl["count"] > 20 else ""
    await _say(interaction, (f"**{pl['name']}**（{pl['count']} 首）\n" + "\n".join(lines))[:1850] + more)


@mylist.command(name="play", description="把整份歌單加入佇列（加在佇列尾端）")
@app_commands.describe(name="歌單名稱", shuffle="隨機順序")
@app_commands.autocomplete(name=_name_ac)
async def play(interaction: discord.Interaction, name: str, shuffle: bool = False):
    from .player import get_voice_client
    from .webcore import web_import_songs
    from .state import queue_full_message, queue_skip_message
    k = _key(interaction)
    if not k:
        return await _say(interaction, NOT_LINKED)
    try:
        pl = userlists.get_playlist(k, _resolve(k, name))
    except KeyError as e:
        return await _say(interaction, str(e.args[0]))
    if not pl["songs"]:
        return await _say(interaction, "這份歌單是空的")
    vc = await get_voice_client(interaction)
    if not vc:
        return
    songs = list(pl["songs"])
    if shuffle:
        random.shuffle(songs)
    stats = {}
    n = await web_import_songs(interaction.guild.id, songs, requester=interaction.user.display_name, stats=stats)
    sk = stats.get("full_skipped", 0)
    if not n and sk:
        return await _say(interaction, f"⚠️ {queue_full_message()}")
    msg = f"✅ 已從「{pl['name']}」加入 **{n}** 首" if n else "歌單裡的歌都已經在佇列中"
    await _say(interaction, msg + (f"（{queue_skip_message(sk)}）" if sk else ""))


@mylist.command(name="add", description="把歌加進我的歌單（不填網址 = 目前播放的歌）")
@app_commands.describe(name="歌單名稱", url="YouTube 網址（可不填）")
@app_commands.autocomplete(name=_name_ac)
async def add(interaction: discord.Interaction, name: str, url: str = None):
    from .state import now_playing, now_playing_url
    k = _key(interaction)
    if not k:
        return await _say(interaction, NOT_LINKED)
    gid = interaction.guild.id if interaction.guild else 0
    title = None
    if not url:
        url, title = now_playing_url.get(gid), now_playing.get(gid)
        if not url:
            return await _say(interaction, "目前沒有正在播放的歌，請填網址")
    from .extapi import normalize_youtube, _lookup_title
    url = url.strip()
    url = normalize_youtube(url) or url             # youtu.be / shorts / &t= 都統一，去重才準
    if not title and url.startswith("http"):
        loop = asyncio.get_running_loop()
        title = await loop.run_in_executor(None, _lookup_title, url)
    try:
        added, _ = userlists.add_songs(k, _resolve(k, name), [{"url": url, "title": title or url}])
    except (KeyError, ValueError) as e:
        return await _say(interaction, str(e.args[0]))
    await _say(interaction, f"✅ 已加入「{name}」" if added else "歌單裡已經有這首歌（或網址無效）")


@mylist.command(name="import", description="匯入 YouTube / Spotify 歌單，存成一份新的個人歌單（不會加入佇列）")
@app_commands.describe(url="YouTube 播放清單或 Spotify 歌單網址", name="新歌單名稱（不填 = 用清單標題）")
async def import_(interaction: discord.Interaction, url: str, name: str = None):
    from .userlistapi import import_into_new_list
    k = _key(interaction)
    if not k:
        return await _say(interaction, NOT_LINKED)
    url = (url or "").strip()
    if not url.startswith("http") or not (importer.is_spotify(url) or "list=" in url):
        return await _say(interaction, "請貼上 YouTube 播放清單（含 list=）或 Spotify 歌單／專輯網址")
    if len(userlists.list_playlists(k)) >= userlists.MAX_LISTS:
        return await _say(interaction, f"每人最多 {userlists.MAX_LISTS} 份歌單")
    await interaction.response.defer(thinking=True, ephemeral=True)
    jid = import_into_new_list(k, url, name)
    for _ in range(300):                # 最多等 10 分鐘（Spotify 要逐首搜尋）
        await asyncio.sleep(2)
        j = importer.get_job(jid) or {}
        if j.get("status") == "done":
            return await _say(interaction, f"✅ 已建立歌單（{j.get('added', 0)} 首）。用 `/mylist play` 播放")
        if j.get("status") == "error":
            return await _say(interaction, f"匯入失敗：{j.get('message') or '未知錯誤'}")
    await _say(interaction, "匯入還在進行中，稍後用 `/mylist show` 查看")


tree.add_command(mylist)
