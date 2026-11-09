"""音訊處理：音量平衡（normalize）、開頭淡入、跳過/停止時淡出

- 音量平衡用 ffmpeg 的 dynaudnorm（動態音量平衡）：實測 CPU 只有 loudnorm 的約 1/9，
  適合即時串流；每個伺服器可各自開關，預設由 .env 的 AUDIO_NORMALIZE 決定（未設定 = 開啟）
- 開關在「下一首」生效（ffmpeg 串流開始後無法換濾鏡）
- 每首歌開頭 0.25 秒淡入（ffmpeg afade）：短到按下去馬上聽得到，又不會爆音
- 跳過 / 停止時先把 PCMVolumeTransformer 的音量在約 0.12 秒內降到 0 再停止，避免突然斷音；
  呼叫端（Web API）在背景執行淡出，不讓 HTTP 回應等它
- 新來源先「預熱」（PrimedSource：在背景執行緒先讀到第一格 PCM）才交給播放執行緒：
  FFmpeg 啟動期間舊歌繼續播（跳轉）/ 播放執行緒不會卡住後一次爆送一堆封包（discord.py 會追趕時間）
"""
import asyncio
import os
import threading

import discord

from .state import volume_transformers

NORMALIZE_DEFAULT = (os.getenv("AUDIO_NORMALIZE", "1").strip().lower() not in ("0", "false", "off", "no"))
# f=150ms 視窗、g=15 個視窗平滑：反應比預設快、前瞻緩衝約 1 秒；p=峰值上限、m=最大增益倍數
# b=1（alternative boundary mode）：開頭直接套用算好的增益。預設模式會從增益 1 慢慢爬升，
#   實測每首歌開頭 / 每次跳轉後要約 1.5 秒才到正常音量（聽起來像按下去要等一下才「出來」），b=1 只要約 0.08 秒
NORMALIZE_FILTER = "dynaudnorm=f=150:g=15:p=0.9:m=10:b=1"
FADE_IN_SECONDS = 0.25
FADE_OUT_SECONDS = 0.12
SEEK_FADE_IN_SECONDS = 0.05   # 跳轉後只做極短淡入（避免爆音）
# -multiple_requests 1：跳轉時 FFmpeg 要對同一個網址連發數個 Range 請求（標頭 / Cues / 目標位置），
# 沿用同一條 HTTP keep-alive 連線，省掉每次重新 TCP + TLS 握手
# -rw_timeout 30 秒：輸入端 30 秒拿不到資料就報錯（重連 / 結束），不會無限期卡住變成靜音（2026-11-05 後段斷流）
RECONNECT_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -multiple_requests 1 -rw_timeout 30000000"
PRIME_TIMEOUT = 35.0   # 冷啟動連續 403 時分段代理可能多次 yt-dlp 重解析，15 秒不夠

normalize_guilds = {}   # guild_id: bool
_fading = {}            # guild_id: True（淡出進行中）


def normalize_enabled(gid):
    return normalize_guilds.get(gid, NORMALIZE_DEFAULT)


def set_normalize(gid, enabled):
    normalize_guilds[gid] = bool(enabled)
    return normalize_guilds[gid]


def build_filter_chain(normalize, fade_in=FADE_IN_SECONDS):
    parts = []
    if normalize:
        parts.append(NORMALIZE_FILTER)
    if fade_in and fade_in > 0:
        # 淡入放在平衡之後：平衡器看到的是原始音量，不會把淡入「補」回來
        parts.append(f"afade=t=in:st=0:d={fade_in:g}")
    return ",".join(parts)


def ffmpeg_options(gid, fade_in=FADE_IN_SECONDS):
    """傳給 discord.FFmpegPCMAudio 的 options（-vn + 濾鏡鏈）"""
    chain = build_filter_chain(normalize_enabled(gid), fade_in=fade_in)
    return f"-vn -af {chain}" if chain else "-vn"


def before_options(seek=0):
    """傳給 FFmpegPCMAudio 的 before_options：-ss 放在 -i 之前 = 輸入端跳轉（HTTP Range，幾乎立即）"""
    if seek and seek > 0:
        return f"-ss {seek:.3f} {RECONNECT_OPTIONS}"
    return RECONNECT_OPTIONS


class PrimedSource(discord.AudioSource):
    """包住 FFmpegPCMAudio：prime() 先在背景執行緒讀出第一格 PCM，之後 read() 先吐這格。
    播放執行緒拿到它時 FFmpeg 已經在出聲，不會在 read() 卡住幾百毫秒（卡住期間 = 無聲 / 跳轉時舊歌被切掉），
    也不會因為卡住而在之後連發封包追趕時間（Discord 端的 jitter buffer 會丟掉 → 歌頭被吃掉）。"""

    def __init__(self, source):
        self.original = source
        self._first = None

    def prime(self):
        data = self.original.read()
        self._first = data
        return bool(data)

    def read(self):
        if self._first is not None:
            data, self._first = self._first, None
            return data
        return self.original.read()

    def is_opus(self):
        return False

    def cleanup(self):
        self.original.cleanup()


async def prime_source(source, timeout=PRIME_TIMEOUT):
    """回傳 PrimedSource（已讀到第一格）。FFmpeg 沒有輸出（網址失效等）→ 收掉並丟出 RuntimeError。"""
    ps = PrimedSource(source)
    try:
        ok = await asyncio.wait_for(_in_thread(ps.prime), timeout)
    except BaseException:
        _in_thread(ps.cleanup)
        raise
    if not ok:
        _in_thread(ps.cleanup)
        raise RuntimeError("FFmpeg 沒有輸出音訊（串流網址可能已失效）")
    return ps


def _in_thread(fn):
    """在專用的短命執行緒跑 fn，回傳 asyncio Future。
    不用預設 executor：yt-dlp 解析 / 預先解析 / FFmpeg 收尾都擠在那裡，排隊時跳轉 / 換歌會多等好幾百毫秒"""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def run():
        try:
            r = fn()
        except BaseException as e:
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_exception(e))
        else:
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(r))

    threading.Thread(target=run, daemon=True, name="audio-prime").start()
    return fut


async def fade_out_stop(gid, vc, duration=FADE_OUT_SECONDS, step=0.02, stop=True):
    """把目前歌曲音量在 duration 秒內降到 0 再 vc.stop()。
    暫停中 / 沒有 transformer / 已在淡出 → 直接停止。只改這首的 transformer，不動使用者設定的音量（volumes）。
    stop=False：只淡出不停止（給「停止並清空」用：呼叫端先清狀態再 stop，避免 after_play 接著播下一首）"""
    if vc is None:
        return
    tr = volume_transformers.get(gid)
    faded = False
    if duration > 0 and tr is not None and vc.is_playing() and not _fading.get(gid):
        faded = True
        _fading[gid] = True
        try:
            start = tr.volume
            steps = max(1, int(duration / step))
            for i in range(1, steps + 1):
                if volume_transformers.get(gid) is not tr or not vc.is_playing():
                    break   # 期間已換歌 / 被暫停 / 被別人停止
                tr.volume = start * (1 - i / steps)
                await asyncio.sleep(duration / steps)
        finally:
            _fading.pop(gid, None)
    if stop and (vc.is_playing() or vc.is_paused()):
        # 淡出期間已換成別首（例如另一個請求先停掉、下一首已開播）→ 不要把新歌也停掉，
        # 也不要留下 manual_advance（否則單曲循環下一次自然播完會被誤判成跳過）
        if faded and volume_transformers.get(gid) is not tr:
            return
        # 使用者主動切歌（跳過 / 指定播放）：單曲循環時也要前進，不是重播
        from .state import manual_advance
        manual_advance[gid] = True
        vc.stop()


def is_fading(gid):
    return bool(_fading.get(gid))


# ==================== 音質：Opus 編碼位元率跟著語音頻道走 ====================
MAX_BITRATE_KBPS = 510          # Opus 上限（discord.py Encoder 允許 16..512）
DEFAULT_BITRATE_KBPS = 128      # 讀不到頻道資訊時（discord.py 2.x 預設）
current_bitrate = {}            # guild_id: kbps  目前播放用的編碼位元率（顯示用）


def target_bitrate(vc):
    """語音頻道位元率（bps），以伺服器加成等級上限（guild.bitrate_limit）與 510k 封頂，回傳 kbps"""
    try:
        ch = vc.channel
        bps = int(getattr(ch, "bitrate", 0) or 0)
        g = getattr(ch, "guild", None)
        lim = int(getattr(g, "bitrate_limit", 0) or 0) if g is not None else 0
        if lim:
            bps = min(bps, lim)
        kbps = bps // 1000
    except Exception:
        kbps = 0
    if kbps <= 0:
        return DEFAULT_BITRATE_KBPS
    return max(16, min(MAX_BITRATE_KBPS, kbps))