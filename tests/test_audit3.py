"""Audit 3：慢速加入語音時先 defer、已回應後改 followup、Modal 走 connect_voice。"""
import asyncio
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import player, views  # noqa: E402


class Resp:
    def __init__(self):
        self.done = False
        self.calls = []

    def is_done(self):
        return self.done

    async def defer(self, **kw):
        if self.done: raise RuntimeError("InteractionResponded")
        self.done = True; self.calls.append("defer")

    async def send_message(self, msg, **kw):
        if self.done: raise RuntimeError("InteractionResponded")
        self.done = True; self.calls.append(("send", msg))


def make_inter(in_voice=True, bot_vc=None):
    i = mock.Mock()
    i.response = Resp()
    i.followup.send = mock.AsyncMock()
    ch = mock.Mock(); ch.name = "VC"
    i.user.voice = mock.Mock(channel=ch) if in_voice else None
    i.guild.voice_client = bot_vc
    i.guild.id = 4242
    return i, ch


class A3(unittest.TestCase):
    def test_not_in_voice(self):
        i, _ = make_inter(in_voice=False)
        self.assertIsNone(asyncio.run(player.get_voice_client(i)))
        self.assertEqual(i.response.calls[0][1], "你必須先加入語音頻道！")

    def test_defers_before_slow_connect_then_followup_on_fail(self):
        i, ch = make_inter()
        async def slow(g, c):
            self.assertTrue(i.response.done)  # 已先 defer
            raise TimeoutError("x")
        with mock.patch.object(player, "connect_voice", slow):
            self.assertIsNone(asyncio.run(player.get_voice_client(i)))
        self.assertEqual(i.response.calls, ["defer"])
        i.followup.send.assert_awaited()

    def test_no_defer_when_already_in_channel(self):
        i, ch = make_inter()
        vc = mock.Mock(); vc.is_connected.return_value = True; vc.channel = ch
        i.guild.voice_client = vc
        with mock.patch.object(player, "connect_voice", mock.AsyncMock(return_value=vc)):
            self.assertIs(asyncio.run(player.get_voice_client(i)), vc)
        self.assertEqual(i.response.calls, [])

    def test_process_and_play_skips_defer_when_done(self):
        i, _ = make_inter()
        i.response.done = True
        with mock.patch.object(player.yt_dlp, "YoutubeDL", side_effect=RuntimeError("stop")):
            try: asyncio.run(player._process_and_play(i, "q", 4242, mock.Mock()))
            except RuntimeError as e: self.assertEqual(str(e), "stop")

    def test_modal_uses_connect_voice(self):
        i, ch = make_inter()
        vc = mock.Mock()
        m = views.MusicLinkModal()
        with mock.patch.object(player, "connect_voice", mock.AsyncMock(return_value=vc)) as cv, \
             mock.patch.object(player, "_process_and_play", mock.AsyncMock()) as pp:
            m.link._value = "https://youtu.be/x"
            asyncio.run(m.on_submit(i))
        cv.assert_awaited_once()
        pp.assert_awaited_once()

    def test_modal_not_in_voice(self):
        i, _ = make_inter(in_voice=False)
        m = views.MusicLinkModal()
        with mock.patch.object(player, "connect_voice", mock.AsyncMock()) as cv:
            asyncio.run(m.on_submit(i))
        cv.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class EnvPort(unittest.TestCase):
    def test_bad_web_port_falls_back(self):
        import importlib
        from musicbot import config
        for bad in ("abc", "", "99999"):
            with mock.patch.dict(os.environ, {"WEB_PORT": bad}):
                importlib.reload(config)
                self.assertEqual(config.WEB_PORT, 5000)
        importlib.reload(config)
