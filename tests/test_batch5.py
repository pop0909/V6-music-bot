"""佇列拖曳排序 API、/move、狀態頁、一鍵更新保護。執行：python -m unittest discover -s tests"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import webapi, sysinfo, state  # noqa: E402

G = 4242


class Reorder(unittest.TestCase):
    def setUp(self):
        self.c = webapi.flask_app.test_client()
        state.queues[G] = [("ua", "A"), ("ub", "B"), ("uc", "C")]

    def tearDown(self):
        state.queues.pop(G, None)

    def post(self, **b):
        return self.c.post("/api/queue/reorder", json=dict(guild_id=G, **b))

    def test_move(self):
        r = self.post(**{"from": 0, "to": 2, "url": "ua"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([t for _, t in state.queues[G]], ["B", "C", "A"])

    def test_stale_url_rejected(self):
        r = self.post(**{"from": 0, "to": 2, "url": "uc"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual([t for _, t in state.queues[G]], ["A", "B", "C"])

    def test_out_of_range(self):
        self.assertEqual(self.post(**{"from": 0, "to": 9}).status_code, 409)


class System(unittest.TestCase):
    def setUp(self):
        self.c = webapi.flask_app.test_client()
        sysinfo._update.update(running=False, last=0.0, result=None)

    def test_status_fields(self):
        d = self.c.get("/api/system").get_json()
        for k in ("uptime", "latency_ms", "voice_latency_ms", "guilds", "voice", "ytdlp", "ffmpeg", "update"):
            self.assertIn(k, d)

    def test_update_needs_confirm(self):
        self.assertEqual(self.c.post("/api/system/update", json={}).status_code, 400)

    def test_update_rate_limited(self):
        with mock.patch.object(sysinfo, "_do_update"):
            self.assertEqual(self.c.post("/api/system/update", json={"confirm": "UPDATE"}).status_code, 202)
            r = self.c.post("/api/system/update", json={"confirm": "UPDATE"})
            self.assertEqual(r.status_code, 429)
        sysinfo._update.update(running=False, last=0.0)


if __name__ == "__main__":
    unittest.main()


class VoiceLatency(unittest.TestCase):
    def _vc(self, gid, avg, lat=0.05):
        vc = mock.Mock(); vc.is_connected.return_value = True
        vc.guild.id = gid; vc.average_latency = avg; vc.latency = lat
        return vc

    def test_ms_and_fallback(self):
        from musicbot.bot import client
        vcs = [self._vc(1, 0.0421), self._vc(2, float("inf"), 0.033)]
        with mock.patch.object(type(client), "voice_clients", new_callable=mock.PropertyMock, return_value=vcs):
            self.assertEqual(sysinfo.voice_latency_ms(1), 42)
            self.assertEqual(sysinfo.voice_latency_ms(2), 33)     # 平均還沒有 → 用單次心跳
            self.assertIsNone(sysinfo.voice_latency_ms(3))        # 該伺服器不在語音
        with mock.patch.object(type(client), "voice_clients", new_callable=mock.PropertyMock, return_value=[]):
            self.assertIsNone(sysinfo.voice_latency_ms())
