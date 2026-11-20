"""網頁面板用 Discord 帳號登入：state / CSRF、換 token、伺服器成員、擁有者權限、session、操作紀錄寫 Discord 名字。
Discord 的 OAuth 端點、機器人查成員全部替換掉，不連網。"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit, parse_qs, quote

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import (actionlog, state, webcore, webapi, extapi, extmembers, userlists, userlistapi,  # noqa: E402
                      webauth, discordauth)

OWN = "o" * 40
HOST = "musbot.kdns.fr"
H = {"Host": HOST, "CF-Connecting-IP": "5.5.5.5"}
G = 8282
U = "https://www.youtube.com/watch?v=Oa%09d"
SECRET = "client-secret-DO-NOT-LOG-123"
CLIENT = "123456789012345678"
REDIRECT = "https://musbot.kdns.fr/auth/discord/callback"
OWNER_DID = "111111111111111111"
FRIEND = "222222222222222222"
STRANGER = "333333333333333333"


def U_(i):
    return U.replace("Oa%09d", f"Oa{i:09d}")


class FakeDiscord:
    """替代 discordauth._http：記下呼叫，回假的 token / 使用者"""
    def __init__(self):
        self.calls = []
        self.token_status = 200
        self.me = {}

    def __call__(self, method, url, data=None, headers=None, timeout=10):
        self.calls.append((method, url, dict(data or {}), dict(headers or {})))
        if url == discordauth.TOKEN_URL:
            if self.token_status != 200:
                return self.token_status, None
            return 200, {"access_token": "ACCESS-TOKEN-SECRET", "token_type": "Bearer", "scope": "identify"}
        if url == discordauth.ME_URL:
            return 200, dict(self.me)
        raise AssertionError("unexpected url " + url)


def member(did, name="小美", avatar=None):
    url = avatar or f"https://cdn.discordapp.com/avatars/{did}/abc123.png?size=64"
    av = SimpleNamespace(url=url)
    return SimpleNamespace(id=int(did), display_name=name, display_avatar=SimpleNamespace(with_size=lambda n: av))


class Base(unittest.TestCase):
    ENV = {"DISCORD_CLIENT_SECRET": SECRET, "OAUTH_REDIRECT_URI": REDIRECT, "DISCORD_CLIENT_ID": CLIENT}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        actionlog._reset_for_tests(os.path.join(self.tmp, "action_log.json"))
        discordauth.reset_for_tests(os.path.join(self.tmp, "web_sessions.json"))
        self.fake = FakeDiscord()
        self.members = {}           # did → member（lookup_member 的結果）
        env = {"EXT_TOKEN": OWN, "PANEL_SECRET": "", "QUEUE_MAX": "500", "OWNER_ID": OWNER_DID}
        env.update(self.ENV)
        self.p = [mock.patch.object(extmembers, "FILE", os.path.join(self.tmp, "m.json")),
                  mock.patch.object(userlists, "FILE", os.path.join(self.tmp, "ul.json")),
                  mock.patch.object(webauth, "SECRET_FILE", os.path.join(self.tmp, "panel_secret")),
                  mock.patch.dict(os.environ, env),
                  mock.patch.object(discordauth, "_http", self.fake),
                  mock.patch.object(discordauth, "lookup_member", side_effect=lambda did: self.members.get(str(did))),
                  mock.patch.object(webcore._prefetch, "ensure", mock.Mock()),
                  mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=None)),
                  mock.patch.object(webapi, "run_async", lambda c: asyncio.run(c)),
                  mock.patch.object(extapi, "run_async", lambda c: asyncio.run(c)),
                  mock.patch.object(userlistapi, "run_async", lambda c: asyncio.run(c)),
                  mock.patch.object(extapi, "_pick_guild", return_value=G),
                  mock.patch.object(extapi, "_lookup_title", return_value="查到的")]
        for p in self.p: p.start()
        extmembers._cache.update(mtime=None, members=[])
        userlists._reset_cache()
        webauth.reset_all()
        self._ld = webapi.flask_app.config.get("LOGIN_DISABLED")
        webapi.flask_app.config["LOGIN_DISABLED"] = False
        self.c = webapi.flask_app.test_client()
        state.queues[G] = []

    def tearDown(self):
        webapi.flask_app.config["LOGIN_DISABLED"] = self._ld
        for p in reversed(self.p): p.stop()
        extmembers._cache.update(mtime=None, members=[])
        userlists._reset_cache()
        webauth.reset_all()
        for d in (state.queues, state.now_playing, state.now_playing_url, state.voice_clients, state.song_meta):
            d.pop(G, None)
        actionlog._reset_for_tests()
        discordauth.reset_for_tests()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def start(self, c=None, nxt=None, https=False):
        h = dict(H)
        if https: h["X-Forwarded-Proto"] = "https"
        r = (c or self.c).get("/auth/discord" + (f"?next={quote(nxt, safe='')}" if nxt else ""), headers=h)
        self.assertEqual(r.status_code, 302)
        return r, parse_qs(urlsplit(r.headers["Location"]).query)

    def dlogin(self, did=FRIEND, name="小美", c=None, nxt=None, is_member=True, avatar=None):
        c = c or self.c
        if is_member:
            self.members[did] = member(did, name, avatar)
        self.fake.me = {"id": did, "username": "user" + did[-3:], "global_name": "全域名", "avatar": "abc123"}
        _, q = self.start(c, nxt)
        return c.get(f"/auth/discord/callback?code=CODE-xyz&state={q['state'][0]}", headers=H)

    def me(self, c=None):
        return (c or self.c).get("/api/me", headers={"Host": HOST})

    def cookie(self, c=None):
        ck = (c or self.c).get_cookie(webauth.COOKIE, domain=HOST)
        return ck.value if ck else None


class Disabled(Base):
    ENV = {"DISCORD_CLIENT_SECRET": "", "OAUTH_REDIRECT_URI": REDIRECT, "DISCORD_CLIENT_ID": CLIENT}

    def test_login_page_exactly_key_only(self):
        self.assertFalse(discordauth.enabled())
        html = self.c.get("/login", headers=H).get_data(as_text=True)
        self.assertIn("輸入金鑰才能使用", html)
        self.assertNotIn("用 Discord 登入", html)
        self.assertNotIn("<details", html)
        self.assertIn('name="key"', html)
        self.assertIn("autofocus", html)

    def test_auth_routes_send_back_to_login(self):
        for path in ("/auth/discord", "/auth/discord/callback?code=x&state=y"):
            r = self.c.get(path, headers=H)
            self.assertEqual((r.status_code, r.headers["Location"]), (302, "/login?err=off"), path)
        self.assertEqual(self.fake.calls, [])

    def test_key_login_still_works(self):
        r = self.c.post("/login", data={"key": OWN}, headers=H)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.me().get_json()["via"], "key")


class Config(Base):
    def test_redirect_uri_must_be_this_site_callback(self):
        for bad in ("http://musbot.kdns.fr/auth/discord/callback", "https://evil.com/auth/discord/callback",
                    "https://musbot.kdns.fr/auth/discord/other", "https://musbot.kdns.fr/auth/discord/callback?x=1",
                    "https://musbot.kdns.fr.evil.com/auth/discord/callback", "javascript:alert(1)",
                    "https://user@musbot.kdns.fr/auth/discord/callback"):
            with mock.patch.dict(os.environ, {"OAUTH_REDIRECT_URI": bad}):
                self.assertEqual(discordauth.redirect_uri(), "", bad)
                self.assertFalse(discordauth.enabled(), bad)
        for good in (REDIRECT, "http://127.0.0.1:5000/auth/discord/callback", "http://localhost:5000/auth/discord/callback"):
            with mock.patch.dict(os.environ, {"OAUTH_REDIRECT_URI": good}):
                self.assertEqual(discordauth.redirect_uri(), good)

    def test_client_id_falls_back_to_bot_application(self):
        with mock.patch.dict(os.environ, {"DISCORD_CLIENT_ID": ""}), \
                mock.patch.object(discordauth, "_bot", return_value=SimpleNamespace(application_id=987654321098765432)):
            self.assertEqual(discordauth.client_id(), "987654321098765432")
            self.assertTrue(discordauth.enabled())
        with mock.patch.dict(os.environ, {"DISCORD_CLIENT_ID": ""}), \
                mock.patch.object(discordauth, "_bot", return_value=SimpleNamespace(application_id=None)):
            self.assertFalse(discordauth.enabled())       # 機器人還沒登入：先只顯示金鑰

    def test_owner_from_application_when_no_owner_id(self):
        app = SimpleNamespace(team=None, owner=SimpleNamespace(id=444444444444444444))
        with mock.patch.dict(os.environ, {"OWNER_ID": ""}), \
                mock.patch.object(discordauth, "_bot", return_value=SimpleNamespace(application=app)):
            self.assertTrue(discordauth.is_owner("444444444444444444"))
            self.assertFalse(discordauth.is_owner(FRIEND))
        team = SimpleNamespace(owner=SimpleNamespace(id=555555555555555555))
        app2 = SimpleNamespace(team=team, owner=SimpleNamespace(id=1))
        with mock.patch.dict(os.environ, {"OWNER_ID": ""}), \
                mock.patch.object(discordauth, "_bot", return_value=SimpleNamespace(application=app2)):
            self.assertTrue(discordauth.is_owner("555555555555555555"))

    def test_http_sends_user_agent_and_never_hits_network(self):
        seen = {}

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return b'{"ok": 1}'

        def fake_open(req, timeout):
            seen.update(ua=req.get_header("User-agent"), ct=req.get_header("Content-type"), method=req.get_method(),
                        timeout=timeout)
            return Resp()
        real_http = self.p[4].temp_original          # 真正的 discordauth._http（urlopen 換掉，不連網）
        with mock.patch("urllib.request.urlopen", fake_open):
            st, j = real_http("POST", "https://x", data={"a": 1})
        self.assertEqual((st, j), (200, {"ok": 1}))
        self.assertIn("DiscordBot", seen["ua"])
        self.assertEqual((seen["ct"], seen["method"], seen["timeout"]), ("application/x-www-form-urlencoded", "POST", 10))
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(real_http("GET", "https://x"), (0, None))


class LoginPage(Base):
    def test_big_discord_button_with_key_fallback(self):
        html = self.c.get("/login?next=/x%3Fa%3D1", headers=H).get_data(as_text=True)
        self.assertIn('id="discordLogin"', html)
        self.assertIn('href="/auth/discord?next=/x%3Fa%3D1"', html)
        self.assertIn("用 Discord 登入", html)
        self.assertIn('<details class="keybox">', html)          # 金鑰收起來
        self.assertIn("改用金鑰登入", html)
        r = self.c.post("/login", data={"key": OWN}, headers=H)  # 金鑰登入照舊
        self.assertEqual(r.status_code, 302)

    def test_wrong_key_opens_key_box(self):
        r = self.c.post("/login", data={"key": "nope"}, headers=H)
        self.assertEqual(r.status_code, 401)
        html = r.get_data(as_text=True)
        self.assertIn('<details class="keybox" open>', html)
        self.assertIn("金鑰錯誤或已停用", html)

    def test_error_codes_fixed_text_only(self):
        html = self.c.get("/login?err=notmember", headers=H).get_data(as_text=True)
        self.assertIn("你不在機器人所在的 Discord 伺服器裡", html)
        html = self.c.get("/login?err=%3Cscript%3Ealert(1)%3C/script%3E", headers=H).get_data(as_text=True)
        self.assertNotIn("alert(1)", html)
        self.assertNotIn("<script>alert", html)


class Flow(Base):
    def test_authorize_redirect_and_state_cookie(self):
        r, q = self.start(nxt="/?tab=log", https=True)
        loc = r.headers["Location"]
        self.assertTrue(loc.startswith("https://discord.com/oauth2/authorize?"))
        self.assertEqual(q["client_id"], [CLIENT])
        self.assertEqual(q["scope"], ["identify"])
        self.assertEqual(q["response_type"], ["code"])
        self.assertEqual(q["redirect_uri"], [REDIRECT])
        self.assertGreaterEqual(len(q["state"][0]), 24)
        self.assertNotIn(SECRET, loc)
        sc = r.headers["Set-Cookie"]
        for attr in ("v6_oauth=", "HttpOnly", "SameSite=Lax", "Path=/auth/discord", "Secure", "Max-Age=600"):
            self.assertIn(attr, sc)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        _, q2 = self.start()
        self.assertNotEqual(q["state"], q2["state"])        # 每次都是新的

    def test_full_login_name_avatar_and_redirect(self):
        r = self.dlogin(FRIEND, "小美", nxt="/?tab=log")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/?tab=log")
        sc = r.headers.getlist("Set-Cookie")
        self.assertTrue(any(s.startswith("v6_session=") and "HttpOnly" in s and "SameSite=Lax" in s for s in sc))
        self.assertTrue(any(s.startswith("v6_oauth=;") or s.startswith('v6_oauth=""') for s in sc))   # 用過就清掉
        j = self.me().get_json()
        self.assertEqual((j["name"], j["owner"], j["via"]), ("小美", False, "discord"))
        self.assertEqual(j["avatar"], f"https://cdn.discordapp.com/avatars/{FRIEND}/abc123.png?size=64")
        tok_call, me_call = self.fake.calls
        self.assertEqual(tok_call[0], "POST")
        self.assertEqual(tok_call[2], {"client_id": CLIENT, "client_secret": SECRET, "grant_type": "authorization_code",
                                       "code": "CODE-xyz", "redirect_uri": REDIRECT})
        self.assertEqual(me_call[3], {"Authorization": "Bearer ACCESS-TOKEN-SECRET"})
        self.assertNotIn("ACCESS-TOKEN-SECRET", json.dumps(discordauth._load()))   # token 不保存

    def test_no_secrets_in_logs(self):
        with self.assertLogs("musicbot", level="INFO") as cm:
            self.dlogin(FRIEND, "小美")
            self.fake.token_status = 401
            self.dlogin(FRIEND, "小美", c=webapi.flask_app.test_client())
        text = "\n".join(cm.output)
        self.assertIn("小美 用 Discord 登入面板", text)
        self.assertIn("HTTP 401", text)
        for s in (SECRET, "ACCESS-TOKEN-SECRET", "CODE-xyz"):
            self.assertNotIn(s, text)

    def test_next_must_be_local(self):
        r = self.dlogin(nxt="//evil.com/x")
        self.assertEqual(r.headers["Location"], "/")

    def test_state_mismatch_missing_expired_or_replayed(self):
        self.members[FRIEND] = member(FRIEND)
        self.fake.me = {"id": FRIEND, "username": "x"}
        self.start()
        r = self.c.get("/auth/discord/callback?code=c&state=WRONG", headers=H)
        self.assertEqual(r.headers["Location"], "/login?err=state")
        fresh = webapi.flask_app.test_client()                 # 沒有 state cookie（別人丟來的連結）
        _, q = self.start()
        r = fresh.get(f"/auth/discord/callback?code=c&state={q['state'][0]}", headers=H)
        self.assertEqual(r.headers["Location"], "/login?err=state")
        with mock.patch.object(discordauth, "STATE_MAX_AGE", -1):
            _, q = self.start()
            r = self.c.get(f"/auth/discord/callback?code=c&state={q['state'][0]}", headers=H)
            self.assertEqual(r.headers["Location"], "/login?err=state")
        self.assertEqual(self.fake.calls, [])                  # 都沒去跟 Discord 換 token
        self.assertEqual(self.me().status_code, 401)
        _, q = self.start()
        ok = self.c.get(f"/auth/discord/callback?code=c&state={q['state'][0]}", headers=H)
        self.assertEqual(ok.headers["Location"], "/")
        c2 = webapi.flask_app.test_client()
        r = c2.get(f"/auth/discord/callback?code=c&state={q['state'][0]}", headers=H)   # 重放
        self.assertEqual(r.headers["Location"], "/login?err=state")

    def test_user_cancelled(self):
        _, q = self.start()
        r = self.c.get(f"/auth/discord/callback?error=access_denied&state={q['state'][0]}", headers=H)
        self.assertEqual(r.headers["Location"], "/login?err=denied")
        html = self.c.get("/login?err=denied", headers=H).get_data(as_text=True)
        self.assertIn("你取消了 Discord 登入", html)

    def test_not_member_refused(self):
        r = self.dlogin(STRANGER, is_member=False)
        self.assertEqual(r.headers["Location"], "/login?err=notmember")
        self.assertEqual(self.me().status_code, 401)
        self.assertEqual(discordauth._load(), {})

    def test_bot_not_ready(self):
        with mock.patch.object(discordauth, "lookup_member", side_effect=discordauth.OAuthError("notready")):
            r = self.dlogin(FRIEND, is_member=False)
        self.assertEqual(r.headers["Location"], "/login?err=notready")

    def test_exchange_failure(self):
        self.fake.token_status = 400
        r = self.dlogin(FRIEND)
        self.assertEqual(r.headers["Location"], "/login?err=exchange")
        self.assertEqual(self.me().status_code, 401)

    def test_callback_rate_limited(self):
        for _ in range(discordauth.CB_LIMIT):
            self.assertEqual(self.c.get("/auth/discord/callback?state=x", headers=H).status_code, 302)
        r = self.c.get("/auth/discord/callback?state=x", headers=H)
        self.assertEqual(r.status_code, 429)
        self.assertIn("嘗試太多次", r.get_data(as_text=True))
        other = dict(H, **{"CF-Connecting-IP": "6.6.6.6"})
        self.assertEqual(self.c.get("/auth/discord/callback?state=x", headers=other).status_code, 302)

    def test_already_logged_in_skips_discord(self):
        self.dlogin(FRIEND)
        n = len(self.fake.calls)
        r = self.c.get("/auth/discord?next=/members", headers=H)
        self.assertEqual(r.headers["Location"], "/members")
        self.assertEqual(len(self.fake.calls), n)

    def test_name_clipped_and_bad_avatar_falls_back(self):
        self.dlogin(FRIEND, "名" * 50, avatar="https://evil.example/x.png")
        j = self.me().get_json()
        self.assertEqual(j["name"], "名" * discordauth.NAME_MAX)
        self.assertEqual(j["avatar"], f"https://cdn.discordapp.com/avatars/{FRIEND}/abc123.png?size=64")
        self.assertEqual(discordauth.user_avatar({"id": FRIEND, "avatar": None}),
                         f"https://cdn.discordapp.com/embed/avatars/{(int(FRIEND) >> 22) % 6}.png")


class Permissions(Base):
    def test_owner_by_owner_id(self):
        self.dlogin(OWNER_DID, "阿管")
        j = self.me().get_json()
        self.assertEqual((j["name"], j["owner"]), ("阿管", True))
        self.assertEqual(self.c.get("/members", headers={"Host": HOST}).status_code, 200)
        self.assertEqual(self.c.get("/api/ext/members", headers={"Host": HOST}).status_code, 200)
        html = self.c.get("/", headers={"Host": HOST}).get_data(as_text=True)
        self.assertIn('id="btnMembers"', html)

    def test_normal_user_no_owner_pages(self):
        self.dlogin(FRIEND)
        self.assertEqual(self.c.get("/members", headers={"Host": HOST}).status_code, 403)
        self.assertEqual(self.c.get("/api/ext/members", headers={"Host": HOST}).status_code, 403)
        r = self.c.post("/api/system/update", json={"confirm": "UPDATE"}, headers={"Host": HOST})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn('id="btnMembers"', self.c.get("/", headers={"Host": HOST}).get_data(as_text=True))

    def test_owner_by_members_page_link(self):
        userlists.link_discord("owner", FRIEND)
        self.dlogin(FRIEND)
        self.assertTrue(self.me().get_json()["owner"])

    def test_linked_member_shares_lists_and_disable_logs_out(self):
        m, _tok = extmembers.create("小明")
        userlists.link_discord(m["id"], FRIEND)
        self.dlogin(FRIEND, "小美")
        r = self.c.post("/api/ext/lists", json={"name": "通勤"}, headers={"Host": HOST})
        self.assertEqual(r.status_code, 201)
        self.assertEqual([p["name"] for p in userlists.list_playlists(m["id"])], ["通勤"])
        self.assertEqual(self.c.get("/api/ext/lists", headers={"Host": HOST}).get_json()["you"], "小美")
        extmembers.set_enabled(m["id"], False)
        self.assertEqual(self.me().status_code, 401)

    def test_unlinked_has_no_personal_lists(self):
        self.dlogin(STRANGER, "路人")
        r = self.c.get("/api/ext/lists", headers={"Host": HOST})
        self.assertEqual(r.status_code, 403)
        j = r.get_json()
        self.assertEqual(j["code"], "not_linked")
        self.assertIn(STRANGER, j["message"])
        self.assertEqual(self.c.post("/api/ext/lists", json={"name": "x"}, headers={"Host": HOST}).status_code, 403)
        self.assertNotIn("d:" + STRANGER, userlists._load()["users"])

    def test_cross_site_post_blocked(self):
        self.dlogin(FRIEND)
        r = self.c.post("/api/queue/add", json={"guild_id": G, "url": U_(1), "title": "x", "no_play": True},
                        headers={"Host": HOST, "Origin": "https://evil.com"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(state.queues[G], [])


class ActionLog(Base):
    def test_web_actions_record_discord_name_and_id(self):
        self.dlogin(FRIEND, "小美")
        r = self.c.post("/api/queue/add", json={"guild_id": G, "url": U_(1), "title": "歌一", "no_play": True},
                        headers={"Host": HOST})
        self.assertEqual(r.get_json()["status"], "added")
        e = actionlog.recent(G)[0]
        self.assertEqual((e["act"], e["who"], e["src"], e["uid"], e["title"]), ("add", "小美", "web", FRIEND, "歌一"))
        self.assertEqual(state.song_meta[G][U_(1)]["requester"], "小美")       # 佇列「誰點的」/ 播放紀錄也是她
        songs = [{"url": U_(i), "title": f"s{i}"} for i in range(2, 5)]
        self.c.post("/api/queue/add-batch", json={"guild_id": G, "songs": songs}, headers={"Host": HOST})
        e = actionlog.recent(G)[0]
        self.assertEqual((e["n"], e["who"], e["uid"]), (3, "小美", FRIEND))
        self.assertEqual(state.song_meta[G][U_(3)]["requester"], "小美")
        self.c.post("/api/queue/remove", json={"guild_id": G, "index": 0}, headers={"Host": HOST})
        e = actionlog.recent(G)[0]
        self.assertEqual((e["act"], e["who"], e["uid"]), ("remove", "小美", FRIEND))

    def test_two_people_get_their_own_names(self):
        self.dlogin(FRIEND, "小美")
        c2 = webapi.flask_app.test_client()
        self.dlogin(OWNER_DID, "阿管", c=c2)
        self.c.post("/api/queue/add", json={"guild_id": G, "url": U_(1), "title": "一", "no_play": True}, headers={"Host": HOST})
        c2.post("/api/queue/add", json={"guild_id": G, "url": U_(2), "title": "二", "no_play": True}, headers={"Host": HOST})
        self.assertEqual([(e["who"], e["uid"]) for e in actionlog.recent(G)], [("阿管", OWNER_DID), ("小美", FRIEND)])

    def test_key_login_unchanged(self):
        self.c.post("/login", data={"key": OWN}, headers=H)
        self.c.post("/api/queue/add", json={"guild_id": G, "url": U_(1), "title": "一", "no_play": True}, headers={"Host": HOST})
        e = actionlog.recent(G)[0]
        self.assertEqual((e["who"], e["src"]), ("擁有者", "web"))
        self.assertNotIn("uid", e)
        self.assertEqual(state.song_meta[G][U_(1)]["requester"], "網頁")

    def test_extension_token_still_ext(self):
        self.dlogin(FRIEND, "小美")          # 同一個瀏覽器有 Discord 登入也不影響擴充功能
        _, tok = extmembers.create("阿華")
        r = self.c.post("/api/ext/add", json={"url": U_(5), "title": "五"},
                        headers={"Host": HOST, "X-Ext-Token": tok})
        self.assertEqual(r.get_json()["status"], "added")
        e = actionlog.recent(G)[0]
        self.assertEqual((e["who"], e["src"]), ("阿華", "ext"))
        self.assertNotIn("uid", e)

    def test_discord_command_actor_has_id(self):
        it = SimpleNamespace(user=SimpleNamespace(id=int(FRIEND), display_name="小美", name="x"))
        a = actionlog.discord_actor(it)
        self.assertEqual(a, ("小美", "discord", int(FRIEND)))
        e = actionlog.record(G, "skip", a, "歌")
        self.assertEqual((e["uid"], e["src"]), (FRIEND, "discord"))
        self.assertNotIn("uid", actionlog.record(G, "skip", ("x", "web", "not-a-number"), "歌"))
        self.assertNotIn("uid", actionlog.record(G, "skip", ("x", "web"), "歌"))


class Sessions(Base):
    def test_logout_kills_server_session(self):
        self.dlogin(FRIEND)
        val = self.cookie()
        self.assertEqual(len(discordauth._load()), 1)
        r = self.c.post("/logout", headers={"Host": HOST, "Origin": "https://" + HOST})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(discordauth._load(), {})
        c2 = webapi.flask_app.test_client()
        c2.set_cookie(webauth.COOKIE, val, domain=HOST)
        self.assertEqual(self.me(c2).status_code, 401)      # 舊 cookie 被偷也沒用

    def test_cross_site_logout_keeps_server_session(self):
        self.dlogin(FRIEND)
        self.c.post("/logout", headers={"Host": HOST, "Origin": "https://evil.com"})
        self.assertEqual(len(discordauth._load()), 1)

    def test_survives_restart_file_0600_no_raw_id(self):
        self.dlogin(FRIEND, "小美")
        val = self.cookie()
        sid = webauth.session_id_from_cookie(val)
        discordauth._sessions.update(data=None, path=None)          # 模擬重啟
        self.assertEqual(self.me().get_json()["name"], "小美")
        path = discordauth.SESSIONS_FILE
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(sid, raw)
        self.assertIn(FRIEND, raw)
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_forged_cookies_rejected(self):
        self.c.set_cookie(webauth.COOKIE, webauth.session_cookie_value("x" * 43), domain=HOST)
        self.assertEqual(self.me().status_code, 401)
        bad = webauth.URLSafeTimedSerializer("wrong", salt="v6-panel-login").dumps({"s": "x" * 43})
        self.c.set_cookie(webauth.COOKIE, bad, domain=HOST)
        self.assertEqual(self.me().status_code, 401)

    def test_idle_expiry(self):
        self.dlogin(FRIEND)
        for rec in discordauth._load().values():
            rec["seen"] = time.time() - discordauth.SESSION_IDLE - 5
        self.assertEqual(self.me().status_code, 401)
        self.assertEqual(discordauth._load(), {})

    def test_left_server_logged_out_on_recheck(self):
        self.dlogin(FRIEND)
        for rec in discordauth._load().values():
            rec["chk"] = 0
        with mock.patch.object(discordauth, "in_any_guild_cached", return_value=True):
            self.assertEqual(self.me().status_code, 200)
        for rec in discordauth._load().values():
            rec["chk"] = 0
        with mock.patch.object(discordauth, "in_any_guild_cached", return_value=False):
            self.assertEqual(self.me().status_code, 401)

    def test_max_devices_per_user(self):
        for _ in range(discordauth.MAX_PER_USER + 3):
            discordauth.create_session({"id": FRIEND, "name": "x"})
        discordauth.create_session({"id": STRANGER, "name": "y"})
        dids = [v["did"] for v in discordauth._load().values()]
        self.assertEqual((dids.count(FRIEND), dids.count(STRANGER)), (discordauth.MAX_PER_USER, 1))

    def test_renewed_cookie_keeps_discord_identity(self):
        self.dlogin(FRIEND, "小美")
        with mock.patch.object(webauth, "RENEW_AFTER", -1):
            r = self.me()
        sc = r.headers.get("Set-Cookie") or ""
        self.assertIn("v6_session=", sc)
        c2 = webapi.flask_app.test_client()
        c2.set_cookie(webauth.COOKIE, sc.split("v6_session=")[1].split(";")[0], domain=HOST)
        self.assertEqual(self.me(c2).get_json()["name"], "小美")

    def test_sse_accepts_discord_session(self):
        self.dlogin(FRIEND)
        self.assertTrue(webauth.ident_from_cookie(self.cookie()))


class Header(Base):
    def test_avatar_and_name_in_header(self):
        self.dlogin(FRIEND, "小美")
        html = self.c.get("/", headers={"Host": HOST}).get_data(as_text=True)
        av = f"https://cdn.discordapp.com/avatars/{FRIEND}/abc123.png?size=64"
        self.assertIn(f'class="hdr-avatar" src="{av}"', html)
        self.assertIn(f'class="set-avatar" src="{av}"', html)
        self.assertIn('data-tip="設定（小美）"', html)
        self.assertIn('<span class="set-who-name">小美</span>', html)
        self.assertIn('referrerpolicy="no-referrer"', html)

    def test_key_login_header_unchanged(self):
        self.c.post("/login", data={"key": OWN}, headers=H)
        html = self.c.get("/", headers={"Host": HOST}).get_data(as_text=True)
        self.assertNotIn("hdr-avatar", html)
        self.assertIn('<div class="set-who" aria-hidden="true"><i class="fas fa-key"></i>', html)
        self.assertIn('data-tip="設定"', html)


if __name__ == "__main__":
    unittest.main()
