"""定期備份。執行：python -m unittest discover -s tests"""
import asyncio
import os
import sqlite3
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import backup, alerts  # noqa: E402


class Backup(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "data"))
        os.makedirs(os.path.join(self.root, "favorites"))
        self.w("data/user_playlists.json", "{}")
        self.w("data/ext_members.json", '{"k":1}')
        self.w("data/last_exit.json", "x")
        self.w("favorites/a.txt", "song")
        self.w("list.txt", "l")
        db = sqlite3.connect(os.path.join(self.root, "data", "history.db"))
        db.execute("create table t(x)"); db.execute("insert into t values(1)"); db.commit(); db.close()
        self.bdir = os.path.join(self.root, "backups")

    def w(self, rel, txt):
        with open(os.path.join(self.root, rel), "w") as f:
            f.write(txt)

    def run_b(self, **k):
        k.setdefault("interval", 0)   # 舊測試只測內容 / 保留份數，不受間隔影響
        return backup.run_backup(self.root, self.bdir, **k)

    def test_interval_blocks_restarts_and_unchanged(self):
        D = 7 * 86400
        self.assertIsNotNone(self.run_b(interval=D, now=1000))
        self.w("list.txt", "changed")
        self.assertIsNone(self.run_b(interval=D, now=1000 + 3600))        # 重啟後 1 小時：資料有變也不備份
        self.assertIsNone(self.run_b(interval=D, now=1000 + D - 1))
        self.assertIsNotNone(self.run_b(interval=D, now=1000 + D))        # 滿 7 天且有變 → 備份
        self.assertIsNone(self.run_b(interval=D, now=1000 + 3 * D))       # 滿間隔但沒變 → 跳過
        with open(os.path.join(self.bdir, ".last_backup")) as f:
            self.assertEqual(float(f.read()), 1000 + D)

    def test_last_time_falls_back_to_newest_archive(self):
        D = 7 * 86400
        p = self.run_b()
        os.remove(os.path.join(self.bdir, ".last_backup"))
        os.utime(p, (5000, 5000))
        self.w("list.txt", "changed")
        self.assertIsNone(self.run_b(interval=D, now=5000 + 60))
        self.assertEqual(backup.next_due(self.bdir, D), 5000 + D)

    def test_interval_env(self):
        with mock.patch.dict(os.environ, {"BACKUP_INTERVAL_DAYS": "3"}):
            self.assertEqual(backup._interval_days(), 3.0)
        with mock.patch.dict(os.environ, {"BACKUP_INTERVAL_DAYS": "x"}):
            self.assertEqual(backup._interval_days(), 7.0)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BACKUP_INTERVAL_DAYS", None)
            self.assertEqual(backup._interval_days(), 7.0)

    def test_create_contents_and_mode(self):
        p = self.run_b()
        self.assertTrue(p and p.endswith(".tar.gz"))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        with tarfile.open(p) as t:
            names = set(t.getnames())
            self.assertIn("data/history.db", names)
            self.assertIn("favorites/a.txt", names)
            self.assertIn("list.txt", names)
            self.assertNotIn("data/last_exit.json", names)
            x = os.path.join(self.root, "restore"); t.extractall(x)
        db = sqlite3.connect(os.path.join(x, "data", "history.db"))
        self.assertEqual(db.execute("select x from t").fetchone()[0], 1); db.close()

    def test_skip_when_unchanged(self):
        self.assertIsNotNone(self.run_b())
        self.w("data/last_exit.json", "changed")   # 不算資料
        self.assertIsNone(self.run_b())
        self.w("data/user_playlists.json", '{"a":1}')
        self.assertIsNotNone(self.run_b())

    def test_keep_seven(self):
        with mock.patch.object(backup, "_stamp", side_effect=[f"data-2026010{i}-000000.tar.gz" if i < 10 else f"data-202601{i}-000000.tar.gz" for i in range(1, 11)]):
            for i in range(10):
                self.w("list.txt", str(i)); self.run_b()
        fs = sorted(f for f in os.listdir(self.bdir) if f.endswith(".tar.gz"))
        self.assertEqual(len(fs), 7)
        self.assertEqual(fs[0], "data-20260104-000000.tar.gz")

    def test_offsite_dm_with_warning(self):
        p = self.run_b()
        user = mock.MagicMock(); user.send = mock.AsyncMock()
        cl = mock.MagicMock(); cl.fetch_user = mock.AsyncMock(return_value=user)
        with mock.patch.object(alerts, "_resolve_owner", mock.AsyncMock(return_value=1)), \
             mock.patch.object(alerts, "_client", return_value=cl):
            self.assertTrue(asyncio.run(backup.send_offsite(p)))
        args, kw = user.send.call_args
        self.assertIn("金鑰", args[0]); self.assertIn("file", kw)

    def test_offsite_too_big_alerts(self):
        p = self.run_b()
        with mock.patch.object(backup, "DM_LIMIT", 1), mock.patch.object(alerts, "alert") as al:
            self.assertFalse(asyncio.run(backup.send_offsite(p)))
        self.assertEqual(al.call_args[0][0], "backup_big")
