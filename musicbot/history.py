"""播放紀錄與排行（SQLite：data/history.db）

- 每首歌播放超過 30 秒或一半長度（取較短者）才記一次
- 寫入走單一背景執行緒（不阻塞 event loop），每筆寫入是一個交易（原子性），WAL 模式讓網頁讀取不會被寫入卡住
- 讀取（網頁 REST / Discord 指令）每次開自己的唯讀連線
"""
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import PROJECT_ROOT, log

DB_PATH = os.getenv("HISTORY_DB") or os.path.join(PROJECT_ROOT, "data", "history.db")
MIN_SECONDS = 30
MIN_FRACTION = 0.5

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="history-db")
_init_lock = threading.Lock()
_initialized = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    thumbnail TEXT DEFAULT '',
    duration INTEGER DEFAULT 0,
    requester TEXT,
    source TEXT DEFAULT 'user',
    played_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plays_guild_time ON plays(guild_id, played_at);
CREATE INDEX IF NOT EXISTS idx_plays_guild_url ON plays(guild_id, url);
"""


def _connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with _init_lock:
            if path not in _initialized or not _has_table(conn):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                conn.commit()
                _initialized.add(path)
    except Exception:
        conn.close()
        raise
    return conn


def _has_table(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plays'").fetchone() is not None


def threshold_seconds(duration):
    """要播多久才算一次：30 秒或一半長度（較短者）；長度未知 → 30 秒"""
    if duration and duration > 0:
        return min(MIN_SECONDS, duration * MIN_FRACTION)
    return MIN_SECONDS


def _insert(row, path=None):
    conn = _connect(path)
    try:
        with conn:   # 單一交易：成功就整筆寫入，失敗就整筆回滾
            conn.execute(
                "INSERT INTO plays (guild_id, url, title, thumbnail, duration, requester, source, played_at) "
                "VALUES (:guild_id, :url, :title, :thumbnail, :duration, :requester, :source, :played_at)", row)
    finally:
        conn.close()


def record_sync(guild_id, url, title, thumbnail="", duration=0, requester=None, source="user",
                played_at=None, path=None):
    row = {
        "guild_id": int(guild_id), "url": str(url), "title": str(title or url)[:300],
        "thumbnail": str(thumbnail or "")[:500], "duration": int(duration or 0),
        "requester": (str(requester)[:100] if requester else None),
        "source": source if source in ("user", "autoplay") else "user",
        "played_at": float(played_at if played_at is not None else time.time()),
    }
    _insert(row, path)
    return row


def record(guild_id, url, title, **kw):
    """非阻塞：丟到背景執行緒寫入，回傳 Future"""
    def _job():
        try:
            return record_sync(guild_id, url, title, **kw)
        except Exception as e:
            log.error(f"[播放紀錄] 寫入失敗: {e}")
            return None
    return _executor.submit(_job)


def _rows(sql, params, path=None):
    conn = _connect(path)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def recent(guild_id, limit=50, path=None):
    limit = max(1, min(int(limit or 50), 200))
    return _rows("SELECT url, title, thumbnail, duration, requester, source, played_at FROM plays "
                 "WHERE guild_id = ? ORDER BY played_at DESC, id DESC LIMIT ?", (int(guild_id), limit), path)


RANGES = {"7d": 7 * 86400, "30d": 30 * 86400, "all": None}


def top(guild_id, range_key="all", limit=20, path=None, now=None):
    limit = max(1, min(int(limit or 20), 100))
    secs = RANGES.get(range_key, None)
    since = ((now or time.time()) - secs) if secs else 0
    # 標題/縮圖取最近一次播放的那筆（歌名可能被編輯過）
    return _rows(
        "SELECT p.url, COUNT(*) AS plays, MAX(p.played_at) AS last_played, "
        "(SELECT title FROM plays q WHERE q.guild_id = p.guild_id AND q.url = p.url ORDER BY q.played_at DESC LIMIT 1) AS title, "
        "(SELECT thumbnail FROM plays q WHERE q.guild_id = p.guild_id AND q.url = p.url ORDER BY q.played_at DESC LIMIT 1) AS thumbnail, "
        "MAX(p.duration) AS duration "
        "FROM plays p WHERE p.guild_id = ? AND p.played_at >= ? "
        "GROUP BY p.url ORDER BY plays DESC, last_played DESC LIMIT ?",
        (int(guild_id), since, limit), path)


def known_durations(path=None, limit=5000):
    """每首歌（網址）記錄過的最長長度：啟動時預先載入，網頁佇列標頭算總長度用"""
    return _rows("SELECT url, MAX(duration) AS duration FROM plays WHERE duration > 0 "
                 "GROUP BY url ORDER BY MAX(played_at) DESC LIMIT ?", (int(limit),), path)


def preload_durations():
    """背景執行：把播放紀錄裡的歌曲長度載入記憶體（失敗只記 log）"""
    from .state import remember_duration
    def _job():
        try:
            rows = known_durations()
            for r in reversed(rows):
                remember_duration(r["url"], r["duration"])
            log.info(f"[播放紀錄] 已載入 {len(rows)} 首歌的長度")
        except Exception as e:
            log.error(f"[播放紀錄] 載入歌曲長度失敗: {e}")
    return _executor.submit(_job)


def flush(timeout=5):
    """測試 / 關機用：等背景寫入全部完成"""
    _executor.submit(lambda: None).result(timeout=timeout)
