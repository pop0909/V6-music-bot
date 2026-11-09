"""定期自動備份 data/、favorites/、list.txt

- 最多每 BACKUP_INTERVAL_DAYS 天（.env，預設 7）備份一次：距上次成功備份滿間隔「且」內容有變才做
- 上次成功備份時間存在 backups/.last_backup（沒有就看最新一份備份檔的時間），重啟不會多備份
- 啟動後 10 分鐘先檢查一次，之後每 6 小時檢查；內容的 SHA-256 跟上次一樣就跳過
- 存成 backups/data-YYYYmmdd-HHMMSS.tar.gz（權限 0600），只留最新 7 份
- 異地備份：私訊擁有者附檔（小於 8MB 才傳），提醒內含成員金鑰
- 只是背景的 asyncio sleep＋一次 tar，休眠模式下也不會喚醒語音
還原：systemctl stop musicbot; tar xzf backups/<檔名> -C /root/v6; systemctl start musicbot
"""
import asyncio
import hashlib
import io
import os
import sqlite3
import tarfile
import tempfile
import time

from .config import PROJECT_ROOT, log

BACKUP_DIR = os.path.join(PROJECT_ROOT, "backups")
SOURCES = ("data", "favorites", "list.txt")
SKIP = {"last_exit.json", "voice_session.json"}   # 每次啟動都會變，不算資料
KEEP = 7



def _interval_days():
    try:
        return max(0.0, float(os.getenv("BACKUP_INTERVAL_DAYS", "7")))
    except ValueError:
        return 7.0


MIN_INTERVAL = _interval_days() * 86400   # 兩次備份最短間隔（秒）
INTERVAL = 6 * 3600                       # 多久檢查一次
FIRST_DELAY = 10 * 60
DM_LIMIT = 8 * 1024 * 1024
HASH_FILE = os.path.join(BACKUP_DIR, ".last_hash")


def _read(path):
    if path.endswith(".db"):   # SQLite 用 backup API 取一致快照
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            src = sqlite3.connect(path)
            dst = sqlite3.connect(tmp)
            with dst:
                src.backup(dst)
            src.close(); dst.close()
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            os.remove(tmp)
    with open(path, "rb") as f:
        return f.read()


def collect(root=None):
    """回傳 [(相對路徑, bytes)]，依路徑排序"""
    root = root or PROJECT_ROOT
    out = []
    for s in SOURCES:
        p = os.path.join(root, s)
        if os.path.isfile(p):
            out.append((s, _read(p)))
        elif os.path.isdir(p):
            for dp, _, fs in os.walk(p):
                for fn in fs:
                    if fn in SKIP or fn.endswith((".tmp", "-journal")):
                        continue
                    full = os.path.join(dp, fn)
                    out.append((os.path.relpath(full, root).replace(os.sep, "/"), _read(full)))
    out.sort()
    return out


def digest(files):
    h = hashlib.sha256()
    for name, data in files:
        h.update(name.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def _stamp():
    return time.strftime("data-%Y%m%d-%H%M%S.tar.gz")


def last_backup_time(bdir):
    """上次成功備份的 epoch 秒；記錄檔沒有就用最新備份檔的修改時間；都沒有回傳 None"""
    try:
        with open(os.path.join(bdir, ".last_backup")) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        pass
    try:
        fs = [os.path.join(bdir, f) for f in os.listdir(bdir) if f.startswith("data-") and f.endswith(".tar.gz")]
    except FileNotFoundError:
        return None
    return max((os.path.getmtime(f) for f in fs), default=None)


def next_due(bdir=None, interval=None):
    """下次可以備份的 epoch 秒（None = 現在就可以）"""
    bdir = bdir or BACKUP_DIR
    last = last_backup_time(bdir)
    return None if last is None else last + (MIN_INTERVAL if interval is None else interval)


def run_backup(root=None, backup_dir=None, force=False, interval=None, now=None):
    """做一次備份。回傳新檔路徑；未滿間隔或內容沒變則回傳 None"""
    root = root or PROJECT_ROOT
    bdir = backup_dir or os.path.join(root, "backups")
    hfile = os.path.join(bdir, ".last_hash")
    now = time.time() if now is None else now
    due = next_due(bdir, interval)
    if not force and due is not None and now < due:
        return None
    files = collect(root)
    hx = digest(files)
    try:
        with open(hfile) as f:
            if not force and f.read().strip() == hx:
                return None
    except FileNotFoundError:
        pass
    os.makedirs(bdir, mode=0o700, exist_ok=True)
    os.chmod(bdir, 0o700)
    name = _stamp()
    path = os.path.join(bdir, name)
    old = os.umask(0o077)
    try:
        with tarfile.open(path + ".tmp", "w:gz") as tar:
            for rel, data in files:
                ti = tarfile.TarInfo(rel)
                ti.size = len(data); ti.mtime = int(time.time()); ti.mode = 0o600
                tar.addfile(ti, io.BytesIO(data))
        os.chmod(path + ".tmp", 0o600)
        os.replace(path + ".tmp", path)
        with open(hfile, "w") as f:
            f.write(hx)
        with open(os.path.join(bdir, ".last_backup"), "w") as f:
            f.write(str(now))
    finally:
        os.umask(old)
    olds = sorted(f for f in os.listdir(bdir) if f.startswith("data-") and f.endswith(".tar.gz"))
    for f in olds[:-KEEP]:
        os.remove(os.path.join(bdir, f))
    log.info(f"[備份] 已建立 {name}（{os.path.getsize(path)} bytes，{len(files)} 個檔）")
    return path


async def send_offsite(path):
    import discord
    from . import alerts
    size = os.path.getsize(path)
    if size > DM_LIMIT:
        alerts.alert("backup_big", f"備份檔 {os.path.basename(path)} 有 {size // 1024} KB，超過私訊附件上限，只存在伺服器。")
        return False
    oid = await alerts._resolve_owner()
    if not oid:
        return False
    user = await alerts._client().fetch_user(oid)
    await user.send(
        f"🗄️ **定期資料備份** {os.path.basename(path)}（{max(1, size // 1024)} KB）\n"
        "⚠️ 內含成員金鑰與個人歌單，請勿轉傳。還原：停機後 `tar xzf 檔名 -C /root/v6` 再啟動。",
        file=discord.File(path))
    log.info("[備份] 已私訊擁有者異地備份")
    return True


async def loop_task():
    await asyncio.sleep(FIRST_DELAY)
    while True:
        try:
            path = await asyncio.to_thread(run_backup)
            if path:
                await send_offsite(path)
        except Exception as e:
            log.warning(f"[備份] 失敗：{e!r}")
            try:
                from . import alerts
                alerts.alert("backup_fail", f"定期備份失敗：{e!r}")
            except Exception:
                pass
        await asyncio.sleep(INTERVAL)


_started = [False]


def start(client):
    if not _started[0]:
        _started[0] = True
        client.loop.create_task(loop_task())
