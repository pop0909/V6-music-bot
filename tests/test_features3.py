"""cookies / 機器人驗證、音量平衡與淡入淡出、播放紀錄 的單元測試（yt-dlp / ffmpeg 以假物件取代）
執行：python -m unittest discover -s tests
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import ytdl, audio, history, state, prefetch, player, webcore, autoplay  # noqa: E402
from musicbot import webapi as _webapi  # noqa: E402
_webapi.flask_app.config["LOGIN_DISABLED"] = True   # 這裡測 API 本身；登入另外在 test_webauth.py 測

G = 777
A = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
B = "https://www.youtube.com/watch?v=BBBBBBBBBBB"
BOT_MSG = ("ERROR: [youtube] AAAAAAAAAAA: Sign in to confirm you\u2019re not a bot. Use --cookies-from-browser "
           "or --cookies for the authentication.")


# ======================= yt-dlp cookies / 機器人驗證 =======================
class YtdlOptsTest(unittest.TestCase):
    def tearDown(self):
        ytdl.reset_cookie_runtime()

    def test_no_env_keeps_base(self):
        with mock.patch.dict(os.environ, {"YTDLP_COOKIES": "", "YTDLP_COOKIES_FROM_BROWSER": "", "YTDLP_PLAYER_CLIENT": ""}):
            base = {"quiet": True}
            out = ytdl.build_opts(base)
            self.assertEqual(out, {"quiet": True})
            self.assertIsNot(out, base)

    def test_cookiefile(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# Netscape HTTP Cookie File\n")
        try:
            with mock.patch.dict(os.environ, {"YTDLP_COOKIES": f.name, "YTDLP_COOKIES_FROM_BROWSER": "firefox"}):
                out = ytdl.build_opts({"quiet": True})
            self.assertNotEqual(out["cookiefile"], f.name)    # 執行時複本，不是主檔
            self.assertTrue(os.path.isfile(out["cookiefile"]))
            self.assertNotIn("cookiesfrombrowser", out)    # 檔案優先
        finally:
            os.unlink(f.name)

    def test_missing_cookiefile_ignored(self):
        with mock.patch.dict(os.environ, {"YTDLP_COOKIES": "/nope/cookies.txt", "YTDLP_COOKIES_FROM_BROWSER": ""}):
            out = ytdl.build_opts({})
        self.assertNotIn("cookiefile", out)

    def test_browser_and_player_client(self):
        with mock.patch.dict(os.environ, {"YTDLP_COOKIES": "", "YTDLP_COOKIES_FROM_BROWSER": "Firefox:work",
                                          "YTDLP_PLAYER_CLIENT": "default, mweb"}):
            out = ytdl.build_opts({})
        self.assertEqual(out["cookiesfrombrowser"], ("firefox", "work", None, None))
        self.assertEqual(out["extractor_args"], {"youtube": {"player_client": ["default", "mweb"]}})

    def test_all_call_sites_use_build_opts(self):
        """播放解析、預先解析、搜尋、網址解析、自動推薦都要經過 build_opts"""
        root = os.path.join(os.path.dirname(__file__), "..", "musicbot")
        for name in ("prefetch.py", "player.py", "autoplay.py", "webapi.py"):
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                src = fh.read()
            n_calls = src.count("yt_dlp.YoutubeDL(")
            self.assertGreater(n_calls, 0, name)
            self.assertEqual(n_calls, src.count("yt_dlp.YoutubeDL(ytdl.build_opts("), name)

    def test_detect_bot_check(self):
        self.assertTrue(ytdl.is_bot_check(BOT_MSG))
        self.assertTrue(ytdl.is_bot_check(Exception("Sign in to confirm you're not a bot")))
        self.assertFalse(ytdl.is_bot_check("HTTP Error 403: Forbidden"))
        self.assertFalse(ytdl.is_bot_check(None))
        self.assertIn("cookies", ytdl.friendly_error(BOT_MSG))
        self.assertEqual(ytdl.friendly_error("Video unavailable"), "Video unavailable")

    def test_report_rate_limited(self):
        ytdl._last_log[0] = 0
        with self.assertLogs("musicbot", "WARNING") as cm:
            ytdl.report_bot_check("x")
            ytdl.report_bot_check("x")
            ytdl.report_bot_check("x")
            ytdl.log.warning("end")
        self.assertEqual(sum("機器人" in m for m in cm.output), 1)


def reset_state():
    for d in (prefetch._cache, prefetch._inflight, prefetch._tasks, prefetch._gen,
              prefetch._autoplay_pick, prefetch._random_pick, prefetch._failed):
        d.clear()
    for name in ("queues", "now_playing", "voice_clients", "random_loop_guilds", "now_playing_url",
                 "now_playing_thumbnail", "song_start_times", "song_durations", "paused_position",
                 "progress_versions", "progress_tasks", "is_playing_state", "is_paused_state",
                 "loop_modes", "saved_queues", "loop_current", "manual_advance", "play_failures", "control_panels", "play_notices", "song_meta"):
        getattr(state, name).clear()
    state.local_playlist.clear()
    autoplay.autoplay_guilds.clear()
    autoplay.recent_history.clear()
    autoplay.failed_ids.clear()
    audio.normalize_guilds.clear()
    audio._fading.clear()


class FakeVC:
    def __init__(self):
        self.playing = False
        self.paused = False
        self.stops = 0

    def is_connected(self): return True
    def is_playing(self): return self.playing
    def is_paused(self): return self.paused

    def play(self, src, after=None, **kw):
        self.playing = True

    def stop(self):
        self.playing = False
        self.paused = False
        self.stops += 1


class PlayerHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "h.db")
        self.vc = FakeVC()
        state.voice_clients[G] = self.vc
        self.ch = mock.MagicMock()
        self.ch.send = mock.AsyncMock(return_value=mock.MagicMock())
        self.ffmpeg = mock.MagicMock()
        self.retries = []
        for p in [
            mock.patch.object(history, "DB_PATH", self.db),
            mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=self.ch)),
            mock.patch.object(player, "_voice_connected", lambda gid: True),
            mock.patch.object(player.discord, "FFmpegPCMAudio", self.ffmpeg),
            mock.patch.object(player.discord, "PCMVolumeTransformer", mock.MagicMock()),
            mock.patch.object(player, "progress_updater", mock.AsyncMock()),
            mock.patch.object(autoplay, "find_next", mock.AsyncMock(return_value=None)),
        ]:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        prefetch.clear(G)
        history.flush()   # 背景寫入完成後才刪暫存資料夾


class BotCheckPlayTest(PlayerHarness):
    async def test_bot_check_pauses_without_counting_failures(self):
        def boom(url):
            raise RuntimeError(BOT_MSG)
        ytdl._last_log[0] = 0
        created = []
        real_create = asyncio.create_task

        def spy(coro, *a, **k):
            created.append(getattr(coro, "__name__", ""))
            return real_create(coro, *a, **k)
        state.queues[G] = [(A, "Song A"), (B, "Song B")]
        with mock.patch.object(prefetch, "extract_stream", boom), \
             mock.patch.object(player.asyncio, "create_task", spy):
            await player.play_next(G)
            await player.play_next(G)           # 使用者又按了一次播放：仍被擋
        self.assertNotIn(G, state.play_failures)                 # 不計入連續失敗
        self.assertNotIn("_retry", created)                     # 不自動重試
        self.assertEqual(state.queues[G][0], (A, "Song A"))     # 歌放回最前面、沒被吃掉
        self.assertEqual(len(state.queues[G]), 2)
        self.assertFalse(state.is_playing_state.get(G))
        notice = webcore.get_guild_status(G)["notice"]
        self.assertEqual(notice["kind"], "bot_check")
        self.assertIn("cookies", notice["message"])
        sent = [c.args[0] for c in self.ch.send.await_args_list if c.args]
        self.assertEqual(sum("cookies" in m for m in sent), 1)  # Discord 只提示一次
        self.assertTrue(autoplay.is_enabled(G) == autoplay.AUTOPLAY_DEFAULT)

    async def test_notice_cleared_on_success(self):
        state.play_notices[G] = {"kind": "bot_check", "message": "x", "ts": 1}
        state.queues[G] = [(A, "Song A")]
        with mock.patch.object(prefetch, "extract_stream",
                               lambda u: {"url": "https://g/v?expire=%d" % (time.time() + 9999), "duration": 100,
                                          "thumbnail": "", "title": "t"}):
            await player.play_next(G)
        self.assertNotIn(G, state.play_notices)


# ======================= 音量平衡 / 淡入淡出 =======================
class AudioFilterTest(unittest.TestCase):
    def test_chain(self):
        self.assertEqual(audio.build_filter_chain(True), "dynaudnorm=f=150:g=15:p=0.9:m=10:b=1,afade=t=in:st=0:d=0.25")
        self.assertEqual(audio.build_filter_chain(False), "afade=t=in:st=0:d=0.25")
        self.assertEqual(audio.build_filter_chain(False, fade_in=1), "afade=t=in:st=0:d=1")
        self.assertEqual(audio.build_filter_chain(False, fade_in=0), "")

    def test_per_guild_toggle_and_default(self):
        audio.normalize_guilds.clear()
        self.assertEqual(audio.normalize_enabled(1), audio.NORMALIZE_DEFAULT)
        audio.set_normalize(1, False)
        self.assertFalse(audio.normalize_enabled(1))
        self.assertTrue(audio.normalize_enabled(2) == audio.NORMALIZE_DEFAULT)
        self.assertEqual(audio.ffmpeg_options(1), "-vn -af afade=t=in:st=0:d=0.25")
        audio.set_normalize(1, True)
        self.assertIn("dynaudnorm", audio.ffmpeg_options(1))

    def test_env_default_parsing(self):
        import importlib
        for val, want in (("0", False), ("off", False), ("1", True), ("", True)):
            with mock.patch.dict(os.environ, {"AUDIO_NORMALIZE": val}):
                importlib.reload(audio)
                self.assertEqual(audio.NORMALIZE_DEFAULT, want, val)
        importlib.reload(audio)


class FadeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_state()

    async def test_fade_ramps_then_stops(self):
        vc = FakeVC(); vc.playing = True
        tr = mock.MagicMock(); tr.volume = 0.8
        state.volume_transformers[G] = tr
        state.volumes[G] = 0.8
        seen = []
        orig_sleep = asyncio.sleep

        async def rec_sleep(t):
            seen.append(tr.volume)
            await orig_sleep(0)
        with mock.patch.object(audio.asyncio, "sleep", rec_sleep):
            await audio.fade_out_stop(G, vc, duration=0.5, step=0.025)
        self.assertEqual(vc.stops, 1)
        self.assertEqual(len(seen), 20)
        self.assertTrue(all(a >= b for a, b in zip(seen, seen[1:])))   # 單調遞減
        self.assertAlmostEqual(seen[-1], 0.0)
        self.assertEqual(state.volumes[G], 0.8)                       # 使用者設定的音量不變

    async def test_fade_only_does_not_stop(self):
        vc = FakeVC(); vc.playing = True
        tr = mock.MagicMock(); tr.volume = 1.0
        state.volume_transformers[G] = tr
        await audio.fade_out_stop(G, vc, duration=0.05, stop=False)
        self.assertEqual(vc.stops, 0)
        self.assertAlmostEqual(tr.volume, 0.0)

    async def test_paused_stops_immediately(self):
        vc = FakeVC(); vc.paused = True
        state.volume_transformers[G] = mock.MagicMock(volume=1.0)
        t = time.perf_counter()
        await audio.fade_out_stop(G, vc, duration=0.5)
        self.assertLess(time.perf_counter() - t, 0.1)
        self.assertEqual(vc.stops, 1)

    async def test_concurrent_skip_single_stop(self):
        vc = FakeVC(); vc.playing = True
        state.volume_transformers[G] = mock.MagicMock(volume=1.0)
        await asyncio.gather(audio.fade_out_stop(G, vc, duration=0.1), audio.fade_out_stop(G, vc, duration=0.1))
        self.assertEqual(vc.stops, 1)

    async def test_web_stop_fades_before_clearing(self):
        """停止：先淡出、再清狀態、最後才 stop（after_play 不會接著播下一首）"""
        vc = FakeVC(); vc.playing = True
        state.voice_clients[G] = vc
        state.queues[G] = [(A, "a")]
        tr = mock.MagicMock(volume=1.0)
        state.volume_transformers[G] = tr
        order = []

        async def fake_fade(gid, v, duration=0.5, step=0.025, stop=True):
            order.append(("fade", stop, list(state.queues.get(gid, []))))
        vc.disconnect = mock.AsyncMock()
        with mock.patch.object(webcore._audio, "fade_out_stop", fake_fade), \
             mock.patch.object(webcore, "_update_panel", mock.AsyncMock()):
            r = await webcore.web_stop(G)
            self.assertTrue(r.get("accepted"))               # 立刻回應，淡出在背景
            self.assertFalse(state.is_playing_state[G])      # 但狀態先標成已停止
            await player.wait_background()
        self.assertEqual(order, [("fade", False, [(A, "a")])])
        self.assertEqual(state.queues[G], [])
        self.assertEqual(vc.stops, 1)
        vc.disconnect.assert_not_called()                   # 停止不離開語音頻道


class FfmpegOptionsInPlayerTest(PlayerHarness):
    async def test_player_uses_filter_chain(self):
        audio.set_normalize(G, False)
        state.queues[G] = [(A, "Song A")]
        with mock.patch.object(prefetch, "extract_stream",
                               lambda u: {"url": "https://g/v?expire=%d" % (time.time() + 9999), "duration": 100,
                                          "thumbnail": "", "title": "t"}):
            await player.play_next(G)
        self.assertEqual(self.ffmpeg.call_args.kwargs["options"], "-vn -af afade=t=in:st=0:d=0.25")
        self.assertIn("-reconnect 1", self.ffmpeg.call_args.kwargs["before_options"])


# ======================= 播放紀錄 =======================
class HistoryDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "sub", "h.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_threshold(self):
        self.assertEqual(history.threshold_seconds(200), 30)
        self.assertEqual(history.threshold_seconds(40), 20)
        self.assertEqual(history.threshold_seconds(0), 30)

    def test_record_recent_top(self):
        now = time.time()
        rec = lambda url, title, ago, **kw: history.record_sync(1, url, title, played_at=now - ago, path=self.db, **kw)
        rec(A, "A old title", 40 * 86400)
        rec(A, "A", 3 * 86400)
        rec(A, "A", 100, requester="誠實")
        rec(B, "B", 10 * 86400, source="autoplay")
        rec(B, "B", 200, source="autoplay")
        history.record_sync(2, A, "other guild", path=self.db)
        r = history.recent(1, 10, path=self.db)
        self.assertEqual([x["title"] for x in r], ["A", "B", "A", "B", "A old title"])
        self.assertEqual(r[0]["requester"], "誠實")
        self.assertEqual(r[1]["source"], "autoplay")
        top_all = history.top(1, "all", path=self.db, now=now)
        self.assertEqual([(x["url"], x["plays"]) for x in top_all], [(A, 3), (B, 2)])
        self.assertEqual(top_all[0]["title"], "A")          # 用最近一次的標題
        self.assertEqual([(x["url"], x["plays"]) for x in history.top(1, "30d", path=self.db, now=now)], [(A, 2), (B, 2)])
        self.assertEqual([(x["url"], x["plays"]) for x in history.top(1, "7d", path=self.db, now=now)], [(A, 2), (B, 1)])

    def test_record_is_async_and_atomic(self):
        with mock.patch.object(history, "DB_PATH", self.db):
            futs = [history.record(5, f"https://x/{i}", f"t{i}", duration=100) for i in range(20)]
            history.flush()
            self.assertTrue(all(f.done() for f in futs))
            self.assertEqual(len(history.recent(5, 100)), 20)
            # 寫入失敗（例如型別錯誤）不會丟例外到呼叫端，也不會留下半筆
            bad = history.record(5, None, None, duration="x")
            history.flush()
            self.assertIsNone(bad.result())
            self.assertEqual(len(history.recent(5, 100)), 20)

    def test_xss_title_stored_verbatim(self):
        evil = '<img src=x onerror=alert(1)>'
        history.record_sync(1, A, evil, path=self.db)
        self.assertEqual(history.recent(1, path=self.db)[0]["title"], evil)   # 原樣存；前端用 textContent 顯示


class HistoryWatchTest(PlayerHarness):
    async def _play(self, duration=200):
        state.queues[G] = [(A, "Song A")]
        with mock.patch.object(prefetch, "extract_stream",
                               lambda u: {"url": "https://g/v?expire=%d" % (time.time() + 9999), "duration": duration,
                                          "thumbnail": "th.jpg", "title": "t"}):
            await player.play_next(G)

    async def test_counts_after_threshold_with_meta(self):
        state.note_song_meta(G, A, requester="誠實", source="user")
        with mock.patch.object(player, "_start_history_watch", lambda *a: None):
            await self._play()
        version = state.progress_versions[G]
        info = {"url": A, "title": "Song A", "thumbnail": "th.jpg", "duration": 200,
                "requester": "誠實", "source": "user"}
        state.song_start_times[G] = time.time() - 31     # 已播 31 秒
        ok = await player._history_watch(G, version, info, poll=0.01)
        self.assertTrue(ok)
        history.flush()
        rows = history.recent(G, path=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["title"], rows[0]["requester"], rows[0]["source"], rows[0]["duration"]),
                         ("Song A", "誠實", "user", 200))

    async def test_skipped_early_not_counted(self):
        with mock.patch.object(player, "_start_history_watch", lambda *a: None):
            await self._play()
        version = state.progress_versions[G]
        info = {"url": A, "title": "Song A", "thumbnail": "", "duration": 200, "requester": None, "source": "user"}
        task = asyncio.create_task(player._history_watch(G, version, info, poll=0.01))
        await asyncio.sleep(0.05)
        state.progress_versions[G] = version + 1           # 換歌了（只播了幾秒）
        self.assertFalse(await task)
        history.flush()
        self.assertEqual(history.recent(G, path=self.db), [])

    async def test_pause_does_not_count(self):
        with mock.patch.object(player, "_start_history_watch", lambda *a: None):
            await self._play(duration=40)                 # 門檻 = 20 秒
        version = state.progress_versions[G]
        state.song_start_times.pop(G)
        state.paused_position[G] = 10                    # 暫停在 10 秒
        info = {"url": A, "title": "Song A", "thumbnail": "", "duration": 40, "requester": None, "source": "user"}
        task = asyncio.create_task(player._history_watch(G, version, info, poll=0.01))
        await asyncio.sleep(0.1)
        self.assertFalse(task.done())
        state.paused_position[G] = 21
        self.assertTrue(await asyncio.wait_for(task, 1))

    async def test_autoplay_source_recorded(self):
        autoplay.set_enabled(G, True)
        autoplay.remember(G, B)
        state.queues[G] = []
        with mock.patch.object(autoplay, "find_next", mock.AsyncMock(return_value=(A, "推薦 A"))), \
             mock.patch.object(player, "_start_history_watch") as sw:
            with mock.patch.object(prefetch, "extract_stream",
                                   lambda u: {"url": "https://g/v?expire=%d" % (time.time() + 9999), "duration": 200,
                                              "thumbnail": "", "title": "t"}):
                await player.play_next(G)
        self.assertEqual(state.song_meta[G][A]["source"], "autoplay")
        sw.assert_called_once()


class HistoryApiTest(unittest.TestCase):
    def setUp(self):
        from musicbot import webapi
        self.webapi = webapi
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "h.db")
        p = mock.patch.object(history, "DB_PATH", self.db)
        p.start(); self.addCleanup(p.stop)
        self.client = webapi.flask_app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_endpoints(self):
        history.record_sync(9, A, "<b>A</b>", requester="u", path=self.db)
        history.record_sync(9, A, "<b>A</b>", path=self.db)
        history.record_sync(9, B, "B", source="autoplay", path=self.db)
        r = self.client.get("/api/history?guild_id=9").get_json()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(r["items"]), 3)
        self.assertEqual(r["items"][0]["title"], "B")
        self.assertEqual(r["items"][0]["source"], "autoplay")
        t = self.client.get("/api/history/top?guild_id=9&range=7d").get_json()
        self.assertEqual(t["range"], "7d")
        self.assertEqual([(i["title"], i["plays"]) for i in t["items"]], [("<b>A</b>", 2), ("B", 1)])
        bad = self.client.get("/api/history/top?guild_id=9&range=evil").get_json()
        self.assertEqual(bad["range"], "all")
        self.assertEqual(self.client.get("/api/history?guild_id=12345").get_json()["items"], [])

    def test_normalize_endpoint(self):
        with mock.patch.object(self.webapi, "run_async", lambda coro: asyncio.run(coro)):
            r = self.client.post("/api/normalize", json={"guild_id": 9, "enabled": False}).get_json()
        self.assertEqual(r, {"status": "ok", "normalize": False, "applies": "next"})   # 沒在播 → 下一首生效
        self.assertFalse(audio.normalize_enabled(9))
        self.assertEqual(self.client.post("/api/normalize", json={}).status_code, 400)

    def test_search_bot_check_message(self):
        class Boom:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, *a, **k): raise RuntimeError(BOT_MSG)
        with mock.patch.object(self.webapi.yt_dlp, "YoutubeDL", Boom):
            r = self.client.post("/api/search", json={"query": "x"})
            r2 = self.client.post("/api/resolve-url", json={"url": "https://youtu.be/AAAAAAAAAAA"})
        self.assertEqual(r.get_json()["code"], "bot_check")
        self.assertIn("cookies", r.get_json()["message"])
        self.assertEqual(r2.get_json()["code"], "bot_check")


if __name__ == "__main__":
    unittest.main()
