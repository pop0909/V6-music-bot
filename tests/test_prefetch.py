"""預先解析下一首（prefetch）的單元測試（yt-dlp 以假函式取代，不連網）
執行：python -m unittest discover -s tests
"""
import asyncio
import os
import sys
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import prefetch, autoplay, state  # noqa: E402

G = 111
A = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
B = "https://www.youtube.com/watch?v=BBBBBBBBBBB"
C = "https://www.youtube.com/watch?v=CCCCCCCCCCC"


class FakeYDL:
    """記錄呼叫次數的假 extract_stream；可設定延遲 / 失敗 / 阻塞"""
    def __init__(self, delay=0.0, fail_times=0, expire_in=6 * 3600):
        self.calls = []
        self.delay = delay
        self.fail_times = fail_times
        self.expire_in = expire_in
        self.gates = {}
        self.lock = threading.Lock()

    def block(self, url):
        ev = threading.Event()
        self.gates[url] = ev
        return ev

    def __call__(self, url):
        with self.lock:
            self.calls.append(url)
            fail = self.fail_times > 0
            if fail:
                self.fail_times -= 1
        if url in self.gates:
            self.gates[url].wait(5)
        if self.delay:
            time.sleep(self.delay)
        if fail:
            raise RuntimeError("HTTP Error 403")
        exp = int(time.time() + self.expire_in)
        return {"url": f"https://rr.googlevideo.com/videoplayback?expire={exp}&id={url[-11:]}",
                "duration": 200, "thumbnail": "t.jpg", "title": "T-" + url[-11:]}


def reset_state():
    for d in (prefetch._cache, prefetch._inflight, prefetch._tasks, prefetch._gen,
              prefetch._autoplay_pick, prefetch._random_pick, prefetch._failed):
        d.clear()
    for d in (state.queues, state.now_playing, state.loop_modes, state.saved_queues, state.loop_current,
              state.random_loop_guilds):
        d.clear()
    state.local_playlist.clear()
    autoplay.autoplay_guilds.clear()
    autoplay.recent_history.clear()
    autoplay.failed_ids.clear()


async def drain():
    """等所有背景 prefetch task 結束"""
    for _ in range(200):
        tasks = [t for _, t in prefetch._tasks.values()] + list(prefetch._inflight.values())
        if not tasks:
            return
        await asyncio.sleep(0.01)


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_state()
        self.ydl = FakeYDL()
        p = mock.patch.object(prefetch, "extract_stream", self.ydl)
        p.start()
        self.addCleanup(p.stop)
        state.now_playing[G] = "current song"


class ExpiryTest(unittest.TestCase):
    def test_compute_expiry(self):
        now = 1_000_000
        # expire 參數 2 小時後 → 提前 10 分鐘，但最長 90 分鐘
        self.assertEqual(prefetch.compute_expiry(f"https://x/v?expire={now + 7200}", now), now + prefetch.MAX_AGE)
        # expire 參數 30 分鐘後 → 20 分鐘後失效
        self.assertEqual(prefetch.compute_expiry(f"https://x/v?expire={now + 1800}", now), now + 1200)
        # 沒有 expire 參數 → 90 分鐘
        self.assertEqual(prefetch.compute_expiry("https://x/v?a=1", now), now + prefetch.MAX_AGE)
        self.assertEqual(prefetch.compute_expiry("https://x/v?expire=bad", now), now + prefetch.MAX_AGE)


class ResolveTest(Base):
    async def test_miss_then_hit(self):
        r1 = await prefetch.resolve(G, A)
        self.assertEqual(self.ydl.calls, [A])
        r2 = await prefetch.resolve(G, A)
        self.assertEqual(self.ydl.calls, [A])          # 第二次命中快取，不再解析
        self.assertEqual(r1["url"], r2["url"])
        self.assertEqual(r2["duration"], 200)

    async def test_prefetch_hit(self):
        state.queues[G] = [(A, "a"), (B, "b")]
        prefetch.ensure(G)
        await drain()
        self.assertEqual(self.ydl.calls, [A])
        self.assertIsNotNone(prefetch.get_cached(G, A))
        with self.assertLogs("musicbot", "INFO") as cm:
            await prefetch.resolve(G, A)
        self.assertEqual(self.ydl.calls, [A])
        self.assertTrue(any("命中" in m for m in cm.output))

    async def test_expired_entry_is_miss(self):
        self.ydl.expire_in = 11 * 60     # 串流網址 11 分鐘後失效 → 快取只能活 1 分鐘
        await prefetch.resolve(G, A)
        self.assertIsNotNone(prefetch.get_cached(G, A))
        real = time.time
        with mock.patch.object(prefetch.time, "time", lambda: real() + 120):
            self.assertIsNone(prefetch.get_cached(G, A))
        self.ydl.expire_in = 6 * 3600
        await prefetch.resolve(G, A)
        self.assertEqual(self.ydl.calls, [A, A])

    async def test_already_expired_not_stored(self):
        self.ydl.expire_in = 60          # 少於 10 分鐘的保留時間 → 不存
        await prefetch.resolve(G, A)
        self.assertIsNone(prefetch.get_cached(G, A))

    async def test_duplicate_inflight_shared(self):
        gate = self.ydl.block(A)
        state.queues[G] = [(A, "a")]
        prefetch.ensure(G)                       # 背景開始解析 A
        await asyncio.sleep(0.05)
        t1 = asyncio.create_task(prefetch.resolve(G, A))      # play_next 同時要 A
        t2 = asyncio.create_task(prefetch.resolve(222, A))    # 另一個 guild 也要 A
        await asyncio.sleep(0.05)
        gate.set()
        r1, r2 = await asyncio.gather(t1, t2)
        await drain()
        self.assertEqual(self.ydl.calls, [A])    # 只解析一次
        self.assertEqual(r1["url"], r2["url"])
        self.assertIsNotNone(prefetch.get_cached(222, A))

    async def test_cache_size_limited(self):
        for i in range(prefetch.MAX_ENTRIES + 3):
            await prefetch.resolve(G, f"https://www.youtube.com/watch?v=X{i:010d}")
        self.assertEqual(len(prefetch._cache[G]), prefetch.MAX_ENTRIES)


class RetargetTest(Base):
    async def test_reorder_retargets_and_ignores_stale(self):
        gate_a = self.ydl.block(A)
        state.queues[G] = [(A, "a"), (B, "b"), (C, "c")]
        prefetch.ensure(G)
        await asyncio.sleep(0.05)
        self.assertEqual(prefetch._tasks[G][0], ("url", A))
        old_task = prefetch._tasks[G][1]
        # 使用者把 B 拖到最前面
        state.queues[G] = [(B, "b"), (A, "a"), (C, "c")]
        prefetch.ensure(G)
        await asyncio.sleep(0)
        self.assertEqual(prefetch._tasks[G][0], ("url", B))
        self.assertTrue(old_task.cancelled() or old_task.cancelling())
        await asyncio.sleep(0.05)
        self.assertIsNotNone(prefetch.get_cached(G, B))
        gate_a.set()
        await drain()
        self.assertIsNone(prefetch.get_cached(G, A))  # 舊目標的結果被忽略
        self.assertLessEqual(len(prefetch._tasks), 1)

    async def test_one_prefetch_per_guild_and_idempotent(self):
        gate = self.ydl.block(A)
        state.queues[G] = [(A, "a")]
        for _ in range(5):
            prefetch.ensure(G)
        await asyncio.sleep(0.05)
        self.assertEqual(self.ydl.calls, [A])
        gate.set()
        await drain()
        prefetch.ensure(G)                       # 已在快取 → 不再解析
        await drain()
        self.assertEqual(self.ydl.calls, [A])

    async def test_remove_and_clear_queue(self):
        gate = self.ydl.block(A)
        state.queues[G] = [(A, "a"), (B, "b")]
        prefetch.ensure(G)
        await asyncio.sleep(0.02)
        state.queues[G] = []                     # 清空佇列（沒有循環/自動推薦）→ 取消
        prefetch.ensure(G)
        self.assertNotIn(G, prefetch._tasks)
        gate.set()
        await drain()

    async def test_loop_one_keeps_current_cached(self):
        state.loop_modes[G] = "one"
        await prefetch.resolve(G, A)             # 目前這首（play_next 解析過）
        state.loop_current[G] = (A, "a")
        state.queues[G] = [(B, "b")]             # 單曲循環：下一首仍是 A
        prefetch.ensure(G)
        await drain()
        self.assertEqual(self.ydl.calls, [A])

    async def test_loop_all_uses_saved_queue(self):
        state.loop_modes[G] = "all"
        state.queues[G] = [(C, "c"), (A, "a")]   # 全部循環：播過的歌已接回尾端
        prefetch.ensure(G)
        await drain()
        self.assertEqual(self.ydl.calls, [C])

    async def test_shuffle_loop_prepicks(self):
        state.random_loop_guilds[G] = True
        state.local_playlist[:] = [{"url": A, "title": "a"}, {"url": B, "title": "b"}]
        prefetch.ensure(G)
        await drain()
        rp = prefetch._random_pick[G]
        self.assertEqual(self.ydl.calls, [rp["url"]])
        self.assertEqual(prefetch.take_random_pick(G)["url"], rp["url"])   # play_next 會播同一首
        self.assertIsNone(prefetch.take_random_pick(G))

    async def test_not_playing_does_nothing(self):
        state.now_playing.pop(G)
        state.queues[G] = [(A, "a")]
        prefetch.ensure(G)
        await drain()
        self.assertEqual(self.ydl.calls, [])


class AutoplayPrefetchTest(Base):
    async def test_autoplay_candidate_prefetched(self):
        autoplay.set_enabled(G, True)
        autoplay.remember(G, A)                 # 目前播放 A（種子）
        state.queues[G] = []
        find = mock.AsyncMock(return_value=(B, "推薦 B"))
        with mock.patch.object(autoplay, "find_next", find):
            prefetch.ensure(G)
            await drain()
            prefetch.ensure(G)                  # 同一個種子不再重讀 Mix
            await drain()
        self.assertEqual(find.await_count, 1)
        self.assertEqual(self.ydl.calls, [B])
        self.assertEqual(prefetch.take_autoplay_pick(G), (B, "推薦 B"))
        self.assertIsNone(prefetch.take_autoplay_pick(G))

    async def test_autoplay_pick_stale_seed_ignored(self):
        autoplay.set_enabled(G, True)
        autoplay.remember(G, A)
        find = mock.AsyncMock(return_value=(B, "推薦 B"))
        with mock.patch.object(autoplay, "find_next", find):
            prefetch.ensure(G)
            await drain()
        autoplay.remember(G, C)                 # 換歌了，種子不同
        self.assertIsNone(prefetch.take_autoplay_pick(G))

    async def test_autoplay_no_candidate_not_retried(self):
        autoplay.set_enabled(G, True)
        autoplay.remember(G, A)
        find = mock.AsyncMock(return_value=None)
        with mock.patch.object(autoplay, "find_next", find):
            for _ in range(3):
                prefetch.ensure(G)
                await drain()
        self.assertEqual(find.await_count, 1)
        self.assertIsNone(prefetch.take_autoplay_pick(G))


class FailureTest(Base):
    async def test_prefetch_failure_falls_back(self):
        self.ydl.fail_times = 1
        state.queues[G] = [(A, "a")]
        prefetch.ensure(G)
        await drain()
        self.assertIsNone(prefetch.get_cached(G, A))
        prefetch.ensure(G)                      # 失敗後短時間內不重試（避免每 5 秒狂打 YouTube）
        await drain()
        self.assertEqual(self.ydl.calls, [A])
        r = await prefetch.resolve(G, A)        # play_next 照常重新解析
        self.assertEqual(self.ydl.calls, [A, A])
        self.assertIn("googlevideo", r["url"])

    async def test_inflight_failure_reresolves(self):
        gate = self.ydl.block(A)
        self.ydl.fail_times = 1
        state.queues[G] = [(A, "a")]
        prefetch.ensure(G)
        await asyncio.sleep(0.02)
        t = asyncio.create_task(prefetch.resolve(G, A))
        await asyncio.sleep(0.02)
        self.ydl.gates.pop(A)
        gate.set()
        r = await t
        self.assertEqual(self.ydl.calls, [A, A])
        self.assertIn("googlevideo", r["url"])

    async def test_resolve_failure_raises(self):
        self.ydl.fail_times = 5
        with self.assertRaises(RuntimeError):
            await prefetch.resolve(G, A)        # 交給 play_next 既有的失敗計數處理
        self.assertIsNone(prefetch.get_cached(G, A))


class ClearTest(Base):
    async def test_stop_clears_cache_and_ignores_late_result(self):
        await prefetch.resolve(G, A)
        gate = self.ydl.block(B)
        state.queues[G] = [(B, "b")]
        prefetch.ensure(G)
        await asyncio.sleep(0.02)
        prefetch.clear(G)                       # /stop
        self.assertNotIn(G, prefetch._cache)
        self.assertNotIn(G, prefetch._tasks)
        gate.set()
        await drain()
        self.assertNotIn(G, prefetch._cache)    # 停止後才完成的解析不會再塞回快取

    async def test_player_clear_guild_state_calls_clear(self):
        from musicbot import player
        await prefetch.resolve(G, A)
        await player._clear_guild_state(G)
        self.assertNotIn(G, prefetch._cache)


class PlayNextIntegrationTest(Base):
    """play_next 取到預先解析過的歌時不再呼叫 yt-dlp"""
    async def test_play_next_uses_cache(self):
        from musicbot import player
        state.now_playing.pop(G)
        state.queues[G] = [(A, "a"), (B, "b")]

        class VC:
            def __init__(self): self.played = []
            def is_connected(self): return True
            def is_playing(self): return bool(self.played)
            def is_paused(self): return False
            def play(self, src, after=None, **kw): self.played.append(src)
        vc = VC()
        state.voice_clients[G] = vc
        ch = mock.MagicMock()
        ch.send = mock.AsyncMock(return_value=mock.MagicMock())
        with mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=ch)), \
             mock.patch.object(player.discord, "FFmpegPCMAudio", mock.MagicMock()), \
             mock.patch.object(player.discord, "PCMVolumeTransformer", mock.MagicMock()), \
             mock.patch.object(player, "progress_updater", mock.AsyncMock()):
            await player.play_next(G)
            self.assertEqual(self.ydl.calls, [A])          # 第一首：未命中，正常解析
            await drain()
            self.assertEqual(self.ydl.calls, [A, B])       # 開播後背景預先解析下一首 B
            vc.played.clear()
            await player.play_next(G)                      # 換到 B
            self.assertEqual(self.ydl.calls, [A, B])       # 命中，沒有再解析
            self.assertEqual(state.now_playing[G], "b")
            url_arg = player.discord.FFmpegPCMAudio.call_args[0][0]
            self.assertIn("BBBBBBBBBBB", url_arg)
        for t in list(state.progress_tasks.values()):
            t.cancel()
        state.progress_tasks.clear()
        await player._clear_guild_state(G)


if __name__ == "__main__":
    unittest.main()
