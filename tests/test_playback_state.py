"""回歸測試：每一種開播路徑都要把 Web/SSE 的「正在播放」狀態設好
（標題、網址、縮圖、長度、開始時間、播放中旗標），快取命中與否都一樣。
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

from musicbot import prefetch, autoplay, state, player, webcore  # noqa: E402

G = 4242
A = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
B = "https://www.youtube.com/watch?v=BBBBBBBBBBB"
C = "https://www.youtube.com/watch?v=CCCCCCCCCCC"
DUR = {"AAAAAAAAAAA": 210, "BBBBBBBBBBB": 199, "CCCCCCCCCCC": 230}


def fake_extract(url):
    vid = url[-11:]
    exp = int(time.time() + 6 * 3600)
    return {"url": f"https://rr.googlevideo.com/videoplayback?expire={exp}&id={vid}",
            "duration": DUR.get(vid, 180), "thumbnail": f"https://i.ytimg.com/vi/{vid}/hq.jpg",
            "title": "yt-" + vid}


class FakeVC:
    def __init__(self):
        self.playing = False
        self.after = None
        self.sources = []

    def is_connected(self): return True
    def is_playing(self): return self.playing
    def is_paused(self): return False

    def play(self, src, after=None, **kw):
        self.playing = True
        self.after = after
        self.sources.append(src)

    def stop(self):
        self.playing = False   # 真的 discord 會在背景呼叫 after → play_next；測試裡手動呼叫


def reset():
    for d in (prefetch._cache, prefetch._inflight, prefetch._tasks, prefetch._gen,
              prefetch._autoplay_pick, prefetch._random_pick, prefetch._failed):
        d.clear()
    for name in ("queues", "now_playing", "voice_clients", "random_loop_guilds", "now_playing_url",
                 "now_playing_thumbnail", "song_start_times", "song_durations", "paused_position",
                 "progress_versions", "progress_tasks", "is_playing_state", "is_paused_state",
                 "loop_modes", "saved_queues", "loop_current", "manual_advance", "play_failures", "control_panels", "current_voice_channel"):
        getattr(state, name).clear()
    state.local_playlist.clear()
    autoplay.autoplay_guilds.clear()
    autoplay.recent_history.clear()
    autoplay.failed_ids.clear()


class PlaybackStateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset()
        self.calls = []

        def extract(url):
            self.calls.append(url)
            return fake_extract(url)
        ch = mock.MagicMock()
        ch.send = mock.AsyncMock(return_value=mock.MagicMock())
        self.vc = FakeVC()
        state.voice_clients[G] = self.vc
        patches = [
            mock.patch.object(prefetch, "extract_stream", extract),
            mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=ch)),
            mock.patch.object(player, "_voice_connected", lambda gid: True),
            mock.patch.object(player.discord, "FFmpegPCMAudio", mock.MagicMock()),
            mock.patch.object(player.discord, "PCMVolumeTransformer", mock.MagicMock()),
            mock.patch.object(player, "progress_updater", mock.AsyncMock()),
            mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=self.vc)),
            # 不連網：自動推薦預設找不到候選（個別測試再覆寫）
            mock.patch.object(autoplay, "find_next", mock.AsyncMock(return_value=None)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.drain()
        for t in list(state.progress_tasks.values()):
            t.cancel()
        state.progress_tasks.clear()
        prefetch.clear(G)

    async def drain(self):
        for _ in range(200):
            pending = [t for _, t in prefetch._tasks.values()] + list(prefetch._inflight.values())
            if not pending:
                return
            await asyncio.sleep(0.01)

    async def finish_song(self):
        """模擬目前這首播完（after_play → play_next）"""
        self.vc.playing = False
        await player.play_next(G)

    def assertWebState(self, url, title):
        s = webcore.get_guild_status(G)
        vid = url[-11:]
        self.assertTrue(s["is_playing"], s)
        self.assertFalse(s["is_paused"])
        self.assertEqual(s["current_title"], title)
        self.assertEqual(s["current_url"], url)
        self.assertEqual(s["thumbnail"], f"https://i.ytimg.com/vi/{vid}/hq.jpg")
        self.assertEqual(s["duration"], DUR[vid])
        self.assertIn(G, state.song_start_times)
        self.assertLess(s["elapsed"], 2)
        self.assertTrue(s["is_connected"])
        # SSE 推送的是 get_full_app_state：同一份資料也要帶歌曲資訊
        with mock.patch.object(webcore, "load_favorites", return_value=[]):
            full = webcore.get_full_app_state(G)
        self.assertEqual(full["current_title"], title)
        self.assertEqual(full["duration"], DUR[vid])
        # 播放器拿到的是對應的串流網址
        url_arg = player.discord.FFmpegPCMAudio.call_args[0][0]
        self.assertIn(vid, url_arg)

    async def test_cache_miss(self):
        state.queues[G] = [(A, "Song A")]
        await player.play_next(G)
        self.assertEqual(self.calls, [A])
        self.assertWebState(A, "Song A")

    async def test_cache_hit_next_song(self):
        state.queues[G] = [(A, "Song A"), (B, "Song B")]
        await player.play_next(G)
        await self.drain()                       # 背景預先解析 B
        self.assertEqual(self.calls, [A, B])
        await self.finish_song()
        self.assertEqual(self.calls, [A, B])     # 命中，沒有再解析
        self.assertWebState(B, "Song B")

    async def test_cache_hit_inflight(self):
        """下一首還在背景解析時就換歌：共用進行中的解析，狀態一樣要設好"""
        import threading
        gate = threading.Event()

        def slow(url):
            self.calls.append(url)
            if url == B:
                gate.wait(5)
            return fake_extract(url)
        with mock.patch.object(prefetch, "extract_stream", slow):
            state.queues[G] = [(A, "Song A"), (B, "Song B")]
            await player.play_next(G)
            await asyncio.sleep(0.02)
            self.vc.playing = False
            t = asyncio.create_task(player.play_next(G))
            await asyncio.sleep(0.02)
            gate.set()
            await t
        self.assertEqual(self.calls, [A, B])
        self.assertWebState(B, "Song B")

    async def test_single_loop(self):
        state.loop_modes[G] = "one"
        state.queues[G] = [(A, "Song A"), (B, "Song B")]
        await player.play_next(G)
        self.assertWebState(A, "Song A")
        self.assertEqual(state.queues[G], [(B, "Song B")])   # 佇列顯示真正的下一首，不會多一個 A
        await self.finish_song()                 # 單曲循環：再播一次 A（快取命中）
        self.assertEqual(self.calls, [A])
        self.assertWebState(A, "Song A")
        state.manual_advance[G] = True           # 使用者按跳過 → 前進到 B
        await self.finish_song()
        self.assertWebState(B, "Song B")

    async def test_loop_all_restore(self):
        """全部循環：播過的歌接回尾端，整個佇列一直轉"""
        state.loop_modes[G] = "all"
        state.queues[G] = [(A, "Song A"), (C, "Song C")]
        await player.play_next(G)
        self.assertEqual(state.queues[G], [(C, "Song C"), (A, "Song A")])
        await self.drain()
        await self.finish_song()
        self.assertWebState(C, "Song C")
        await self.drain()
        await self.finish_song()
        self.assertWebState(A, "Song A")
        self.assertEqual(state.queues[G], [(C, "Song C"), (A, "Song A")])

    async def test_shuffle_loop(self):
        state.random_loop_guilds[G] = True
        state.local_playlist[:] = [{"url": B, "title": "Song B"}, {"url": C, "title": "Song C"}]
        state.queues[G] = [(A, "Song A")]
        await player.play_next(G)
        await self.drain()
        picked = prefetch._random_pick[G]
        await self.finish_song()
        self.assertEqual(self.calls, [A, picked["url"]])   # 播的就是預先抽好並解析的那首
        self.assertWebState(picked["url"], picked["title"])

    async def test_autoplay_prefetched(self):
        autoplay.set_enabled(G, True)
        state.queues[G] = [(A, "Song A")]
        find = mock.AsyncMock(return_value=(C, "推薦 C"))
        with mock.patch.object(autoplay, "find_next", find):
            await player.play_next(G)
            await self.drain()
            await self.finish_song()
        self.assertEqual(find.await_count, 1)
        self.assertEqual(self.calls, [A, C])
        self.assertWebState(C, "推薦 C")

    async def test_autoplay_not_prefetched(self):
        autoplay.set_enabled(G, True)
        state.queues[G] = []
        autoplay.remember(G, A)
        find = mock.AsyncMock(return_value=(B, "推薦 B"))
        with mock.patch.object(autoplay, "find_next", find):
            await player.play_next(G)            # 閒置時開啟自動推薦：沒有預先挑歌
        self.assertWebState(B, "推薦 B")

    async def test_web_play_index(self):
        state.queues[G] = [(A, "Song A"), (B, "Song B"), (C, "Song C")]
        await player.play_next(G)
        await self.drain()
        r = await webcore.web_play_index(G, 1)   # 網頁點佇列第 2 首（C）
        self.assertEqual(r["status"], "playing")
        self.assertTrue(r.get("accepted"))       # 淡出 + 停止在背景，API 立刻回應
        await player.wait_background()
        self.assertFalse(self.vc.playing)        # 已 stop，接著 after_play 會呼叫 play_next
        await player.play_next(G)
        self.assertWebState(C, "Song C")

    async def test_web_skip(self):
        state.queues[G] = [(A, "Song A"), (B, "Song B")]
        await player.play_next(G)
        await self.drain()
        r = await webcore.web_skip(G)
        self.assertEqual(r["status"], "skipped")
        self.assertEqual(r.get("next"), "Song B")
        await player.wait_background()
        await player.play_next(G)
        self.assertEqual(self.calls, [A, B])
        self.assertWebState(B, "Song B")

    async def test_failure_clears_now_playing_flags(self):
        """解析失敗且沒在播：不能留下「播放中但沒有標題」的狀態"""
        def boom(url):
            raise RuntimeError("HTTP Error 403")
        with mock.patch.object(prefetch, "extract_stream", boom), \
             mock.patch.object(player, "_keep", side_effect=lambda c: c.close()):
            state.queues[G] = [(A, "Song A")]
            await player.play_next(G)
        s = webcore.get_guild_status(G)
        self.assertFalse(s["is_playing"] and not s["current_title"], s)


if __name__ == "__main__":
    unittest.main()
