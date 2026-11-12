"""網頁面板金鑰登入：每個路由都要登入、擁有者 / 朋友權限、輸錯鎖定、停用即登出、擴充功能照舊。"""
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import webapi, extapi, userlistapi, extmembers, userlists, webauth, state, sysinfo  # noqa: E402,F401

OWN = "o" * 40
HOST = "musbot.kdns.fr"
G = 7373


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = [mock.patch.object(extmembers, "FILE", os.path.join(self.tmp, "m.json")),
                  mock.patch.object(userlists, "FILE", os.path.join(self.tmp, "ul.json")),
                  mock.patch.object(webauth, "SECRET_FILE", os.path.join(self.tmp, "panel_secret")),
                  mock.patch.dict(os.environ, {"EXT_TOKEN": OWN, "PANEL_SECRET": ""})]
        for p in self.p: p.start()
        extmembers._cache.update(mtime=None, members=[])
        userlists._reset_cache()
        webauth.reset_all()
        self._login_disabled = webapi.flask_app.config.get("LOGIN_DISABLED")
        webapi.flask_app.config["LOGIN_DISABLED"] = False
        self.c = webapi.flask_app.test_client()

    def tearDown(self):
        webapi.flask_app.config["LOGIN_DISABLED"] = self._login_disabled
        for p in reversed(self.p): p.stop()
        extmembers._cache.update(mtime=None, members=[])
        userlists._reset_cache()
        webauth.reset_all()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def client(self):
        return webapi.flask_app.test_client()

    def login(self, key, c=None, ip="1.1.1.1", https=False, nxt=None):
        c = c or self.c
        h = {"Host": HOST, "CF-Connecting-IP": ip}
        if https: h["X-Forwarded-Proto"] = "https"
        data = {"key": key}
        if nxt is not None: data["next"] = nxt
        return c.post("/login", data=data, headers=h)

    def friend(self, name="小明"):
        m, tok = extmembers.create(name)
        return m, tok

    def get(self, path, c=None, **kw):
        return (c or self.c).get(path, headers={"Host": HOST}, **kw)

    def post(self, path, c=None, origin=None, **kw):
        h = {"Host": HOST}
        if origin: h["Origin"] = origin
        return (c or self.c).post(path, headers=h, **kw)


def _fill(rule):
    p = rule.rule
    for k, v in (("<int:gid>", "111"), ("<mid>", "nosuch"), ("<lid>", "nosuch"), ("<jid>", "nosuch"),
                 ("<path:filename>", "css/style.css")):
        p = p.replace(k, v)
    return None if "<" in p else p


class EveryRouteNeedsLogin(Base):
    PUBLIC = {"/login", "/logout"}

    def test_unauthenticated_blocked_everywhere(self):
        checked = 0
        for rule in webapi.flask_app.url_map.iter_rules():
            path = _fill(rule)
            self.assertIsNotNone(path, rule.rule)
            if path in self.PUBLIC or path.startswith("/static/"):
                continue
            for m in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                r = self.c.open(path, method=m, headers={"Host": HOST}, json={"guild_id": 111, "confirm": "UPDATE"})
                checked += 1
                if path.startswith("/api/"):
                    self.assertEqual(r.status_code, 401, f"{m} {path} → {r.status_code}")
                elif m == "GET":
                    self.assertEqual(r.status_code, 302, f"{m} {path}")
                    self.assertTrue(r.headers["Location"].startswith("/login"), f"{m} {path}")
                else:
                    self.assertEqual(r.status_code, 401, f"{m} {path}")
        self.assertGreater(checked, 50)

    def test_unknown_path_and_sse_blocked(self):
        self.assertEqual(self.get("/nope").status_code, 302)
        self.assertEqual(self.get("/api/progress-stream?guild_id=1").status_code, 401)

    def test_forged_and_garbage_cookie_rejected(self):
        self.c.set_cookie(webauth.COOKIE, "garbage", domain=HOST)
        self.assertEqual(self.get("/api/status").status_code, 401)
        other = webauth.URLSafeTimedSerializer("wrong-secret", salt="v6-panel-login").dumps({"u": "owner", "f": "x"})
        self.c.set_cookie(webauth.COOKIE, other, domain=HOST)
        self.assertEqual(self.get("/api/status").status_code, 401)

    def test_public_things_reachable(self):
        self.assertEqual(self.get("/login").status_code, 200)
        self.assertIn("輸入金鑰", self.get("/login").get_data(as_text=True))
        self.assertEqual(self.get("/static/css/style.css").status_code, 200)

    def test_redirect_keeps_next(self):
        r = self.get("/members")
        self.assertEqual(r.headers["Location"], "/login?next=/members")
        self.assertEqual(self.get("/").headers["Location"], "/login")


class LoginFlow(Base):
    def test_owner_login_cookie_attributes(self):
        r = self.login(OWN, https=True)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/")
        sc = r.headers["Set-Cookie"]
        self.assertIn(webauth.COOKIE + "=", sc)
        self.assertIn("HttpOnly", sc)
        self.assertIn("Secure", sc)
        self.assertIn("SameSite=Lax", sc)
        self.assertIn("Max-Age=2592000", sc)
        self.assertNotIn(OWN, sc)                       # cookie 不含金鑰本身
        self.assertEqual(self.get("/").status_code, 200)
        me = self.get("/api/me").get_json()
        self.assertEqual(me, {"status": "ok", "name": "擁有者", "owner": True})

    def test_http_cookie_not_secure(self):
        sc = self.login(OWN).headers["Set-Cookie"]
        self.assertNotIn("Secure", sc)

    def test_friend_login_and_identity(self):
        m, tok = self.friend("阿華")
        self.assertEqual(self.login(tok).status_code, 302)
        self.assertEqual(self.get("/api/me").get_json()["name"], "阿華")
        self.assertFalse(self.get("/api/me").get_json()["owner"])
        self.assertIsNotNone(extmembers.list_members()[0]["last_used"])

    def test_wrong_key_message_and_counts(self):
        r = self.login("nope")
        self.assertEqual(r.status_code, 401)
        self.assertIn("還可以再試 4 次", r.get_data(as_text=True))
        self.assertNotIn("Set-Cookie", r.headers)

    def test_next_is_sanitized(self):
        for bad in ("//evil.com", "https://evil.com", "/\\evil.com", "/login", "evil"):
            r = self.login(OWN, c=self.client(), nxt=bad)
            self.assertEqual(r.headers["Location"], "/", bad)
        r = self.login(OWN, c=self.client(), nxt="/members")
        self.assertEqual(r.headers["Location"], "/members")

    def test_logged_in_login_page_redirects_and_logout(self):
        self.login(OWN)
        self.assertEqual(self.get("/login").status_code, 302)
        r = self.post("/logout")
        self.assertEqual(r.status_code, 302)
        self.assertIn(webauth.COOKIE + "=;", r.headers["Set-Cookie"].replace('=""', "=").replace("= ;", "=;"))
        self.assertEqual(self.get("/api/status").status_code, 401)

    def test_cross_site_login_rejected(self):
        r = self.c.post("/login", data={"key": OWN}, headers={"Host": HOST, "Origin": "https://evil.com"})
        self.assertEqual(r.status_code, 403)

    def test_expired_session(self):
        self.login(OWN)
        real = time.time
        with mock.patch("time.time", lambda: real() + webauth.MAX_AGE + 60):
            self.assertEqual(self.get("/api/me").status_code, 401)

    def test_no_ext_token_configured(self):
        with mock.patch.dict(os.environ, {"EXT_TOKEN": ""}):
            self.assertEqual(self.login("anything").status_code, 503)

    def test_secret_file_created_private(self):
        self.login(OWN)
        self.assertTrue(os.path.exists(webauth.SECRET_FILE))
        if os.name == "posix":
            self.assertEqual(os.stat(webauth.SECRET_FILE).st_mode & 0o777, 0o600)


class Lockout(Base):
    def test_five_wrong_locks_ten_minutes(self):
        for i in range(4):
            self.assertEqual(self.login("bad%d" % i, ip="9.9.9.9").status_code, 401)
        r = self.login("bad5", ip="9.9.9.9")
        self.assertEqual(r.status_code, 429)
        self.assertIn("10 分鐘", r.get_data(as_text=True))
        # 鎖住時連正確金鑰也不行，登入頁也顯示鎖定
        self.assertEqual(self.login(OWN, ip="9.9.9.9").status_code, 429)
        self.assertIn("disabled", self.c.get("/login", headers={"Host": HOST, "CF-Connecting-IP": "9.9.9.9"}).get_data(as_text=True))
        # 其他 IP（Cloudflare 標頭不同）不受影響
        self.assertEqual(self.login(OWN, c=self.client(), ip="8.8.8.8").status_code, 302)
        # 10 分鐘後解鎖
        real = time.time
        with mock.patch.object(webauth.time, "time", lambda: real() + webauth.LOCK_SECS + 1):
            self.assertEqual(self.login(OWN, c=self.client(), ip="9.9.9.9").status_code, 302)

    def test_success_resets_counter(self):
        for i in range(4):
            self.login("bad", ip="7.7.7.7")
        self.assertEqual(self.login(OWN, c=self.client(), ip="7.7.7.7").status_code, 302)
        for i in range(4):
            self.assertEqual(self.login("bad", c=self.client(), ip="7.7.7.7").status_code, 401)

    def test_uses_cf_connecting_ip_not_remote_addr(self):
        # 全部經過 Cloudflare Tunnel（remote_addr 都是 127.0.0.1），要用 CF-Connecting-IP 分辨
        for i in range(5):
            self.login("bad", ip="5.5.5.5")
        self.assertEqual(self.login(OWN, c=self.client(), ip="5.5.5.6").status_code, 302)
        self.assertEqual(webauth.client_ip(type("R", (), {"headers": {}, "remote_addr": "127.0.0.1"})()), "127.0.0.1")


class OwnerOnly(Base):
    def test_friend_cannot_update_or_manage(self):
        m, tok = self.friend()
        self.login(tok)
        page = self.get("/").get_data(as_text=True)
        self.assertNotIn('id="btnMembers"', page)
        self.assertNotIn('id="btnUpdate"', page)
        self.assertNotIn('href="/members"', page)
        self.assertIn('id="btnLogout"', page)
        self.assertIn("小明", page)
        with mock.patch.object(sysinfo, "start_update", side_effect=AssertionError("must not run")):
            self.assertEqual(self.post("/api/system/update", json={"confirm": "UPDATE"}).status_code, 403)
        self.assertEqual(self.get("/members").status_code, 403)
        self.assertEqual(self.get("/api/ext/members").status_code, 403)
        self.assertEqual(self.post("/api/ext/members", json={"name": "x"}).status_code, 403)
        self.assertEqual(self.post("/api/ext/members/" + m["id"], json={"action": "disable"}).status_code, 403)
        self.assertEqual(self.c.delete("/api/ext/members/" + m["id"], headers={"Host": HOST}).status_code, 403)
        self.assertTrue(extmembers.list_members()[0]["enabled"])
        # 一般功能可以用
        self.assertEqual(self.get("/api/system").status_code, 200)

    def test_owner_has_everything(self):
        self.login(OWN)
        page = self.get("/").get_data(as_text=True)
        self.assertIn('id="btnMembers"', page)
        self.assertIn('id="btnUpdate"', page)
        self.assertIn("擁有者", page)
        with mock.patch.object(sysinfo, "start_update", return_value=(True, "開始更新")) as su:
            r = self.post("/api/system/update", json={"confirm": "UPDATE"})
        self.assertEqual(r.status_code, 202)
        su.assert_called_once()
        self.assertEqual(self.get("/members").status_code, 200)
        r = self.post("/api/ext/members", json={"name": "新朋友"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.get("/api/ext/members").get_json()["members"][0]["name"], "新朋友")

    def test_cross_site_post_blocked_even_with_cookie(self):
        self.login(OWN)
        with mock.patch.object(sysinfo, "start_update", side_effect=AssertionError("must not run")):
            r = self.post("/api/system/update", origin="https://evil.com", json={"confirm": "UPDATE"})
        self.assertEqual(r.status_code, 403)
        r = self.post("/api/ext/members", origin="https://evil.com", json={"name": "x"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(extmembers.list_members(), [])
        r = self.post("/api/ext/members", origin="https://" + HOST, json={"name": "ok"})
        self.assertEqual(r.status_code, 201)


class Revocation(Base):
    def test_disable_delete_reset_log_out_immediately(self):
        m, tok = self.friend()
        self.login(tok)
        self.assertEqual(self.get("/api/me").status_code, 200)
        extmembers.set_enabled(m["id"], False)
        self.assertEqual(self.get("/api/me").status_code, 401)
        self.assertEqual(self.get("/").status_code, 302)
        self.assertEqual(self.get("/api/ext/lists").status_code, 401)
        extmembers.set_enabled(m["id"], True)
        self.assertEqual(self.get("/api/me").status_code, 200)
        extmembers.reset_token(m["id"])                 # 重設金鑰 → 舊登入失效
        self.assertEqual(self.get("/api/me").status_code, 401)
        m2, tok2 = self.friend("B")
        c2 = self.client(); self.login(tok2, c=c2)
        self.assertEqual(self.get("/api/me", c=c2).status_code, 200)
        extmembers.delete(m2["id"])
        self.assertEqual(self.get("/api/me", c=c2).status_code, 401)

    def test_owner_token_change_logs_owner_out(self):
        self.login(OWN)
        with mock.patch.dict(os.environ, {"EXT_TOKEN": "n" * 40}):
            self.assertEqual(self.get("/api/me").status_code, 401)

    def test_disable_via_members_api_ends_friend_session(self):
        m, tok = self.friend()
        cf = self.client(); self.login(tok, c=cf)
        co = self.client(); self.login(OWN, c=co)
        self.assertEqual(self.post("/api/ext/members/" + m["id"], c=co, json={"action": "disable"}).status_code, 200)
        self.assertEqual(self.get("/api/me", c=cf).status_code, 401)

    def test_sse_stream_closes_when_revoked(self):
        m, tok = self.friend()
        self.login(tok)
        with mock.patch.object(webapi, "get_full_app_state", lambda gid: {"t": time.time()}):
            r = self.c.get("/api/progress-stream?guild_id=1", headers={"Host": HOST}, buffered=False)
            self.assertEqual(r.status_code, 200)
            it = iter(r.response)
            first = next(it)
            self.assertIn(b"data:", first)
            extmembers.set_enabled(m["id"], False)
            state.notify_change()
            chunks = [c for _, c in zip(range(5), it)]
            r.close()
        self.assertTrue(any(b"event: auth" in c for c in chunks), chunks)


class PanelUsesOwnLists(Base):
    def test_cookie_identity_sees_own_lists(self):
        m, tok = self.friend("小明")
        userlists.create(m["id"], "小明的歌")
        userlists.create("owner", "老闆的歌")
        cf = self.client(); self.login(tok, c=cf)
        j = self.get("/api/ext/lists", c=cf).get_json()
        self.assertEqual(j["you"], "小明")
        self.assertEqual([p["name"] for p in j["lists"]], ["小明的歌"])
        co = self.client(); self.login(OWN, c=co)
        self.assertEqual([p["name"] for p in self.get("/api/ext/lists", c=co).get_json()["lists"]], ["老闆的歌"])
        r = self.post("/api/ext/lists", c=cf, json={"name": "新歌單"})
        self.assertIn(r.status_code, (200, 201))
        self.assertEqual(len(userlists.list_playlists(m["id"])), 2)


class ExtensionUnchanged(Base):
    def setUp(self):
        super().setUp()
        self.ep = [mock.patch.object(extapi, "_pick_guild", return_value=G)]
        for p in self.ep: p.start()

    def tearDown(self):
        for p in self.ep: p.stop()
        super().tearDown()

    def test_key_header_works_without_cookie(self):
        m, tok = self.friend()
        for key in (OWN, tok):
            r = self.c.get("/api/ext/status", headers={"Host": HOST, "X-Ext-Token": key, "Origin": "chrome-extension://abc"})
            self.assertEqual(r.status_code, 200, key)
            self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "chrome-extension://abc")
            r = self.c.get("/api/ext/lists", headers={"Host": HOST, "X-Ext-Token": key})
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get("/api/ext/members", headers={"Host": HOST, "X-Ext-Token": OWN}).status_code, 200)
        self.assertEqual(self.c.get("/api/ext/members", headers={"Host": HOST, "X-Ext-Token": tok}).status_code, 403)
        r = self.c.open("/api/ext/add", method="OPTIONS", headers={"Host": HOST, "Origin": "chrome-extension://abc"})
        self.assertLess(r.status_code, 400)

    def test_disabled_key_stops_extension(self):
        m, tok = self.friend()
        extmembers.set_enabled(m["id"], False)
        self.assertEqual(self.c.get("/api/ext/status", headers={"Host": HOST, "X-Ext-Token": tok}).status_code, 401)

    def test_wrong_header_does_not_fall_back_to_cookie(self):
        self.login(OWN)
        r = self.c.get("/api/ext/status", headers={"Host": HOST, "X-Ext-Token": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_extension_failures_do_not_lock_login(self):
        for _ in range(10):
            self.c.get("/api/ext/status", headers={"Host": HOST, "X-Ext-Token": "wrong", "CF-Connecting-IP": "4.4.4.4"})
        self.assertEqual(self.login(OWN, ip="4.4.4.4").status_code, 302)


class Frontend(unittest.TestCase):
    ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    def read(self, *p):
        return open(os.path.join(self.ROOT, *p), encoding="utf-8").read()

    def test_login_page_and_panel_hooks(self):
        html = self.read("templates", "login.html")
        self.assertIn('action="/login"', html)
        self.assertIn('type="password"', html)
        self.assertIn("stroke-bg", html)                  # 隨意筆劃主題
        js = self.read("static", "js", "app.js")
        self.assertIn("function goLogin()", js)
        self.assertIn("res.status === 401", js)
        self.assertIn("addEventListener('auth'", js)
        idx = self.read("templates", "index.html")
        self.assertIn('action="/logout"', idx)
        self.assertNotIn('id="mlToken"', idx)
        mem = self.read("templates", "members.html")
        self.assertNotIn("X-Ext-Token", mem)
        self.assertNotIn('id="owner"', mem)


if __name__ == "__main__":
    unittest.main()
