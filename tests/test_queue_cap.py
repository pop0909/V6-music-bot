"""佇列上限（QUEUE_MAX，預設 500）：每條加歌路徑都擋，批次加到滿並回報略過數。"""
import asyncio
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import state, webcore, webapi, extapi, idlesleep, importer  # noqa: E402

G = 5757
U = "https://www.youtube.com/watch?v=%011d"


def songs(n, start=0):
    return [{"url": U % i, "title": "s%d" % i} for i in range(start, start + n)]


class CapTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"QUEUE_MAX": "5", "EXT_TOKEN": "o" * 40})
        self.env.start()
        self.p = [mock.patch.object(webcore._prefetch, "ensure", mock.Mock()),
                  mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=None)),
                  mock.patch.object(webapi, "run_async", lambda c: asyncio.run(c)),
                  mock.patch.object(extapi, "run_async", lambda c: asyncio.run(c))]
        for p in self.p: p.start()
        self.c = webapi.flask_app.test_client()

    def tearDown(self):
        for p in self.p: p.stop()
        self.env.stop()
        for d in (state.queues, state.random_loop_guilds, idlesleep.saved):
            d.pop(G, None)

    def fill(self, n):
        state.queues[G] = [(U % (900 + i), "f") for i in range(n)]

    def test_default_and_env(self):
        with mock.patch.dict(os.environ, {"QUEUE_MAX": ""}):
            os.environ.pop("QUEUE_MAX")
            self.assertEqual(state.queue_max(), 500)
        with mock.patch.dict(os.environ, {"QUEUE_MAX": "abc"}):
            self.assertEqual(state.queue_max(), 500)
        self.assertEqual(state.queue_max(), 5)

    def test_single_add_full(self):
        self.fill(5)
        r = self.c.post("/api/queue/add", json={"guild_id": G, "url": U % 1, "title": "x", "no_play": True})
        self.assertEqual(r.status_code, 409)
        self.assertIn("佇列已滿", r.get_json()["message"])
        self.assertEqual(len(state.queues[G]), 5)

    def test_batch_partial(self):
        self.fill(3)
        j = self.c.post("/api/queue/add-batch", json={"guild_id": G, "songs": songs(4)}).get_json()
        self.assertEqual((j["count"], j["full_skipped"]), (2, 2))
        self.assertIn("2 首沒有加入", j["message"])
        r = self.c.post("/api/queue/add-batch", json={"guild_id": G, "songs": songs(2, 50)})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(state.queues[G]), 5)

    def test_play_all_truncates(self):
        stats = {}
        n = asyncio.run(webcore.web_play_all(G, songs(8), stats=stats))
        self.assertEqual((n, stats["full_skipped"], len(state.queues[G])), (5, 3, 5))

    def test_import_partial(self):
        self.fill(4)
        stats = {}
        with mock.patch.object(webcore, "notify_change", mock.Mock()):
            n = asyncio.run(webcore.web_import_songs(G, songs(3), stats=stats))
        self.assertEqual((n, stats["full_skipped"]), (1, 2))

    def test_import_job_reports_full_skipped(self):
        with mock.patch.object(importer, "resolve", return_value=("清單", songs(3), 0)):
            jid = importer.start_job("https://x", lambda name, s: (1, 2))
            for _ in range(100):
                j = importer.get_job(jid)
                if j and j["status"] != "running":
                    break
                import time; time.sleep(0.01)
        self.assertEqual((j["added"], j["full_skipped"]), (1, 2))

    def test_extension_add_full(self):
        self.fill(5)
        with mock.patch.object(extapi, "_pick_guild", return_value=G):
            r = self.c.post("/api/ext/add", json={"url": "https://youtu.be/BBBBBBBBBBB", "title": "B"},
                            headers={"Host": "musbot.kdns.fr", "X-Ext-Token": "o" * 40})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "queue_full")

    def test_restore_capped(self):
        idlesleep.saved[G] = {"queue": [(U % i, "s") for i in range(9)], "position": 0, "ts": __import__("time").time(), "channel": {}}
        with mock.patch.object(idlesleep, "wake", mock.Mock()):
            asyncio.run(idlesleep.restore(G, play=False))
        self.assertEqual(len(state.queues[G]), 5)


class DiscordPlayCapTest(unittest.IsolatedAsyncioTestCase):
    async def test_play_command_playlist_capped(self):
        from musicbot import player
        with mock.patch.dict(os.environ, {"QUEUE_MAX": "3"}):
            state.queues[G] = [(U % 900, "f")]
            info = {"entries": [{"webpage_url": U % i, "title": "t"} for i in range(5)]}

            class YDL:
                def __init__(self, *a): pass
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def extract_info(self, q, download=False): return info
            it = mock.MagicMock()
            it.response.defer = mock.AsyncMock(); it.followup.send = mock.AsyncMock()
            vc = mock.Mock(); vc.is_playing.return_value = True
            try:
                with mock.patch.object(player.yt_dlp, "YoutubeDL", YDL), \
                     mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=None)):
                    await player._process_and_play(it, "q", G, vc)
                self.assertEqual(len(state.queues[G]), 3)
                self.assertIn("另有 3 首沒加入", it.followup.send.call_args[0][0])
            finally:
                state.queues.pop(G, None)


if __name__ == "__main__":
    unittest.main()
