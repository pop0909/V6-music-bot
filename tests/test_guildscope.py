"""伺服器權限：只能看 / 操作自己有加入的伺服器；每個人各自的「目前伺服器」；擴充功能金鑰。"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import webapi, extapi, extmembers, userlists, webauth, state, guildscope  # noqa: E402

OWN = "g" * 40
HOST = "musbot.kdns.fr"
G1, G2, G3 = 9101, 9102, 9103
OWNER = "100000000000000001"
ALICE = "200000000000000002"     # 只在 G1
BOB = "300000000000000003"       # 只在 G2
CAROL = "400000000000000004"     # G1 + G2


class FakeGuild:
    def __init__(self, gid, members):
        self.id = gid
        self._m = {int(x) for x in members}

    def get_member(self, uid):
        return object() if uid in self._m else None


class FakeClient:
    def __init__(self, guilds):
        self.guilds = guilds


class Base(unittest.TestCase):
    OWNER_ID = OWNER

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        client = FakeClient([FakeGuild(G1, [OWNER, ALICE, CAROL]), FakeGuild(G2, [BOB, CAROL]),
                             FakeGuild(G3, [OWNER])])
        self.p = [mock.patch.object(extmembers, "FILE", os.path.join(self.tmp, "m.json")),
                  mock.patch.object(userlists, "FILE", os.path.join(self.tmp, "ul.json")),
                  mock.patch.object(webauth, "SECRET_FILE", os.path.join(self.tmp, "panel_secret")),
                  mock.patch.dict(os.environ, {"EXT_TOKEN": OWN, "PANEL_SECRET": "", "OWNER_ID": self.OWNER_ID,
                                               "DISCORD_CLIENT_SECRET": ""}),
                  mock.patch.object(guildscope, "_client", return_value=client),
                  mock.patch("musicbot.discordauth._bot", side_effect=RuntimeError("no bot")),
                  mock.patch.object(webapi, "run_async", lambda c: (c.close(), {"status": "ok"})[1])]
        for p in self.p: p.start()
        extmembers._cache.update(mtime=None, members=[])
        userlists._reset_cache()
        webauth.reset_all()
        guildscope.reset_for_tests()
        self._saved_active = state.active_guild_id
        state.active_guild_id = None
        for gid, name in ((G1, "一號"), (G2, "二號"), (G3, "三號")):
            state.guild_names[gid] = name
        self._ld = webapi.flask_app.config.get("LOGIN_DISABLED")
        webapi.flask_app.config["LOGIN_DISABLED"] = False

    def tearDown(self):
        webapi.flask_app.config["LOGIN_DISABLED"] = self._ld
        for p in reversed(self.p): p.stop()
        for gid in (G1, G2, G3, 999):
            state.guild_names.pop(gid, None)
        state.active_guild_id = self._saved_active
        guildscope.reset_for_tests()
        extmembers._cache.update(mtime=None, members=[])
        userlists._reset_cache()
        webauth.reset_all()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def member(self, name, did=None):
        m, tok = extmembers.create(name)
        if did:
            userlists.link_discord(m["id"], did)
        return tok

    def panel(self, key, ip="1.1.1.1"):
        c = webapi.flask_app.test_client()
        r = c.post("/login", data={"key": key}, headers={"Host": HOST, "CF-Connecting-IP": ip})
        self.assertIn(r.status_code, (302, 303))
        return c

    def get(self, c, path):
        return c.get(path, headers={"Host": HOST})

    def post(self, c, path, body):
        return c.post(path, json=body, headers={"Host": HOST, "Origin": "https://" + HOST})

    def guilds(self, c):
        return sorted(int(x["id"]) for x in self.get(c, "/api/guilds").get_json()["guilds"])


class Listing(Base):
    def test_each_user_sees_only_their_guilds(self):
        self.assertEqual(self.guilds(self.panel(self.member("愛麗絲", ALICE))), [G1])
        self.assertEqual(self.guilds(self.panel(self.member("鮑伯", BOB))), [G2])
        self.assertEqual(self.guilds(self.panel(self.member("卡蘿", CAROL))), [G1, G2])
        self.assertEqual(self.guilds(self.panel(OWN)), [G1, G3])          # 擁有者 = OWNER_ID 所在的伺服器

    def test_unlinked_member_sees_nothing(self):
        self.assertEqual(self.guilds(self.panel(self.member("沒連結"))), [])

    def test_owner_fallback_all_when_owner_unknown(self):
        with mock.patch.dict(os.environ, {"OWNER_ID": ""}):
            self.assertEqual(self.guilds(self.panel(OWN)), [G1, G2, G3])

    def test_owner_fallback_all_when_owner_not_in_any_guild(self):
        with mock.patch.dict(os.environ, {"OWNER_ID": "999999999999999999"}):
            self.assertEqual(self.guilds(self.panel(OWN)), [G1, G2, G3])

    def test_bad_owner_id_warns_and_falls_back(self):
        from musicbot import owner
        owner._warned.clear()
        with mock.patch.dict(os.environ, {"OWNER_ID": "abc"}), self.assertLogs("musicbot", "WARNING") as cm:
            self.assertEqual(self.guilds(self.panel(OWN)), [G1, G2, G3])
        self.assertTrue(any("OWNER_ID" in x for x in cm.output))


class Control(Base):
    def test_other_guild_forbidden_everywhere(self):
        c = self.panel(self.member("愛麗絲", ALICE))
        for path in (f"/api/status?guild_id={G2}", f"/api/guilds/{G2}/voice-channels",
                     f"/api/progress-stream?guild_id={G2}", f"/api/queue?guild_id={G2}", f"/api/history?guild_id={G2}"):
            r = self.get(c, path)
            self.assertEqual(r.status_code, 403, path)
            self.assertEqual(r.get_json()["message"], guildscope.FORBIDDEN)
        for path, body in (("/api/skip", {}), ("/api/stop", {}), ("/api/volume", {"level": 10}),
                           ("/api/join", {"channel_id": 5}), ("/api/queue/add", {"url": "https://youtu.be/abcdefghijk"}),
                           ("/api/queue/clear", {}),
                           ("/api/active-guild", {}), ("/api/sleep-timer", {"mode": "off"})):
            r = self.post(c, path, dict(body, guild_id=str(G2)))
            self.assertEqual(r.status_code, 403, path)

    def test_own_guild_allowed(self):
        c = self.panel(self.member("愛麗絲", ALICE))
        self.assertEqual(self.get(c, f"/api/status?guild_id={G1}").status_code, 200)
        self.assertEqual(self.post(c, "/api/active-guild", {"guild_id": str(G1)}).get_json()["active_guild_id"], str(G1))

    def test_favorites_api_removed(self):
        """最愛功能已拿掉：舊的 /api/favorites* 一律 404（不會再寫 favorites/*.txt）"""
        c = self.panel(OWN)
        self.assertEqual(self.get(c, f"/api/favorites?guild_id={G1}").status_code, 404)
        for path in ("/api/favorites/add", "/api/favorites/remove"):
            r = self.post(c, path, {"guild_id": str(G1), "url": "https://youtu.be/abcdefghijk", "index": 0})
            self.assertEqual(r.status_code, 404, path)

    def test_missing_guild_id_defaults_inside_allowed(self):
        alice = self.panel(self.member("愛麗絲", ALICE))
        state.active_guild_id = G2                                   # 全域預設是愛麗絲進不去的伺服器
        r = self.get(alice, "/api/status").get_json()
        self.assertEqual(r["guild_name"], "一號")


class ActiveGuild(Base):
    def active(self, c):
        return self.get(c, "/api/active-guild").get_json()["active_guild_id"]

    def test_per_user_and_owner_default(self):
        owner = self.panel(OWN)
        alice = self.panel(self.member("愛麗絲", ALICE))
        carol = self.panel(self.member("卡蘿", CAROL))
        bob = self.panel(self.member("鮑伯", BOB))
        self.post(owner, "/api/active-guild", {"guild_id": str(G1)})
        self.assertEqual(state.active_guild_id, G1)                   # 擁有者的選擇 = 大家的預設
        self.assertEqual(self.active(carol), str(G1))                 # 沒選過 → 跟預設
        self.assertIsNone(self.active(bob))                           # 預設的伺服器鮑伯進不去 → 沒有
        self.post(carol, "/api/active-guild", {"guild_id": str(G2)})
        self.assertEqual(self.active(carol), str(G2))                 # 卡蘿換到 G2 只影響自己
        self.assertEqual(self.active(owner), str(G1))
        self.assertEqual(self.active(alice), str(G1))
        self.assertEqual(state.active_guild_id, G1)
        self.post(carol, "/api/active-guild", {"guild_id": None})     # 清掉 → 回到預設
        self.assertEqual(self.active(carol), str(G1))
        # status / SSE 帶的 active_guild_id 也是自己的
        self.post(carol, "/api/active-guild", {"guild_id": str(G2)})
        self.assertEqual(self.get(carol, f"/api/status?guild_id={G1}").get_json()["active_guild_id"], str(G2))
        self.assertEqual(self.get(alice, f"/api/status?guild_id={G1}").get_json()["active_guild_id"], str(G1))

    def test_same_person_shares_between_panel_and_extension(self):
        tok = self.member("愛麗絲", ALICE)
        c = self.panel(tok)
        self.post(c, "/api/active-guild", {"guild_id": str(G1)})
        ident = extapi.with_discord_name(extmembers.authenticate(tok))
        self.assertEqual(guildscope.default_guild(ident), G1)


class Extension(Base):
    def ext(self, tok, path, body):
        return webapi.flask_app.test_client().post(path, json=body, headers={"Host": HOST, "X-Ext-Token": tok})

    def test_member_key_only_linked_guilds(self):
        tok = self.member("鮑伯", BOB)
        r = self.ext(tok, "/api/ext/add", {"guild_id": str(G1), "url": "https://www.youtube.com/watch?v=abcdefghijk"})
        self.assertEqual(r.status_code, 403)
        ident = extapi.with_discord_name(extmembers.authenticate(tok))
        self.assertEqual(guildscope.default_guild(ident), G2)

    def test_owner_key_owner_guilds(self):
        ident = extapi.with_discord_name(extmembers.authenticate(OWN))
        self.assertEqual(guildscope.allowed_guilds(ident), {G1, G3})
        r = webapi.flask_app.test_client().get(f"/api/ext/status?guild_id={G2}", headers={"Host": HOST, "X-Ext-Token": OWN})
        self.assertEqual(r.status_code, 403)

    def test_unlinked_member_key_has_no_guild(self):
        tok = self.member("沒連結")
        ident = extapi.with_discord_name(extmembers.authenticate(tok))
        self.assertEqual(guildscope.allowed_guilds(ident), set())
        self.assertEqual(guildscope.default_guild(ident), 0)


if __name__ == "__main__":
    unittest.main()
