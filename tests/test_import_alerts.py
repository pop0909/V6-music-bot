"""面板網域限制、歌單匯入、出事通知。執行：python -m unittest discover -s tests"""
import os
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import webapi, alerts, importer  # noqa: E402


class HostCheck(unittest.TestCase):
    def setUp(self):
        self.c = webapi.flask_app.test_client()

    def code(self, host):
        return self.c.get("/api/guilds", headers={"Host": host}).status_code

    def test_allowed(self):
        for h in ("musbot.kdns.fr", "MUSBOT.kdns.fr", "localhost:5000", "127.0.0.1:5000"):
            self.assertNotEqual(self.code(h), 403, h)

    def test_rejected(self):
        for h in ("103.252.119.45:5000", "103.252.119.45", "evil.example.com"):
            self.assertEqual(self.code(h), 403, h)


class Importer(unittest.TestCase):
    def test_detect(self):
        self.assertTrue(importer.is_spotify("https://open.spotify.com/intl-zh/playlist/abc123"))
        self.assertTrue(importer.looks_like_playlist("https://www.youtube.com/watch?v=x&list=PL1"))
        self.assertFalse(importer.looks_like_playlist("https://www.youtube.com/watch?v=x"))

    def test_spotify_resolve_parallel(self):
        tracks = [(f"t{i}", "a") for i in range(5)]
        with mock.patch.object(importer, "_spotify_tracks", return_value=("L", tracks)), \
             mock.patch.object(importer, "_yt_search_one",
                               side_effect=lambda t, a: None if t == "t2" else {"url": "u" + t, "title": t, "duration": 1}):
            prog = []
            name, songs, skipped = importer.resolve("https://open.spotify.com/playlist/x", progress=lambda d, t: prog.append(d))
        self.assertEqual((name, len(songs), skipped), ("L", 4, 1))
        self.assertEqual([s["title"] for s in songs], ["t0", "t1", "t3", "t4"])
        self.assertEqual(max(prog), 5)


class Alerts(unittest.TestCase):
    def setUp(self):
        alerts._last_sent.clear(); alerts._recent.clear()

    def test_rate_limit_and_threshold(self):
        with mock.patch.object(alerts, "alert", wraps=lambda k, t, force=False: True) as al:
            for _ in range(2):
                alerts.note_error(Exception("ERROR: Sign in to confirm you're not a bot"), "x")
            al.assert_not_called()
            alerts.note_error(Exception("Sign in to confirm you’re not a bot"), "x")
            self.assertEqual(al.call_args[0][0], "youtube_block")
            alerts.note_error(Exception("HTTP Error 403: Forbidden"), "x")
            alerts.note_error(Exception("random"), "x")
            self.assertEqual(al.call_count, 1)

    def test_alert_once_per_30min(self):
        fake = mock.Mock(); fake.loop = None
        with mock.patch.object(alerts, "_client", return_value=fake):
            alerts.alert("k", "a")
            self.assertFalse(alerts.alert("k", "b"))
            alerts._last_sent["k"] = time.time() - 31 * 60
            alerts.alert("k", "c")
            self.assertGreater(alerts._last_sent["k"], time.time() - 5)

    def test_exit_marker(self):
        with mock.patch.object(alerts, "EXIT_FILE", os.path.join(os.path.dirname(__file__), "_exit_test.json")):
            try:
                alerts.mark_stopped(); alerts.mark_started()
                self.assertIsNone(alerts._startup_reason[0])
                alerts.mark_started()   # 上次沒關就又啟動 → 異常
                self.assertIn("沒有正常關閉", alerts._startup_reason[0])
                alerts.mark_stopped("RuntimeError: boom"); alerts.mark_started()
                self.assertIn("boom", alerts._startup_reason[0])
            finally:
                alerts._startup_reason[0] = None
                os.remove(alerts.EXIT_FILE)


if __name__ == "__main__":
    unittest.main()
