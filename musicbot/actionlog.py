"""操作紀錄：誰點歌加進佇列、跳過、刪歌（每個伺服器各一份，只保留 7 天）。

存在 data/action_log.json（.gitignore 已排除 data/）：
    {"v": 1, "guilds": {"<guild_id>": [ {ts, act, who, src, title, url?, n?, detail?}, ... 舊 → 新 ]}}
- act：add 點歌 / skip 跳過 / remove 刪歌
- src：discord（Discord 指令或按鈕）/ web（網頁面板）/ ext（Chrome 擴充功能）
- 一次加很多首（匯入歌單、YouTube 播放清單、整份歌單播放）只記一筆，n = 首數
寫入：先寫暫存檔再 os.replace；每次寫入和讀取都會清掉超過 7 天的，每個伺服器最多 MAX_PER_GUILD 筆。
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import log

FILE = (os.getenv("ACTION_LOG_FILE") or "").strip() or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "action_log.json")
KEEP_DAYS = 7
KEEP_SECONDS = KEEP_DAYS * 86400
MAX_PER_GUILD = 2000
TITLE_MAX = 200
WHO_MAX = 40
ACTIONS = ("add", "skip", "remove")
SOURCES = ("discord", "web", "ext")

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Taipei")
except Exception:           # Windows 沒裝 tzdata 時：台北沒有日光節約，固定 +8
    TZ = timezone(timedelta(hours=8), "Asia/Taipei")

_lock = threading.RLock()
_data = None                # {gid(str): [entry, ...]}；第一次用到才載入
_rev = 0


def _clip(s, n):
    s = " ".join(str(s or "").split())
    return s[:n]


def _prune_locked(now=None):
    """清掉超過 7 天的、每個伺服器只留最新 MAX_PER_GUILD 筆；回傳是否有變動"""
    cutoff = (now or time.time()) - KEEP_SECONDS
    changed = False
    for gid in list(_data):
        items = _data[gid]
        keep = [e for e in items if isinstance(e, dict) and e.get("ts", 0) >= cutoff][-MAX_PER_GUILD:]
        if len(keep) != len(items):
            changed = True
        if keep:
            _data[gid] = keep
        else:
            del _data[gid]
    return changed


def _load_locked():
    global _data
    if _data is not None:
        return
    _data = {}
    try:
        with open(FILE, encoding="utf-8") as f:
            raw = json.load(f)
        guilds = raw.get("guilds") if isinstance(raw, dict) else None
        if isinstance(guilds, dict):
            _data = {str(k): list(v) for k, v in guilds.items() if isinstance(v, list)}
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"[操作紀錄] 讀取失敗，從空白開始：{e!r}")
    if _prune_locked():
        _save_locked()


def _save_locked():
    tmp = f"{FILE}.tmp"
    try:
        os.makedirs(os.path.dirname(FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "guilds": _data}, f, ensure_ascii=False, separators=(",", ":"))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, FILE)
    except Exception as e:
        log.warning(f"[操作紀錄] 寫入失敗：{e!r}")


def record(gid, act, actor, title="", url=None, count=None, detail=None):
    """記一筆。actor = (名字, 來源)；actor 是 None（內部 / 自動操作）就不記。不會丟例外。"""
    global _rev
    try:
        if not actor or not gid or act not in ACTIONS:
            return None
        who, src = actor
        src = src if src in SOURCES else "web"
        e = {"ts": round(time.time(), 3), "act": act, "who": _clip(who, WHO_MAX) or "未知",
             "src": src, "title": _clip(title, TITLE_MAX)}
        if url:
            e["url"] = _clip(url, 500)
        if count is not None and count != 1:
            e["n"] = int(count)
        if detail:
            e["detail"] = _clip(detail, TITLE_MAX)
        with _lock:
            _load_locked()
            _data.setdefault(str(gid), []).append(e)
            _prune_locked(e["ts"])
            _save_locked()
            _rev += 1
        try:
            from .state import notify_change
            notify_change()
        except Exception:
            pass
        return e
    except Exception as ex:      # 紀錄失敗不能影響點歌 / 跳過
        log.warning(f"[操作紀錄] 記錄失敗：{ex!r}")
        return None


def record_add(gid, actor, songs_added, label=None):
    """點歌：一首 → 記歌名；多首 → 只記一筆「N 首」，歌名放第一首、detail 放歌單名稱。
    songs_added = [(url, title), ...]（真的加進佇列的那些）"""
    if not songs_added:
        return None
    if len(songs_added) == 1:
        url, title = songs_added[0]
        return record(gid, "add", actor, title, url=url, detail=label)
    return record(gid, "add", actor, songs_added[0][1], count=len(songs_added), detail=label)


def prune():
    """啟動時呼叫：載入並清掉過期紀錄"""
    with _lock:
        _load_locked()
        if _prune_locked():
            _save_locked()


def rev():
    return _rev


def fmt_time(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%m/%d %H:%M:%S")


def recent(gid, limit=200):
    """新 → 舊，附上台北時間字串"""
    try:
        limit = max(1, min(int(limit), MAX_PER_GUILD))
    except (TypeError, ValueError):
        limit = 200
    with _lock:
        _load_locked()
        if _prune_locked():
            _save_locked()
        items = list(_data.get(str(gid), []))[-limit:]
    out = []
    for e in reversed(items):
        d = dict(e)
        d["time"] = fmt_time(d.get("ts", 0))
        d["date"] = datetime.fromtimestamp(d.get("ts", 0), TZ).strftime("%Y-%m-%d")
        out.append(d)
    return out


def discord_actor(interaction):
    """Discord 指令 / 按鈕：伺服器暱稱"""
    u = getattr(interaction, "user", None)
    return (getattr(u, "display_name", None) or getattr(u, "name", None) or "Discord 使用者", "discord")


def _reset_for_tests(path=None):
    """測試 / 模擬器用：換檔案（不給 = 新的暫存資料夾，不碰專案的 data/）並清掉記憶體快取"""
    global _data, FILE
    if not path:
        import tempfile
        path = os.path.join(tempfile.mkdtemp(prefix="v6act-"), "action_log.json")
    with _lock:
        _data = None
        FILE = path
    return path
