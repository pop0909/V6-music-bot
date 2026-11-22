"""Discord 指令 / 按鈕共用的小工具：歌名跳脫、控制面板文字、連點跳過防護、公告、佇列分頁、錯誤訊息"""
import asyncio
import math
import time

import discord

from .config import log
from .utils import format_time


def md(text, limit=None):
    """歌名 / 使用者名稱放進訊息前先跳脫 Markdown（* _ ~ ` | > 等），不然「**a*b**」會把粗體弄壞。
    換行收成空白；limit：超過就截斷加「…」"""
    s = " ".join(("" if text is None else str(text)).split())
    if limit and len(s) > limit:
        s = s[:limit - 1] + "…"
    return discord.utils.escape_markdown(s)


# ---------- 控制面板文字（/panel、播放中進度、暫停鍵）----------
BAR_LEN = 15


def panel_text(gid, title=None, paused=None, elapsed=None):
    """「▶ 正在播放：**歌名**」＋進度條；長度未知（直播）→「🔴 直播」，跟網頁面板一樣；沒在播 →「⏹ 目前沒有在播放」"""
    from .state import now_playing, song_start_times, paused_position, song_durations
    title = now_playing.get(gid) if title is None else title
    if not title:
        return "⏹ 目前沒有在播放"
    if elapsed is None:
        if gid in song_start_times:
            elapsed = time.time() - song_start_times[gid]
        else:
            elapsed = paused_position.get(gid, 0)
    if paused is None:
        paused = gid not in song_start_times and gid in paused_position
    head = f"{'⏸' if paused else '▶'} 正在播放：**{md(title)}**"
    dur = song_durations.get(gid, 0) or 0
    if dur <= 0:
        return head + "\n🔴 直播"
    filled = int(min(max(elapsed, 0) / dur, 1.0) * BAR_LEN)
    bar = "\u2588" * filled + "\u2591" * (BAR_LEN - filled)
    return head + f"\n{bar} `{format_time(elapsed)} / {format_time(dur)}`"


# ---------- 跳過：同一首歌 1.5 秒內再按（連點 / 兩個人同時按）只跳一次 ----------
SKIP_GUARD_S = 1.5
_last_skip = {}


def claim_skip(gid, url):
    """True = 這次要跳；False = 剛剛已經跳過同一首（連點）"""
    now = time.monotonic()
    prev = _last_skip.get(gid)
    if prev and prev[0] == url and now - prev[1] < SKIP_GUARD_S:
        return False
    _last_skip[gid] = (url, now)
    return True


def reset_for_tests():
    _last_skip.clear()


# ---------- 公告到輸出頻道：失敗（沒權限 / 頻道被刪）只記日誌，不影響已回覆的指令 ----------
async def announce(gid, content=None, fallback=None, **kw):
    """回傳送出的訊息或 None。fallback：輸出頻道不存在時改發到這個頻道（例如指令所在頻道）"""
    from .player import _get_output_channel
    ch = await _get_output_channel(gid) or fallback
    if ch is None:
        return None
    try:
        return await ch.send(content, **kw)
    except Exception as e:
        log.warning(f"[Discord] 發送到 #{getattr(ch, 'name', '?')} 失敗：{e!r}"[:300])
        return None


# ---------- 語音連線失敗的說明 ----------
def voice_error_text(e, channel=None):
    name = f"「{md(getattr(channel, 'name', ''))}」" if channel is not None and getattr(channel, "name", None) else ""
    if isinstance(e, discord.Forbidden):
        return f"機器人沒有加入語音頻道{name}的權限（需要「連接」與「說話」）"
    if isinstance(e, asyncio.TimeoutError):
        return f"加入語音頻道{name}逾時，請再試一次"
    return f"無法加入語音頻道{name}，請再試一次"


def voice_precheck(guild, channel):
    """加入前先看權限 / 人數上限；有問題回傳說明文字，沒問題回傳 None"""
    try:
        me = guild.me
        perms = channel.permissions_for(me)
        if perms.connect is False or perms.speak is False:
            return f"機器人沒有加入語音頻道「{md(channel.name)}」的權限（需要「連接」與「說話」）"
        limit = getattr(channel, "user_limit", 0)
        if isinstance(limit, int) and limit > 0 and len(channel.members) >= limit \
                and perms.move_members is not True and me not in channel.members:
            return f"語音頻道「{md(channel.name)}」已經滿了（上限 {limit} 人）"
    except Exception:
        return None
    return None


# ---------- 佇列（/queue）：一頁 10 首，總長度跟網頁一樣「N 首・X 分鐘（以上）」----------
PAGE = 10


# ---------- 跳過 / 停止 / 離開 / 控制面板按鈕：只有跟機器人在同一個語音頻道的人能按 ----------
def _is_owner(user):
    try:
        from .discordauth import is_owner
        return is_owner(getattr(user, "id", None))
    except Exception:
        return False


def voice_gate(interaction, bot_vc=None):
    """可以操作 → None；不行 → 要私下回給按的人的訊息。
    規則：機器人不在語音 → 不限制（沒有人在聽，也就沒有人會被打擾）；擁有者 → 不限制；
    其他人要在機器人所在的語音頻道裡。bot_vc：已知的語音連線（沒給就用 interaction.guild.voice_client）"""
    guild = getattr(interaction, "guild", None)
    vc = bot_vc if bot_vc is not None else getattr(guild, "voice_client", None)
    if not vc or not vc.is_connected():
        return None
    if _is_owner(interaction.user):
        return None
    mine = getattr(getattr(interaction.user, "voice", None), "channel", None)
    if mine is not None and mine == vc.channel:
        return None
    name = getattr(vc.channel, "name", "")
    return f"你要先加入機器人所在的語音頻道「{md(name)}」才能操作" if name else "你要先加入機器人所在的語音頻道才能操作"


def fmt_total(sec):
    m = max(1, round(sec / 60))
    if m < 60:
        return f"{m} 分鐘"
    h, mm = divmod(m, 60)
    return f"{h} 小時 {mm} 分鐘" if mm else f"{h} 小時"


def queue_summary(items):
    """items：[(url, title)]；跟網頁面板的 queueSummary 一致"""
    from .state import known_durations
    n = len(items)
    if not n:
        return "0 首"
    total = unknown = 0
    for u, _ in items:
        d = known_durations.get(u, 0) if isinstance(u, str) else 0
        if d and d > 0:
            total += d
        else:
            unknown += 1
    if unknown == n:
        return f"{n} 首"
    return f"{n} 首・{fmt_total(total)}{'以上' if unknown else ''}"


def queue_pages(gid):
    from .state import queues
    return max(1, math.ceil(len(queues.get(gid) or []) / PAGE))


def queue_embed(gid, page=0):
    from .state import queues, now_playing, known_durations
    q = list(queues.get(gid) or [])
    pages = max(1, math.ceil(len(q) / PAGE))
    page = max(0, min(page, pages - 1))
    cur = now_playing.get(gid)
    desc = f"正在播放：**{md(cur, 150)}**" if cur else "目前沒有在播放"
    embed = discord.Embed(title="播放佇列", description=desc, color=discord.Color.blue())
    lines = []
    for i, (u, t) in enumerate(q[page * PAGE:(page + 1) * PAGE], page * PAGE + 1):
        d = known_durations.get(u, 0) if isinstance(u, str) else 0
        lines.append(f"`{i}.` {md(t, 80)}" + (f" `{format_time(d)}`" if d and d > 0 else ""))
    value = "\n".join(lines) or "（空）"
    embed.add_field(name=f"即將播放（{queue_summary(q)}）", value=value[:1024], inline=False)
    if pages > 1:
        embed.set_footer(text=f"第 {page + 1}／{pages} 頁")
    return embed


class QueuePager(discord.ui.View):
    """佇列翻頁：上一頁 / 下一頁（5 分鐘沒人按就收起按鈕）"""
    def __init__(self, gid, page=0, timeout=300):
        super().__init__(timeout=timeout)
        self.gid = gid
        self.page = page
        self.message = None
        self._sync()

    def _sync(self):
        pages = queue_pages(self.gid)
        self.page = max(0, min(self.page, pages - 1))
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1

    async def _show(self, interaction):
        self._sync()
        await interaction.response.edit_message(embed=queue_embed(self.gid, self.page), view=self)

    @discord.ui.button(label="上一頁", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction, button):
        self.page -= 1
        await self._show(interaction)

    @discord.ui.button(label="下一頁", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page += 1
        await self._show(interaction)

    async def on_timeout(self):
        if self.message is None:
            return
        try:
            await self.message.edit(view=None)
        except Exception:
            pass


# ---------- 例外 → 使用者看得懂的回覆（指令 / 按鈕 / 表單共用）----------
async def reply_error(interaction, text):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception as e:
        log.warning(f"[互動] 錯誤回覆失敗：{e!r}")


GENERIC_ERROR = "⚠️ 出了點問題，這次操作沒有完成，請再試一次"
