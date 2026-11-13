"""音訊處理：音量平衡（normalize）、開頭淡入、跳過/停止時淡出

- 音量平衡用 ffmpeg 的 dynaudnorm（動態音量平衡）：實測 CPU 只有 loudnorm 的約 1/9，
  適合即時串流；每個伺服器可各自開關，預設由 .env 的 AUDIO_NORMALIZE 決定（未設定 = 開啟）
- 播放中切換也立即生效：和音效等化器一樣由 player.reapply_audio 從目前位置重開 FFmpeg
- 音效等化器（EQ 預設：原音 / 重低音 / 人聲 / 高音清晰 / 夜間）：每個伺服器各自設定、存檔（重啟保留）；
  播放中切換由 player.reapply_audio 用跳轉機制從目前位置重開 FFmpeg（舊來源播到新來源預熱好才換上，不跳歌、不重複）
- 每首歌開頭 0.25 秒淡入（ffmpeg afade）：短到按下去馬上聽得到，又不會爆音
- 跳過 / 停止時先把 PCMVolumeTransformer 的音量在約 0.12 秒內降到 0 再停止，避免突然斷音；
  呼叫端（Web API）在背景執行淡出，不讓 HTTP 回應等它
- 新來源先「預熱」（PrimedSource：在背景執行緒先讀到第一格 PCM）才交給播放執行緒：
  FFmpeg 啟動期間舊歌繼續播（跳轉）/ 播放執行緒不會卡住後一次爆送一堆封包（discord.py 會追趕時間）
"""
import array
import asyncio
import json
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


def filters_current(gid):
    """目前播放中的 FFmpeg 濾鏡是否已經是最新設定（音效 + 音量平衡）"""
    return (applied_eq.get(gid, EQ_DEFAULT) == eq_preset(gid)
            and applied_norm.get(gid, normalize_enabled(gid)) == normalize_enabled(gid))


# ---------- 音效等化器 ----------
# pre = 音色濾鏡（放在音量平衡之前：平衡器會把加強後的整體音量拉回來，不會越調越大聲）
# post = 動態處理（放在平衡之後：夜間模式的壓縮若放前面會被 dynaudnorm 補回去，實測 LRA 幾乎沒變）
# boost = 有加強某段頻率 → 沒開音量平衡時尾端加限幅器，避免削波爆音（平衡器本身已把峰值壓在 0.9）
# 實測（ffmpeg 7.1、3.5 分鐘歌曲）：各預設只比原音多 0.2~0.6 秒 CPU / 首（約 +0.1~0.3% 單核），峰值都在 0 dBFS 以下
EQ_DEFAULT = "off"
EQ_LIMITER = "alimiter=limit=0.95:attack=5:release=50:level=disabled"
EQ_PRESETS = {
    "off":    {"label": "原音", "desc": "不加任何音效", "pre": "", "post": "", "boost": False},
    "bass":   {"label": "重低音", "desc": "低頻 +6 dB（90 Hz 以下更有力）",
               "pre": "bass=g=6:f=90:w=0.7", "post": "", "boost": True},
    "vocal":  {"label": "人聲", "desc": "人聲頻段 +4 dB、壓低悶聲與低頻轟鳴",
               "pre": "highpass=f=80,equalizer=f=250:t=q:w=1:g=-2,equalizer=f=3000:t=q:w=1.2:g=4",
               "post": "", "boost": True},
    "treble": {"label": "高音清晰", "desc": "高頻 +4 dB（5 kHz 以上更清楚）",
               "pre": "treble=g=4:f=5000:w=0.6", "post": "", "boost": True},
    # 夜間：壓縮動態（大聲處變小、小聲處相對清楚）+ 低頻 -3 dB + 峰值限制在 -3 dB；整體比原音小約 4 LU
    "night":  {"label": "夜間", "desc": "壓縮音量起伏、峰值更柔和、低頻減少，適合深夜小聲聽",
               "pre": "",
               "post": "acompressor=threshold=-24dB:ratio=4:attack=10:release=250:knee=6:makeup=6dB,"
                       "bass=g=-3:f=100:w=0.7,alimiter=limit=0.7:attack=5:release=80:level=disabled",
               "boost": False},
}
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "audio_settings.json")
eq_guilds = {}          # guild_id: preset key（只存非預設值）
applied_eq = {}         # guild_id: 目前 FFmpeg 實際使用的 preset
applied_norm = {}       # guild_id: 目前 FFmpeg 實際有沒有開音量平衡
_settings_loaded = False
_settings_lock = threading.Lock()


def _load_settings():
    global _settings_loaded
    if _settings_loaded:
        return
    _settings_loaded = True
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in (data.get("eq") or {}).items():
            if v in EQ_PRESETS and v != EQ_DEFAULT:
                eq_guilds[int(k)] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        from .config import log
        log.warning(f"[等化器] 讀取設定失敗，使用預設：{e}")


def _save_settings():
    data = {"eq": {str(g): v for g, v in eq_guilds.items() if v != EQ_DEFAULT}}
    try:
        with _settings_lock:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        from .config import log
        log.warning(f"[等化器] 儲存設定失敗：{e}")


def eq_preset(gid):
    _load_settings()
    return eq_guilds.get(gid, EQ_DEFAULT)


def set_eq(gid, preset):
    """設定伺服器的 EQ 預設並存檔；回傳 (新預設, 是否有變)。不認得的預設 → ValueError"""
    preset = str(preset or "").strip().lower()
    if preset not in EQ_PRESETS:
        raise ValueError(f"未知的音效：{preset}")
    _load_settings()
    prev = eq_guilds.get(gid, EQ_DEFAULT)
    if preset == EQ_DEFAULT:
        eq_guilds.pop(gid, None)
    else:
        eq_guilds[gid] = preset
    if prev != preset:
        _save_settings()
    return preset, prev != preset


def eq_choices():
    """前端 / 指令用的選項清單（順序固定）"""
    return [{"key": k, "label": v["label"], "desc": v["desc"]} for k, v in EQ_PRESETS.items()]


def build_filter_chain(normalize, fade_in=FADE_IN_SECONDS, eq=EQ_DEFAULT):
    p = EQ_PRESETS.get(eq) or EQ_PRESETS[EQ_DEFAULT]
    parts = []
    if p["pre"]:
        parts.append(p["pre"])
    if normalize:
        parts.append(NORMALIZE_FILTER)
    if p["post"]:
        parts.append(p["post"])
    elif p["boost"] and not normalize:
        parts.append(EQ_LIMITER)
    if fade_in and fade_in > 0:
        # 淡入放在平衡之後：平衡器看到的是原始音量，不會把淡入「補」回來
        parts.append(f"afade=t=in:st=0:d={fade_in:g}")
    return ",".join(parts)


def ffmpeg_options(gid, fade_in=FADE_IN_SECONDS):
    """傳給 discord.FFmpegPCMAudio 的 options（-vn + 濾鏡鏈）"""
    eq = eq_preset(gid)
    norm = normalize_enabled(gid)
    applied_eq[gid] = eq      # 最近一次開 FFmpeg 用的音效 / 音量平衡（播放中切換時用來判斷要不要重開）
    applied_norm[gid] = norm
    chain = build_filter_chain(norm, fade_in=fade_in, eq=eq)
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
        self._fade = None       # [已淡入格數, 總格數]：快轉後接上時做短淡入（快轉掉了 FFmpeg 的 afade）

    def prime(self):
        data = self.original.read()
        self._first = data
        return bool(data)

    def read(self):
        if self._first is not None:
            data, self._first = self._first, None
        else:
            data = self.original.read()
        if self._fade is not None and data:
            data = self._apply_fade(data)
        return data

    def fade_in(self, frames=3):
        self._fade = [0, max(1, int(frames))]

    def _apply_fade(self, data):
        k, total = self._fade
        pcm = array.array("h")
        pcm.frombytes(data[: len(data) - len(data) % 2])
        n = len(pcm)
        start, span = k * (n // 2), total * (n // 2) or 1   # 以「取樣點」計（立體聲左右聲道同一個增益）
        for i in range(n):
            pcm[i] = int(pcm[i] * (start + i // 2) / span)
        k += 1
        self._fade = None if k >= total else [k, total]
        return pcm.tobytes()

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


FRAME_SECONDS = 0.02    # discord.py 每次 read() = 20 ms PCM


async def drop_frames(source, seconds, max_seconds=15.0):
    """把已預熱的來源往前快轉 seconds 秒（讀掉 PCM 不播）。換音效時用：FFmpeg 重開花的時間比預估久，
    舊來源在這段期間已經往前播了 → 新來源丟掉同樣長度，換上時剛好接上（不重複一段）。回傳實際丟掉的秒數。"""
    n = int(min(seconds, max_seconds) / FRAME_SECONDS)
    if n <= 0:
        return 0.0

    def run():
        done = 0
        while done < n:
            if not source.read():
                break
            done += 1
        return done
    done = await _in_thread(run)
    if done and hasattr(source, "fade_in"):
        source.fade_in()          # 60 ms 淡入，避免接點爆音
    return done * FRAME_SECONDS


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