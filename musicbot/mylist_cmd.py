"""/mylist 指令群組：Discord 上用自己的個人歌單。
每個 Discord 帳號自動有自己的歌單（不用連結）；有在 /members 連結到成員 → 用成員那份（跟擴充功能、網頁共用）。
跟網頁「我的歌單」（用 Discord 登入）是同一份：都用 userlists.discord_key()。"""
import asyncio
import random

import discord
from discord import app_commands

from .utils import song_host_allowed
from .bot import tree
from . import userlists, importer
from .discord_ux import md

mylist = app_commands.Group(name="mylist", description="我的個人歌單（看 / 播放 / 加歌 / 匯入 YouTube 播放清單）",
                            guild_only=True)


def _key(interaction):
    from . import discordauth
    uid = interaction.user.id
    return userlists.discord_key(uid, owner=discordauth.is_owner(uid))


async def _say(interaction, msg):
    from .player import reply
    await reply(interaction, msg)


async def _name_ac(interaction, current):
    k = _key(interaction)
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
    if not name:
        ls = userlists.list_playlists(k)
        if not ls:
            return await _say(interaction, "你還沒有歌單。用 `/mylist import` 匯入 YouTube 播放清單，或到網頁面板「我的歌單」建立。")
        lines = [f"• **{md(p['name'])}**（{p['count']} 首）" for p in ls]
        return await _say(interaction, "\n".join(lines)[:1900])
    try:
        pl = userlists.get_playlist(k, _resolve(k, name))
    except KeyError as e:
        return await _say(interaction, str(e.args[0]))
    lines = [f"{i}. {md(s['title'], 90)}" for i, s in enumerate(pl["songs"][:20], 1)]
    more = f"\n…還有 {pl['count'] - 20} 首（完整內容請到網頁「我的歌單」）" if pl["count"] > 20 else ""
    await _say(interaction, (f"**{md(pl['name'])}**（{pl['count']} 首）\n" + "\n".join(lines))[:1850] + more)


@mylist.command(name="play", description="把整份歌單加入佇列（加在佇列尾端）")
@app_commands.describe(name="歌單名稱", shuffle="隨機順序")
@app_commands.autocomplete(name=_name_ac)
async def play(interaction: discord.Interaction, name: str, shuffle: bool = False):
    from .player import get_voice_client
    from .webcore import web_import_songs
    from .state import queue_full_message, queue_skip_message
    k = _key(interaction)
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
    from . import actionlog
    n = await web_import_songs(interaction.guild.id, songs, requester=interaction.user.display_name, stats=stats,
                               actor=actionlog.discord_actor(interaction), label=f"歌單：{pl['name']}")
    sk = stats.get("full_skipped", 0)
    if not n and sk:
        return await _say(interaction, f"⚠️ {queue_full_message()}")
    msg = f"✅ 已從「{md(pl['name'])}」加入 **{n}** 首" if n else "歌單裡的歌都已經在佇列中"
    await _say(interaction, msg + (f"（{queue_skip_message(sk)}）" if sk else ""))


@mylist.command(name="add", description="把歌加進我的歌單（不填網址 = 目前播放的歌）")
@app_commands.describe(name="歌單名稱", url="YouTube 網址（可不填）")
@app_commands.autocomplete(name=_name_ac)
async def add(interaction: discord.Interaction, name: str, url: str = None):
    from .state import now_playing, now_playing_url
    k = _key(interaction)
    gid = interaction.guild.id if interaction.guild else 0
    title = None
    if not (url or "").strip():
        url, title = now_playing_url.get(gid), now_playing.get(gid)
        if not url:
            return await _say(interaction, "目前沒有正在播放的歌，請填網址")
    from .extapi import normalize_youtube, _lookup_title
    from .utils import song_url_error
    url = url.strip()
    url = normalize_youtube(url) or url             # youtu.be / shorts / &t= 都統一，去重才準
    if song_url_error(url):
        return await _say(interaction, f"{song_url_error(url)}，請貼上 YouTube 影片網址（https://…）")
    try:
        lid = _resolve(k, name)                     # 先確認歌單存在，再去查標題（查標題要幾秒）
    except KeyError as e:
        return await _say(interaction, str(e.args[0]))
    if not title:
        # 用 yt-dlp 查標題可能超過 3 秒 → 先 defer，不然 Discord 會顯示「應用程式沒有回應」
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True, ephemeral=True)
        loop = asyncio.get_running_loop()
        title = await loop.run_in_executor(None, _lookup_title, url)
    try:
        added, _ = userlists.add_songs(k, lid, [{"url": url, "title": title or url}])
    except (KeyError, ValueError) as e:
        return await _say(interaction, str(e.args[0]))
    pname = md(userlists.get_playlist(k, lid)["name"])
    song = md(title or url, 120)
    await _say(interaction, f"✅ 已把 **{song}** 加入「{pname}」" if added else f"「{pname}」裡已經有 **{song}** 了")


@mylist.command(name="import", description="匯入 YouTube / Spotify 歌單，存成一份新的個人歌單（不會加入佇列）")
@app_commands.describe(url="YouTube 播放清單或 Spotify 歌單網址", name="新歌單名稱（不填 = 用清單標題）")
async def import_(interaction: discord.Interaction, url: str, name: str = None):
    from .userlistapi import import_into_new_list
    k = _key(interaction)
    url = (url or "").strip()
    if not url.startswith("http") or not song_host_allowed(url) or not (importer.is_spotify(url) or "list=" in url):
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
