"""音效等化器（EQ 預設）：濾鏡鏈組合、真 ffmpeg 解析、每個伺服器存檔、播放中切換（沿用跳轉重開）、
暫停中切換、連按、下一首套用、API、/volume eq 選項。
執行：python -m unittest discover -s tests
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import audio, player, state, webcore, webapi  # noqa: E402
webapi.flask_app.config["LOGIN_DISABLED"] = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_playback_state import A, B, G  # noqa: E402
from test_seek import SeekTestBase  # noqa: E402

NORM = audio.NORMALIZE_FILTER


class _TmpSettings:
    def setUp(self):
        super().setUp() if hasattr(super(), "setUp") else None
        self._tmp = tempfile.mkdtemp()
        self._p = mock.patch.object(audio, "SETTINGS_FILE", os.path.join(self._tmp, "audio_settings.json"))
        self._p.start()
        audio.eq_guilds.clear()
        audio.applied_eq.clear()
        audio._settings_loaded = False

    def tearDown(self):
        self._p.stop()
        audio.eq_guilds.clear()
        audio.applied_eq.clear()
        audio._settings_loaded = False
        shutil.rmtree(self._tmp, ignore_errors=True)


class FilterChainTest(unittest.TestCase):
    def test_off_is_unchanged(self):
        # 原音 = 和加入等化器之前完全一樣
        self.assertEqual(audio.build_filter_chain(True), f"{NORM},afade=t=in:st=0:d=0.25")
        self.assertEqual(audio.build_filter_chain(True, eq="off"), f"{NORM},afade=t=in:st=0:d=0.25")
        self.assertEqual(audio.build_filter_chain(False, eq="off", fade_in=0), "")

    def test_tone_presets_go_before_normalizer(self):
        for k in ("bass", "vocal", "treble"):
            c = audio.build_filter_chain(True, eq=k)
            pre = audio.EQ_PRESETS[k]["pre"]
            self.assertTrue(c.startswith(pre + "," + NORM), c)
            self.assertNotIn("alimiter", c)               # 平衡器已限制峰值
            self.assertTrue(c.endswith("afade=t=in:st=0:d=0.25"))

    def test_boost_without_normalizer_adds_limiter(self):
        for k in ("bass", "vocal", "treble"):
            c = audio.build_filter_chain(False, eq=k, fade_in=0)
            self.assertEqual(c, audio.EQ_PRESETS[k]["pre"] + "," + audio.EQ_LIMITER)

    def test_night_goes_after_normalizer(self):
        c = audio.build_filter_chain(True, eq="night", fade_in=0.05)
        self.assertTrue(c.startswith(NORM + ",acompressor="), c)
        self.assertIn("alimiter=limit=0.7", c)
        self.assertTrue(c.endswith("afade=t=in:st=0:d=0.05"))
        c2 = audio.build_filter_chain(False, eq="night", fade_in=0)
        self.assertTrue(c2.startswith("acompressor="))
        self.assertEqual(c2.count("alimiter"), 1)

    def test_unknown_preset_falls_back_to_off(self):
        self.assertEqual(audio.build_filter_chain(True, eq="nope"), audio.build_filter_chain(True))

    def test_no_spaces_or_quotes(self):
        # options 會被 shlex 切開：濾鏡鏈不能有空白
        for k in audio.EQ_PRESETS:
            for n in (True, False):
                c = audio.build_filter_chain(n, eq=k)
                self.assertNotIn(" ", c)
                self.assertNotIn("'", c)

    def test_choices_order_and_labels(self):
        ch = audio.eq_choices()
        self.assertEqual([c["key"] for c in ch], ["off", "bass", "vocal", "treble", "night"])
        self.assertEqual([c["label"] for c in ch], ["原音", "重低音", "人聲", "高音清晰", "夜間"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "沒有 ffmpeg")
    def test_every_chain_parses_in_real_ffmpeg(self):
        for k in audio.EQ_PRESETS:
            for n in (True, False):
                for fade in (audio.FADE_IN_SECONDS, audio.SEEK_FADE_IN_SECONDS):
                    c = audio.build_filter_chain(n, eq=k, fade_in=fade)
                    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=0.5:r=48000",
                                        "-af", c, "-f", "s16le", "-ar", "48000", "-ac", "2", "-"],
                                       capture_output=True, timeout=30)
                    self.assertEqual(r.returncode, 0, f"{k} norm={n}: {r.stderr[-300:]!r}")
                    self.assertGreater(len(r.stdout), 48000 * 4 * 0.4)


class PersistenceTest(_TmpSettings, unittest.TestCase):
    def test_default_off(self):
        self.assertEqual(audio.eq_preset(G), "off")
        self.assertFalse(os.path.exists(audio.SETTINGS_FILE))

    def test_set_saves_and_reloads(self):
        self.assertEqual(audio.set_eq(G, "bass"), ("bass", True))
        self.assertEqual(audio.set_eq(G, "BASS "), ("bass", False))   # 不分大小寫；沒變就不重寫
        audio.set_eq(222, "night")
        with open(audio.SETTINGS_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"eq": {str(G): "bass", "222": "night"}})
        # 模擬重啟
        audio.eq_guilds.clear()
        audio._settings_loaded = False
        self.assertEqual(audio.eq_preset(G), "bass")
        self.assertEqual(audio.eq_preset(222), "night")
        self.assertEqual(audio.eq_preset(333), "off")
        self.assertIn("bass=g=6", audio.ffmpeg_options(G))

    def test_off_removes_entry(self):
        audio.set_eq(G, "vocal")
        audio.set_eq(G, "off")
        with open(audio.SETTINGS_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"eq": {}})

    def test_invalid_preset(self):
        for bad in ("", None, "loud", "../x"):
            with self.assertRaises(ValueError):
                audio.set_eq(G, bad)
        self.assertEqual(audio.eq_preset(G), "off")

    def test_corrupt_or_unknown_file_values(self):
        with open(audio.SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(audio.eq_preset(G), "off")
        audio._settings_loaded = False
        with open(audio.SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"eq": {str(G): "loud", "5": "treble"}}, f)
        self.assertEqual(audio.eq_preset(G), "off")
        self.assertEqual(audio.eq_preset(5), "treble")

    def test_no_tmp_left_behind(self):
        audio.set_eq(G, "treble")
        self.assertEqual(os.listdir(self._tmp), ["audio_settings.json"])


class EqPlaybackTest(_TmpSettings, SeekTestBase):
    async def asyncSetUp(self):
        _TmpSettings.setUp(self)
        await super().asyncSetUp()

    async def asyncTearDown(self):
        await player.wait_background()
        await super().asyncTearDown()
        self.vc.stop()
        state.voice_clients.pop(G, None)   # 別讓後面的同步測試看到「播放中」而去重開 FFmpeg
        _TmpSettings.tearDown(self)

    def setUp(self):   # IsolatedAsyncioTestCase：交給 asyncSetUp
        unittest.IsolatedAsyncioTestCase.setUp(self)

    def tearDown(self):
        unittest.IsolatedAsyncioTestCase.tearDown(self)

    async def test_first_song_uses_saved_preset(self):
        # setUp 開播時還是原音
        self.assertNotIn("bass=", self.last_ffmpeg()[2])
        self.assertEqual(audio.applied_eq[G], "off")

    async def test_switch_while_playing_restarts_at_current_position(self):
        state.song_start_times[G] = time.time() - 42
        old = self.vc.source
        n = self.ffmpeg.call_count
        with mock.patch.object(player, "_cleanup_later") as cl:
            r = await webcore.web_set_eq(G, "bass")
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["applies"], "now")
            self.assertEqual(r["label"], "重低音")
            await player.wait_background()
        self.assertEqual(self.ffmpeg.call_count, n + 1)
        url, before, opts = self.last_ffmpeg()
        self.assertIn("AAAAAAAAAAA", url)
        pos = float(before.split()[1])
        self.assertGreater(pos, 42.0)                  # 從目前位置 + 預估重開時間開始（不重複一段）
        self.assertLess(pos, 42.0 + player.REAPPLY_LEAD_MAX + 0.5)
        self.assertIn("bass=g=6", opts)
        self.assertIn("afade=t=in:st=0:d=0.05", opts)   # 只做極短淡入
        self.assertEqual(self.vc.swaps, 1)             # 換來源，不是 stop + play
        cl.assert_called_once_with(old)
        self.assertEqual(webcore.get_guild_status(G)["current_url"], A)   # 沒有切歌
        self.assertEqual(state.queues[G], [(B, "Song B")])
        self.assertAlmostEqual(self.elapsed(), pos, delta=1)
        self.assertEqual(webcore.get_full_app_state(G)["eq"], "bass")
        st = player.reapply_stats[G]
        self.assertTrue(st["ok"])
        self.assertLess(st["ms"], 1000)

    async def test_same_preset_does_nothing(self):
        n = self.ffmpeg.call_count
        r = await webcore.web_set_eq(G, "off")
        await player.wait_background()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(self.ffmpeg.call_count, n)
        self.assertEqual(self.vc.swaps, 0)

    async def test_rapid_switches_settle_on_last(self):
        n = self.ffmpeg.call_count
        for k in ("bass", "vocal", "treble", "night"):
            await webcore.web_set_eq(G, k)
        await player.wait_background()
        self.assertIn("acompressor", self.last_ffmpeg()[2])
        self.assertLessEqual(self.ffmpeg.call_count - n, 4)
        self.assertGreaterEqual(self.vc.swaps, 1)
        self.assertTrue(self.vc.playing)
        self.assertEqual(webcore.get_guild_status(G)["current_url"], A)
        self.assertEqual(audio.applied_eq[G], "night")

    async def test_switch_while_paused_applies_on_resume(self):
        self.vc.pause()
        state.paused_position[G] = 30.0
        state.song_start_times.pop(G, None)
        r = await webcore.web_set_eq(G, "vocal")
        self.assertEqual(r["applies"], "paused")
        await player.wait_background()
        self.assertTrue(self.vc.is_paused())           # 維持暫停、不出聲
        self.assertEqual(self.vc.swaps, 0)
        _, before, opts = self.last_ffmpeg()
        self.assertTrue(before.startswith("-ss 30.000 "))
        self.assertIn("equalizer=f=3000", opts)
        await player.resume_playback(G, self.vc)
        self.assertEqual(self.vc.swaps, 1)
        self.assertFalse(self.vc.is_paused())
        self.assertAlmostEqual(self.elapsed(), 30, delta=1)

    async def test_next_song_uses_new_preset(self):
        await webcore.web_set_eq(G, "treble")
        await player.wait_background()
        self.vc.stop()
        await player.play_next(G)
        await self.drain()
        url, before, opts = self.last_ffmpeg()
        self.assertIn("BBBBBBBBBBB", url)
        self.assertIn("treble=g=4", opts)
        self.assertIn("afade=t=in:st=0:d=0.25", opts)
        self.assertNotIn("-ss", before)

    async def test_not_playing_applies_next(self):
        self.vc.stop()
        n = self.ffmpeg.call_count
        r = await webcore.web_set_eq(G, "bass")
        await player.wait_background()
        self.assertEqual(r["applies"], "next")
        self.assertEqual(self.ffmpeg.call_count, n)

    async def test_live_stream_applies_next(self):
        state.song_durations[G] = 0
        n = self.ffmpeg.call_count
        r = await webcore.web_set_eq(G, "bass")
        await player.wait_background()
        self.assertEqual(r["applies"], "live")
        self.assertEqual(self.ffmpeg.call_count, n)
        self.assertEqual(self.vc.swaps, 0)

    async def test_switch_while_song_is_starting_waits(self):
        lock = state.play_locks.setdefault(G, asyncio.Lock())
        await lock.acquire()
        try:
            task = asyncio.create_task(player.reapply_audio(G))
            audio.set_eq(G, "bass")
            await asyncio.sleep(0.35)
            self.assertFalse(task.done())              # 正在開播：等
        finally:
            lock.release()
        self.assertEqual(await asyncio.wait_for(task, 5), "now")
        self.assertIn("bass=g=6", self.last_ffmpeg()[2])

    async def test_invalid_preset_rejected(self):
        r = await webcore.web_set_eq(G, "loud")
        self.assertEqual(r["status"], "error")
        self.assertEqual(audio.eq_preset(G), "off")


class EqApiTest(_TmpSettings, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.c = webapi.flask_app.test_client()

    def test_get_presets(self):
        r = self.c.get(f"/api/eq?guild_id={G}")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["eq"], "off")
        self.assertEqual(len(d["presets"]), 5)

    def test_post_errors(self):
        self.assertEqual(self.c.post("/api/eq", json={"preset": "bass"}).status_code, 400)
        self.assertEqual(self.c.post("/api/eq", json={"guild_id": "abc", "preset": "bass"}).status_code, 400)
        self.assertEqual(self.c.post("/api/eq", data="x", content_type="application/json").status_code, 400)
        r = self.c.post("/api/eq", json={"guild_id": G, "preset": "loud"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("未知", r.get_json()["message"])

    def test_post_ok_not_playing(self):
        with mock.patch.object(webapi, "run_async", lambda coro: asyncio.run(coro)):
            r = self.c.post("/api/eq", json={"guild_id": G, "preset": "night"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["eq"], "night")
        self.assertEqual(audio.eq_preset(G), "night")


CG = 98765   # 指令測試用另一個伺服器（不受前面播放測試留下的狀態影響）


class VolumeCommandEqTest(_TmpSettings, unittest.TestCase):
    def _it(self):
        it = mock.MagicMock()
        it.guild.id = CG
        it.user.display_name = "u"
        it.response.send_message = mock.AsyncMock()
        return it

    def test_eq_option(self):
        from musicbot import commands
        from discord import app_commands
        it = self._it()
        with mock.patch.object(commands, "_get_output_channel", mock.AsyncMock(return_value=None)):
            asyncio.run(commands.volume.callback(it, eq=app_commands.Choice(name="夜間", value="night")))
        self.assertEqual(audio.eq_preset(CG), "night")
        self.assertIn("夜間", it.response.send_message.call_args[0][0])

    def test_no_args_shows_current(self):
        from musicbot import commands
        audio.set_eq(CG, "bass")
        it = self._it()
        asyncio.run(commands.volume.callback(it))
        msg = it.response.send_message.call_args[0][0]
        self.assertIn("重低音", msg)
        self.assertIn("%", msg)

    def test_choices_registered(self):
        from musicbot import commands
        names = [c.name for c in commands.volume.parameters if c.name == "eq"]
        self.assertEqual(names, ["eq"])
        eqp = [c for c in commands.volume.parameters if c.name == "eq"][0]
        self.assertEqual([c.value for c in eqp.choices], list(audio.EQ_PRESETS))
        self.assertFalse(eqp.required)


if __name__ == "__main__":
    unittest.main()
