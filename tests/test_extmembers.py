"""擴充功能成員：雜湊儲存、啟用/停用、重設、擁有者限定的管理 API、點歌記錄名字。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import webapi, extapi, extmembers, state  # noqa: E402
webapi.flask_app.config["LOGIN_DISABLED"] = True   # 這裡測 API 本身；登入另外在 test_webauth.py 測

OWN = "o" * 40
G = 6262


class MembersBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = [mock.patch.object(extmembers, "FILE", os.path.join(self.tmp.name, "ext_members.json")),
                  mock.patch.dict(os.environ, {"EXT_TOKEN": OWN})]
        for p in self.p: p.start()
        extmembers._cache.update(mtime=None, members=[])

    def tearDown(self):
        for p in reversed(self.p): p.stop()
        extmembers._cache.update(mtime=None, members=[])
        self.tmp.cleanup()


class StoreTest(MembersBase):
    def test_create_hashes_and_authenticates(self):
        m, tok = extmembers.create("  小明  ")
        self.assertEqual(m["name"], "小明")
        raw = open(extmembers.FILE, encoding="utf-8").read()
        self.assertNotIn(tok, raw)
        self.assertNotIn("token_hash", json.dumps(extmembers.list_members()))
        self.assertEqual(extmembers.authenticate(tok)["name"], "小明")
        self.assertTrue(extmembers.authenticate(OWN)["owner"])
        self.assertIsNone(extmembers.authenticate("nope"))
        if os.name == "posix":
            self.assertEqual(os.stat(extmembers.FILE).st_mode & 0o777, 0o600)

    def test_disable_reset_delete(self):
        m, tok = extmembers.create("A")
        extmembers.set_enabled(m["id"], False)
        self.assertIsNone(extmembers.authenticate(tok))
        extmembers.set_enabled(m["id"], True)
        _, tok2 = extmembers.reset_token(m["id"])
        self.assertIsNone(extmembers.authenticate(tok))
        self.assertIsNotNone(extmembers.authenticate(tok2))
        extmembers.delete(m["id"])
        self.assertIsNone(extmembers.authenticate(tok2))
        with self.assertRaises(KeyError):
            extmembers.delete(m["id"])

    def test_names(self):
        extmembers.create("A")
        with self.assertRaises(ValueError):
            extmembers.create("A")
        with self.assertRaises(ValueError):
            extmembers.create("   ")

    def test_record_use(self):
        m, tok = extmembers.create("A")
        ident = extmembers.authenticate(tok)
        extmembers.record_use(ident, added=True)
        extmembers.record_use(ident, added=True)
        got = extmembers.list_members()[0]
        self.assertEqual(got["add_count"], 2)
        self.assertIsNotNone(got["last_used"])


class ApiTest(MembersBase):
    def setUp(self):
        super().setUp()
        self.c = webapi.flask_app.test_client()

    def req(self, method, path, tok=OWN, **kw):
        return getattr(self.c, method)("/api/ext/" + path, headers={"Host": "musbot.kdns.fr", "X-Ext-Token": tok}, **kw)

    def test_owner_crud(self):
        r = self.req("post", "members", json={"name": "小華"})
        self.assertEqual(r.status_code, 201)
        j = r.get_json(); mid, tok = j["member"]["id"], j["token"]
        self.assertEqual([m["name"] for m in self.req("get", "members").get_json()["members"]], ["小華"])
        self.assertEqual(self.req("post", f"members/{mid}", json={"action": "rename", "name": "阿華"}).get_json()["member"]["name"], "阿華")
        self.assertEqual(self.req("post", f"members/{mid}", json={"action": "disable"}).status_code, 200)
        self.assertEqual(self.req("get", "status", tok=tok).status_code, 401)
        self.assertEqual(self.req("post", f"members/{mid}", json={"action": "enable"}).status_code, 200)
        self.assertEqual(self.req("get", "status", tok=tok).get_json()["you"], "阿華")
        self.assertEqual(self.req("delete", f"members/{mid}").status_code, 200)
        self.assertEqual(self.req("delete", f"members/{mid}").status_code, 404)

    def test_member_cannot_manage(self):
        _, tok = extmembers.create("A")
        self.assertEqual(self.req("get", "members", tok=tok).status_code, 403)
        self.assertEqual(self.req("post", "members", tok=tok, json={"name": "B"}).status_code, 403)
        self.assertEqual(self.req("get", "members", tok="bad").status_code, 401)

    def test_add_records_requester(self):
        _, tok = extmembers.create("小明")
        seen = {}

        async def fake_add(gid, url, title, auto_play=True, duration=None, requester="網頁", **kw):
            seen["requester"] = requester
            return {"ok": True, "position": 1, "starts_now": True}
        import asyncio
        with mock.patch.object(extapi, "_pick_guild", return_value=G), \
             mock.patch.object(extapi, "run_async", lambda c: asyncio.run(c)), \
             mock.patch.object(extapi, "web_add_to_queue", fake_add):
            r = self.req("post", "add", tok=tok, json={"url": "https://youtu.be/BBBBBBBBBBB", "title": "B"})
        self.assertEqual(r.get_json()["status"], "added")
        self.assertEqual(seen["requester"], "小明")
        self.assertEqual(extmembers.list_members()[0]["add_count"], 1)

    def test_members_page_served(self):
        r = self.c.get("/members", headers={"Host": "musbot.kdns.fr"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("成員管理", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
