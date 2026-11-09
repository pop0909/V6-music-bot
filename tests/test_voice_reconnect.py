"""停止留在語音、/leave 強制斷線、殘留 VoiceClient 時仍能重新加入。執行：python -m unittest discover -s tests"""
import asyncio
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import player, state  # noqa: E402

G = 9191


class VC:
    def __init__(self, channel, connected=True):
        self.channel = channel
        self.connected = connected
        self.stops = 0
        self.disconnect = mock.AsyncMock(side_effect=self._dc)
        self.move_to = mock.AsyncMock(side_effect=lambda ch: setattr(self, "channel", ch))
        self.cleanup = mock.Mock()

    async def _dc(self, force=False):
        self.connected = False

    def is_connected(self): return self.connected
    def is_playing(self): return False
    def is_paused(self): return False
    def stop(self): self.stops += 1


class Channel:
    def __init__(self, cid, guild, fail=0):
        self.id, self.name, self.guild, self.fail = cid, f"ch{cid}", guild, fail
        self.calls = 0

    async def connect(self, **kw):
        self.calls += 1
        if self.calls <= self.fail:
            raise RuntimeError("Already connected to a voice channel.")
        vc = VC(self)
        self.guild.voice_client = vc
        return vc


class Guild:
    def __init__(self):
        self.id = G
        self.voice_client = None


class Reconnect(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for d in (state.voice_clients, state.current_voice_channel, state.queues, state.idle_leave_tasks):
            d.pop(G, None)
        self.sleep = mock.patch.object(player.asyncio, "sleep", mock.AsyncMock())
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    async def test_stale_voice_client_is_cleaned_then_connects(self):
        g = Guild(); ch = Channel(1, g)
        stale = VC(ch, connected=False)          # 已斷線但還掛在 guild.voice_client 上
        g.voice_client = stale
        state.voice_clients[G] = stale
        vc = await player.connect_voice(g, ch)
        stale.disconnect.assert_awaited()        # 先強制拆掉殘留連線
        self.assertTrue(vc.is_connected())
        self.assertIs(state.voice_clients[G], vc)
        self.assertEqual(state.current_voice_channel[G]["id"], 1)

    async def test_retry_on_connect_error(self):
        g = Guild(); ch = Channel(1, g, fail=2)
        vc = await player.connect_voice(g, ch)
        self.assertEqual(ch.calls, 3)
        self.assertTrue(vc.is_connected())

    async def test_gives_up_after_attempts(self):
        g = Guild(); ch = Channel(1, g, fail=99)
        with self.assertRaises(RuntimeError):
            await player.connect_voice(g, ch, attempts=3)
        self.assertNotIn(G, state.voice_clients)

    async def test_move_when_connected_elsewhere(self):
        g = Guild(); a = Channel(1, g); b = Channel(2, g)
        g.voice_client = VC(a)
        vc = await player.connect_voice(g, b)
        vc.move_to.assert_awaited_once()
        self.assertEqual(b.calls, 0)

    async def test_join_cancels_idle_leave_timer(self):
        g = Guild(); ch = Channel(1, g)
        t = asyncio.get_running_loop().create_task(asyncio.Event().wait())
        state.idle_leave_tasks[G] = t
        await player.connect_voice(g, ch)
        await asyncio.sleep(0)
        self.assertTrue(t.cancelled() or t.cancelling())

    async def test_stop_stays_in_voice(self):
        g = Guild(); ch = Channel(1, g)
        vc = VC(ch); g.voice_client = vc
        state.queues[G] = [("u", "t")]
        with mock.patch.object(player.audio, "fade_out_stop", mock.AsyncMock()), \
             mock.patch.object(player, "_update_panel", mock.AsyncMock()):
            await player.stop_playback(G, guild=g)
        self.assertEqual(state.queues[G], [])
        self.assertEqual(vc.stops, 1)
        vc.disconnect.assert_not_called()
        self.assertIs(state.voice_clients[G], vc)

    async def test_leave_then_rejoin(self):
        g = Guild(); ch = Channel(1, g)
        vc = VC(ch); g.voice_client = vc            # discord.py 斷線後有時不清 guild.voice_client
        with mock.patch.object(player.audio, "fade_out_stop", mock.AsyncMock()):
            await player.leave_voice(G, g)
        vc.disconnect.assert_awaited_with(force=True)
        new = await player.connect_voice(g, ch)
        self.assertTrue(new.is_connected())
        self.assertIsNot(new, vc)


if __name__ == "__main__":
    unittest.main()
