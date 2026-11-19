"""隨機循環 / 自動推薦也預熱下一首：預熱的就是 play_next 實際會播的那首；
加歌 / 換模式 / 刪歌 → 預熱的立刻收掉；隨機抽歌避開正在播和最近播過的。
執行：python -m unittest discover -s tests
"""
import asyncio
import os
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from musicbot import prewarm, prefetch, player, autoplay, state  # noqa: E402
from test_prefetch import reset_state, G, A, B, C  # noqa: E402
from test_prewarm import Base, FakeFF, VC  # noqa: E402

D = "https://www.youtube.com/watch?v=DDDDDDDDDDD"


class Mixin:
    def random_mode(self, urls=(A, B)):
        state.random_loop_guilds[G] = True
        state.local_playlist[:] = [{"url": u, "title": u[-3:]} for u in urls]

    def autoplay_mode(self, pick=B, seed_url=C):
        autoplay.set_enabled(G, True)
        autoplay.remember(G, seed_url)
        state.now_playing[G] = "cur"
        state.now_playing_url[G] = seed_url
        if pick:
            prefetch._autoplay_pick[G] = {"seed": autoplay.last_seed(G), "pick": (pick, "推薦"), "ts": time.time()}

    async def wait_url(self, url, t=2.0):
        end = time.monotonic() + t
        while time.monotonic() < end:
            w = prewarm._warm.get(G)
            if w and w["url"] == url:
                return True
            await asyncio.sleep(0.01)
        return False


class RandomWarm(Base, Mixin):
    async def test_warms_the_prepicked_song_and_play_next_uses_it(self):
        state.now_playing[G] = "cur"
        state.now_playing_url[G] = C
        self.random_mode()
        self.playing()
        prewarm.start(G, 1)
        self.assertTrue(await self.wait_warm())
        warm_url = prewarm._warm[G]["url"]
        self.assertEqual(warm_url, prefetch._random_pick[G]["url"])   # 預熱的就是預選的
        self.assertEqual(prefetch.take_random_pick(G)["url"], warm_url)   # play_next 取到同一首
        self.assertIsNotNone(prewarm.take(G, warm_url))

    async def test_turning_random_off_drops_warm(self):
        state.now_playing[G] = "cur"
        self.random_mode()
        self.playing()
        prewarm.start(G, 1)
        self.assertTrue(await self.wait_warm())
        ff = FakeFF.made[-1]
        state.random_loop_guilds[G] = False            # 使用者關掉隨機循環
        await asyncio.sleep(0.1)
        self.assertFalse(prewarm.has(G))
        self.assertGreaterEqual(ff.cleaned, 1)          # FFmpeg 收掉了，不會佔著

    async def test_adding_to_queue_switches_warm(self):
        state.now_playing[G] = "cur"
        self.random_mode()
        self.playing()
        prewarm.start(G, 1)
        self.assertTrue(await self.wait_warm())
        old = FakeFF.made[-1]
        state.queues[G] = [(D, "新加的")]               # 有人點歌 → 下一首變成佇列第一首
        self.assertTrue(await self.wait_url(D))
        self.assertGreaterEqual(old.cleaned, 1)

    async def test_too_late_drops_without_rewarm(self):
        state.now_playing[G] = "cur"
        self.random_mode()
        self.playing()
        prewarm.start(G, 1)
        self.assertTrue(await self.wait_warm())
        n = len(FakeFF.made)
        state.song_start_times[G] = time.time() - 199.2      # 剩 0.8 秒
        state.queues[G] = [(D, "新加的")]
        await asyncio.sleep(0.1)
        self.assertFalse(prewarm.has(G))
        self.assertEqual(len(FakeFF.made), n)               # 來不及就不再開新的


class AutoplayWarm(Base, Mixin):
    async def test_warms_the_prepicked_recommendation(self):
        self.autoplay_mode(pick=B)
        self.playing()
        prewarm.start(G, 1)
        self.assertTrue(await self.wait_url(B))
        self.assertEqual(prefetch.take_autoplay_pick(G)[0], B)     # play_next 取到同一首
        self.assertIsNotNone(prewarm.take(G, B))

    async def test_waits_for_pick_then_warms(self):
        self.autoplay_mode(pick=None)                  # 推薦還在挑
        self.playing()
        prewarm.start(G, 1)
        await asyncio.sleep(0.1)
        self.assertFalse(prewarm.has(G))
        prefetch._autoplay_pick[G] = {"seed": autoplay.last_seed(G), "pick": (B, "推薦"), "ts": time.time()}
        self.assertTrue(await self.wait_url(B))

    async def test_stale_seed_or_disabled_not_warmed(self):
        self.autoplay_mode(pick=B)
        prefetch._autoplay_pick[G]["seed"] = "ZZZZZZZZZZZ"   # 別首的推薦
        self.playing()
        prewarm.start(G, 1)
        await asyncio.sleep(0.1)
        self.assertFalse(prewarm.has(G))

    async def test_turning_autoplay_off_drops_warm(self):
        self.autoplay_mode(pick=B)
        self.playing()
        prewarm.start(G, 1)
        self.assertTrue(await self.wait_url(B))
        ff = FakeFF.made[-1]
        autoplay.set_enabled(G, False)
        await asyncio.sleep(0.1)
        self.assertFalse(prewarm.has(G))
        self.assertGreaterEqual(ff.cleaned, 1)

    async def test_loop_one_never_warms(self):
        self.autoplay_mode(pick=B)
        state.loop_modes[G] = "one"
        state.loop_current[G] = (C, "cur")
        self.playing()
        prewarm.start(G, 1)
        await asyncio.sleep(0.1)
        self.assertFalse(prewarm.has(G))


class PlayNextIntegration(Base, Mixin):
    async def run_play_next(self):
        ch = mock.MagicMock()
        ch.send = mock.AsyncMock(return_value=mock.MagicMock())
        with mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=ch)), \
             mock.patch.object(player.discord, "PCMVolumeTransformer", lambda s, volume=1: s), \
             mock.patch.object(player, "_voice_connected", lambda gid: True), \
             mock.patch.object(autoplay, "find_next", mock.AsyncMock(return_value=None)), \
             mock.patch.object(player, "progress_updater", mock.AsyncMock()):
            await player.play_next(G)

    async def asyncTearDown(self):
        for t in list(state.progress_tasks.values()):
            t.cancel()
        state.progress_tasks.clear()
        await player._clear_guild_state(G)
        autoplay.autoplay_guilds.clear()
        await super().asyncTearDown()

    async def _check(self):
        self.playing()
        prewarm.start(G, 1)
        self.assertTrue(await self.wait_warm())
        ff, url = FakeFF.made[-1], prewarm._warm[G]["url"]
        vc = state.voice_clients[G] = VC()
        await self.run_play_next()
        self.assertEqual(len(vc.played), 1)
        self.assertIs(vc.played[0].original, ff)       # 直接用預熱好的
        self.assertEqual(state.now_playing_url[G], url)

    async def test_random_end_to_end(self):
        state.now_playing[G] = "cur"
        state.now_playing_url[G] = C
        self.random_mode()
        await self._check()

    async def test_autoplay_end_to_end(self):
        self.autoplay_mode(pick=B)
        await self._check()
        self.assertEqual(state.now_playing_url[G], B)


class NoRepeat(unittest.TestCase):
    def setUp(self):
        reset_state()

    def test_avoids_current_and_recent(self):
        state.local_playlist[:] = [{"url": u, "title": "x"} for u in (A, B, C, D)]
        state.now_playing_url[G] = A
        seen = []
        for _ in range(3):
            s = prefetch.take_random_pick(G)
            self.assertNotEqual(s["url"], state.now_playing_url[G])
            self.assertNotIn(s["url"], seen)
            seen.append(s["url"])
            state.now_playing_url[G] = s["url"]
        self.assertEqual(len(set(seen)), 3)

    def test_two_songs_alternate(self):
        state.local_playlist[:] = [{"url": A, "title": "a"}, {"url": B, "title": "b"}]
        state.now_playing_url[G] = A
        for _ in range(6):
            s = prefetch.take_random_pick(G)
            self.assertNotEqual(s["url"], state.now_playing_url[G])
            state.now_playing_url[G] = s["url"]

    def test_single_song_still_plays(self):
        state.local_playlist[:] = [{"url": A, "title": "a"}]
        state.now_playing_url[G] = A
        self.assertEqual(prefetch.take_random_pick(G)["url"], A)

    def test_prepick_stable_until_taken(self):
        state.random_loop_guilds[G] = True
        state.now_playing[G] = "cur"
        state.local_playlist[:] = [{"url": u, "title": "x"} for u in (A, B, C, D)]
        first = prefetch.predict_next(G)
        for _ in range(10):
            self.assertEqual(prefetch.predict_next(G), first)    # 每秒問一次也不會換一首

    def test_recent_bounded(self):
        state.local_playlist[:] = [{"url": f"https://www.youtube.com/watch?v=R{i:010d}", "title": "x"} for i in range(60)]
        for _ in range(100):
            prefetch.take_random_pick(G)
        self.assertLessEqual(len(prefetch._random_recent[G]), prefetch.RANDOM_RECENT)
        prefetch.forget_guild(G)
        self.assertNotIn(G, prefetch._random_recent)


if __name__ == "__main__":
    unittest.main()
