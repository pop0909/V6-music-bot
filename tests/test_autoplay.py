"""自動推薦挑歌邏輯的單元測試（不連網，yt-dlp 結果用假資料）
執行：python -m unittest discover -s tests
"""
import asyncio
import os
import random
import sys
import unittest
from collections import deque
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import autoplay  # noqa: E402


def E(vid, title="Song", duration=200, **kw):
    d = {"id": vid, "title": title, "duration": duration}
    d.update(kw)
    return d


SEED = "AAAAAAAAAAA"


class ExtractIdTest(unittest.TestCase):
    def test_variants(self):
        f = autoplay.extract_video_id
        self.assertEqual(f("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(f("https://youtube.com/watch?v=dQw4w9WgXcQ&list=RDxx&t=3"), "dQw4w9WgXcQ")
        self.assertEqual(f("https://youtu.be/dQw4w9WgXcQ?si=abc"), "dQw4w9WgXcQ")
        self.assertEqual(f("https://www.youtube.com/shorts/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(f("https://music.youtube.com/watch?v=dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertIsNone(f("https://soundcloud.com/x/y"))
        self.assertIsNone(f("not a url"))
        self.assertIsNone(f(""))
        self.assertIsNone(f(None))


class PickTest(unittest.TestCase):
    def pick(self, entries, recent=(), queued=(), seed=SEED):
        return autoplay.pick_recommendation(entries, recent, seed, queued, rng=random.Random(0))

    def test_skips_seed_recent_queued(self):
        entries = [E(SEED), E("BBBBBBBBBBB"), E("CCCCCCCCCCC"), E("DDDDDDDDDDD", "Good")]
        r = self.pick(entries, recent=["BBBBBBBBBBB"], queued=["CCCCCCCCCCC"])
        self.assertEqual(r, ("https://www.youtube.com/watch?v=DDDDDDDDDDD", "Good"))

    def test_skips_long_and_live_and_blocklisted(self):
        entries = [
            E("BBBBBBBBBBB", duration=601),
            E("CCCCCCCCCCC", live_status="is_live"),
            E("DDDDDDDDDDD", title="Lofi 1 Hour Mix"),
            E("EEEEEEEEEEE", title="[Deleted video]"),
            E("FFFFFFFFFFF", title="OK", duration=None),  # 未知長度 → 放行
        ]
        self.assertEqual(self.pick(entries)[1], "OK")

    def test_none_when_no_candidates(self):
        self.assertIsNone(self.pick([E(SEED), E("BBBBBBBBBBB", duration=9999), None, {"title": "no id"}]))
        self.assertIsNone(self.pick([]))
        self.assertIsNone(self.pick(None))

    def test_random_among_first_five_only(self):
        entries = [E(f"{chr(66 + i) * 11}", title=f"T{i}") for i in range(10)]
        seen = set()
        for s in range(200):
            seen.add(autoplay.pick_recommendation(entries, (), SEED, (), rng=random.Random(s))[1])
        self.assertEqual(seen, {f"T{i}" for i in range(5)})


class HistoryTest(unittest.TestCase):
    def setUp(self):
        autoplay.recent_history.clear()
        autoplay.failed_ids.clear()
        autoplay.autoplay_guilds.clear()

    def test_remember_dedup_and_cap(self):
        for i in range(autoplay.HISTORY_SIZE + 10):
            autoplay.remember(1, f"https://youtu.be/{i:011d}")
        autoplay.remember(1, f"https://youtu.be/{5 + 20:011d}")
        h = autoplay.recent_history[1]
        self.assertEqual(len(h), autoplay.HISTORY_SIZE)
        self.assertEqual(h[-1], f"{25:011d}")
        self.assertEqual(list(h).count(f"{25:011d}"), 1)
        self.assertEqual(autoplay.last_seed(1), f"{25:011d}")

    def test_toggle_default_off(self):
        self.assertFalse(autoplay.AUTOPLAY_DEFAULT)
        self.assertFalse(autoplay.is_enabled(9))
        autoplay.set_enabled(9, True)
        self.assertTrue(autoplay.is_enabled(9))

    def test_find_next_uses_mocked_mix_and_excludes_history_and_failed(self):
        autoplay.remember(1, "https://youtu.be/BBBBBBBBBBB")
        autoplay.remember(1, f"https://youtu.be/{SEED}")
        autoplay.mark_failed(1, "https://youtu.be/CCCCCCCCCCC")
        fake = [E(SEED), E("BBBBBBBBBBB"), E("CCCCCCCCCCC"), E("DDDDDDDDDDD", "Next!")]
        with mock.patch.object(autoplay, "fetch_mix_entries", return_value=fake) as m:
            r = asyncio.run(autoplay.find_next(1, ["https://youtu.be/ZZZZZZZZZZZ"]))
        m.assert_called_once_with(SEED)
        self.assertEqual(r, ("https://www.youtube.com/watch?v=DDDDDDDDDDD", "Next!"))

    def test_find_next_handles_errors_and_no_seed(self):
        self.assertIsNone(asyncio.run(autoplay.find_next(2)))
        autoplay.remember(2, f"https://youtu.be/{SEED}")
        with mock.patch.object(autoplay, "fetch_mix_entries", side_effect=RuntimeError("HTTP 403")):
            self.assertIsNone(asyncio.run(autoplay.find_next(2)))


class PlayerIntegrationTest(unittest.TestCase):
    """_autoplay_pick：關閉時不推薦、不在語音不推薦、解析期間被停止也不推薦"""
    def setUp(self):
        from musicbot import player
        self.player = player
        autoplay.recent_history.clear()
        autoplay.autoplay_guilds.clear()
        autoplay.remember(7, f"https://youtu.be/{SEED}")
        player.queues.pop(7, None)

    def run_pick(self, connected_seq, enabled=True):
        autoplay.set_enabled(7, enabled)
        seq = iter(connected_seq)
        with mock.patch.object(self.player, "_voice_connected", side_effect=lambda g: next(seq)), \
             mock.patch.object(autoplay, "fetch_mix_entries", return_value=[E("DDDDDDDDDDD", "Rec")]):
            return asyncio.run(self.player._autoplay_pick(7))

    def test_enabled_connected(self):
        self.assertEqual(self.run_pick([True, True])[1], "Rec")

    def test_disabled(self):
        self.assertIsNone(self.run_pick([True, True], enabled=False))

    def test_not_in_voice(self):
        self.assertIsNone(self.run_pick([False]))

    def test_stopped_while_fetching(self):
        self.assertIsNone(self.run_pick([True, False]))


if __name__ == "__main__":
    unittest.main()
