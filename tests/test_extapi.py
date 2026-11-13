"""Chrome 擴充功能 API：金鑰、CORS、網址正規化、加入 / 下一首播、狀態。"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import webapi, extapi, state  # noqa: E402

G = 5151
TOK = "t" * 40
H = {"X-Ext-Token": TOK, "Host": "musbot.kdns.fr"}


def fake_run_async(coro):
    import asyncio
    return asyncio.run(coro)


class ExtApiTest(unittest.TestCase):
    def setUp(self):
        self.c = webapi.flask_app.test_client()
        self.env = mock.patch.dict(os.environ, {"EXT_TOKEN": TOK})
        self.env.start()
        state.queues[G] = [("https://www.youtube.com/watch?v=AAAAAAAAAAA", "A")]
        self.p = [mock.patch.object(extapi, "_pick_guild", return_value=G),
                  mock.patch.object(extapi, "run_async", fake_run_async),
                  mock.patch.object(extapi, "_lookup_title", return_value="Looked Up")]
        for p in self.p: p.start()

    def tearDown(self):
        for p in self.p: p.stop()
        self.env.stop()
        state.queues.pop(G, None)

    def add(self, **body):
        async def fake_add(gid, url, title, auto_play=True, duration=None, requester="網頁", **kw):
            q = state.queues.setdefault(gid, [])
            for i, (u, _t) in enumerate(q):
                if u == url:
                    return {"duplicate": True, "position": i + 1, "message": "dup"}
            q.append((url, title))
            return {"ok": True, "position": len(q), "starts_now": False}
        with mock.patch.object(extapi, "web_add_to_queue", fake_add):
            return self.c.post("/api/ext/add", json=body, headers=H)

    def test_token_required(self):
        r = self.c.post("/api/ext/add", json={"url": "https://youtu.be/BBBBBBBBBBB"}, headers={"Host": "musbot.kdns.fr"})
        self.assertEqual(r.status_code, 401)
        r = self.c.get("/api/ext/status", headers={"Host": "musbot.kdns.fr", "X-Ext-Token": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_disabled_without_env(self):
        with mock.patch.dict(os.environ, {"EXT_TOKEN": ""}):
            self.assertEqual(self.c.get("/api/ext/status", headers=H).status_code, 503)

    def test_host_check_still_applies(self):
        r = self.c.get("/api/ext/status", headers={"X-Ext-Token": TOK, "Host": "1.2.3.4:5000"})
        self.assertEqual(r.status_code, 403)

    def test_cors_preflight(self):
        r = self.c.options("/api/ext/add", headers={"Host": "musbot.kdns.fr", "Origin": "chrome-extension://abc",
                                                     "Access-Control-Request-Method": "POST"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], "chrome-extension://abc")
        self.assertIn("X-Ext-Token", r.headers["Access-Control-Allow-Headers"])
        self.assertEqual(len(state.queues[G]), 1)          # 預檢不會真的加歌
        r = self.c.options("/api/ext/add", headers={"Host": "musbot.kdns.fr", "Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)

    def test_normalize(self):
        n = extapi.normalize_youtube
        want = "https://www.youtube.com/watch?v=BBBBBBBBBBB"
        for u in ("https://music.youtube.com/watch?v=BBBBBBBBBBB&list=RDAMVM",
                  "https://youtu.be/BBBBBBBBBBB?t=3", "https://www.youtube.com/shorts/BBBBBBBBBBB",
                  "https://m.youtube.com/watch?v=BBBBBBBBBBB&index=2"):
            self.assertEqual(n(u), want, u)
        for u in ("https://www.youtube.com/playlist?list=PL1", "https://evil.com/watch?v=BBBBBBBBBBB", "", None):
            self.assertIsNone(n(u))

    def test_add_appends_and_looks_up_title(self):
        r = self.add(url="https://music.youtube.com/watch?v=BBBBBBBBBBB")
        j = r.get_json()
        self.assertEqual(j["status"], "added"); self.assertEqual(j["position"], 2)
        self.assertEqual(state.queues[G][-1], ("https://www.youtube.com/watch?v=BBBBBBBBBBB", "Looked Up"))

    def test_play_next_moves_to_front(self):
        j = self.add(url="https://youtu.be/BBBBBBBBBBB", title="B", next=True).get_json()
        self.assertEqual(j["status"], "next")
        self.assertEqual(state.queues[G][0][1], "B")
        # 已在佇列的也能移到下一首
        state.queues[G].append(("https://www.youtube.com/watch?v=CCCCCCCCCCC", "C"))
        self.add(url="https://youtu.be/CCCCCCCCCCC", next=True)
        self.assertEqual(state.queues[G][0][1], "C")

    def test_rejects_non_youtube(self):
        self.assertEqual(self.add(url="https://example.com/x").status_code, 400)

    def test_status(self):
        j = self.c.get("/api/ext/status", headers=H).get_json()
        self.assertEqual(j["queue_length"], 1)
        self.assertFalse(j["connected"])


if __name__ == "__main__":
    unittest.main()
