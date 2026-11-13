"""音量平衡播放中切換：沿用跳轉機制從目前位置重開 FFmpeg（不切歌、不重複）、暫停中切換、連按、
重開比預估慢時快轉補齊、開播中等待、直播 / 沒在播下一首套用；舊音效等化器設定檔不影響啟動。
執行：python -m unittest discover -s tests
"""
import asyncio
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

from musicbot import audio, player, state, webcore  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_playback_state import A, B, G  # noqa: E402
from test_seek import SeekTestBase  # noqa: E402


class FilterChainTest(unittest.TestCase):
    def test_chain(self):
        n = audio.NORMALIZE_FILTER
        self.assertEqual(audio.build_filter_chain(True), f"{n},afade=t=in:st=0:d=0.25")
        self.assertEqual(audio.build_filter_chain(False, fade_in=0.05), "afade=t=in:st=0:d=0.05")
        self.assertEqual(audio.build_filter_chain(False, fade_in=0), "")

    def test_options_record_applied(self):
        audio.applied_norm.pop(G, None)
        old = audio.normalize_guilds.get(G)
        try:
            audio.set_normalize(G, False)
            self.assertEqual(audio.ffmpeg_options(G, fade_in=0), "-vn")
            self.assertTrue(audio.filters_current(G))
            audio.set_normalize(G, True)
            self.assertFalse(audio.filters_current(G))
            self.assertIn("dynaudnorm", audio.ffmpeg_options(G))
            self.assertTrue(audio.filters_current(G))
        finally:
            audio.applied_norm.pop(G, None)
            if old is None:
                audio.normalize_guilds.pop(G, None)
            else:
                audio.normalize_guilds[G] = old

    def test_eq_is_gone(self):
        for name in ("EQ_PRESETS", "eq_preset", "set_eq", "applied_eq", "SETTINGS_FILE"):
            self.assertFalse(hasattr(audio, name), name)
        self.assertFalse(hasattr(webcore, "web_set_eq"))


class LegacySettingsTest(unittest.TestCase):
    def test_old_eq_file_removed_or_ignored(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        p = os.path.join(tmp, "audio_settings.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"eq": {"123": "bass"}, broken')
        self.assertTrue(audio.remove_legacy_settings(p))
        self.assertFalse(os.path.exists(p))
        self.assertFalse(audio.remove_legacy_settings(p))   # 沒有檔案：不報錯
        self.assertTrue(audio.LEGACY_SETTINGS_FILE.endswith(os.path.join("data", "audio_settings.json")))


class NormalizeLiveTest(SeekTestBase):
    async def asyncSetUp(self):
        self._norm_old = audio.normalize_guilds.get(G)
        audio.applied_norm.clear()
        await super().asyncSetUp()

    async def asyncTearDown(self):
        await player.wait_background()
        await super().asyncTearDown()
        self.vc.stop()
        state.voice_clients.pop(G, None)   # 別讓後面的同步測試看到「播放中」而去重開 FFmpeg
        audio.applied_norm.clear()
        if self._norm_old is None:
            audio.normalize_guilds.pop(G, None)
        else:
            audio.normalize_guilds[G] = self._norm_old

    async def test_normalize_toggle_applies_mid_song(self):
        """音量平衡：播放中切換 → 從目前位置重開（不切歌），下一首也用新設定"""
        state.song_start_times[G] = time.time() - 30
        on = audio.normalize_enabled(G)
        self.assertEqual(audio.applied_norm[G], on)
        n = self.ffmpeg.call_count
        with mock.patch.object(player, "_cleanup_later"):
            r = await webcore.web_set_normalize(G, not on)
            self.assertEqual(r, {"status": "ok", "normalize": (not on), "applies": "now"})
            await player.wait_background()
        self.assertEqual(self.ffmpeg.call_count, n + 1)
        _, before, opts = self.last_ffmpeg()
        self.assertGreater(float(before.split()[1]), 30.0)
        self.assertEqual("dynaudnorm" in opts, not on)
        self.assertIn("afade=t=in:st=0:d=0.05", opts)
        self.assertEqual(self.vc.swaps, 1)
        self.assertEqual(webcore.get_guild_status(G)["current_url"], A)
        self.assertAlmostEqual(self.elapsed(), float(before.split()[1]), delta=1)
        self.assertEqual(webcore.get_full_app_state(G)["normalize"], not on)
        # 同樣的值再送一次：不重開
        await webcore.web_set_normalize(G, not on)
        await player.wait_background()
        self.assertEqual(self.ffmpeg.call_count, n + 1)
        # 下一首
        self.vc.stop()
        await player.play_next(G)
        await self.drain()
        url, before, opts = self.last_ffmpeg()
        self.assertIn("BBBBBBBBBBB", url)
        self.assertEqual("dynaudnorm" in opts, not on)

    async def test_normalize_while_paused(self):
        self.vc.pause()
        state.paused_position[G] = 12.0
        state.song_start_times.pop(G, None)
        on = audio.normalize_enabled(G)
        r = await webcore.web_set_normalize(G, not on)
        self.assertEqual(r["applies"], "paused")
        await player.wait_background()
        self.assertTrue(self.vc.is_paused())
        self.assertEqual(self.vc.swaps, 0)
        _, before, opts = self.last_ffmpeg()
        self.assertTrue(before.startswith("-ss 12.000 "))
        self.assertEqual("dynaudnorm" in opts, not on)

    async def test_slow_restart_is_caught_up(self):
        """FFmpeg 重開比預估慢：新來源先快轉同樣長度，換上時接在舊來源正在播的位置"""
        state.song_start_times[G] = time.time() - 50
        player._reapply_lead = 0.2
        real_primed = player._primed_at

        async def slow_primed(gid, url, pos):
            await asyncio.sleep(0.6)
            ps = audio.PrimedSource(_FakePCM())
            ps.prime()
            return ps
        with mock.patch.object(player, "_primed_at", slow_primed), mock.patch.object(player, "_cleanup_later"):
            t0 = time.time()
            audio.set_normalize(G, not audio.normalize_enabled(G))
            r = await player.reapply_audio(G)
        self.assertEqual(r, "now")
        st = player.reapply_stats[G]
        self.assertAlmostEqual(st["lead_ms"], 140, delta=2)          # 0.2 × 0.7
        self.assertGreater(st["dropped_ms"], 400)                    # 慢了約 0.46 秒 → 快轉補齊
        self.assertLess(st["dropped_ms"], 700)
        # 進度 = 換上時舊來源播到的位置（50 + 經過時間），誤差 < 60 ms
        self.assertAlmostEqual(self.elapsed(), 50 + (time.time() - t0), delta=0.06)
        self.assertGreater(player._reapply_lead, 0.2)                # 預估往實測值靠
        self.assertIsNotNone(real_primed)

    async def test_rapid_toggles_settle_on_last(self):
        on = audio.normalize_enabled(G)
        n = self.ffmpeg.call_count
        for v in (not on, on, not on, on, not on):
            await webcore.web_set_normalize(G, v)
        await player.wait_background()
        self.assertEqual("dynaudnorm" in self.last_ffmpeg()[2], not on)
        self.assertLessEqual(self.ffmpeg.call_count - n, 5)
        self.assertTrue(self.vc.playing)
        self.assertEqual(webcore.get_guild_status(G)["current_url"], A)
        self.assertEqual(audio.applied_norm[G], not on)
        self.assertTrue(audio.filters_current(G))

    async def test_paused_then_resume(self):
        self.vc.pause()
        state.paused_position[G] = 30.0
        state.song_start_times.pop(G, None)
        on = audio.normalize_enabled(G)
        await webcore.web_set_normalize(G, not on)
        await player.wait_background()
        await player.resume_playback(G, self.vc)
        self.assertEqual(self.vc.swaps, 1)
        self.assertFalse(self.vc.is_paused())
        self.assertAlmostEqual(self.elapsed(), 30, delta=1)

    async def test_not_playing_applies_next(self):
        self.vc.stop()
        n = self.ffmpeg.call_count
        r = await webcore.web_set_normalize(G, not audio.normalize_enabled(G))
        await player.wait_background()
        self.assertEqual(r["applies"], "next")
        self.assertEqual(self.ffmpeg.call_count, n)

    async def test_live_stream_applies_next(self):
        state.song_durations[G] = 0
        n = self.ffmpeg.call_count
        r = await webcore.web_set_normalize(G, not audio.normalize_enabled(G))
        await player.wait_background()
        self.assertEqual(r["applies"], "live")
        self.assertEqual(self.ffmpeg.call_count, n)
        self.assertEqual(self.vc.swaps, 0)

    async def test_switch_while_song_is_starting_waits(self):
        on = audio.normalize_enabled(G)
        lock = state.play_locks.setdefault(G, asyncio.Lock())
        await lock.acquire()
        try:
            task = asyncio.create_task(player.reapply_audio(G))
            audio.set_normalize(G, not on)
            await asyncio.sleep(0.35)
            self.assertFalse(task.done())              # 正在開播：等
        finally:
            lock.release()
        self.assertEqual(await asyncio.wait_for(task, 5), "now")
        self.assertEqual("dynaudnorm" in self.last_ffmpeg()[2], not on)

    async def test_unchanged_does_nothing(self):
        n = self.ffmpeg.call_count
        self.assertEqual(await player.reapply_audio(G), "now")
        await webcore.web_set_normalize(G, audio.normalize_enabled(G))
        await player.wait_background()
        self.assertEqual(self.ffmpeg.call_count, n)
        self.assertEqual(self.vc.swaps, 0)


class _FakePCM:
    """假 FFmpeg 來源：每格 3840 bytes（20 ms 48k 立體聲 s16），內容 = 格號（看得出丟了幾格）"""
    def __init__(self, frames=10_000, value=1000):
        self.i, self.frames, self.value = 0, frames, value

    def read(self):
        if self.i >= self.frames:
            return b""
        self.i += 1
        import array as _a
        return _a.array("h", [self.value] * 1920).tobytes()

    def cleanup(self):
        pass


class DropFramesTest(unittest.IsolatedAsyncioTestCase):
    async def test_drop_and_fade(self):
        import array as _a
        src = audio.PrimedSource(_FakePCM())
        src.prime()
        got = await audio.drop_frames(src, 0.5)
        self.assertAlmostEqual(got, 0.5)
        self.assertEqual(src.original.i, 25)          # 第 1 格（預熱讀的）+ 再讀 24 格 = 丟掉 25 格
        f = [_a.array("h", src.read()) for _ in range(4)]
        self.assertEqual(f[0][0], 0)                  # 接上時從 0 淡入
        self.assertTrue(f[0][-1] <= f[1][0] < f[1][-1] <= f[2][0] < f[2][-1] <= 1000)
        self.assertEqual(f[0][0], f[0][1])               # 左右聲道同增益
        self.assertEqual(list(set(f[3])), [1000])     # 60 ms 後恢復原音量

    async def test_stops_at_end_and_caps(self):
        src = audio.PrimedSource(_FakePCM(frames=5))
        self.assertAlmostEqual(await audio.drop_frames(src, 1.0), 0.1)
        src2 = audio.PrimedSource(_FakePCM())
        self.assertAlmostEqual(await audio.drop_frames(src2, 60, max_seconds=2), 2.0)
        self.assertEqual(await audio.drop_frames(src2, 0.01), 0.0)

    @unittest.skipUnless(shutil.which("ffmpeg"), "沒有 ffmpeg")
    async def test_real_ffmpeg_drop_lines_up_exactly(self):
        """真 FFmpeg：-ss P 開檔後快轉 D 秒，接上的第一格 = 同一個 -ss P 串流第 D/0.02 格（逐位元相同：不重複、不漏）"""
        import discord
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        f = os.path.join(tmp, "noise.webm")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=12:c=white:seed=7:r=48000:a=0.3",
                        "-ac", "2", "-c:a", "libopus", "-b:a", "128k", f], check=True, timeout=60)
        P, D = 4.0, 1.24
        opts = "-vn"                                   # 不加濾鏡：比對原始樣本
        ref = discord.FFmpegPCMAudio(f, executable="ffmpeg", before_options=f"-ss {P:.3f}", options=opts)
        frames = [ref.read() for _ in range(80)]
        ref.cleanup()
        src = discord.FFmpegPCMAudio(f, executable="ffmpeg", before_options=f"-ss {P:.3f}", options=opts)
        ps = await audio.prime_source(src)
        dropped = await audio.drop_frames(ps, D)
        ps._fade = None                                # 比對樣本時不要淡入
        got = [ps.read() for _ in range(3)]
        ps.cleanup()
        n = round(dropped / audio.FRAME_SECONDS)
        self.assertEqual(n, 62)
        self.assertEqual(got, frames[n:n + 3])



if __name__ == "__main__":
    unittest.main()
