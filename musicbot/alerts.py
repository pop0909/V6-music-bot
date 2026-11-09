"""出事通知：私訊擁有者（.env 的 OWNER_ID，沒設就用 Discord 應用程式擁有者）

- 異常重啟／當機：啟動時讀 data/last_exit.json 判斷上次是否正常關閉
- YouTube 機器人驗證／403 一直發生（10 分鐘內 3 次）
- 連續播放失敗
每種通知 30 分鐘最多 1 則。
"""
import asyncio
import json
import os
import threading
import time

from .config import PROJECT_ROOT, log

RATE_LIMIT = 30 * 60
_WINDOW = 10 * 60
_THRESHOLD = 3
EXIT_FILE = os.path.join(PROJECT_ROOT, "data", "last_exit.json")

_last_sent = {}
_recent = {}
_lock = threading.Lock()
_owner_id = [None]


def _client():
    from .bot import client
    return client


async def _resolve_owner():
    if _owner_id[0]:
        return _owner_id[0]
    raw = (os.getenv("OWNER_ID") or "").strip()
    if raw.isdigit():
        _owner_id[0] = int(raw)
        return _owner_id[0]
    try:
        app = await _client().application_info()
        owner = app.team.owner if app.team and app.team.owner else app.owner
        _owner_id[0] = owner.id if owner else None
    except Exception as e:
        log.warning(f"[通知] 取得擁有者失敗：{e}")
    return _owner_id[0]


async def _send(text):
    oid = await _resolve_owner()
    if not oid:
        log.warning("[通知] 找不到擁有者（可在 .env 設 OWNER_ID），略過私訊")
        return
    try:
        user = await _client().fetch_user(oid)
        await user.send(text[:1900])
        log.info(f"[通知] 已私訊擁有者：{text.splitlines()[0][:80]}")
    except Exception as e:
        log.warning(f"[通知] 私訊失敗：{e}")


def alert(kind, text, force=False):
    """送出通知（任何執行緒都可呼叫）。同 kind 30 分鐘內只送一次。回傳是否有送出。"""
    now = time.time()
    with _lock:
        if not force and now - _last_sent.get(kind, 0) < RATE_LIMIT:
            return False
        _last_sent[kind] = now
    c = _client()
    loop = getattr(c, "loop", None)
    if not loop or loop.is_closed() or not c.is_ready():
        log.warning(f"[通知] bot 未就緒，略過：{text[:80]}")
        return False
    msg = f"🚨 **音樂機器人通知**\n{text}"
    try:
        if threading.current_thread() is threading.main_thread() and _in_loop(loop):
            loop.create_task(_send(msg))
        else:
            asyncio.run_coroutine_threadsafe(_send(msg), loop)
    except Exception as e:
        log.warning(f"[通知] 排程失敗：{e}")
        return False
    return True


def _in_loop(loop):
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


def _count(kind):
    now = time.time()
    with _lock:
        q = [t for t in _recent.get(kind, []) if now - t < _WINDOW]
        q.append(now)
        _recent[kind] = q
        return len(q)


def note_error(err, context=""):
    """記錄 yt-dlp 錯誤；機器人驗證或 403 在 10 分鐘內累積 3 次就通知"""
    from . import ytdl
    msg = str(err or "")
    if ytdl.is_bot_check(err):
        kind, label = "youtube_block", "YouTube 要求機器人驗證（伺服器 IP 被擋，可能需要更新 cookies）"
    elif "403" in msg and ("forbidden" in msg.lower() or "http error" in msg.lower()):
        kind, label = "youtube_403", "YouTube 一直回 403 Forbidden（可能需要更新 yt-dlp 或 cookies）"
    else:
        return
    n = _count(kind)
    if n >= _THRESHOLD:
        alert(kind, f"{label}\n10 分鐘內發生 {n} 次，最近一次在「{context}」：\n```{msg[:300]}```")


def playback_failed(guild_id, fails, last_error=""):
    alert("playback", f"伺服器 {guild_id} 連續 {fails} 首播放失敗，已暫停自動播放。\n```{str(last_error)[:300]}```")


# ---------- 啟動／關閉紀錄 ----------
def _write_exit(data):
    try:
        os.makedirs(os.path.dirname(EXIT_FILE), exist_ok=True)
        with open(EXIT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"[通知] 寫入 {EXIT_FILE} 失敗：{e}")


_startup_reason = [None]


def mark_started():
    """啟動時呼叫：讀上次狀態，記下這次是否為異常重啟，再標記為執行中"""
    prev = None
    try:
        with open(EXIT_FILE, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        pass
    if prev:
        st = prev.get("state")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(prev.get("ts", 0)))
        if st == "running":
            _startup_reason[0] = f"上次（{when} 起）沒有正常關閉：可能被強制結束、記憶體不足（OOM）或主機重開機"
        elif st == "crashed":
            _startup_reason[0] = f"上次在 {when} 當機：{prev.get('reason', '未知錯誤')}"
    _write_exit({"state": "running", "ts": time.time(), "pid": os.getpid()})


def mark_stopped(reason=None):
    if reason:
        _write_exit({"state": "crashed", "ts": time.time(), "reason": str(reason)[:500]})
    else:
        _write_exit({"state": "clean", "ts": time.time()})


def startup_notice():
    """on_ready 第一次時呼叫：若上次是異常結束就私訊"""
    if _startup_reason[0]:
        log.warning(f"[通知] 異常重啟：{_startup_reason[0]}")
        alert("restart", f"機器人已重新啟動，原因：{_startup_reason[0]}")
        _startup_reason[0] = None
