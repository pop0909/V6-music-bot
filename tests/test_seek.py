"""跳轉（seek）：FFmpeg 參數、狀態/進度、暫停中跳轉、範圍檢查、串流網址過期重新解析、API。
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

from musicbot import prefetch, autoplay, state, player, webcore, audio, utils, webapi  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_playback_state import reset, fake_extract, A, B, G  # noqa: E402


class SeekVC:
    """模擬 discord VoiceClient：可暫停、可替換來源（vc.source = ...）"""
    def __init__(self):
        self.playing = False
        self.paused = False
        self.after = None
        self.sources = []
        self._source = None
        self.swaps = 0

    def is_connected(self): return True
    def is_playing(self): return self.playing and not self.paused
    def is_paused(self): return self.playing and self.paused

    def play(self, src, after=None, **kw):
        self.playing, self.paused, self.after = True, False, after
        self._source = src
        self.sources.append(src)

    @property
    def source(self): return self._source

    @source.setter
    def source(self, value):
        if not self.playing:
            raise ValueError("Not playing anything.")
        self._source = value
        self.swaps += 1
        self.paused = False      # 和 discord.py AudioPlayer.set_source 一樣：替換時會恢復播放執行緒

    def pause(self): self.paused = True
    def resume(self): self.paused = False
    def stop(self): self.playing = False; self.paused = False


class SeekTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset()
        state.pending_seeks.clear()
        state.now_playing_stream.clear()
        audio.normalize_guilds.clear()
        self.calls = []

        def extract(url):
            self.calls.append(url)
            return fake_extract(url)
        ch = mock.MagicMock()
        ch.send = mock.AsyncMock(return_value=mock.MagicMock())
        self.vc = SeekVC()
        state.voice_clients[G] = self.vc
        self.ffmpeg = mock.MagicMock(side_effect=lambda *a, **k: mock.MagicMock(name="ffmpeg"))
        self.pcm = mock.MagicMock(side_effect=lambda src, volume=1.0: mock.MagicMock(name="pcm", original=src, volume=volume))
        patches = [
            mock.patch.object(prefetch, "extract_stream", extract),
            mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=ch)),
            mock.patch.object(player, "_voice_connected", lambda gid: True),
            mock.patch.object(player.discord, "FFmpegPCMAudio", self.ffmpeg),
            mock.patch.object(player.discord, "PCMVolumeTransformer", self.pcm),
            mock.patch.object(player, "progress_updater", mock.AsyncMock()),
            mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=self.vc)),
            mock.patch.object(autoplay, "find_next", mock.AsyncMock(return_value=None)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        state.queues[G] = [(A, "Song A"), (B, "Song B")]
        await player.play_next(G)
        await self.drain()

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

    def last_ffmpeg(self):
        args, kwargs = self.ffmpeg.call_args
        return args[0], kwargs["before_options"], kwargs["options"]

    def elapsed(self):
        return webcore.get_guild_status(G)["elapsed"]


class SeekFfmpegOptionsTest(unittest.TestCase):
    def test_before_options(self):
        self.assertEqual(audio.before_options(), audio.RECONNECT_OPTIONS)
        self.assertEqual(audio.before_options(0), audio.RECONNECT_OPTIONS)
        b = audio.before_options(83.5)
        self.assertTrue(b.startswith("-ss 83.500 "), b)   # 輸入端跳轉：-ss 在 -i 之前
        self.assertIn("-reconnect 1", b)

    def test_filters_keep_normalize_short_fade(self):
        audio.set_normalize(1, True)
        o = audio.ffmpeg_options(1, fade_in=audio.SEEK_FADE_IN_SECONDS)
        self.assertIn(audio.NORMALIZE_FILTER, o)
        self.assertIn("afade=t=in:st=0:d=0.05", o)
        self.assertNotIn("d=1", o)
        audio.set_normalize(1, False)
        o = audio.ffmpeg_options(1, fade_in=audio.SEEK_FADE_IN_SECONDS)
        self.assertNotIn("dynaudnorm", o)
        self.assertIn("afade", o)
        self.assertEqual(audio.ffmpeg_options(1, fade_in=0), "-vn")
        audio.normalize_guilds.clear()

    def test_parse_time(self):
        P = utils.parse_time_str
        self.assertEqual(P("83"), 83)
        self.assertEqual(P("83.5"), 83.5)
        self.assertEqual(P("1:23"), 83)
        self.assertEqual(P(" 01:02:03 "), 3723)
        self.assertEqual(P("0:00"), 0)
        for bad in ("", "abc", "1:75", "1:60:00", "-5", "1:-2", "1:2:3:4", "nan", "inf", None, "1::2"):
            self.assertIsNone(P(bad), bad)
        S = player.parse_seek_position
        self.assertEqual(S(12), 12.0)
        self.assertEqual(S("2:00"), 120.0)
        for bad in (True, None, -1, float("nan"), float("inf"), "x", [], {}):
            self.assertIsNone(S(bad), bad)


class SeekPlayingTest(SeekTestBase):
    async def test_seek_while_playing_swaps_source(self):
        self.assertEqual(self.calls, [A, B])
        old_src = self.vc.source
        n = self.ffmpeg.call_count
        with mock.patch.object(player, "_cleanup_later") as cl:
            r = await player.seek(G, 60)
        self.assertEqual(r, {"status": "ok", "position": 60.0, "paused": False})
        self.assertEqual(self.ffmpeg.call_count, n + 1)
        url, before, opts = self.last_ffmpeg()
        self.assertIn("AAAAAAAAAAA", url)                 # 用快取的串流網址
        self.assertEqual(self.calls, [A, B])               # 沒有重新解析
        self.assertTrue(before.startswith("-ss 60.000 "))
        self.assertIn("afade=t=in:st=0:d=0.05", opts)
        self.assertEqual(self.vc.swaps, 1)                 # 替換來源，不是 stop + play
        self.assertTrue(self.vc.playing)
        self.assertIs(state.volume_transformers[G], self.vc.source)
        cl.assert_called_once_with(old_src)                # 舊 FFmpeg 稍後才清理
        self.assertAlmostEqual(self.elapsed(), 60, delta=1)
        s = webcore.get_guild_status(G)
        self.assertTrue(s["is_playing"] and not s["is_paused"])
        self.assertEqual(s["current_url"], A)
        self.assertEqual(state.queues[G], [(B, "Song B")])   # 佇列沒被動到（沒有切歌）

    async def test_volume_and_normalize_preserved(self):
        state.volumes[G] = 0.42
        audio.set_normalize(G, True)
        await player.seek(G, "1:00")
        _, _, opts = self.last_ffmpeg()
        self.assertIn("dynaudnorm", opts)
        self.assertEqual(self.pcm.call_args.kwargs["volume"], 0.42)

    async def test_old_source_cleanup_is_deferred(self):
        old_src = self.vc.source
        await player.seek(G, 30)
        old_src.cleanup.assert_not_called()              # 立即 kill 會讓播放執行緒以為播完而切歌
        await asyncio.sleep(1.2)
        await asyncio.sleep(0.05)
        old_src.cleanup.assert_called_once()

    async def test_bounds(self):
        for bad in (-1, "abc", float("nan"), None, True, "1:75"):
            r = await player.seek(G, bad)
            self.assertEqual(r["code"], "invalid", bad)
        r = await player.seek(G, 211)                     # 長度 210
        self.assertEqual(r["code"], "out_of_range")
        self.assertEqual(self.vc.swaps, 0)
        r = await player.seek(G, 210)                     # 剛好結尾 → 夾到最後 1 秒
        self.assertEqual(r["position"], 209.0)
        r = await player.seek(G, 0)
        self.assertEqual(r["position"], 0.0)
        self.assertEqual(self.last_ffmpeg()[1], audio.RECONNECT_OPTIONS)

    async def test_unseekable_and_not_playing(self):
        state.song_durations[G] = 0
        self.assertEqual((await player.seek(G, 10))["code"], "unseekable")
        state.song_durations[G] = 210
        self.vc.playing = False
        self.assertEqual((await player.seek(G, 10))["code"], "not_playing")
        state.now_playing.pop(G)
        self.assertEqual((await player.seek(G, 10))["code"], "not_playing")

    async def test_busy_while_switching(self):
        lock = state.play_locks.setdefault(G, asyncio.Lock())
        async with lock:
            self.assertEqual((await player.seek(G, 10))["code"], "busy")

    async def test_expired_stream_url_is_re_resolved(self):
        state.now_playing_stream[G]["expires_at"] = time.time() - 1
        prefetch._cache.get(G, {}).pop(A, None)
        r = await player.seek(G, 50)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(self.calls, [A, B, A])            # 重新解析目前這首
        self.assertGreater(state.now_playing_stream[G]["expires_at"], time.time() + 3600)
        self.assertTrue(self.last_ffmpeg()[1].startswith("-ss 50.000"))

    async def test_resolve_failure_reports_error(self):
        state.now_playing_stream[G]["expires_at"] = time.time() - 1
        prefetch._cache.get(G, {}).pop(A, None)

        def boom(url):
            raise RuntimeError("HTTP Error 403: Forbidden")
        with mock.patch.object(prefetch, "extract_stream", boom):
            r = await player.seek(G, 50)
        self.assertEqual(r["code"], "failed")
        self.assertEqual(self.vc.swaps, 0)
        self.assertLess(self.elapsed(), 3)                 # 進度沒被改

    async def test_song_changed_during_resolve(self):
        state.now_playing_stream[G]["expires_at"] = time.time() - 1
        prefetch._cache.get(G, {}).pop(A, None)
        real = prefetch.resolve

        async def slow_resolve(gid, url):
            state.progress_versions[G] += 1               # 期間換歌了
            return await real(gid, url)
        with mock.patch.object(prefetch, "resolve", slow_resolve):
            r = await player.seek(G, 50)
        self.assertEqual(r["code"], "busy")
        self.assertEqual(self.vc.swaps, 0)

    async def test_play_next_clears_pending_seek(self):
        await webcore.web_toggle_play(G)                  # 暫停
        await player.seek(G, 100)
        self.assertIn(G, state.pending_seeks)
        self.vc.stop()
        await player.play_next(G)                         # 換歌
        self.assertNotIn(G, state.pending_seeks)
        self.assertEqual(state.now_playing_stream[G]["page"], B)


class SeekPausedTest(SeekTestBase):
    async def test_seek_while_paused_stays_paused(self):
        r = await webcore.web_toggle_play(G)
        self.assertEqual(r["status"], "paused")
        n = self.ffmpeg.call_count
        r = await player.seek(G, 100)
        self.assertEqual(r, {"status": "ok", "position": 100.0, "paused": True})
        await player.wait_background()
        self.assertEqual(self.ffmpeg.call_count, n + 1)   # 暫停中：背景預熱新位置的 FFmpeg…
        self.assertEqual(self.vc.swaps, 0)                # …但還不換上（不出聲）
        self.assertIn("src", state.pending_seeks[G])
        self.assertTrue(self.vc.is_paused())
        s = webcore.get_guild_status(G)
        self.assertTrue(s["is_paused"] and not s["is_playing"])
        self.assertEqual(s["elapsed"], 100)
        await asyncio.sleep(0.05)
        self.assertEqual(webcore.get_guild_status(G)["elapsed"], 100)   # 暫停中不前進
        # 再跳一次：以最後一次為準
        first = state.pending_seeks[G]["src"]
        await player.seek(G, 120)
        self.assertEqual(webcore.get_guild_status(G)["elapsed"], 120)
        await player.wait_background()
        first.original.cleanup.assert_called()           # 上一個預熱的 FFmpeg 已收掉
        # 繼續播放 → 直接換上預熱好的來源（不必再開 FFmpeg）
        r = await webcore.web_toggle_play(G)
        self.assertEqual(r["status"], "resumed")
        self.assertTrue(r.get("accepted"))               # 換上 + 恢復在背景，HTTP 先回應
        self.assertTrue(webcore.get_guild_status(G)["is_playing"])   # 狀態先標成播放中
        await player.wait_background()
        self.assertEqual(self.ffmpeg.call_count, n + 2)
        self.assertEqual(self.vc.swaps, 1)
        self.assertTrue(self.last_ffmpeg()[1].startswith("-ss 120.000"))
        self.assertTrue(self.vc.is_playing())
        s = webcore.get_guild_status(G)
        self.assertTrue(s["is_playing"] and not s["is_paused"])
        self.assertAlmostEqual(s["elapsed"], 120, delta=1)
        self.assertNotIn(G, state.pending_seeks)

    async def test_resume_while_priming_uses_primed_source(self):
        """暫停中跳轉後馬上按繼續（預熱還沒好）：等預熱完成後直接換上，不另外重開 FFmpeg"""
        await asyncio.sleep(0)
        state.song_start_times[G] = time.time() - 30
        await webcore.web_toggle_play(G)                  # 暫停
        gate = asyncio.Event()
        src = mock.MagicMock()
        async def slow_primed(gid, url, pos):
            await gate.wait()
            return src
        restart = mock.AsyncMock(return_value=True)
        with mock.patch.object(player, "_primed_at", slow_primed), mock.patch.object(player, "_restart_at", restart):
            await player.seek(G, 90)
            r = await webcore.web_toggle_play(G)          # 預熱中按繼續 → 立刻回應
            self.assertEqual(r["status"], "resumed")
            await asyncio.sleep(0.01)
            gate.set()
            await player.wait_background()
        restart.assert_not_called()
        src.cleanup.assert_not_called()
        self.assertEqual(self.vc.swaps, 1)
        self.assertTrue(self.vc.is_playing())
        self.assertAlmostEqual(self.elapsed(), 90, delta=1)

    async def test_resume_failure_falls_back_to_original_position(self):
        await asyncio.sleep(0)
        state.song_start_times[G] = time.time() - 30
        await webcore.web_toggle_play(G)                  # 在 30 秒處暫停
        with mock.patch.object(player, "_primed_at", mock.AsyncMock(side_effect=RuntimeError("x"))), \
             mock.patch.object(player, "_restart_at", mock.AsyncMock(side_effect=RuntimeError("x"))):
            await player.seek(G, 150)                     # 背景預熱失敗
            await player.wait_background()
            await webcore.web_toggle_play(G)              # 恢復時重開也失敗
            await player.wait_background()
        self.assertTrue(self.vc.is_playing())
        self.assertAlmostEqual(self.elapsed(), 30, delta=1)   # 實際聲音仍在 30 秒 → 進度也回到 30


async def _run_and_drain(coro):
    r = await coro
    await player.wait_background()
    return r


class SeekApiTest(unittest.TestCase):
    """Flask 路由：狀態碼與回應格式（run_async 改成在新 loop 執行）"""
    def setUp(self):
        reset()
        state.pending_seeks.clear()
        state.now_playing_stream.clear()
        state.known_durations.clear()
        self.vc = SeekVC()
        self.vc.play(mock.MagicMock())
        state.voice_clients[G] = self.vc
        state.now_playing[G] = "Song A"
        state.now_playing_url[G] = A
        state.song_durations[G] = 210
        state.song_start_times[G] = time.time()
        state.is_playing_state[G] = True
        state.progress_versions[G] = 1
        state.now_playing_stream[G] = {"page": A, "url": "https://rr/x?expire=9999999999", "expires_at": time.time() + 9999}
        patches = [
            mock.patch.object(webapi, "run_async", lambda c: asyncio.run(_run_and_drain(c))),
            mock.patch.object(player.discord, "FFmpegPCMAudio", mock.MagicMock()),
            mock.patch.object(player.discord, "PCMVolumeTransformer", mock.MagicMock()),
            mock.patch.object(player, "_cleanup_later", mock.MagicMock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = webapi.flask_app.test_client()

    def post(self, body, path="/api/seek"):
        r = self.client.post(path, json=body)
        return r.status_code, r.get_json()

    def test_ok(self):
        code, j = self.post({"guild_id": str(G), "position": 42.5})
        self.assertEqual(code, 202, j)                  # 驗證完立刻回應，FFmpeg 在背景重開
        self.assertTrue(j["accepted"])
        self.assertEqual(j["status"], "ok")
        self.assertEqual(j["position"], 42.5)
        self.assertAlmostEqual(webcore.get_guild_status(G)["elapsed"], 42.5, delta=1)

    def test_errors(self):
        self.assertEqual(self.post({"position": 10})[0], 400)
        self.assertEqual(self.post({"guild_id": "abc", "position": 10})[0], 400)
        code, j = self.post({"guild_id": G, "position": "oops"})
        self.assertEqual((code, j["code"]), (400, "invalid"))
        code, j = self.post({"guild_id": G, "position": -3})
        self.assertEqual((code, j["code"]), (400, "invalid"))
        code, j = self.post({"guild_id": G, "position": 999})
        self.assertEqual((code, j["code"]), (400, "out_of_range"))
        self.vc.stop()
        code, j = self.post({"guild_id": G, "position": 10})
        self.assertEqual((code, j["code"]), (409, "not_playing"))
        self.assertTrue(j["message"])

    def test_queue_add_reports_position(self):
        self.vc.stop()
        state.now_playing.pop(G)
        state.control_panels[G] = mock.MagicMock()      # 有可通知的文字頻道 → 閒置時馬上開播
        with mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=self.vc)), \
             mock.patch.object(webcore, "play_next", mock.AsyncMock()):
            code, j = self.post({"guild_id": G, "url": A, "title": "Song A", "duration": 200}, "/api/queue/add")
            self.assertEqual((code, j["status"], j["position"], j["starts_now"]), (200, "added", 1, True))
            code, j = self.post({"guild_id": G, "url": B, "title": "Song B", "no_play": True}, "/api/queue/add")
            self.assertEqual((j["status"], j["position"], j["starts_now"]), ("added", 2, False))
            code, j = self.post({"guild_id": G, "url": A, "title": "Song A"}, "/api/queue/add")
            self.assertEqual((j["status"], j["position"]), ("duplicate", 1))
            code, j = self.post({"guild_id": G, "songs": [{"url": "https://www.youtube.com/watch?v=UUUUUUUUUU3", "title": "t3", "duration": 61}]}, "/api/queue/add-batch")
            self.assertEqual((j["count"], j["position"]), (1, 3))
        q = webcore.get_guild_status(G)["queue"]
        self.assertEqual([x["duration"] for x in q], [200, 0, 61])   # 已知長度；未知 = 0

    def test_remember_duration_bounds(self):
        state.known_durations.clear()
        for bad in (None, "x", -1, 0, 90000):
            state.remember_duration("u", bad)
        self.assertNotIn("u", state.known_durations)
        state.remember_duration("u", "125.7")
        self.assertEqual(state.known_durations["u"], 125)


if __name__ == "__main__":
    unittest.main()
