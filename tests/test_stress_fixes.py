"""壓力測試找到的問題：亂填參數回 400、網址 / 標題驗證、音量回傳夾在範圍內、成員名字型別、播完清掉長度。"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import webapi, webcore, state, player, extmembers  # noqa: E402
from musicbot.utils import valid_song_url, safe_title  # noqa: E402

G = 8686


class InputTest(unittest.TestCase):
    def setUp(self):
        self.c = webapi.flask_app.test_client()

    def tearDown(self):
        for d in (state.queues, state.volumes):
            d.pop(G, None)

    def test_bad_params_are_400_not_500(self):
        for path, body in [("/api/volume", {"guild_id": G, "level": "abc"}), ("/api/play", {"guild_id": "x"}),
                           ("/api/queue/remove", {"guild_id": G, "index": "x"}), ("/api/sleep-timer", {"guild_id": G, "mode": "minutes", "minutes": "x"}),
                           ("/api/queue/add", [1, 2])]:
            r = self.c.post(path, json=body)
            self.assertEqual(r.status_code, 400, (path, r.get_json()))

    def test_volume_response_clamped(self):
        self.assertEqual(self.c.post("/api/volume", json={"guild_id": G, "level": 10**9}).get_json()["volume"], 200)
        self.assertEqual(self.c.post("/api/volume", json={"guild_id": G, "level": -50}).get_json()["volume"], 0)

    def test_url_and_title_validation(self):
        self.assertTrue(valid_song_url("https://www.youtube.com/watch?v=AAAAAAAAAAA"))
        for bad in ("javascript:alert(1)", "not a url", "", None, 5, "https://x/" + "a" * 3000, "https://a b"):
            self.assertFalse(valid_song_url(bad), bad)
        self.assertEqual(safe_title(None, "https://u"), "https://u")
        self.assertEqual(len(safe_title("💥" * 3000)), 200)
        self.assertEqual(safe_title("  a \n b "), "a b")

    def test_add_rejects_bad_url(self):
        r = self.c.post("/api/queue/add", json={"guild_id": G, "url": "javascript:alert(1)", "title": "x", "no_play": True})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(state.queues.get(G))

    def test_batch_skips_bad_entries(self):
        songs = [{"url": "https://www.youtube.com/watch?v=AAAAAAAAAAA", "title": None}, {"url": "nope"}, "str", {"url": 5}]
        import asyncio
        with mock.patch.object(webcore._prefetch, "ensure", mock.Mock()):
            count = asyncio.run(webcore.web_add_to_queue_batch(G, songs))
        self.assertEqual(count, 1)
        self.assertEqual(state.queues[G][0][1], "https://www.youtube.com/watch?v=AAAAAAAAAAA")


class MemberNameTest(unittest.TestCase):
    def test_non_string_name_rejected(self):
        with self.assertRaises(ValueError):
            extmembers.create({"a": 1})


class EndClearsDurationTest(unittest.IsolatedAsyncioTestCase):
    async def test_queue_end_clears_duration(self):
        state.song_durations[G] = 200
        state.now_playing_url[G] = "u"
        state.queues[G] = []
        try:
            with mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=mock.Mock())), \
                 mock.patch.object(player, "_autoplay_pick", mock.AsyncMock(return_value=None)), \
                 mock.patch.object(player, "_update_panel", mock.AsyncMock()):
                await player._play_next_locked(G)
            self.assertNotIn(G, state.song_durations)
            self.assertEqual(webcore.get_guild_status(G)["duration"], 0)
        finally:
            for d in (state.song_durations, state.now_playing_url, state.queues):
                d.pop(G, None)


if __name__ == "__main__":
    unittest.main()
