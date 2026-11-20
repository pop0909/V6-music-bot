"""擴充功能點歌記成 Discord 名字：擁有者金鑰 → 擁有者的 Discord；成員金鑰 → 成員管理連結的 Discord；沒連結 → 金鑰名稱。
另測 discordauth.owner_discord_id / display_name（只查快取，不連網）。"""
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import webapi, extapi, extmembers, state, discordauth, userlists  # noqa: E402

G = 6161
TOK = "o" * 40
OWNER_DID = "111111111111111111"
FRIEND_DID = "222222222222222222"


def fake_run_async(coro):
    import asyncio
    return asyncio.run(coro)


class ExtDiscordNameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.c = webapi.flask_app.test_client()
        self.calls = []
        names = {OWNER_DID: "小明", FRIEND_DID: "阿華"}
        ps = [mock.patch.dict(os.environ, {"EXT_TOKEN": TOK}),
              mock.patch.object(extapi, "_pick_guild", return_value=G),
              mock.patch.object(extapi, "run_async", fake_run_async),
              mock.patch.object(extapi, "_lookup_title", return_value="Song"),
              mock.patch.object(discordauth, "owner_discord_id", return_value=OWNER_DID),
              mock.patch.object(discordauth, "display_name", side_effect=lambda d: names.get(str(d)))]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        state.queues[G] = []
        self.addCleanup(state.queues.pop, G, None)
        self.links = {}
        p = mock.patch.object(userlists, "discord_links", side_effect=lambda: dict(self.links))
        p.start(); self.addCleanup(p.stop)

    def add(self, token):
        async def fake_add(gid, url, title, auto_play=True, duration=None, requester="網頁", actor=None, **kw):
            self.calls.append((requester, actor))
            state.queues[gid].append((url, title))
            return {"ok": True, "position": len(state.queues[gid]), "starts_now": False}
        with mock.patch.object(extapi, "web_add_to_queue", fake_add):
            return self.c.post("/api/ext/add", json={"url": "https://youtu.be/AAAAAAAAAAA"},
                               headers={"X-Ext-Token": token, "Host": "musbot.kdns.fr"})


    def test_owner_key_records_owner_discord_name(self):
        self.assertEqual(self.add(TOK).status_code, 200)
        self.assertEqual(self.calls[-1], ("小明", ("小明", "ext", OWNER_DID)))
        j = self.c.get("/api/ext/status", headers={"X-Ext-Token": TOK, "Host": "musbot.kdns.fr"}).get_json()
        self.assertEqual(j["you"], "小明")
        self.assertTrue(j["you_discord"])

    def test_owner_key_without_discord_keeps_old_name(self):
        with mock.patch.object(discordauth, "owner_discord_id", return_value=None):
            self.add(TOK)
            j = self.c.get("/api/ext/status", headers={"X-Ext-Token": TOK, "Host": "musbot.kdns.fr"}).get_json()
        self.assertEqual(self.calls[-1][0], extmembers.OWNER["name"])
        self.assertEqual(self.calls[-1][1], (extmembers.OWNER["name"], "ext", None))
        self.assertFalse(j["you_discord"])

    def test_linked_member_uses_discord_name_unlinked_uses_key_name(self):
        ident = {"id": "m1", "name": "鑰匙名", "owner": False}
        with mock.patch.object(extmembers, "authenticate", return_value=dict(ident)):
            self.add("x" * 40)
            self.assertEqual(self.calls[-1], ("鑰匙名", ("鑰匙名", "ext", None)))
            self.links["m1"] = FRIEND_DID
            self.add("x" * 40)
            self.assertEqual(self.calls[-1], ("阿華", ("阿華", "ext", FRIEND_DID)))

    def test_linked_but_name_unknown_falls_back_to_key_name_keeps_did(self):
        ident = {"id": "m1", "name": "鑰匙名", "owner": False}
        self.links["m1"] = "333333333333333333"
        with mock.patch.object(extmembers, "authenticate", return_value=dict(ident)):
            self.add("x" * 40)
        self.assertEqual(self.calls[-1], ("鑰匙名", ("鑰匙名", "ext", "333333333333333333")))

    def test_identity_id_unchanged(self):
        out = extapi.with_discord_name({"id": "m1", "name": "鑰匙名", "owner": False})
        self.assertEqual(out["id"], "m1")
        self.links["m1"] = FRIEND_DID
        out = extapi.with_discord_name({"id": "m1", "name": "鑰匙名", "owner": False})
        self.assertEqual((out["id"], out["name"], out["key_name"], out["discord_id"]), ("m1", "阿華", "鑰匙名", FRIEND_DID))
        own = extapi.with_discord_name(dict(extmembers.OWNER))
        self.assertEqual((own["id"], own["owner"], own["name"]), ("owner", True, "小明"))
        self.assertIsNone(extapi.with_discord_name(None))

    def test_members_list_shows_discord_names(self):
        fake = [{"id": "m1", "name": "鑰匙名", "enabled": True}, {"id": "m2", "name": "沒連", "enabled": True}]
        self.links.update(m1=FRIEND_DID, owner=OWNER_DID)
        with mock.patch.object(extmembers, "list_members", return_value=fake):
            j = self.c.get("/api/ext/members", headers={"X-Ext-Token": TOK, "Host": "musbot.kdns.fr"}).get_json()
        by = {m["id"]: m for m in j["members"]}
        self.assertEqual((by["m1"]["discord_id"], by["m1"]["discord_name"]), (FRIEND_DID, "阿華"))
        self.assertIsNone(by["m2"]["discord_name"])
        self.assertEqual((j["owner_key_discord_id"], j["owner_key_discord_name"]), (OWNER_DID, "小明"))

    def test_members_page_renders_discord_name(self):
        import pathlib
        html = (pathlib.Path(__file__).resolve().parent.parent / "templates" / "members.html").read_text(encoding="utf-8")
        self.assertIn("m.discord_name", html)
        self.assertIn('id="ownerKeyName"', html)
        self.assertIn("owner_key_discord_name", html)


class DiscordAuthNameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        discordauth.reset_for_tests(os.path.join(self.tmp, "web_sessions.json"))
        self.addCleanup(discordauth.reset_for_tests)
        self.links = {}
        p = mock.patch.object(userlists, "discord_links", side_effect=lambda: dict(self.links))
        p.start(); self.addCleanup(p.stop)

    def bot(self, guilds=(), users=None, ready=True, owner=None):
        app = SimpleNamespace(team=None, owner=SimpleNamespace(id=int(owner)) if owner else None)
        users = users or {}
        return SimpleNamespace(is_ready=lambda: ready, loop=SimpleNamespace(is_closed=lambda: False), guilds=list(guilds),
                               get_user=lambda i: users.get(i), application=app)

    def guild(self, gid, members):
        return SimpleNamespace(id=gid, get_member=lambda i: members.get(i))

    def test_owner_discord_id_order(self):
        b = self.bot(owner=444)
        with mock.patch.object(discordauth, "_bot", return_value=b), mock.patch.dict(os.environ, {"OWNER_ID": ""}):
            self.assertEqual(discordauth.owner_discord_id(), "444")           # 應用程式擁有者
            with mock.patch.dict(os.environ, {"OWNER_ID": "555"}):
                self.assertEqual(discordauth.owner_discord_id(), "555")       # OWNER_ID 優先
                self.links["owner"] = "666"
                self.assertEqual(discordauth.owner_discord_id(), "666")       # 「我的 Discord」連結最優先
        with mock.patch.object(discordauth, "_bot", side_effect=RuntimeError), mock.patch.dict(os.environ, {"OWNER_ID": ""}):
            self.links.clear()
            self.assertIsNone(discordauth.owner_discord_id())

    def test_display_name_member_cache_active_guild_first(self):
        g1 = self.guild(1, {7: SimpleNamespace(display_name="伺服器一暱稱")})
        g2 = self.guild(2, {7: SimpleNamespace(display_name="伺服器二暱稱")})
        b = self.bot(guilds=[g1, g2])
        with mock.patch.object(discordauth, "_bot", return_value=b), \
             mock.patch("musicbot.webcore.get_active_guild", return_value=2):
            self.assertEqual(discordauth.display_name("7"), "伺服器二暱稱")
        with mock.patch.object(discordauth, "_bot", return_value=b), \
             mock.patch("musicbot.webcore.get_active_guild", return_value=None):
            self.assertEqual(discordauth.display_name("7"), "伺服器一暱稱")

    def test_display_name_user_cache_then_session_then_none(self):
        b = self.bot(users={8: SimpleNamespace(global_name="全域名", name="帳號名")})
        with mock.patch.object(discordauth, "_bot", return_value=b):
            self.assertEqual(discordauth.display_name("8"), "全域名")
            self.assertIsNone(discordauth.display_name("9"))
        with mock.patch.object(discordauth.time, "time", return_value=1000.0):
            discordauth.create_session({"id": "9", "name": "舊網頁名"})
            discordauth.create_session({"id": "9", "name": "新網頁名"})        # 同一時間 → 後建立的勝
        self.assertEqual(discordauth.display_name("9"), "新網頁名")
        with mock.patch.object(discordauth.time, "time", return_value=900.0):
            discordauth.create_session({"id": "9", "name": "更舊的"})          # 較早的不會蓋掉較新的
        with mock.patch.object(discordauth, "_bot", return_value=self.bot(ready=False)):
            self.assertEqual(discordauth.display_name("9"), "新網頁名")
        self.assertIsNone(discordauth.display_name(None))
        self.assertIsNone(discordauth.display_name("not-a-number"))

    def test_display_name_never_hits_network(self):
        b = self.bot()
        b.fetch_user = mock.Mock(side_effect=AssertionError("network"))
        with mock.patch.object(discordauth, "_bot", return_value=b), \
             mock.patch.object(discordauth, "_http", side_effect=AssertionError("network")):
            self.assertIsNone(discordauth.display_name("123"))


if __name__ == "__main__":
    unittest.main()
