"""第二次稽核：單曲循環跳到底後不重播舊歌、標題上限、網域比對、不觸發 @everyone。"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import state, player, extapi, bot  # noqa: E402

G = 9393


class LoopOneEndTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        for d in (state.queues, state.loop_modes, state.loop_current, state.manual_advance, state.random_loop_guilds):
            d.pop(G, None)

    async def test_skip_at_end_clears_loop_current(self):
        state.loop_modes[G] = "one"
        state.loop_current[G] = ("old", "Old")
        state.manual_advance[G] = True
        state.queues[G] = []
        with mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=mock.Mock())), \
             mock.patch.object(player, "_autoplay_pick", mock.AsyncMock(return_value=None)), \
             mock.patch.object(player, "_update_panel", mock.AsyncMock()):
            await player._play_next_locked(G)
        self.assertNotIn(G, state.loop_current)


class ExtInputTest(unittest.TestCase):
    def test_lookalike_domain_rejected(self):
        self.assertIsNone(extapi.normalize_youtube("https://evilyoutube.com/watch?v=BBBBBBBBBBB"))
        self.assertEqual(extapi.normalize_youtube("https://youtube.com/watch?v=BBBBBBBBBBB"),
                         "https://www.youtube.com/watch?v=BBBBBBBBBBB")

    def test_title_cap(self):
        self.assertLessEqual(extapi.TITLE_MAX, 300)


class MentionTest(unittest.TestCase):
    def test_no_mass_mentions(self):
        am = bot.client.allowed_mentions
        self.assertIsNotNone(am)
        self.assertFalse(am.everyone)
        self.assertFalse(am.roles)
        self.assertFalse(am.users)


if __name__ == "__main__":
    unittest.main()
