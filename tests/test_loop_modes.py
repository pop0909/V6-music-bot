"""循環模式：停止後保留、全部循環開啟時包含目前歌曲、跳過會設定前進旗標、休眠快照不重複。"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import state, player, webcore, audio, idlesleep  # noqa: E402

G = 7373


def clean():
    for d in (state.queues, state.loop_modes, state.loop_current, state.manual_advance,
              state.now_playing_url, state.now_playing, state.saved_queues):
        d.pop(G, None)


class LoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self): clean()
    def tearDown(self): clean()

    async def test_mode_survives_stop(self):
        state.loop_modes[G] = "one"
        state.queues[G] = [("u", "t")]
        with mock.patch.object(player, "_update_panel", mock.AsyncMock()):
            await player._clear_guild_state(G)
        self.assertEqual(state.loop_modes[G], "one")
        self.assertEqual(state.queues[G], [])

    async def test_enable_all_includes_current_and_disable_removes(self):
        state.now_playing_url[G] = "cur"; state.loop_current[G] = ("cur", "C")
        state.queues[G] = [("b", "B")]
        await webcore.web_set_loop(G, "all")
        self.assertEqual(state.queues[G], [("b", "B"), ("cur", "C")])
        await webcore.web_set_loop(G, "none")
        self.assertEqual(state.queues[G], [("b", "B")])

    async def test_skip_sets_manual_advance(self):
        vc = mock.Mock(); vc.is_playing.return_value = True; vc.is_paused.return_value = False
        await audio.fade_out_stop(G, vc, duration=0)
        self.assertTrue(state.manual_advance.get(G))
        vc.stop.assert_called_once()

    async def test_stop_fade_does_not_set_advance(self):
        vc = mock.Mock(); vc.is_playing.return_value = True; vc.is_paused.return_value = False
        await audio.fade_out_stop(G, vc, duration=0, stop=False)
        self.assertNotIn(G, state.manual_advance)

    def test_sleep_snapshot_no_duplicate_in_loop_all(self):
        state.loop_modes[G] = "all"
        q = idlesleep._with_current_first(G, [("b", "B"), ("cur", "C")], "cur", "C")
        self.assertEqual(q, [("cur", "C"), ("b", "B")])


if __name__ == "__main__":
    unittest.main()
