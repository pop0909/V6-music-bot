"""所有 guild 狀態的單一真相來源（以 guild_id 為 key 的 dict + 全域鎖）"""
import threading

state_lock = threading.RLock()

# ---- 狀態變更通知（SSE 事件驅動推送）----
# 任何會改變播放狀態的地方呼叫 notify_change()；SSE 產生器用 wait_change() 等待，
# 有變更就立刻推送（不必等下一個 1 秒 tick），沒變更時仍以 1 秒為上限更新進度。
_change_cond = threading.Condition()
_change_ver = 0


def notify_change():
    global _change_ver
    with _change_cond:
        _change_ver += 1
        _change_cond.notify_all()


def change_version():
    return _change_ver


def wait_change(last_ver, timeout):
    """等到版本號不同於 last_ver 或逾時；回傳目前版本號"""
    with _change_cond:
        if _change_ver == last_ver:
            _change_cond.wait(timeout)
        return _change_ver

queues = {}                 # guild_id: [(url, title)]
now_playing = {}            # guild_id: title
voice_clients = {}          # guild_id: VoiceClient
random_loop_guilds = {}     # guild_id: bool
volumes = {}                # guild_id: float
volume_transformers = {}    # guild_id: PCMVolumeTransformer
control_panels = {}         # guild_id: Message — Discord 控制面板訊息
now_playing_url = {}
now_playing_thumbnail = {}
song_start_times = {}       # guild_id: timestamp（播放中才有）
song_durations = {}
paused_position = {}        # guild_id: 秒數（暫停時才有）
progress_versions = {}
progress_tasks = {}         # guild_id: Task
guild_names = {}
active_guild_id = None      # 當前所有 Web 使用者看到的伺服器
current_voice_channel = {}  # guild_id: {"id": int, "name": str} — 避開直接存取 VoiceClient
idle_leave_tasks = {}       # guild_id: Task — 閒置自動離開排程

# 用於 Web API 讀取的簡化狀態（避免跨執行緒讀取 VoiceClient）
is_playing_state = {}       # guild_id: bool
is_paused_state = {}        # guild_id: bool

# 循環播放模式
loop_modes = {}             # guild_id: "none" | "one" | "all"
saved_queues = {}           # guild_id: [(url, title)] — loop_all 時保存的佇列備份

# 睡眠計時器： guild_id: {"mode": "minutes"|"track_end", "deadline": timestamp 或 None, "task": Task|None}
sleep_timers = {}

local_playlist = []         # list.txt 歌單

# 連續播放失敗次數（防止 YouTube 403 / 單曲循環壞歌時無限重試洗頻）
play_failures = {}          # guild_id: int
# play_next 的 per-guild 鎖（防止兩個請求同時開播把歌吃掉）
play_locks = {}             # guild_id: asyncio.Lock

# 給 Web 顯示的最近一次播放問題（例如 YouTube 機器人驗證）：guild_id: {"kind", "message", "ts"}
play_notices = {}
# 歌曲來源資訊（佇列項目仍是 (url, title)，額外資訊放這裡）：guild_id: {url: {"requester", "source"}}
song_meta = {}


def note_song_meta(gid, url, requester=None, source="user"):
    """記錄某首歌是誰點的 / 是否為自動推薦（播放紀錄用）"""
    m = song_meta.setdefault(gid, {})
    m.pop(url, None)
    m[url] = {"requester": requester, "source": source}
    while len(m) > 500:
        m.pop(next(iter(m)))

# 跳轉（seek）用
# 暫停中跳轉：只記位置，恢復播放時才用新位置重開 FFmpeg：guild_id: {"pos", "orig", "version"}
pending_seeks = {}
# 目前歌曲的串流網址（跳轉時重開 FFmpeg 用）：guild_id: {"page", "url", "expires_at"}
now_playing_stream = {}

# 已知的歌曲長度（佇列總長度用，跨 guild 共用）：url: 秒數
known_durations = {}
MAX_KNOWN_DURATIONS = 5000


def remember_duration(url, duration):
    """記下某首歌的長度（只收 0 < 秒數 < 24 小時的合理值）"""
    try:
        d = int(float(duration or 0))
    except (TypeError, ValueError):
        return
    if not url or not (0 < d < 86400):
        return
    known_durations.pop(url, None)
    known_durations[url] = d
    while len(known_durations) > MAX_KNOWN_DURATIONS:
        known_durations.pop(next(iter(known_durations)))

# 循環模式輔助狀態
loop_current = {}           # guild_id: (url, title)  單曲循環要重播的那首（目前歌曲）
manual_advance = {}         # guild_id: True  使用者按了跳過 / 指定播放 → 下一次 play_next 不重播


# ==================== 佇列上限（.env 的 QUEUE_MAX，預設 500） ====================
QUEUE_MAX_DEFAULT = 500


def queue_max():
    import os
    try:
        return max(1, int(os.getenv("QUEUE_MAX", str(QUEUE_MAX_DEFAULT)).strip()))
    except ValueError:
        return QUEUE_MAX_DEFAULT


def queue_room(gid):
    """佇列還能放幾首"""
    return max(0, queue_max() - len(queues.get(gid) or []))


def queue_full_message():
    return f"佇列已滿（上限 {queue_max()} 首），請先播完或移除一些歌再加"


def queue_skip_message(skipped):
    return f"佇列已滿（上限 {queue_max()} 首），另有 {skipped} 首沒有加入" if skipped else ""
