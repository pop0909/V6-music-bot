"""換歌預熱（prewarm）的單元測試：快播完時預開下一首的 FFmpeg，play_next 直接接上
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

from musicbot import prewarm, prefetch, player, audio, state  # noqa: E402
from test_prefetch import FakeYDL, reset_state, drain, G, A, B, C  # noqa: E402,F401


class FakeFF:
    """假 FFmpegPCMAudio：read() 回固定資料，記錄 cleanup"""
    made = []

    def __init__(self, url, **kw):
        self.url, self.kw, self.cleaned = url, kw, 0
        FakeFF.made.append(self)

    def read(self):
        return b"\x01\x00" * 1920

    def is_opus(self):
        return False

    def cleanup(self):
        self.cleaned += 1


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_state()
        FakeFF.made = []
        prewarm._warm.clear()
        prewarm._watch.clear()
        self.ydl = FakeYDL()
        for p in (mock.patch.object(prefetch, "extract_stream", self.ydl),
                  mock.patch.object(player.discord, "FFmpegPCMAudio", FakeFF),
                  mock.patch.object(prewarm, "POLL", 0.01)):
            p.start()
            self.addCleanup(p.stop)
        for d in (state.song_durations, state.song_start_times, state.progress_versions):
            d.pop(G, None)

    async def asyncTearDown(self):
        prewarm.discard(G)
        await asyncio.sleep(0.05)

    def playing(self, dur=200, elapsed=195, version=1):
        state.song_durations[G] = dur
        state.song_start_times[G] = time.time() - elapsed
        state.progress_versions[G] = version

    async def wait_warm(self, t=2.0):
        end = time.monotonic() + t
        while time.monotonic() < end and not prewarm.has(G):
            await asyncio.sleep(0.01)
        return prewarm.has(G)


class WatchTest(Base):
    async def test_warms_next_near_end(self):
        state.queues[G] = [(B, "b")]
        self.playing(elapsed=195)                       # 剩 5 秒 < LEAD
        prewarm.start(G, 1)
        self.assertTrue(await self.wait_warm())
        self.assertEqual(len(FakeFF.made), 1)
        self.assertIn("BBBBBBBBBBB", FakeFF.made[0].url)
        self.assertEqual(FakeFF.made[0].kw["options"], audio.ffmpeg_options(G))
        self.assertEqual(self.ydl.calls, [B])

    async def test_waits_until_lead(self):
        state.queues[G] = [(B, "b")]
        self.playing(elapsed=100)                       # 還剩 100 秒
        prewarm.start(G, 1)
        await asyncio.sleep(0.1)
        self.assertFalse(prewarm.has(G))
        self.assertEqual(FakeFF.made, [])
        state.song_start_times[G] = time.time() - 195   # 跳轉到結尾附近 → 開始預熱
        self.assertTrue(await self.wait_warm())

    async def test_skips_cases(self):
        cases = {
            "loop_one": lambda: state.loop_modes.__setitem__(G, "one"),
            "queue_empty": lambda: state.queues.__setitem__(G, []),
            "paused": lambda: state.song_start_times.pop(G),
            "live": lambda: state.song_durations.__setitem__(G, 0),
        }
        for name, setup in cases.items():
            with self.subTest(name):
                reset_state()
                state.queues[G] = [(B, "b")]
                self.playing()
                setup()
                prewarm.start(G, 1)
                await asyncio.sleep(0.1)
                self.assertFalse(prewarm.has(G))
                self.assertEqual(FakeFF.made, [])
                prewarm.discard(G)

    async def test_song_changed_while_priming_discards(self):
        state.queues[G] = [(B, "b")]
        self.playing()
        gate = self.ydl.block(B)
        prewarm.start(G, 1)
        await asyncio.sleep(0.05)
        state.progress_versions[G] = 2                  # 期間換歌了
        gate.set()
        await asyncio.sleep(0.2)
        self.assertFalse(prewarm.has(G))
        self.assertTrue(all(f.cleaned for f in FakeFF.made))

    async def test_failure_is_quiet(self):
        state.queues[G] = [(B, "b")]
        self.playing()
        self.ydl.fail_times = 5
        prewarm.start(G, 1)
        await asyncio.sleep(0.2)
        self.assertFalse(prewarm.has(G))                # 失敗 → 換歌時照常開啟
        self.assertNotIn(G, prewarm._watch)


class TakeTest(Base):
    async def warm(self):
        state.queues[G] = [(B, "b")]
        self.playing()
        prewarm.start(G, 1)
        self.assertTrue(await self.wait_warm())
        return FakeFF.made[0]

    async def test_match(self):
        ff = await self.warm()
        src, res = prewarm.take(G, B)
        self.assertIs(src.original, ff)
        self.assertIn("BBBBBBBBBBB", res["url"])
        self.assertFalse(prewarm.has(G))
        self.assertEqual(ff.cleaned, 0)  # 拿去播，不收掉

    async def test_mismatch_url(self):
        ff = await self.warm()
        self.assertIsNone(prewarm.take(G, C))           # 佇列被改 / 跳到別首
        await asyncio.sleep(0.05)
        self.assertGreaterEqual(ff.cleaned, 1)

    async def test_options_changed(self):
        ff = await self.warm()
        with mock.patch.object(audio, "ffmpeg_options", lambda gid, **k: "-af changed"):
            self.assertIsNone(prewarm.take(G, B))       # 音量平衡切換了 → 不用舊濾鏡
        await asyncio.sleep(0.05)
        self.assertGreaterEqual(ff.cleaned, 1)

    async def test_stale(self):
        ff = await self.warm()
        prewarm._warm[G]["ts"] -= prewarm.MAX_AGE + 1
        self.assertIsNone(prewarm.take(G, B))
        await asyncio.sleep(0.05)
        self.assertGreaterEqual(ff.cleaned, 1)

    async def test_discard_and_clear_state(self):
        ff = await self.warm()
        await player._clear_guild_state(G)              # 停止 / 離開
        self.assertFalse(prewarm.has(G))
        await asyncio.sleep(0.05)
        self.assertGreaterEqual(ff.cleaned, 1)


class VC:
    def __init__(self): self.played = []
    def is_connected(self): return True
    def is_playing(self): return False
    def is_paused(self): return False
    def play(self, src, after=None, **kw): self.played.append(src)


class PlayNextTest(Base):
    async def run_play_next(self):
        ch = mock.MagicMock()
        ch.send = mock.AsyncMock(return_value=mock.MagicMock())
        with mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=ch)), \
             mock.patch.object(player.discord, "PCMVolumeTransformer", lambda s, volume=1: s), \
             mock.patch.object(player, "progress_updater", mock.AsyncMock()):
            await player.play_next(G)

    async def asyncTearDown(self):
        await drain()
        for t in list(state.progress_tasks.values()):
            t.cancel()
        state.progress_tasks.clear()
        await player._clear_guild_state(G)
        await super().asyncTearDown()

    async def test_uses_warm_source(self):
        state.queues[G] = [(B, "b")]
        self.playing()
        prewarm.start(G, 1)
        self.assertTrue(await self.wait_warm())
        ff = FakeFF.made[0]
        vc = state.voice_clients[G] = VC()
        await self.run_play_next()
        self.assertEqual(len(vc.played), 1)
        self.assertIs(vc.played[0].original, ff)        # 直接用預熱好的，沒有再開 FFmpeg
        self.assertEqual(len(FakeFF.made), 1)
        self.assertEqual(state.now_playing[G], "b")
        self.assertEqual(prewarm.stats["used"] >= 1, True)

    async def test_mismatch_opens_normally(self):
        state.queues[G] = [(B, "b")]
        self.playing()
        prewarm.start(G, 1)
        self.assertTrue(await self.wait_warm())
        warm_ff = FakeFF.made[0]
        state.queues[G] = [(C, "c")]                    # 預熱後佇列被改
        vc = state.voice_clients[G] = VC()
        await self.run_play_next()
        self.assertEqual(len(FakeFF.made), 2)
        self.assertIs(vc.played[0].original, FakeFF.made[1])
        self.assertIn("CCCCCCCCCCC", FakeFF.made[1].url)
        await asyncio.sleep(0.05)
        self.assertGreaterEqual(warm_ff.cleaned, 1)

    async def test_sleep_after_song_discards(self):
        state.queues[G] = [(B, "b")]
        self.playing()
        prewarm.start(G, 1)
        self.assertTrue(await self.wait_warm())
        ff = FakeFF.made[0]
        state.voice_clients[G] = VC()
        with mock.patch.object(player, "check_track_end_sleep", lambda gid: True), \
             mock.patch.object(player, "execute_sleep", mock.AsyncMock(), create=True):
            try:
                await self.run_play_next()
            except Exception:
                pass
        self.assertFalse(prewarm.has(G))
        await asyncio.sleep(0.05)
        self.assertGreaterEqual(ff.cleaned, 1)

    async def test_play_next_starts_watch(self):
        state.queues[G] = [(B, "b"), (C, "c")]
        vc = state.voice_clients[G] = VC()
        await self.run_play_next()
        self.assertEqual(len(vc.played), 1)
        self.assertIn(G, prewarm._watch)                # 開播後開始監看，快結束時預熱 C


class InThreadTest(unittest.IsolatedAsyncioTestCase):
    async def test_exception_propagates(self):
        # 修正前：except 區塊結束後 e 被刪，回呼 NameError → future 永遠不完成（只能等逾時）
        with self.assertRaises(ZeroDivisionError):
            await asyncio.wait_for(audio._in_thread(lambda: 1 / 0), 2)

    async def test_prime_error_is_fast(self):
        class Boom(FakeFF):
            def read(self):
                raise OSError("pipe broken")
        t = time.monotonic()
        with self.assertRaises(OSError):
            await audio.prime_source(Boom("x"), timeout=5)
        self.assertLess(time.monotonic() - t, 1.0)


if __name__ == "__main__":
    unittest.main()
