import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_playback_state  # noqa: F401
from musicbot import commands, idlesleep
from musicbot.state import voice_clients, now_playing, queues


class VC:
    def __init__(s, playing=True, paused=False):
        s.p, s.z = playing, paused
        s.channel = mock.Mock(); s.channel.name = "暈船勒戒所"
    def is_connected(s): return True
    def is_playing(s): return s.p
    def is_paused(s): return s.z


class WebStatusTest(unittest.TestCase):
    G = 987654321
    def setUp(self):
        self.tearDown()
    def tearDown(self):
        for d in (voice_clients, now_playing, queues): d.pop(self.G, None)

    def test_playing(self):
        voice_clients[self.G] = VC(); now_playing[self.G] = "歌"; queues[self.G] = [1, 2]
        line = commands._web_status_line(self.G)
        self.assertIn("正在播放", line); self.assertIn("佇列 2 首", line); self.assertIn("暈船勒戒所", line)

    def test_idle_in_voice(self):
        voice_clients[self.G] = VC(playing=False)
        self.assertIn("待機", commands._web_status_line(self.G))

    def test_asleep(self):
        with mock.patch.object(idlesleep, "is_asleep", return_value=True):
            line = commands._web_status_line(self.G)
        self.assertIn("休眠中", line); self.assertNotIn("🔊", line)

    def test_no_ip_in_source(self):
        src = open(commands.__file__, encoding="utf-8").read()
        self.assertNotIn(":5000", src)


if __name__ == "__main__":
    unittest.main()