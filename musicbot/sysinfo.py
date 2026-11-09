"""狀態頁資料、一鍵更新 yt-dlp、Discord 狀態列（正在聆聽）"""
import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time

import discord

from .config import FFMPEG_PATH, log

START_TIME = time.time()
UPDATE_COOLDOWN = 10 * 60
_update = {"running": False, "last": 0.0, "result": None}
_update_lock = threading.Lock()

try:
    import psutil
    _proc = psutil.Process()
    psutil.cpu_percent(None)
    _proc.cpu_percent(None)
except Exception:          # psutil 沒裝也能跑，只是少 CPU / 記憶體
    psutil = None
    _proc = None


def _ytdlp_version():
    try:
        import yt_dlp.version
        return yt_dlp.version.__version__
    except Exception:
        return None


_ffmpeg_cache = {"t": 0, "v": None}


def _ffmpeg():
    if time.time() - _ffmpeg_cache["t"] < 600:
        return _ffmpeg_cache["v"]
    path = shutil.which(FFMPEG_PATH) or (FFMPEG_PATH if os.path.isfile(FFMPEG_PATH) else None)
    info = {"ok": bool(path), "path": path, "version": None}
    if path:
        try:
            out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=5).stdout
            info["version"] = (out.split("\n", 1)[0].split(" ")[2:3] or [None])[0]
        except Exception:
            pass
    _ffmpeg_cache.update(t=time.time(), v=info)
    return info


def _ms(sec):
    """秒 → 毫秒；None / NaN / inf（尚未收到心跳）→ None"""
    if sec is None or sec != sec or sec in (float("inf"), float("-inf")):
        return None
    return round(sec * 1000)


def voice_latency_ms(gid=None):
    """語音 WebSocket 延遲（ms）：指定伺服器的語音連線，否則第一個已連線的；不在語音 → None"""
    from .bot import client
    vcs = [vc for vc in client.voice_clients if vc.is_connected()]
    if gid:
        vcs = [vc for vc in vcs if vc.guild and vc.guild.id == gid] or []
    for vc in vcs:
        try:
            v = _ms(vc.average_latency)
            if v is None:
                v = _ms(vc.latency)
            if v is not None:
                return v
        except Exception:
            continue
    return None


def status(gid=None):
    from .bot import client
    lat = client.latency if client.is_ready() else None
    data = {
        "uptime": int(time.time() - START_TIME),
        "latency_ms": round(lat * 1000) if lat is not None and lat == lat and lat != float("inf") else None,
        "voice_latency_ms": voice_latency_ms(gid),
        "ready": client.is_ready(),
        "guilds": len(client.guilds),
        "voice": sum(1 for vc in client.voice_clients if vc.is_connected()),
        "playing": sum(1 for vc in client.voice_clients if vc.is_connected() and vc.is_playing()),
        "ytdlp": _ytdlp_version(),
        "ffmpeg": _ffmpeg(),
        "python": sys.version.split()[0],
        "systemd": bool(os.getenv("INVOCATION_ID")),
        "update": {"running": _update["running"], "result": _update["result"],
                   "cooldown": max(0, int(UPDATE_COOLDOWN - (time.time() - _update["last"])))},
    }
    if psutil:
        vm = psutil.virtual_memory()
        data.update(cpu=psutil.cpu_percent(None), proc_cpu=round(_proc.cpu_percent(None), 1),
                    mem_used=vm.used, mem_total=vm.total, mem_pct=vm.percent,
                    proc_rss=_proc.memory_info().rss)
    return data


def start_update():
    """更新 venv 裡的 yt-dlp，成功後經 systemd 重啟。回傳 (ok, message)。"""
    with _update_lock:
        if _update["running"]:
            return False, "更新已在進行中"
        wait = UPDATE_COOLDOWN - (time.time() - _update["last"])
        if wait > 0:
            return False, f"剛更新過，請 {int(wait // 60) + 1} 分鐘後再試"
        _update.update(running=True, last=time.time(), result=None)
    threading.Thread(target=_do_update, daemon=True).start()
    return True, "開始更新"


def _do_update():
    before = _ytdlp_version()
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-U", "yt-dlp"],
                           capture_output=True, text=True, timeout=240)
        after = subprocess.run([sys.executable, "-c", "import yt_dlp.version as v;print(v.__version__)"],
                               capture_output=True, text=True, timeout=30).stdout.strip() or before
        if r.returncode != 0:
            res = {"ok": False, "message": "pip 更新失敗", "detail": (r.stderr or r.stdout)[-600:],
                   "before": before, "after": after}
        else:
            res = {"ok": True, "before": before, "after": after, "restart": False,
                   "message": f"yt-dlp {before} → {after}" if after != before else f"yt-dlp 已是最新（{after}）"}
            if os.getenv("INVOCATION_ID") and shutil.which("systemctl"):
                res["restart"] = True
                res["message"] += "，2 秒後重新啟動"
            elif after != before:
                res["message"] += "（不是 systemd 執行，請手動重新啟動 bot 才會生效）"
    except Exception as e:
        res = {"ok": False, "message": f"更新失敗：{e}", "before": before}
    log.info(f"[更新] {res.get('message')}")
    _update.update(running=False, result=res)
    if res.get("restart"):
        time.sleep(2)
        unit = os.getenv("MUSICBOT_UNIT", "musicbot")
        subprocess.Popen(["systemctl", "restart", unit], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------- Discord 狀態列：正在聆聽 <歌名> ----------
PRESENCE_INTERVAL = 15   # 秒；最多每 15 秒更新一次，避免撞 Discord rate limit
_presence_task = [None]


def _current_title():
    from .bot import client
    from .state import now_playing
    for vc in client.voice_clients:
        gid = vc.guild.id
        if vc.is_connected() and vc.is_playing() and now_playing.get(gid):
            return str(now_playing[gid])[:120]
    return None


_presence_kick = [None]


def kick_presence():
    """休眠 / 醒來時立刻更新一次狀態列（不用等下一輪）"""
    ev = _presence_kick[0]
    if ev is not None:
        try:
            ev.set()
        except Exception:
            pass


async def _presence_loop():
    from .bot import client
    from . import idlesleep
    _presence_kick[0] = asyncio.Event()
    last = object()
    last_change = 0.0
    while True:
        try:
            title = _current_title()
            sleeping = title is None and idlesleep.all_asleep()
            key = ("sleep",) if sleeping else title
            if key != last and time.time() - last_change >= 5:
                if sleeping:
                    await client.change_presence(status=discord.Status.idle,
                                                 activity=discord.CustomActivity(name="💤 休眠中"))
                elif title:
                    await client.change_presence(status=discord.Status.online,
                                                 activity=discord.Activity(type=discord.ActivityType.listening, name=title))
                else:
                    await client.change_presence(status=discord.Status.online, activity=None)
                last = key
                last_change = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug(f"[狀態列] 更新失敗：{e}")
        # 休眠時拉長間隔（只等喚醒事件），醒著時最多每 15 秒檢查一次
        try:
            await asyncio.wait_for(_presence_kick[0].wait(), timeout=PRESENCE_INTERVAL * (8 if last == ("sleep",) else 1))
        except asyncio.TimeoutError:
            pass
        _presence_kick[0].clear()


def start_presence():
    t = _presence_task[0]
    if t is None or t.done():
        _presence_task[0] = asyncio.get_running_loop().create_task(_presence_loop())
