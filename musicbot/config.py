"""設定：logging、環境變數、yt-dlp 選項、常數"""
import os
import sys
import logging

from dotenv import load_dotenv

# 終端機非 UTF-8（例如 Windows cp950、Linux 的 LANG=C / POSIX）時改用 UTF-8 輸出：必須在 logging.basicConfig 之前設定，
# 否則 logging 的 StreamHandler 已綁定舊的 stderr，emoji/日文歌名仍會 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    if _stream and _stream.encoding and _stream.encoding.lower() not in ("utf-8", "utf8"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

# ================== Logging ==================
# Linux 伺服器上建議搭配 systemd（journalctl -u musicbot 看日誌）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("musicbot")
# 降低 discord/httpx 冗餘日誌
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise ValueError("找不到 DISCORD_TOKEN！請在 .env 檔案中設定")

# 公開網址（供 Discord /web 指令顯示，建議搭配 Cloudflare Tunnel 使用）
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://musbot.kdns.fr")

# 限制 Discord 輸出到指定頻道（所有非暫態訊息集中在這裡）
OUTPUT_CHANNEL_ID = 842947987033686037

# slash 指令同步的測試伺服器（可在 .env 用 SYNC_GUILD_ID 覆寫；設 0 = 不做測試伺服器同步）
try:
    SYNC_GUILD_ID = int(os.getenv("SYNC_GUILD_ID", "626741812790427659").strip() or 0)
except ValueError:
    SYNC_GUILD_ID = 626741812790427659

# 以專案根目錄為基準（不依賴啟動時的工作目錄；systemd / cron / 其他目錄啟動都能找到）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LIST_FILE = os.path.join(PROJECT_ROOT, "list.txt")
FAVORITES_DIR = os.path.join(PROJECT_ROOT, "favorites")

# ffmpeg 執行檔（預設從 PATH 找；Linux 通常是 /usr/bin/ffmpeg，可用 FFMPEG_PATH 覆寫）
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

# 網頁面板監聽位址（預設與舊版相同 0.0.0.0:5000；只走 Cloudflare Tunnel 時可設 WEB_HOST=127.0.0.1）
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
try:  # .env 打錯（如 WEB_PORT=5000a / 空字串 / 超出範圍）不要讓整個 bot 起不來
    WEB_PORT = int((os.getenv("WEB_PORT") or "5000").strip())
    if not 1 <= WEB_PORT <= 65535: raise ValueError
except ValueError:
    WEB_PORT = 5000

ydl_opts = {
    "format": "bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio/best",   # 優先 Opus（webm），免二次轉碼損失
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "extract_flat": "in_playlist",
    "playlistend": 50,
    "noplaylist": False,
}

ydl_opts_single = ydl_opts.copy()
ydl_opts_single["extract_flat"] = False
ydl_opts_single["noplaylist"] = True


# 預設音量（0.0~2.0），可用 .env 的 DEFAULT_VOLUME 覆寫，例如 0.01 = 1%、20 = 20%
def _parse_default_volume():
    raw = os.getenv("DEFAULT_VOLUME", "0.01").strip()
    try:
        v = float(raw)
    except ValueError:
        return 0.01
    if v > 2:
        v = v / 100.0
    return max(0.0, min(2.0, v))

DEFAULT_VOLUME = _parse_default_volume()
