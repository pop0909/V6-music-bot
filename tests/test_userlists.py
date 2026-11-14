"""個人歌單：儲存、上限、身分隔離、API、匯入、Discord 連結、/mylist。"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from musicbot import userlists, userlistapi, extmembers, webapi, importer, state  # noqa: E402

from musicbot import actionlog as _actionlog  # noqa: E402
_actionlog._reset_for_tests()   # 操作紀錄寫到暫存資料夾，不碰專案的 data/
webapi.flask_app.config["LOGIN_DISABLED"] = True   # 這裡測 API 本身；登入另外在 test_webauth.py 測

U = "https://www.youtube.com/watch?v=%011d"
OWN = "o" * 40


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = [mock.patch.object(userlists, "FILE", os.path.join(self.tmp, "ul.json")),
                  mock.patch.object(extmembers, "FILE", os.path.join(self.tmp, "m.json")),
                  mock.patch.dict(os.environ, {"EXT_TOKEN": OWN})]
        for p in self.p: p.start()
        userlists._reset_cache()
        extmembers._cache.clear() if isinstance(getattr(extmembers, "_cache", None), dict) else None
        self.c = webapi.flask_app.test_client()

    def tearDown(self):
        for p in self.p: p.stop()
        userlists._reset_cache()
        if isinstance(getattr(extmembers, "_cache", None), dict): extmembers._cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def h(self, tok=OWN):
        return {"X-Ext-Token": tok}


class Storage(Base):
    def test_crud_and_limits(self):
        pl = userlists.create("a", "Mix", [{"url": U % 1, "title": "x" * 300}, {"url": "javascript:x"}, {"url": U % 1}])
        self.assertEqual(pl["count"], 1)
        self.assertEqual(len(userlists.get_playlist("a", pl["id"])["songs"][0]["title"]), 200)
        with self.assertRaises(ValueError): userlists.create("a", "Mix")
        with self.assertRaises(ValueError): userlists.create("a", "  ")
        for i in range(userlists.MAX_LISTS - 1): userlists.create("a", "L%d" % i)
        with self.assertRaises(ValueError): userlists.create("a", "one too many")
        self.assertEqual(userlists.list_playlists("b"), [])        # 其他人看不到
        with self.assertRaises(KeyError): userlists.get_playlist("b", pl["id"])

    def test_song_cap_and_persist_atomic(self):
        pl = userlists.create("a", "Big")
        added, sk = userlists.add_songs("a", pl["id"], [{"url": U % i, "title": "t"} for i in range(userlists.MAX_SONGS + 5)])
        self.assertEqual((added, sk), (userlists.MAX_SONGS, 5))
        with self.assertRaises(ValueError): userlists.add_songs("a", pl["id"], [{"url": U % 9999, "title": "t"}])
        self.assertFalse(os.path.exists(userlists.FILE + ".tmp"))
        if os.name != "nt": self.assertEqual(os.stat(userlists.FILE).st_mode & 0o777, 0o600)
        userlists._reset_cache()
        self.assertEqual(userlists.list_playlists("a")[0]["count"], userlists.MAX_SONGS)

    def test_corrupt_file_recovers(self):
        with open(userlists.FILE, "w") as f: f.write("{broken")
        self.assertEqual(userlists.list_playlists("a"), [])
        self.assertTrue(os.path.exists(userlists.FILE + ".corrupt"))

    def test_discord_link(self):
        userlists.link_discord("m1", "123456789012345678")
        self.assertEqual(userlists.key_for_discord(123456789012345678), "m1")
        userlists.link_discord("m1", "223456789012345678")   # 換 id → 舊的解除
        self.assertIsNone(userlists.key_for_discord("123456789012345678"))
        with self.assertRaises(ValueError): userlists.link_discord("m1", "abc")
        userlists.drop_user("m1")
        self.assertIsNone(userlists.key_for_discord("223456789012345678"))


class Api(Base):
    def test_isolation_between_members(self):
        _, t1 = extmembers.create("A"); _, t2 = extmembers.create("B")
        r = self.c.post("/api/ext/lists", json={"name": "mine"}, headers=self.h(t1))
        self.assertEqual(r.status_code, 201)
        lid = r.get_json()["list"]["id"]
        self.assertEqual(self.c.get("/api/ext/lists", headers=self.h(t2)).get_json()["lists"], [])
        self.assertEqual(self.c.get("/api/ext/lists/" + lid, headers=self.h(t2)).status_code, 404)
        self.assertEqual(self.c.post("/api/ext/lists/" + lid, json={"action": "rename", "name": "x"}, headers=self.h(t2)).status_code, 404)
        self.assertEqual(self.c.delete("/api/ext/lists/" + lid, headers=self.h(t2)).status_code, 404)
        self.assertEqual(self.c.delete("/api/ext/lists/" + lid, headers=self.h(OWN)).status_code, 404)   # 擁有者也是另一個人
        self.assertEqual(self.c.get("/api/ext/lists").status_code, 401)
        self.assertEqual(len(self.c.get("/api/ext/lists", headers=self.h(t1)).get_json()["lists"]), 1)

    def test_add_remove_rename_play(self):
        lid = self.c.post("/api/ext/lists", json={"name": "p"}, headers=self.h()).get_json()["list"]["id"]
        j = self.c.post("/api/ext/lists/" + lid, json={"action": "add", "url": U % 1, "title": "A"}, headers=self.h()).get_json()
        self.assertEqual(j["added"], 1)
        self.assertEqual(self.c.post("/api/ext/lists/" + lid, json={"action": "add", "url": U % 1}, headers=self.h()).get_json()["status"], "duplicate")
        self.c.post("/api/ext/lists/" + lid, json={"action": "add", "songs": [{"url": U % 2, "title": "B"}]}, headers=self.h())
        self.assertEqual(self.c.post("/api/ext/lists/" + lid, json={"action": "rename", "name": "q"}, headers=self.h()).get_json()["list"]["name"], "q")
        fake = mock.AsyncMock(return_value=2)
        with mock.patch.object(userlistapi, "web_import_songs", fake), \
             mock.patch.object(userlistapi, "run_async", lambda c: asyncio.run(c)):
            j = self.c.post("/api/ext/lists/" + lid, json={"action": "play", "shuffle": True, "guild_id": 5}, headers=self.h()).get_json()
        self.assertEqual(j["count"], 2)
        self.assertEqual(fake.await_args.args[0], 5)
        self.assertEqual(len(fake.await_args.args[1]), 2)
        self.assertEqual(self.c.post("/api/ext/lists/" + lid, json={"action": "remove", "url": U % 1}, headers=self.h()).status_code, 200)
        self.assertEqual(self.c.post("/api/ext/lists/" + lid, json={"action": "remove", "url": U % 1}, headers=self.h()).status_code, 404)

    def test_play_respects_queue_cap(self):
        from musicbot import webcore
        lid = self.c.post("/api/ext/lists", json={"name": "p", "songs": [{"url": U % i, "title": "t"} for i in range(10)]},
                          headers=self.h()).get_json()["list"]["id"]
        G = 6161
        state.queues[G] = [(U % (900 + i), "f") for i in range(3)]
        try:
            with mock.patch.dict(os.environ, {"QUEUE_MAX": "5"}), \
                 mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=None)), \
                 mock.patch.object(webcore._prefetch, "ensure", mock.Mock()), \
                 mock.patch.object(userlistapi, "run_async", lambda c: asyncio.run(c)):
                j = self.c.post("/api/ext/lists/" + lid, json={"action": "play", "guild_id": G}, headers=self.h()).get_json()
            self.assertEqual((j["count"], j["full_skipped"]), (2, 8))
            self.assertEqual(len(state.queues[G]), 5)
        finally:
            state.queues.pop(G, None)

    def test_import_creates_list_without_enqueue(self):
        songs = [{"url": U % i, "title": "s%d" % i, "duration": 1} for i in range(3)]
        with mock.patch.object(importer, "_youtube_list", return_value=("YT List", songs)) as yl, \
             mock.patch.object(importer.threading, "Thread", lambda target, daemon: mock.Mock(start=target)):
            self.assertEqual(self.c.post("/api/ext/lists/import", json={"url": "https://youtube.com/watch?v=x"}, headers=self.h()).status_code, 400)
            r = self.c.post("/api/ext/lists/import", json={"url": "https://www.youtube.com/playlist?list=PL1"}, headers=self.h())
        self.assertEqual(yl.call_args.args[1], userlists.MAX_SONGS)
        jid = r.get_json()["job"]
        j = self.c.get("/api/ext/lists/import/" + jid, headers=self.h()).get_json()["job"]
        self.assertEqual((j["status"], j["added"]), ("done", 3))
        self.assertNotIn("owner_key", j)
        _, t2 = extmembers.create("C")
        self.assertEqual(self.c.get("/api/ext/lists/import/" + jid, headers=self.h(t2)).status_code, 404)
        ls = userlists.list_playlists("owner")
        self.assertEqual([(p["name"], p["count"]) for p in ls], [("YT List", 3)])
        self.assertEqual(state.queues.get(0), None)

    def test_members_link_owner_only(self):
        m, t1 = extmembers.create("D")
        self.assertEqual(self.c.post("/api/ext/members/" + m["id"], json={"action": "link_discord", "discord_id": "123456789012345678"},
                                     headers=self.h(t1)).status_code, 403)
        self.assertEqual(self.c.post("/api/ext/members/" + m["id"], json={"action": "link_discord", "discord_id": "123456789012345678"},
                                     headers=self.h()).status_code, 200)
        self.assertEqual(self.c.post("/api/ext/members/owner", json={"action": "link_discord", "discord_id": "999456789012345678"},
                                     headers=self.h()).status_code, 200)
        self.assertEqual(self.c.post("/api/ext/members/nope", json={"action": "link_discord", "discord_id": "1"},
                                     headers=self.h()).status_code, 404)
        j = self.c.get("/api/ext/members", headers=self.h()).get_json()
        self.assertEqual(j["members"][0]["discord_id"], "123456789012345678")
        self.assertEqual(j["owner_discord_id"], "999456789012345678")
        userlists.create(m["id"], "x")
        self.c.delete("/api/ext/members/" + m["id"], headers=self.h())
        self.assertEqual(userlists.list_playlists(m["id"]), [])
        self.assertIsNone(userlists.key_for_discord("123456789012345678"))

    def test_separate_from_local_playlist_and_favorites(self):
        before = list(state.local_playlist)
        self.c.post("/api/ext/lists", json={"name": "z", "songs": [{"url": U % 1, "title": "a"}]}, headers=self.h())
        self.assertEqual(state.local_playlist, before)


class Discord(Base):
    def inter(self, uid=123456789012345678):
        i = mock.Mock(); i.user.id = uid; i.user.display_name = "u"; i.guild.id = 42
        i.response.is_done.return_value = False
        i.response.send_message = mock.AsyncMock(); i.response.defer = mock.AsyncMock()
        i.followup.send = mock.AsyncMock()
        return i

    def test_unlinked_gets_help(self):
        from musicbot import mylist_cmd
        i = self.inter()
        asyncio.run(mylist_cmd.show.callback(i))
        self.assertIn("/members", i.response.send_message.await_args.args[0])

    def test_linked_show_and_play(self):
        from musicbot import mylist_cmd, player, webcore
        userlists.link_discord("owner", "123456789012345678")
        pl = userlists.create("owner", "Chill", [{"url": U % 1, "title": "A"}])
        i = self.inter()
        asyncio.run(mylist_cmd.show.callback(i))
        self.assertIn("Chill", i.response.send_message.await_args.args[0])
        i = self.inter()
        fake = mock.AsyncMock(return_value=1)
        with mock.patch.object(player, "get_voice_client", mock.AsyncMock(return_value=mock.Mock())), \
             mock.patch.object(webcore, "web_import_songs", fake):
            asyncio.run(mylist_cmd.play.callback(i, "chi", False))
        self.assertEqual(fake.await_args.args[0], 42)
        self.assertIn("1", i.response.send_message.await_args.args[0])
        # 別人（未連結）看不到
        j = self.inter(uid=555555555555555555)
        asyncio.run(mylist_cmd.show.callback(j))
        self.assertNotIn("Chill", j.response.send_message.await_args.args[0])

    def test_one_command_group(self):
        from musicbot.bot import tree
        import musicbot.commands  # noqa
        g = tree.get_command("mylist")
        self.assertEqual(sorted(c.name for c in g.commands), ["add", "import", "play", "show"])


class Frontend(unittest.TestCase):
    def test_tab_and_version(self):
        html = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
        self.assertIn('data-tab="mylists"', html)
        self.assertIn('id="tabMylists"', html)
        self.assertIn("app.js?v=20261114perf", html)
        js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        self.assertIn("'/api/ext/lists'", js)


class FollowUps(Base):
    def wait(self, r):
        import time
        jid = r.get_json()["job"]
        for _ in range(200):
            if (importer.get_job(jid) or {}).get("status") != "running": return
            time.sleep(0.02)

    def test_spotify_import_into_list_skips_failures(self):
        tracks = [("t%d" % i, "a") for i in range(4)]
        def search(t, a):
            if t == "t1": raise RuntimeError("no result")
            return {"url": U % int(t[1:]), "title": t, "duration": 1}
        with mock.patch.object(importer, "_spotify_tracks", return_value=("Sp List", tracks)), \
             mock.patch.object(importer, "_yt_search_one", search):
            r = self.c.post("/api/ext/lists/import", json={"url": "https://open.spotify.com/playlist/abc"}, headers=self.h())
            self.wait(r)
        self.assertEqual(r.status_code, 202)
        j = self.c.get("/api/ext/lists/import/" + r.get_json()["job"], headers=self.h()).get_json()["job"]
        self.assertEqual((j["status"], j["added"], j["skipped"]), ("done", 3, 1))
        self.assertEqual([(p["name"], p["count"]) for p in userlists.list_playlists("owner")], [("Sp List", 3)])

    def test_spotify_bot_check_errors_without_creating(self):
        def search(t, a): raise RuntimeError("Sign in to confirm you're not a bot")
        with mock.patch.object(importer, "_spotify_tracks", return_value=("S", [("x", "")] * 30)), \
             mock.patch.object(importer, "_yt_search_one", search):
            r = self.c.post("/api/ext/lists/import", json={"url": "https://open.spotify.com/album/abc"}, headers=self.h())
            self.wait(r)
        j = self.c.get("/api/ext/lists/import/" + r.get_json()["job"], headers=self.h()).get_json()["job"]
        self.assertEqual(j["status"], "error")
        self.assertEqual(userlists.list_playlists("owner"), [])

    def test_mylist_import_accepts_spotify(self):
        src = open(os.path.join(ROOT, "musicbot", "mylist_cmd.py"), encoding="utf-8").read()
        self.assertIn('not (importer.is_spotify(url) or "list=" in url)', src)

    def test_mobile_more_menu_markup(self):
        js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        css = open(os.path.join(ROOT, "static", "css", "style.css"), encoding="utf-8").read()
        self.assertIn('data-a="more"', js)
        self.assertIn('class="ml-menu"', js)
        self.assertIn(".ml-item .ml-opt { display: none; }", css)

    def test_extension_wiring(self):
        ext = os.path.join(ROOT, "chrome-extension")
        bg = open(os.path.join(ext, "background.js"), encoding="utf-8").read()
        for t in ("'lists'", "'listCreate'", "'listAdd'"):
            self.assertIn(t, bg)
        man = json.load(open(os.path.join(ext, "manifest.json"), encoding="utf-8"))
        self.assertEqual(man["version"], "1.1.1")


class LiveUpdate(Base):
    def test_rev_bumps_and_in_sse_state(self):
        from musicbot import webcore, state as st
        r0 = userlists.rev(); v0 = st.change_version()
        pl = userlists.create("owner", "x")
        userlists.add_songs("owner", pl["id"], [{"url": U % 1, "title": "a"}])
        self.assertEqual(userlists.rev(), r0 + 2)
        self.assertGreater(st.change_version(), v0)          # SSE 會被喚醒
        self.assertEqual(webcore.get_full_app_state(0)["ul_rev"], userlists.rev())
        v1 = st.change_version()
        self.c.delete("/api/ext/lists/" + pl["id"], headers=self.h())   # DELETE 也要推
        self.assertGreater(st.change_version(), v1)

    def test_api_no_store(self):
        for path in ("/api/ext/lists", "/api/playlist", "/api/favorites?guild_id=1", "/api/status?guild_id=1"):
            r = self.c.get(path, headers=self.h())
            self.assertEqual(r.headers.get("Cache-Control"), "no-store", path)
        self.assertNotEqual(self.c.get("/").headers.get("Cache-Control"), "no-store")

    def test_add_by_url_only_looks_up_title_and_normalizes(self):
        from musicbot import extapi
        lid = userlists.create("owner", "u")["id"]
        with mock.patch.object(extapi, "_lookup_title", return_value="Real Title") as lt:
            j = self.c.post("/api/ext/lists/" + lid, json={"action": "add", "url": "https://youtu.be/dQw4w9WgXcQ?t=3"}, headers=self.h()).get_json()
        self.assertEqual(j["added"], 1)
        lt.assert_called_once()
        s = userlists.get_playlist("owner", lid)["songs"][0]
        self.assertEqual((s["url"], s["title"]), ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Real Title"))
        j = self.c.post("/api/ext/lists/" + lid, json={"action": "add", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "title": "t"}, headers=self.h()).get_json()
        self.assertEqual(j["status"], "duplicate")

    def test_frontend_listens(self):
        js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        self.assertIn("ML.onRev(data.ul_rev)", js)
        self.assertIn("cache: 'no-store'", js)


class HeadCount(unittest.TestCase):
    def test_header_count_synced_from_fetched_songs(self):
        js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        self.assertIn("function syncHead(pl, wrap, fresh)", js)
        i = js.index("songsCache.set(pl.id, j.list.songs);")
        self.assertIn("syncHead(pl, wrap, j.list);", js[i:i + 200])


class Chaos(Base):
    def test_concurrent_writes_keep_file_valid(self):
        import threading
        keys = ["owner", "m1", "m2", "m3"]
        ids = {k: userlists.create(k, "L")["id"] for k in keys}
        def work(k, base):
            for i in range(40):
                userlists.add_songs(k, ids[k], [{"url": U % (base + i), "title": "t"}])
                if i % 10 == 0: userlists.rename(k, ids[k], "L%d" % i)
        ts = [threading.Thread(target=work, args=(k, n * 1000)) for n, k in enumerate(keys)]
        for t in ts: t.start()
        for t in ts: t.join()
        raw = json.load(open(userlists.FILE, encoding="utf-8"))
        for k in keys:
            self.assertEqual(len(raw["users"][k]["lists"][0]["songs"]), 40)

    def test_discord_id_cannot_be_stolen_and_unlink(self):
        userlists.link_discord("m1", "123456789012345678")
        with self.assertRaises(ValueError): userlists.link_discord("m2", "123456789012345678")
        self.assertEqual(userlists.key_for_discord("123456789012345678"), "m1")
        userlists.link_discord("m1", "123456789012345678")        # 自己重設同一個 OK
        userlists.link_discord("m1", "")
        self.assertIsNone(userlists.key_for_discord("123456789012345678"))
        userlists.link_discord("m2", "123456789012345678")        # 解除後別人可以用
        for bad in ("12", "abc", "1" * 30, "-123456789012345678"):
            with self.assertRaises(ValueError): userlists.link_discord("m3", bad)

    def test_public_import_status_hides_personal_jobs(self):
        songs = [{"url": U % 1, "title": "a"}]
        with mock.patch.object(importer, "_youtube_list", return_value=("X", songs)), \
             mock.patch.object(importer.threading, "Thread", lambda target, daemon: mock.Mock(start=target)):
            jid = self.c.post("/api/ext/lists/import", json={"url": "https://www.youtube.com/playlist?list=PL1"}, headers=self.h()).get_json()["job"]
        self.assertEqual(self.c.get("/api/import/" + jid).status_code, 404)
        self.assertEqual(self.c.get("/api/ext/lists/import/" + jid, headers=self.h()).status_code, 200)

    def test_sse_state_has_no_list_content(self):
        from musicbot import webcore
        userlists.create("owner", "Secret Name", [{"url": U % 1, "title": "Secret Song"}])
        s = json.dumps(webcore.get_full_app_state(0), ensure_ascii=False)
        self.assertNotIn("Secret", s)

    def test_play_does_not_readd_now_playing(self):
        from musicbot import webcore
        G = 7171
        lid = userlists.create("owner", "p", [{"url": U % i, "title": "t"} for i in range(3)])["id"]
        state.queues[G] = []; state.now_playing_url[G] = U % 0
        try:
            with mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=None)), \
                 mock.patch.object(webcore._prefetch, "ensure", mock.Mock()), \
                 mock.patch.object(userlistapi, "run_async", lambda c: asyncio.run(c)):
                j = self.c.post("/api/ext/lists/" + lid, json={"action": "play", "guild_id": G}, headers=self.h()).get_json()
            self.assertEqual(j["count"], 2)
        finally:
            state.queues.pop(G, None); state.now_playing_url.pop(G, None)

    def test_mylist_wrong_name_lists_names(self):
        from musicbot import mylist_cmd
        userlists.link_discord("owner", "123456789012345678")
        userlists.create("owner", "Chill")
        i = mock.Mock(); i.user.id = 123456789012345678; i.guild.id = 1
        i.response.is_done.return_value = False; i.response.send_message = mock.AsyncMock()
        asyncio.run(mylist_cmd.play.callback(i, "nope", False))
        self.assertIn("Chill", i.response.send_message.await_args.args[0])

    def test_frontend_logout_sticks_and_long_names(self):
        js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        # 登入改成 cookie：我的歌單不再送 X-Ext-Token、不再把金鑰存在 localStorage（舊的會被清掉）
        self.assertIn("localStorage.removeItem('musbot-my-token')", js)
        self.assertNotIn("'X-Ext-Token': tok()", js)
        self.assertNotIn("localStorage.setItem(KEY", js)
        self.assertIn('class="ml-name"', js)


class UxPass(Base):
    def test_discord_id_length_matches_message(self):
        with self.assertRaises(ValueError): userlists.link_discord("m1", "1" * 16)
        with self.assertRaises(ValueError): userlists.link_discord("m1", "1" * 21)
        userlists.link_discord("m1", "1" * 17); userlists.link_discord("m1", "2" * 20)

    def test_frontend_ux_fixes(self):
        js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        self.assertIn("ic.hidden = _TOAST_EMOJI.test(String(msg));", js)
        self.assertIn("e.target.closest('.ml-menu, .ml-more')", js)
        self.assertNotIn("nextMode === 'none' ? 'warning'", js)
        api = open(os.path.join(ROOT, "chrome-extension", "api.js"), encoding="utf-8").read()
        self.assertNotIn("貼上 token", api)


class CyberPalette(unittest.TestCase):
    def test_palette_applied_flat(self):
        import re
        css = open(os.path.join(ROOT, "static", "css", "style.css"), encoding="utf-8").read()
        self.assertIn("--accent: #ff2e97;", css)
        self.assertIn("--accent: #d6006f;", css)
        self.assertIn("--on-accent:", css)
        self.assertNotRegex(css, r"rgba\(\s*(139,\s*92,\s*246|236,\s*72,\s*153|217,\s*70,\s*239)")
        self.assertNotIn("bg-aurora", css)
        self.assertNotIn("rgba(217,70,239", css)


class PopoverLayering(unittest.TestCase):
    def test_popovers_exclusive_and_tip_hidden(self):
        js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        css = open(os.path.join(ROOT, "static", "css", "style.css"), encoding="utf-8").read()
        self.assertIn("if (sp && !sp.hidden) $('statusClose').click();", js)
        self.assertIn("if (o) { setKbdHelp(false);", js)
        self.assertIn('.has-tip[aria-expanded="true"]::after { display: none !important; }', css)
        self.assertIn("--pop-border:", css)

    def test_popovers_close_outside_and_esc(self):
        js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        self.assertIn("open(false); }, { capture: true, passive: true });", js)
        self.assertIn("if (e.key === 'Escape') closeMenus();", js)
        self.assertIn("$('editModal').classList.contains('active')) $('editModal').classList.remove('active');", js)


class CustomDropdown(unittest.TestCase):
    def test_dropdown_wiring(self):
        js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        css = open(os.path.join(ROOT, "static", "css", "style.css"), encoding="utf-8").read()
        html = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
        self.assertIn("enhance(el.voiceChannelSelect, 'voice');", js)
        self.assertIn("sel.dispatchEvent(new Event('change', { bubbles: true }));", js)
        self.assertIn("window.closeDropdowns", js)
        self.assertIn("e.key === 'Escape') close(true)", js)
        self.assertIn(".cdd-list {", css)
        self.assertIn('<option value="">選擇語音頻道</option>', html)
        # 機器人所在頻道的 🟢 標記：正規式要有 u 旗標才比對得到 emoji
        self.assertIn(r"/^[\u{1F50A}\u{1F7E2}]/u", js)
        self.assertNotIn(r"/^[\u{1F50A}\u{1F7E2}]/,", js)


class MembersButton(unittest.TestCase):
    def test_members_in_topbar(self):
        html = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
        css = open(os.path.join(ROOT, "static", "css", "style.css"), encoding="utf-8").read()
        self.assertIn('id="btnMembers" href="/members"', html)
        self.assertIn('data-tip="成員管理"', html)
        self.assertNotIn("👥 擴充功能成員管理", html)
        self.assertIn("@media (max-width: 760px) { .members-link { display: block; } }", css)


class BugHuntRegressionTest(unittest.TestCase):
    """全面巡檢修正：便宜的字串檢查，防止回退"""
    def setUp(self):
        r = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()
        self.css, self.js, self.members = r("static", "css", "style.css"), r("static", "js", "app.js"), r("templates", "members.html")

    def test_hidden_attr_wins(self):
        self.assertIn("[hidden] { display: none !important; }", self.css)

    def test_light_status_colors_readable(self):
        self.assertIn("--success: #15803d", self.css)
        self.assertIn('.live-pill.is-offline { color: var(--danger); }', self.css)

    def test_search_results_use_popover_bg(self):
        i = self.css.index("全面巡檢修正")
        self.assertIn(".search-results", self.css[i:])
        self.assertIn("var(--pop-bg)", self.css[i:])

    def test_voice_options_refresh_and_clamp(self):
        self.assertIn("function refreshVoiceOptions()", self.js)
        self.assertIn("refreshVoiceOptions();", self.js)
        self.assertIn("clamp()", self.js)
        self.assertIn("/^[\\u{1F50A}\\u{1F7E2}]/u", self.js)

    def test_members_page_has_light_theme(self):
        self.assertIn('html[data-theme="light"]', self.members)
        self.assertIn("localStorage", self.members)


class BugHuntRegressionTest2(unittest.TestCase):
    def test_light_history_meta_readable(self):
        css = open(os.path.join(ROOT, "static", "css", "style.css"), encoding="utf-8").read()
        self.assertIn('.hist-meta { color: var(--sub); }', css)


class DropdownAlignTest(unittest.TestCase):
    def test_menu_matches_trigger_width(self):
        js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        css = open(os.path.join(ROOT, "static", "css", "style.css"), encoding="utf-8").read()
        self.assertIn("list.style.width = `${br.width}px`;", js)
        self.assertIn("list.style.left = `${br.left - pr.left", js)
        self.assertIn(".cdd-list { min-width: 0; max-width: none; box-sizing: border-box; }", css)


class DropdownShortNameTest(unittest.TestCase):
    def test_seven_grapheme_limit_with_title(self):
        js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        self.assertIn("granularity: 'grapheme'", js)
        self.assertIn("g.length > max ? g.slice(0, max).join('') + '…' : name", js)
        self.assertIn('title="${escapeHtml(p.name)}">${escapeHtml(shortName(p.name))}', js)
        self.assertIn("setText(label, p ? shortName(p.name) : ph);", js)
