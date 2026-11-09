"""閒置休眠：語音頻道沒有真人、或 bot 閒著（沒在播、佇列空）超過 IDLE_SLEEP_MINUTES（預設 2 分鐘）
→ 記下佇列與播放位置、強制離開語音、停掉背景工作（預先解析、進度、自動推薦、狀態列），標成「休眠中」。
任何點歌 / 加入語音 / 網頁操作都會立刻喚醒；之後可用網頁的「繼續播放」繼續上次的佇列與位置。
"""
import asyncio
import os
import time

from .config import log
from .state import (
    loop_modes,
    queues, now_playing, now_playing_url, voice_clients, current_voice_channel,
    is_playing_state, is_paused_state, notify_change,
)


def _grace_seconds():
    try:
        return max(0.25, float(os.getenv("IDLE_SLEEP_MINUTES", "2"))) * 60
    except ValueError:
        return 120


CHECK_INTERVAL = 15
SAVE_TTL = 6 * 3600          # 存下來的佇列保留 6 小時

asleep = {}                  # gid: {"since": ts}
saved = {}                   # gid: {"queue": [(url,title)], "position": float, "channel": {...}, "ts": ts}
_idle_since = {}
_task = [None]


def _connected(gid):
    from .bot import client
    g = client.get_guild(gid) if hasattr(client, "get_guild") else None
    vc = g.voice_client if g else None
    return bool(vc and vc.is_connected())


def is_asleep(gid=None):
    """在休眠清單裡，或根本不在語音中（啟動後還沒人用）都算休眠"""
    if gid is None:
        return bool(asleep)
    if gid in asleep:
        return True
    try:
        return not _connected(gid)
    except Exception:
        return False


def all_asleep():
    """沒有任何語音連線 → 整個 bot 可以降速（SSE 15 秒心跳、狀態列休眠）"""
    from .bot import client
    return not any(vc.is_connected() for vc in client.voice_clients)


def saved_info(gid):
    s = saved.get(gid)
    if not s or time.time() - s["ts"] > SAVE_TTL:
        saved.pop(gid, None)
        return None
    return {"count": len(s["queue"]), "title": s["queue"][0][1] if s["queue"] else None,
            "position": int(s.get("position") or 0), "ts": int(s["ts"])}


def _with_current_first(gid, q, cur_url, cur_title):
    """把目前歌曲放到最前面（恢復時先播它）；全部循環時佇列尾端那份目前歌曲拿掉，避免重複"""
    q = list(q)
    if loop_modes.get(gid) == "all" and q and q[-1][0] == cur_url:
        q.pop()
    if not q or q[0][0] != cur_url:
        q.insert(0, (cur_url, cur_title or cur_url))
    return q


def _is_idle(vc):
    """這個語音連線算不算「沒人在用」"""
    ch = vc.channel
    humans = [m for m in (ch.members if ch else []) if not m.bot]
    if not humans:
        return True
    gid = vc.guild.id
    from .state import play_locks
    lock = play_locks.get(gid)
    if lock is not None and lock.locked():
        return False        # 正在解析 / 開播下一首（佇列已 pop、還沒開始播）→ 不算閒置
    return not vc.is_playing() and not vc.is_paused() and not queues.get(gid)


async def go_to_sleep(gid, guild, reason="閒置"):
    from .player import leave_voice, _elapsed, _update_panel
    vc = guild.voice_client
    cur_url = now_playing_url.get(gid)
    cur_title = now_playing.get(gid)
    q = list(queues.get(gid) or [])
    pos = 0.0
    if cur_url and (vc and (vc.is_playing() or vc.is_paused())):
        pos = float(_elapsed(gid) or 0)
        q = _with_current_first(gid, q, cur_url, cur_title)
    if q:
        saved[gid] = {"queue": q, "position": pos, "ts": time.time(), "loop_mode": loop_modes.get(gid),
                      "channel": dict(current_voice_channel.get(gid) or {})}
    await leave_voice(gid, guild)
    clear_session(gid)          # 休眠 = 刻意離開，重啟後不要自動回去
    asleep[gid] = {"since": time.time(), "reason": reason}
    _idle_since.pop(gid, None)
    log.info(f"[休眠] {guild.name}：{reason}，已離開語音並進入休眠（保留 {len(q)} 首）")
    try:
        await _update_panel(gid, "💤 休眠中：沒人在聽，已離開語音。點歌或 /join 就會醒來，網頁面板可恢復上次的佇列", view=None, remove=True)
    except Exception:
        pass
    _refresh_presence()
    notify_change()


def wake(gid, reason=""):
    """喚醒（點歌、加入語音、網頁操作時呼叫）。回傳是否原本在休眠。"""
    _idle_since.pop(gid, None)
    if asleep.pop(gid, None) is None:
        return False
    log.info(f"[休眠] {gid} 醒來{('：' + reason) if reason else ''}")
    _refresh_presence()
    notify_change()
    return True


async def restore(gid, play=True):
    """把休眠前的佇列放回去（接在目前佇列前面），play=True 時從上次的位置開始播"""
    s = saved.pop(gid, None)
    if not s or time.time() - s["ts"] > SAVE_TTL:
        return {"status": "error", "message": "沒有可恢復的佇列"}
    from .webcore import ensure_voice_connected
    from .player import play_next, seek
    wake(gid, "恢復佇列")
    if s.get("loop_mode") and gid not in loop_modes:
        loop_modes[gid] = s["loop_mode"]
    if not current_voice_channel.get(gid) and s.get("channel"):
        current_voice_channel[gid] = s["channel"]
    existing = {u for u, _ in queues.get(gid, [])}
    from .state import queue_max
    queues[gid] = ([x for x in s["queue"] if x[0] not in existing] + list(queues.get(gid, [])))[:queue_max()]
    vc = await ensure_voice_connected(gid)
    if not vc:
        saved[gid] = s
        return {"status": "error", "message": "無法加入語音頻道"}
    started = False
    if play and not vc.is_playing() and not vc.is_paused():
        await play_next(gid)
        started = True
        pos = s.get("position") or 0
        if pos > 5:
            for _ in range(40):                 # 等開播後再跳到上次的位置
                if vc.is_playing():
                    break
                await asyncio.sleep(0.25)
            try:
                await seek(gid, pos)
            except Exception as e:
                log.info(f"[休眠] 恢復位置失敗：{e}")
    notify_change()
    return {"status": "ok", "count": len(s["queue"]), "started": started}


def _refresh_presence():
    try:
        from . import sysinfo
        sysinfo.kick_presence()
    except Exception:
        pass


async def check_once(now=None):
    from .bot import client
    now = now or time.time()
    grace = _grace_seconds()
    for vc in list(client.voice_clients):
        if not vc.is_connected() or not vc.guild:
            continue
        gid = vc.guild.id
        if _is_idle(vc):
            since = _idle_since.setdefault(gid, now)
            if now - since >= grace:
                humans = [m for m in (vc.channel.members if vc.channel else []) if not m.bot]
                await go_to_sleep(gid, vc.guild, "語音頻道沒有人" if not humans else "閒置沒有播放")
        else:
            _idle_since.pop(gid, None)


async def _loop():
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)
            await check_once()
            from .bot import client
            if any(vc.is_connected() for vc in client.voice_clients):
                save_session()      # 每 15 秒記一次（意外當機也能接回去）
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"[休眠] 檢查失敗：{e!r}")


def start():
    t = _task[0]
    if t is None or t.done():
        _task[0] = asyncio.get_running_loop().create_task(_loop())


# ---------- 重啟後自動回到語音（部署重啟無縫接續） ----------
SESSION_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "voice_session.json")
SESSION_MAX_AGE = 30 * 60


def snapshot():
    """目前所有語音連線的頻道 + 佇列 + 播放位置（純讀記憶體，可在任何執行緒呼叫）"""
    from .bot import client
    from .player import _elapsed
    out = {}
    for vc in list(getattr(client, "voice_clients", [])):
        try:
            if not vc.is_connected() or not vc.channel:
                continue
            gid = vc.guild.id
            q = list(queues.get(gid) or [])
            cur = now_playing_url.get(gid)
            pos = 0.0
            if cur and (vc.is_playing() or vc.is_paused()):
                pos = float(_elapsed(gid) or 0)
                q = _with_current_first(gid, q, cur, now_playing.get(gid))
            out[str(gid)] = {"loop_mode": loop_modes.get(gid), "channel": {"id": vc.channel.id, "name": vc.channel.name},
                             "queue": q, "position": pos, "playing": bool(cur)}
        except Exception:
            continue
    return out


def save_session():
    import json
    try:
        data = {"ts": time.time(), "guilds": snapshot()}
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        tmp = SESSION_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, SESSION_FILE)
    except Exception as e:
        log.warning(f"[語音續播] 儲存失敗：{e}")


def clear_session(gid):
    import json
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("guilds", {}).pop(str(gid), None) is not None:
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


async def resume_after_restart():
    """啟動時：上次（30 分鐘內）在語音裡、而且頻道裡還有人 → 自動加入並從上次位置續播；沒人就不加入（尊重休眠）"""
    import json
    from .bot import client
    from .player import connect_voice
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        log.warning("[語音續播] voice_session.json 格式錯誤，略過")
        return
    try:
        if time.time() - float(data.get("ts") or 0) > SESSION_MAX_AGE:
            return
    except (TypeError, ValueError):
        return
    guilds = data.get("guilds")
    if not isinstance(guilds, dict):
        return
    for gid_s, info in guilds.items():
        try:
            gid = int(gid_s)
            chan = info.get("channel") if isinstance(info, dict) else None
            cid = int(chan["id"]) if isinstance(chan, dict) and chan.get("id") is not None else None
        except (TypeError, ValueError, KeyError, AttributeError):
            log.warning(f"[語音續播] 略過格式錯誤的紀錄：{str(gid_s)[:40]}")
            continue
        guild = client.get_guild(gid)
        ch = guild.get_channel(cid) if guild and cid else None
        if not ch:
            continue
        humans = [m for m in ch.members if not m.bot]
        if info.get("loop_mode") in ("none", "one", "all"):
            loop_modes[gid] = info["loop_mode"]      # 重啟後循環模式不會跑掉
        q = [(str(x[0]), str(x[1])[:200]) for x in (info.get("queue") or [])
             if isinstance(x, (list, tuple)) and len(x) >= 2 and isinstance(x[0], str) and x[0]]
        if q:
            try:
                pos = max(0.0, float(info.get("position") or 0))
            except (TypeError, ValueError):
                pos = 0.0
            saved[gid] = {"queue": q, "position": pos,
                          "ts": time.time(), "channel": {"id": ch.id, "name": ch.name}}
        if not humans:
            log.info(f"[語音續播] {ch.name} 沒有人，不自動加入（佇列保留，可在網頁恢復）")
            asleep[gid] = {"since": time.time(), "reason": "重啟時頻道沒人"}
            continue
        try:
            await connect_voice(guild, ch)
            log.info(f"[語音續播] 重啟後自動回到 {ch.name}")
            if gid in saved and info.get("playing"):
                await restore(gid, play=True)
        except Exception as e:
            log.warning(f"[語音續播] 回到 {ch.name} 失敗：{e!r}")
