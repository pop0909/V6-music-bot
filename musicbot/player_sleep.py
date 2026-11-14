"""睡眠計時器（N 分鐘後 / 播完這首停止）。從 player.py 拆出，行為不變。

留在 player.py 的函式與測試會替換的名字（player.X）一律經 `_p.X` 在呼叫時取用：
測試 mock.patch.object(player, ...) 照樣生效，也避免循環 import（_p 在檔案最後才 import）。
"""
import asyncio
import time


from .bot import client
from .config import log
from .state import voice_clients, sleep_timers



# ================== 睡眠計時器 ==================
async def _sleep_timer_fire(gid):
    """計時器到期：停止播放、清佇列、離開語音"""
    try:
        await asyncio.sleep(1)  # 讓 set 呼叫先完成
        sleep_timers.pop(gid, None)
        log.info(f"[睡眠計時器] {gid} 時間到，停止播放")
        vc = voice_clients.get(gid) or (client.get_guild(gid).voice_client if client.get_guild(gid) else None)
        await _p._clear_guild_state(gid)
        if vc:
            vc.stop()
            await vc.disconnect(force=True)
        await _p._update_panel(gid, "😴 睡眠計時器時間到，已停止播放並離開", view=None, remove=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error(f"sleep timer error: {e}")


def set_sleep_timer(gid, mode, minutes=0):
    """設定睡眠計時器。mode: 'minutes' | 'track_end' | 'off'
    回傳 (success, 訊息)
    """
    cancel_sleep_timer(gid)
    if mode == "off":
        return True, "已取消睡眠計時器"
    entry = {"mode": mode, "deadline": None, "task": None}
    if mode == "minutes":
        entry["deadline"] = time.time() + minutes * 60

        async def _timer():
            try:
                await asyncio.sleep(minutes * 60)
                # 到時間了：若還在播就整組停止
                await _sleep_timer_fire(gid)
            except asyncio.CancelledError:
                pass

        entry["task"] = asyncio.create_task(_timer())
        sleep_timers[gid] = entry
        return True, f"😴 {minutes} 分鐘後將停止播放並離開"
    elif mode == "track_end":
        # 在 play_next 取完下一首歌時檢查（旗標式，不需 task）
        sleep_timers[gid] = entry
        return True, "😴 播完目前這首後將停止"
    return False, "無效模式"


def cancel_sleep_timer(gid):
    old = sleep_timers.pop(gid, None)
    if old and old.get("task"):
        old["task"].cancel()


def check_track_end_sleep(gid):
    """play_next 開始播新歌前呼叫：若 track_end 計時器生效，回傳 True 表示應停止"""
    info = sleep_timers.get(gid)
    if info and info.get("mode") == "track_end":
        sleep_timers.pop(gid, None)

        async def _fire():
            await _sleep_timer_fire(gid)
        _p._keep(_fire())
        return True
    return False


from . import player as _p  # noqa: E402  放最後：player 載入完才取用（見上方說明）
