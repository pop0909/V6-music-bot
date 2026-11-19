"""第六輪稽核修正。執行：python -m unittest discover -s tests

- 佇列「刪除 / 播放」帶網址：面板上的位置過時（上一首剛播完佇列前移、連點兩下、別人剛刪歌）
  時以網址為準，不會刪到 / 播到別首；這首已不在佇列 → 409 stale，佇列不動
- 本地歌單「播放」帶 expect_url：歌單被改過也不會加到別首
- 刪除 / 清空佇列後立刻重新瞄準預先解析
- waitress 單一請求內容上限（擋沒登入的人灌大檔塞爆磁碟）
"""
import asyncio
import http.client
import os
import sys
import threading
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from flask import Flask, request, jsonify  # noqa: E402
from musicbot import webapi, webcore, state, webserver  # noqa: E402
from musicbot import webapi_queue  # noqa: E402

G = 6161
U = "https://www.youtube.com/watch?v=AUDIT6SONG%d"


def q_titles():
    return [t for _, t in state.queues[G]]


class QueueRemoveByUrl(unittest.TestCase):
    def setUp(self):
        self._ld = webapi.flask_app.config.get("LOGIN_DISABLED")
        webapi.flask_app.config["LOGIN_DISABLED"] = True
        self.c = webapi.flask_app.test_client()
        state.queues[G] = [(U % i, "S%d" % i) for i in range(1, 6)]
        self.ens = mock.patch.object(webapi_queue, "_ensure_prefetch", mock.Mock())
        self.ensure = self.ens.start()

    def tearDown(self):
        self.ens.stop()
        state.queues.pop(G, None)
        webapi.flask_app.config["LOGIN_DISABLED"] = self._ld

    def rm(self, index, url=None):
        b = {"guild_id": G, "index": index}
        if url is not None:
            b["url"] = url
        return self.c.post("/api/queue/remove", json=b)

    def test_double_click_removes_only_one(self):
        """舊版：連點兩下 → 第二下用同一個位置，刪到下一首（實測 S2、S3 都不見）"""
        self.assertEqual(self.rm(1, U % 2).status_code, 200)
        r = self.rm(1, U % 2)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "stale")
        self.assertEqual(q_titles(), ["S1", "S3", "S4", "S5"])

    def test_stale_index_after_queue_shift(self):
        """面板畫面還是 [S1..S5]，但 S1 剛開始播（從佇列取走）→ 點 S3（位置 2）仍刪 S3，不是 S4"""
        state.queues[G].pop(0)
        self.assertEqual(self.rm(2, U % 3).status_code, 200)
        self.assertEqual(q_titles(), ["S2", "S4", "S5"])
        self.ensure.assert_called_with(G)

    def test_without_url_keeps_old_behaviour(self):
        """舊版擴充功能 / 其他呼叫者沒帶網址：照位置刪"""
        self.assertEqual(self.rm(0).status_code, 200)
        self.assertEqual(q_titles(), ["S2", "S3", "S4", "S5"])

    def test_bad_input_is_400_not_500(self):
        r = self.c.post("/api/queue/remove", json={"guild_id": "abc", "index": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(q_titles(), ["S1", "S2", "S3", "S4", "S5"])

    def test_clear_retargets_prefetch(self):
        self.c.post("/api/queue/clear", json={"guild_id": G})
        self.assertEqual(state.queues[G], [])
        self.ensure.assert_called_with(G)


class ResolveIndex(unittest.TestCase):
    def setUp(self):
        state.queues[G] = [(U % i, "S%d" % i) for i in range(1, 4)]

    def tearDown(self):
        state.queues.pop(G, None)

    def test_cases(self):
        r = webcore.resolve_queue_index
        self.assertEqual(r(G, 1, U % 2), 1)
        self.assertEqual(r(G, 0, U % 3), 2)        # 位置過時 → 找同一首
        self.assertIsNone(r(G, 1, U % 9))          # 不在佇列
        self.assertEqual(r(G, 2, None), 2)
        self.assertIsNone(r(G, 7, None))
        self.assertIsNone(r(G, -1, None))
        self.assertIsNone(r(999999, 0, U % 1))
        self.assertEqual(r(G, 0, 12345), 0)        # 網址不是字串 → 當沒帶


class QueuePlayByUrl(unittest.TestCase):
    def setUp(self):
        self._ld = webapi.flask_app.config.get("LOGIN_DISABLED")
        webapi.flask_app.config["LOGIN_DISABLED"] = True
        self.c = webapi.flask_app.test_client()
        state.queues[G] = [(U % i, "S%d" % i) for i in range(1, 6)]
        vc = mock.Mock()
        vc.is_playing.return_value = True
        vc.is_paused.return_value = False
        self.p = [mock.patch.object(webapi, "run_async", lambda c: asyncio.run(c)),
                  mock.patch.object(webcore._prefetch, "ensure", mock.Mock()),
                  mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=vc)),
                  mock.patch.object(webcore, "_spawn", lambda coro: coro.close())]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        state.queues.pop(G, None)
        webapi.flask_app.config["LOGIN_DISABLED"] = self._ld

    def play(self, index, url=None):
        b = {"guild_id": G, "index": index}
        if url is not None:
            b["url"] = url
        return self.c.post("/api/queue/play", json=b)

    def test_double_click_moves_only_one(self):
        """舊版：連點兩下「播放」S2 → 第二下把 S3 也插到最前面（實測 S3 排到 S1 前面）"""
        self.assertEqual(self.play(1, U % 2).status_code, 202)
        self.assertEqual(q_titles()[0], "S2")
        state.queues[G].pop(0)                    # play_next 取走 S2 開始播
        r = self.play(1, U % 2)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(q_titles(), ["S1", "S3", "S4", "S5"])

    def test_stale_index_plays_right_song(self):
        state.queues[G].pop(0)                    # 畫面還沒更新：S1 已被取走
        self.assertEqual(self.play(3, U % 4).status_code, 202)
        self.assertEqual(q_titles()[0], "S4")

    def test_without_url_old_behaviour(self):
        self.assertEqual(self.play(2).status_code, 202)
        self.assertEqual(q_titles()[0], "S3")


class PlaylistPlayExpect(unittest.TestCase):
    def setUp(self):
        self._ld = webapi.flask_app.config.get("LOGIN_DISABLED")
        webapi.flask_app.config["LOGIN_DISABLED"] = True
        self.c = webapi.flask_app.test_client()
        self.pl = [{"url": U % i, "title": "P%d" % i} for i in range(1, 4)]
        self.added = []

        async def fake_add(gid, url, title, **kw):
            self.added.append(title)
            return {"status": "ok", "position": 1}
        from musicbot import webapi_library
        self.p = [mock.patch.object(webapi, "local_playlist", self.pl),
                  mock.patch.object(webapi, "run_async", lambda c: asyncio.run(c)),
                  mock.patch.object(webapi_library, "web_add_to_queue", fake_add)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        webapi.flask_app.config["LOGIN_DISABLED"] = self._ld

    def post(self, **b):
        return self.c.post("/api/playlist/play", json=dict(guild_id=G, **b))

    def test_shifted_playlist_adds_the_clicked_song(self):
        del self.pl[0]                              # 別人剛刪掉第 1 首；畫面上 P3 還在位置 2
        self.assertEqual(self.post(index=2, expect_url=U % 3).status_code, 200)
        self.assertEqual(self.added, ["P3"])

    def test_gone_is_409(self):
        r = self.post(index=0, expect_url=U % 9)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.added, [])

    def test_no_expect_old_behaviour(self):
        self.assertEqual(self.post(index=1).status_code, 200)
        self.assertEqual(self.added, ["P2"])


try:
    import waitress  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False


@unittest.skipUnless(HAVE, "沒裝 waitress")
class BodyLimit(unittest.TestCase):
    """waitress 預設可收 1 GB，而且在登入檢查前就整個收進暫存檔"""
    def setUp(self):
        import socket
        app = Flask("bodylimit")

        @app.route("/echo", methods=["POST"])
        def echo():
            return jsonify(n=len(request.get_data()))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.srv = webserver.create(app, "127.0.0.1", self.port)
        threading.Thread(target=self.srv.run, daemon=True).start()

    def tearDown(self):
        try:
            self.srv.task_dispatcher.shutdown(timeout=3)
        except Exception:
            pass
        self.srv.close()

    def send(self, n):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            c.request("POST", "/echo", body=b"x" * n, headers={"Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_limit_value(self):
        self.assertGreaterEqual(webserver.MAX_BODY, 2 * 1024 * 1024)   # 500 首歌的批次要放得下
        self.assertLessEqual(webserver.MAX_BODY, 16 * 1024 * 1024)

    def test_normal_large_batch_ok(self):
        st, body = self.send(2 * 1024 * 1024)
        self.assertEqual(st, 200)
        self.assertIn(b'"n":2097152', body.replace(b" ", b""))

    def test_oversize_rejected_before_body(self):
        """只送標頭（宣告超大內容）就立刻回 413，不必等內容傳完、也不會收進暫存檔"""
        import socket
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(b"POST /echo HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                      b"Content-Length: %d\r\n\r\n" % (webserver.MAX_BODY + 1))
            data = s.recv(4096)
        self.assertTrue(data.startswith(b"HTTP/1.1 413"), data[:60])


class GuildRemove(unittest.TestCase):
    """機器人被踢出伺服器 / 伺服器改名"""
    def setUp(self):
        from musicbot import bot, idlesleep
        self.bot, self.idle = bot, idlesleep
        state.guild_names[G] = "舊名字"
        state.queues[G] = [(U % 1, "S1")]
        state.current_voice_channel[G] = {"id": 1, "name": "語音"}
        state.loop_modes[G] = "all"
        idlesleep.asleep[G] = {"since": 1}
        idlesleep.saved[G] = {"queue": [(U % 1, "S1")]}
        self._active = state.active_guild_id
        state.active_guild_id = G

    def tearDown(self):
        for d in (state.guild_names, state.queues, state.current_voice_channel, state.loop_modes,
                  self.idle.asleep, self.idle.saved):
            d.pop(G, None)
        state.active_guild_id = self._active

    def test_remove_clears_everything(self):
        g = mock.Mock(); g.id = G; g.name = "舊名字"
        from musicbot import player_voice
        with mock.patch.object(player_voice, "leave_voice", mock.AsyncMock(return_value=True)) as lv:
            asyncio.run(self.bot.on_guild_remove(g))
        lv.assert_awaited_once_with(G, g)
        self.assertNotIn(G, state.guild_names)
        self.assertNotIn(G, state.queues)
        self.assertNotIn(G, state.current_voice_channel)
        self.assertNotIn(G, self.idle.asleep)
        self.assertNotIn(G, self.idle.saved)
        self.assertIsNone(state.active_guild_id)
        self.assertNotIn(str(G), [x["id"] for x in webcore.get_guild_list()])

    def test_remove_survives_leave_failure(self):
        g = mock.Mock(); g.id = G; g.name = "x"
        from musicbot import player_voice
        with mock.patch.object(player_voice, "leave_voice", mock.AsyncMock(side_effect=RuntimeError("boom"))):
            asyncio.run(self.bot.on_guild_remove(g))
        self.assertNotIn(G, state.guild_names)

    def test_rename_updates_panel_list(self):
        b = mock.Mock(); b.id = G
        a = mock.Mock(); a.id = G; a.name = "新名字"
        asyncio.run(self.bot.on_guild_update(b, a))
        self.assertEqual(state.guild_names[G], "新名字")


if __name__ == "__main__":
    unittest.main()
