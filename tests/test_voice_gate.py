"""跳過 / 停止 / 離開 / 控制面板按鈕：只有跟機器人在同一個語音頻道的人能用（擁有者不限；機器人不在語音不限）
假的 interaction，不連 Discord。執行：python -m unittest discover -s tests
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import commands, views, state, discord_ux, actionlog  # noqa: E402
from test_discord_ux import inter, texts, playing_vc, run, Base, G  # noqa: E402

MSG = "你要先加入機器人所在的語音頻道「語音二」才能操作"


def bot_elsewhere(it, playing=True):
    """機器人在另一個語音頻道「語音二」"""
    vc = mock.MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = playing
    vc.is_paused.return_value = False
    vc.channel = mock.MagicMock()
    vc.channel.name = "語音二"
    it.guild.voice_client = vc
    return vc


class Gate(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch("musicbot.discordauth.is_owner", return_value=False)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_bot_not_in_voice_allows(self):
        it = inter(in_voice=False)
        self.assertIsNone(discord_ux.voice_gate(it))
        vc = mock.MagicMock(); vc.is_connected.return_value = False
        self.assertIsNone(discord_ux.voice_gate(it, vc))

    def test_same_channel_allows(self):
        it = inter()
        playing_vc(it)
        self.assertIsNone(discord_ux.voice_gate(it))

    def test_other_channel_or_not_in_voice_blocked(self):
        it = inter()
        bot_elsewhere(it)
        self.assertEqual(discord_ux.voice_gate(it), MSG)
        it2 = inter(in_voice=False)
        bot_elsewhere(it2)
        self.assertEqual(discord_ux.voice_gate(it2), MSG)

    def test_channel_name_escaped(self):
        it = inter(in_voice=False)
        bot_elsewhere(it).channel.name = "a*b"
        self.assertIn(r"「a\*b」", discord_ux.voice_gate(it))

    def test_owner_bypasses(self):
        it = inter(in_voice=False)
        bot_elsewhere(it)
        with mock.patch("musicbot.discordauth.is_owner", return_value=True):
            self.assertIsNone(discord_ux.voice_gate(it))

    def test_owner_check_error_means_not_owner(self):
        it = inter(in_voice=False)
        bot_elsewhere(it)
        with mock.patch("musicbot.discordauth.is_owner", side_effect=RuntimeError("x")):
            self.assertEqual(discord_ux.voice_gate(it), MSG)


class Commands(Base):
    def setUp(self):
        super().setUp()
        self.own = mock.patch("musicbot.discordauth.is_owner", return_value=False)
        self.own.start()
        state.now_playing[G] = "正在播"
        state.now_playing_url[G] = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
        state.queues[G] = [("https://www.youtube.com/watch?v=BBBBBBBBBBB", "下一首")]

    def tearDown(self):
        self.own.stop()
        super().tearDown()

    def test_skip_blocked_from_other_channel(self):
        it = inter()
        bot_elsewhere(it)
        with mock.patch.object(commands.audio, "fade_out_stop", mock.AsyncMock()) as fo:
            run(commands.skip.callback(it))
        fo.assert_not_awaited()
        self.assertEqual(texts(it), [MSG])
        self.assertTrue(it.response.send_message.await_args.kwargs.get("ephemeral"))
        self.assertEqual(actionlog.recent(G), [])
        self.assertEqual(self.public(), [])          # 沒公告

    def test_skip_owner_from_anywhere(self):
        it = inter(in_voice=False)
        bot_elsewhere(it)
        with mock.patch("musicbot.discordauth.is_owner", return_value=True), \
             mock.patch.object(commands.audio, "fade_out_stop", mock.AsyncMock()) as fo:
            run(commands.skip.callback(it))
        fo.assert_awaited_once()

    def test_skip_same_channel_works(self):
        it = inter()
        playing_vc(it)
        with mock.patch.object(commands.audio, "fade_out_stop", mock.AsyncMock()) as fo:
            run(commands.skip.callback(it))
        fo.assert_awaited_once()

    def test_stop_blocked_from_other_channel(self):
        it = inter()
        bot_elsewhere(it)
        with mock.patch("musicbot.player.stop_playback", mock.AsyncMock()) as sp:
            run(commands.stop.callback(it))
        sp.assert_not_awaited()
        self.assertEqual(texts(it), [MSG])
        self.assertEqual(len(state.queues[G]), 1)

    def test_stop_allowed_when_bot_not_in_voice(self):
        it = inter(in_voice=False)
        with mock.patch.dict(state.voice_clients, {}, clear=False), \
             mock.patch("musicbot.player.stop_playback", mock.AsyncMock()) as sp:
            state.voice_clients.pop(G, None)
            run(commands.stop.callback(it))
        sp.assert_awaited_once()

    def test_leave_blocked_from_other_channel(self):
        it = inter()
        bot_elsewhere(it)
        with mock.patch("musicbot.player.leave_voice", mock.AsyncMock()) as lv:
            run(commands.leave.callback(it))
        lv.assert_not_awaited()
        self.assertEqual(texts(it), [MSG])

    def test_leave_same_channel_works(self):
        it = inter()
        playing_vc(it)
        with mock.patch("musicbot.player.leave_voice", mock.AsyncMock()) as lv, \
             mock.patch.object(commands, "_update_panel", mock.AsyncMock()):
            run(commands.leave.callback(it))
        lv.assert_awaited_once()


class PanelButtons(Base):
    def setUp(self):
        super().setUp()
        self.own = mock.patch("musicbot.discordauth.is_owner", return_value=False)
        self.own.start()

    def tearDown(self):
        self.own.stop()
        super().tearDown()

    def check(self, it):
        return run(views.MusicControlView(G).interaction_check(it))

    def test_other_channel_blocked(self):
        it = inter()
        bot_elsewhere(it)
        self.assertFalse(self.check(it))
        self.assertEqual(texts(it), [MSG])

    def test_not_in_voice_blocked(self):
        it = inter(in_voice=False)
        bot_elsewhere(it)
        self.assertFalse(self.check(it))
        self.assertEqual(texts(it), [MSG])

    def test_same_channel_ok(self):
        it = inter()
        playing_vc(it)
        self.assertTrue(self.check(it))

    def test_owner_ok_from_anywhere(self):
        it = inter(in_voice=False)
        bot_elsewhere(it)
        with mock.patch("musicbot.discordauth.is_owner", return_value=True):
            self.assertTrue(self.check(it))

    def test_bot_not_in_voice_explains(self):
        it = inter()
        self.assertFalse(self.check(it))
        self.assertIn("機器人不在語音頻道中", texts(it)[0])

    def test_other_guild_panel(self):
        it = inter()
        it.guild_id = G + 1
        self.assertFalse(self.check(it))
        self.assertEqual(texts(it), ["此面板不屬於這個伺服器"])


if __name__ == "__main__":
    unittest.main()
