"""Audit 4：voice_session.json 格式錯誤、休眠與開播競爭、cookies 過期、Spotify 中止、持久化面板。"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import idlesleep, ytdl, importer, views, state, bot  # noqa: E402

G = 77001


class FakeCh:
    id = 55; name = "VC"
    def __init__(self, humans=0):
        self.members = [mock.Mock(bot=False) for _ in range(humans)]


class Resume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.f = os.path.join(self.tmp, "vs.json")
        self.p = mock.patch.object(idlesleep, "SESSION_FILE", self.f); self.p.start()
        self.ch = FakeCh(0)
        guild = mock.Mock(); guild.get_channel = lambda cid: self.ch if cid == 55 else None
        self.c = mock.patch.object(bot.client, "get_guild", lambda gid: guild if gid == G else None)
        self.c.start()

    def tearDown(self):
        self.p.stop(); self.c.stop()
        for d in (idlesleep.saved, idlesleep.asleep, state.loop_modes): d.pop(G, None)

    def run_with(self, data):
        with open(self.f, "w", encoding="utf-8") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))
        asyncio.run(idlesleep.resume_after_restart())

    def test_garbage_shapes_do_not_raise(self):
        import time
        for d in ("[]", "null", "{", {"ts": "x"}, {"ts": time.time(), "guilds": []},
                  {"ts": time.time(), "guilds": {"abc": {}, str(G): {"channel": None}}},
                  {"ts": time.time(), "guilds": {str(G): "str"}}):
            self.run_with(d)

    def test_bad_entries_skipped_but_good_guild_kept(self):
        import time
        self.run_with({"ts": time.time(), "guilds": {
            "notint": {"channel": {"id": 55}},
            str(G): {"channel": {"id": "55"}, "loop_mode": "weird", "position": "bad",
                     "queue": [["https://youtu.be/a", "A"], "junk", [1, 2], ["https://youtu.be/b"]]}}})
        self.assertEqual(idlesleep.saved[G]["queue"], [("https://youtu.be/a", "A")])
        self.assertEqual(idlesleep.saved[G]["position"], 0.0)
        self.assertNotIn(G, state.loop_modes)
        self.assertIn(G, idlesleep.asleep)


class SleepRace(unittest.TestCase):
    def test_not_idle_while_loading_next(self):
        vc = mock.Mock(); vc.guild.id = G; vc.channel = FakeCh(1)
        vc.is_playing.return_value = False; vc.is_paused.return_value = False
        state.queues.pop(G, None)
        self.assertTrue(idlesleep._is_idle(vc))
        async def go():
            lock = state.play_locks.setdefault(G, asyncio.Lock())
            async with lock:
                return idlesleep._is_idle(vc)
        self.assertFalse(asyncio.run(go()))
        state.play_locks.pop(G, None)


class Cookies(unittest.TestCase):
    def test_expired_cookie_detected(self):
        self.assertTrue(ytdl.is_bot_check("ERROR: The provided YouTube account cookies are no longer valid."))


class SpotifyAbort(unittest.TestCase):
    def test_bot_check_stops_remaining(self):
        calls = []
        def search(t, a):
            calls.append(t)
            raise RuntimeError("Sign in to confirm you're not a bot")
        with mock.patch.object(importer, "_spotify_tracks", return_value=("n", [("t%d" % i, "") for i in range(60)])), \
             mock.patch.object(importer, "_yt_search_one", search):
            with self.assertRaises(RuntimeError):
                importer.resolve("https://open.spotify.com/playlist/abc")
        self.assertLess(len(calls), 20)

    def test_bad_spotify_json(self):
        html = '<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>'
        r = mock.MagicMock(); r.__enter__.return_value.read.return_value = html.encode()
        with mock.patch.object(importer.urllib.request, "urlopen", return_value=r):
            with self.assertRaisesRegex(RuntimeError, "Spotify"):
                importer._spotify_tracks("https://open.spotify.com/playlist/abc")


class Persistent(unittest.TestCase):
    def test_all_buttons_have_custom_id_and_is_persistent(self):
        async def go():
            v = views.MusicControlView(None)
            self.assertTrue(v.is_persistent())
            ids = [c.custom_id for c in v.children]
            self.assertTrue(all(i and i.startswith("mb:") for i in ids))
            self.assertEqual(len(ids), len(set(ids)))
            i = mock.Mock(); i.guild_id = 9
            self.assertEqual(v._gid(i), 9)
            self.assertEqual(views.MusicControlView(5)._gid(i), 5)
        asyncio.run(go())

    def test_registered_on_ready(self):
        src = open(bot.__file__, encoding="utf-8").read()
        self.assertIn("add_view(MusicControlView(None))", src)
