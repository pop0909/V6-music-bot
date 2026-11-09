"""稽核修正：閒置時點佇列 / 跳過會開播、淡出中換歌不留 manual_advance、開播失敗收掉 FFmpeg。"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import state, player, webcore, audio  # noqa: E402

G = 8484


def clean():
    for d in (state.queues, state.manual_advance, state.volume_transformers, state.loop_modes):
        d.pop(G, None)


def idle_vc():
    vc = mock.Mock()
    vc.is_playing.return_value = False
    vc.is_paused.return_value = False
    vc.is_connected.return_value = True
    return vc


class AuditFixTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self): clean()
    def tearDown(self): clean()

    async def test_play_index_when_idle_starts_playback(self):
        state.queues[G] = [("a", "A"), ("b", "B")]
        pn = mock.AsyncMock()
        with mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=idle_vc())), \
             mock.patch.object(webcore, "play_next", pn), \
             mock.patch.object(webcore._prefetch, "ensure", mock.Mock()):
            r = await webcore.web_play_index(G, 1)
            await player.wait_background(1)
        self.assertEqual(r["status"], "playing")
        self.assertEqual(state.queues[G][0], ("b", "B"))
        pn.assert_awaited_once_with(G)

    async def test_skip_when_idle_with_queue_plays_next(self):
        state.queues[G] = [("a", "A")]
        pn = mock.AsyncMock()
        with mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=idle_vc())), \
             mock.patch.object(webcore, "play_next", pn):
            await webcore.web_skip(G)
            await player.wait_background(1)
        pn.assert_awaited_once_with(G)

    async def test_fade_superseded_leaves_no_manual_advance(self):
        vc = mock.Mock(); vc.is_playing.return_value = True; vc.is_paused.return_value = False
        tr = mock.Mock(); tr.volume = 1.0
        state.volume_transformers[G] = tr

        async def swap(*_a, **_k):
            state.volume_transformers[G] = mock.Mock()    # 淡出期間另一條路徑已換歌
        with mock.patch.object(audio.asyncio, "sleep", swap):
            await audio.fade_out_stop(G, vc, duration=0.04, step=0.02)
        self.assertNotIn(G, state.manual_advance)
        vc.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
