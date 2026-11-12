"""閒置休眠：判斷閒置、休眠時存佇列並強制離開、喚醒、恢復。執行：python -m unittest discover -s tests"""
import os
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import idlesleep, state, player, webcore, webapi  # noqa: E402
webapi.flask_app.config["LOGIN_DISABLED"] = True   # 這裡測 API 本身；登入另外在 test_webauth.py 測
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_voice_reconnect import VC, Channel, Guild, G  # noqa: E402


class M:
    def __init__(self, bot): self.bot = bot


class FakeClient:
    def __init__(self, vcs, guild=None): self.voice_clients = vcs; self.guild = guild
    def get_guild(self, gid): return self.guild


def reset():
    idlesleep.asleep.clear(); idlesleep.saved.clear(); idlesleep._idle_since.clear()
    for d in (state.queues, state.voice_clients, state.now_playing, state.now_playing_url, state.current_voice_channel):
        d.pop(G, None)


class SleepTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()
        self.g = Guild(); self.g.name = "g"
        self.ch = Channel(1, self.g); self.ch.members = [M(True)]
        self.vc = VC(self.ch); self.vc.guild = self.g; self.g.voice_client = self.vc
        self.p = [mock.patch.object(player.audio, "fade_out_stop", mock.AsyncMock()),
                  mock.patch.object(player, "_update_panel", mock.AsyncMock()),
                  mock.patch("musicbot.bot.client", FakeClient([self.vc], self.g))]
        for x in self.p: x.start()

    def tearDown(self):
        for x in self.p: x.stop()
        reset()

    def test_idle_rules(self):
        self.assertTrue(idlesleep._is_idle(self.vc))              # 沒有真人
        self.ch.members = [M(False)]
        self.assertTrue(idlesleep._is_idle(self.vc))              # 有人但沒播、佇列空
        state.queues[G] = [("u", "t")]
        self.assertFalse(idlesleep._is_idle(self.vc))             # 有人、有佇列 → 在用

    async def test_sleeps_after_grace_and_saves_queue(self):
        state.queues[G] = [("u1", "A"), ("u2", "B")]
        with mock.patch.dict(os.environ, {"IDLE_SLEEP_MINUTES": "2"}):
            await idlesleep.check_once(now=1000)
            self.assertFalse(idlesleep.is_asleep(G))               # 寬限期內不睡
            await idlesleep.check_once(now=1000 + 121)
        self.assertTrue(idlesleep.is_asleep(G))
        self.vc.disconnect.assert_awaited_with(force=True)          # 強制離開語音
        self.assertEqual(idlesleep.saved_info(G)["count"], 2)
        self.assertEqual(state.queues[G], [])

    async def test_activity_resets_timer(self):
        await idlesleep.check_once(now=1000)
        self.ch.members = [M(False)]; state.queues[G] = [("u", "t")]
        await idlesleep.check_once(now=1100)
        self.assertNotIn(G, idlesleep._idle_since)

    async def test_join_wakes(self):
        idlesleep.asleep[G] = {"since": time.time()}
        self.g.voice_client = None
        await player.connect_voice(self.g, self.ch)
        self.assertFalse(idlesleep.is_asleep(G))

    async def test_restore_puts_queue_back(self):
        idlesleep.saved[G] = {"queue": [("u1", "A")], "position": 0, "ts": time.time(), "channel": {"id": 1, "name": "c"}}
        idlesleep.asleep[G] = {"since": time.time()}
        with mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=self.vc)):
            r = await idlesleep.restore(G, play=False)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(state.queues[G], [("u1", "A")])
        self.assertFalse(idlesleep.is_asleep(G))
        self.assertIsNone(idlesleep.saved_info(G))

    def test_web_post_wakes(self):
        idlesleep.asleep[G] = {"since": time.time()}
        webapi.flask_app.test_client().post("/api/volume", json={"guild_id": G, "level": 10})
        self.assertFalse(idlesleep.is_asleep(G))


if __name__ == "__main__":
    unittest.main()


class RestartResume(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()
        self.tmp = os.path.join(os.path.dirname(__file__), "_vs_test.json")
        self.p = mock.patch.object(idlesleep, "SESSION_FILE", self.tmp); self.p.start()
        self.g = Guild(); self.g.name = "g"
        self.ch = Channel(5, self.g); self.ch.members = [M(False)]
        self.g.get_channel = lambda cid: self.ch

    def tearDown(self):
        self.p.stop(); reset()
        if os.path.exists(self.tmp): os.remove(self.tmp)

    def _write(self, playing=True):
        import json
        json.dump({"ts": time.time(), "guilds": {str(G): {"channel": {"id": 5, "name": "c"},
                   "queue": [["u1", "A"]], "position": 30, "playing": playing}}}, open(self.tmp, "w"))

    async def test_rejoins_when_humans_present(self):
        self._write()
        with mock.patch("musicbot.bot.client", FakeClient([], self.g)), \
             mock.patch.object(idlesleep, "restore", mock.AsyncMock()) as rs:
            await idlesleep.resume_after_restart()
        self.assertTrue(self.g.voice_client and self.g.voice_client.is_connected())
        rs.assert_awaited_once()

    async def test_no_rejoin_when_empty(self):
        self._write(); self.ch.members = [M(True)]
        with mock.patch("musicbot.bot.client", FakeClient([], self.g)):
            await idlesleep.resume_after_restart()
        self.assertIsNone(self.g.voice_client)
        self.assertEqual(idlesleep.saved_info(G)["count"], 1)     # 佇列留著，可在網頁恢復

    def test_status_hides_channel_when_disconnected(self):
        state.current_voice_channel[G] = {"id": 5, "name": "c"}
        stale = VC(self.ch, connected=False)
        state.voice_clients[G] = stale
        with mock.patch("musicbot.bot.client", FakeClient([], None)), \
             mock.patch.object(webcore, "client", FakeClient([], None)):
            st = webcore.get_guild_status(G)
        self.assertIsNone(st["voice_channel"])
        self.assertFalse(st["is_connected"])
        self.assertEqual(st["last_voice_channel"]["id"], "5")

    def test_status_channel_id_is_string_for_big_ids(self):
        """Discord id 超過 2^53，用數字傳給瀏覽器會被四捨五入 → 「機器人在這」對不上"""
        big = 1309731500538986567
        state.current_voice_channel[G] = {"id": big, "name": "暈船勒戒所"}
        state.voice_clients[G] = VC(self.ch, connected=True)
        with mock.patch("musicbot.bot.client", FakeClient([], None)), \
             mock.patch.object(webcore, "client", FakeClient([], None)):
            st = webcore.get_guild_status(G)
        self.assertEqual(st["voice_channel"]["id"], str(big))
        self.assertEqual(st["last_voice_channel"]["id"], str(big))
        self.assertEqual(state.current_voice_channel[G]["id"], big)   # 內部狀態不受影響
