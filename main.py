"""
🎵 Music Bot V5 — 入口
啟動順序：載入歌單 → 背景執行緒跑 Flask → 主執行緒跑 Discord bot

模組結構：
  musicbot/config.py   設定（logging、env、yt-dlp）
  musicbot/state.py    所有 guild 狀態 + 全域鎖
  musicbot/utils.py    clean_str / format_time
  musicbot/storage.py  list.txt 歌單與最愛清單
  musicbot/bot.py      Discord client 與事件
  musicbot/views.py    按鈕面板與 Modal
  musicbot/player.py   播放引擎核心
  musicbot/commands.py slash 指令
  musicbot/webcore.py  Web 控制輔助函數
  musicbot/webapi.py   Flask API + SSE
"""
import os
import threading

# 先 import bot（建立 client），再 import commands（註冊 slash 指令）
from musicbot.bot import run as run_bot, client
from musicbot import commands as _commands  # noqa: F401 — 觸發指令註冊
from musicbot.config import log
from musicbot.storage import load_local_playlist
from musicbot.config import FAVORITES_DIR
from musicbot.webapi import start_flask


if __name__ == "__main__":
    log.info("=" * 30)
    log.info("Music Bot V5 啟動中...")
    log.info("=" * 30)

    # 載入歌單
    load_local_playlist()
    os.makedirs(FAVORITES_DIR, exist_ok=True)
    # 操作紀錄：啟動時清掉超過 7 天的
    from musicbot import actionlog
    actionlog.prune()

    # 啟動 Flask 網頁伺服器（背景執行緒）
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # 啟動 Discord 機器人（主執行緒，封鎖）
    log.info("[Discord] 正在連線...")
    from musicbot import alerts
    alerts.mark_started()

    import signal
    def _on_term(signum, frame):
        # systemctl stop / restart：視為正常關閉；先記下語音頻道與佇列，重啟後自動接回
        try:
            from musicbot import idlesleep
            idlesleep.save_session()
        except Exception:
            pass
        alerts.mark_stopped()
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, AttributeError):
        pass
    try:
        run_bot()
    except (KeyboardInterrupt, SystemExit):
        alerts.mark_stopped()
        raise
    except BaseException as e:
        log.critical(f"[主程式] 當機：{e!r}", exc_info=True)
        alerts.mark_stopped(f"{type(e).__name__}: {e}")
        raise
    else:
        alerts.mark_stopped()
