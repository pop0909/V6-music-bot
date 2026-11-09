"""本機歌單（list.txt）與各伺服器最愛清單的存取"""
import os

from .config import LIST_FILE, FAVORITES_DIR, log
from .state import local_playlist, state_lock
from .utils import clean_str


def _one_line(text):
    """避免網址/標題中的換行把 list.txt 一行拆成多行"""
    return " ".join(str(text).split())


def _atomic_write_lines(path, items):
    """先寫暫存檔再 os.replace，避免寫到一半當機把檔案清空"""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for s in items:
            url = _one_line(s["url"]).replace(" ", "%20")
            f.write(f"{url} {_one_line(s['title'])}\n")
    os.replace(tmp, path)


def load_local_playlist():
    with state_lock:
        local_playlist.clear()
        try:
            with open(LIST_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if " " in line:
                            url, title = line.split(maxsplit=1)
                        else:
                            url = line
                            title = url
                        local_playlist.append({"url": url, "title": clean_str(title)})
            log.info(f"[歌單] 已載入 {len(local_playlist)} 首")
        except FileNotFoundError:
            log.warning(f"找不到 {LIST_FILE}")
        except Exception as e:
            log.error(f"載入 list.txt 錯誤: {e}")


def save_local_playlist():
    try:
        _atomic_write_lines(LIST_FILE, local_playlist)
        return True
    except Exception as e:
        log.error(f"儲存 list.txt 錯誤: {e}")
        return False


favorites_cache = {}


def _get_fav_path(guild_id):
    return os.path.join(FAVORITES_DIR, f"{guild_id}.txt")


def load_favorites(guild_id):
    if guild_id in favorites_cache:
        return favorites_cache[guild_id]
    favs = []
    path = _get_fav_path(guild_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split(" ", 1)
                    url = parts[0]
                    title = parts[1] if len(parts) > 1 else url
                    favs.append({"url": url, "title": clean_str(title)})
    except FileNotFoundError:
        pass
    favorites_cache[guild_id] = favs
    return favs


def save_favorites(guild_id):
    favs = favorites_cache.get(guild_id, [])
    path = _get_fav_path(guild_id)
    os.makedirs(FAVORITES_DIR, exist_ok=True)
    try:
        _atomic_write_lines(path, favs)
    except Exception as e:
        log.error(f"儲存最愛錯誤: {e}")


def add_favorite_song(guild_id, url, title):
    favs = load_favorites(guild_id)
    for f in favs:
        if f["url"] == url:
            return False
    favs.append({"url": url, "title": title})
    favorites_cache[guild_id] = favs
    save_favorites(guild_id)
    return True
