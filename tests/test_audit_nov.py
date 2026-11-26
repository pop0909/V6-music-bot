"""2026-11 健檢（第二輪）修正：加歌網址白名單、安全標頭 / 日誌遮蔽 / 擴充功能金鑰鎖定、SSE 只送變動、
自帶圖示與系統字型、共用模組（owner / jsonfile / tz）、清掉沒用的程式碼。"""
import asyncio
import io
import json
import logging
import os
import pathlib
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from musicbot import (webapi, webcore, webserver, webauth, extmembers, userlists, utils, storage, state,  # noqa: E402
                      jsonfile, tz, owner, player, commands, discordauth)
from appjs import app_js_paths  # noqa: E402
webapi.flask_app.config["LOGIN_DISABLED"] = True

HOST = "musbot.kdns.fr"
G = 93001


class Allowlist(unittest.TestCase):
    def test_hosts(self):
        ok = ["https://www.youtube.com/watch?v=abcdefghijk", "https://music.youtube.com/watch?v=abcdefghijk",
              "https://m.youtube.com/watch?v=x", "https://youtube.com/playlist?list=PL1", "https://youtu.be/abcdefghijk",
              "http://YOUTU.BE/abc", "https://open.spotify.com/track/1", "https://spotify.com/album/1",
              "https://soundcloud.com/a/b", "https://on.soundcloud.com/xyz", "https://m.soundcloud.com/a/b",
              "https://www.bilibili.com/video/BV1xx411c7mD", "https://b23.tv/abcdef"]
        bad = ["https://evil.com/x", "https://youtube.com.evil.com/x", "https://evilyoutube.com/x",
               "https://youtube.com@evil.com/x", "https://bilibili.com.evil.com/video/BV1xx411c7mD", "javascript:alert(1)",
               "ftp://youtube.com/x", "https://notspotify.com/x", "", None]
        for u in ok:
            self.assertTrue(utils.allowed_song_url(u), u)
        for u in bad:
            self.assertFalse(utils.allowed_song_url(u), u)

    def test_schemeless_for_discord_play(self):
        self.assertTrue(utils.song_host_allowed("www.youtube.com/watch?v=abc"))
        self.assertTrue(utils.song_host_allowed("youtu.be/abc"))
        self.assertFalse(utils.song_host_allowed("example.com/song.mp3"))

    def test_error_messages(self):
        self.assertIsNone(utils.song_url_error("https://youtu.be/abc"))
        self.assertIn("http/https", utils.song_url_error("hello"))
        self.assertEqual(utils.song_url_error("https://evil.com/a"), utils.URL_NOT_ALLOWED)
        self.assertIn("YouTube", utils.URL_NOT_ALLOWED)


class AllowlistRoutes(unittest.TestCase):
    def setUp(self):
        self.c = webapi.flask_app.test_client()
        state.guild_names[G] = "測試"
        self.saved = list(state.local_playlist)
        self.p = [mock.patch.object(webapi, "save_local_playlist", lambda: True),
                  mock.patch("musicbot.guildscope.allowed_guilds", return_value=None)]
        for p in self.p: p.start()

    def tearDown(self):
        for p in self.p: p.stop()
        state.guild_names.pop(G, None)
        state.queues.pop(G, None)
        state.local_playlist[:] = self.saved

    def test_panel_routes_reject_other_sites(self):
        evil = "https://evil.example.com/song.mp3"
        for path, body in (("/api/queue/add", {"guild_id": G, "url": evil}),
                           ("/api/playlist/add", {"url": evil}),
                           ("/api/resolve-url", {"url": evil})):
            r = self.c.post(path, json=body)
            self.assertEqual(r.status_code, 400, path)
            self.assertEqual(r.get_json()["message"], utils.URL_NOT_ALLOWED, path)
        r = self.c.post("/api/import", json={"guild_id": G, "url": "https://evil.com/x?list=PL1"})
        self.assertEqual(r.status_code, 400)

    def test_batch_paths_skip_other_sites(self):
        songs = [{"url": "https://evil.com/a", "title": "壞"}, {"url": "https://youtu.be/abcdefghijk", "title": "好"}]
        with mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=None)), \
                mock.patch.object(webcore._prefetch, "ensure", mock.Mock()):
            asyncio.run(webcore.web_add_to_queue_batch(G, songs))
            self.assertEqual([u for u, _ in state.queues[G]], ["https://youtu.be/abcdefghijk"])
            asyncio.run(webcore.web_play_all(G, songs))
            self.assertEqual([u for u, _ in state.queues[G]], ["https://youtu.be/abcdefghijk"])
            r = asyncio.run(webcore.web_add_to_queue(G, "https://evil.com/a", "壞", auto_play=False))
            self.assertEqual(r["message"], utils.URL_NOT_ALLOWED)

    def test_personal_lists_skip_other_sites(self):
        self.assertEqual(userlists._clean_songs([{"url": "https://evil.com/a"}, {"url": "https://youtu.be/x"}]),
                         [{"url": "https://youtu.be/x", "title": "https://youtu.be/x"}])

    def test_discord_play_rejects_other_sites(self):
        from musicbot import player_voice
        it = mock.MagicMock()
        it.response.is_done.return_value = True
        it.followup.send = mock.AsyncMock()
        with mock.patch.object(player_voice, "_lookup", mock.AsyncMock()) as look:
            asyncio.run(player_voice._process_and_play(it, "https://evil.com/song.mp3", G, mock.MagicMock()))
            look.assert_not_called()
        self.assertIn(utils.URL_NOT_ALLOWED, it.followup.send.call_args[0][0])


class SecurityHeaders(unittest.TestCase):
    def setUp(self):
        self.c = webapi.flask_app.test_client()

    def test_headers(self):
        r = self.c.get("/api/me", headers={"Host": HOST})
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Strict-Transport-Security", r.headers)           # http：不送 HSTS
        r = self.c.get("/api/me", headers={"Host": HOST, "X-Forwarded-Proto": "https"})
        self.assertIn("max-age=", r.headers["Strict-Transport-Security"])
        r = self.c.get("/static/css/style.css", headers={"Host": HOST})
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        r.close()

    def test_access_log_hides_oauth_query(self):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        lg = logging.getLogger("werkzeug")
        lg.addHandler(h)
        old = lg.level
        lg.setLevel(logging.INFO)
        try:
            def app(environ, start_response):
                start_response("302 FOUND", [])
                return [b""]
            mw = webserver.AccessLog(app)
            mw({"PATH_INFO": "/auth/discord/callback", "QUERY_STRING": "code=SECRETCODE&state=SECRETSTATE",
                "REQUEST_METHOD": "GET"}, lambda s, h, e=None: None)
            mw({"PATH_INFO": "/api/status", "QUERY_STRING": "guild_id=5", "REQUEST_METHOD": "GET"},
               lambda s, h, e=None: None)
        finally:
            lg.removeHandler(h)
            lg.setLevel(old)
        out = buf.getvalue()
        self.assertNotIn("SECRETCODE", out)
        self.assertNotIn("SECRETSTATE", out)
        self.assertIn("/auth/discord/callback?…", out)
        self.assertIn("/api/status?guild_id=5", out)


class ExtKeyLockout(unittest.TestCase):
    OWN = "k" * 40

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = [mock.patch.object(extmembers, "FILE", os.path.join(self.tmp, "m.json")),
                  mock.patch.dict(os.environ, {"EXT_TOKEN": self.OWN})]
        for p in self.p: p.start()
        extmembers._cache.update(mtime=None, members=[])
        webauth.reset_all()
        self.c = webapi.flask_app.test_client()

    def tearDown(self):
        for p in reversed(self.p): p.stop()
        extmembers._cache.update(mtime=None, members=[])
        webauth.reset_all()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, tok, ip):
        return self.c.get("/api/ext/status", headers={"Host": HOST, "X-Ext-Token": tok, "CF-Connecting-IP": ip})

    def test_locks_after_many_bad_keys_per_ip(self):
        for _ in range(webauth.EXT_MAX_FAILS - 1):
            self.assertEqual(self.get("wrong", "203.0.113.9").status_code, 401)
        self.assertEqual(self.get("wrong", "203.0.113.9").status_code, 401)   # 第 20 次：記下並上鎖
        r = self.get(self.OWN, "203.0.113.9")                                 # 鎖住期間連正確的金鑰也先擋
        self.assertEqual(r.status_code, 429)
        self.assertIn("分鐘後再試", r.get_json()["message"])
        self.assertNotEqual(self.get(self.OWN, "198.51.100.7").status_code, 429)   # 別的 IP 不受影響
        self.assertEqual(webauth.locked_for("203.0.113.9"), 0)                     # 網頁登入另外計數，不會被一起鎖

    def test_good_key_does_not_count(self):
        for _ in range(webauth.EXT_MAX_FAILS + 5):
            self.assertNotEqual(self.get(self.OWN, "203.0.113.10").status_code, 429)


class SseRevisions(unittest.TestCase):
    def setUp(self):
        state.guild_names[G] = "測試"
        self.saved = list(state.local_playlist)
        state.local_playlist[:] = [{"url": "https://youtu.be/%011d" % i, "title": "s%d" % i} for i in range(300)]
        self.p = [mock.patch.object(storage, "_atomic_write_lines", lambda p, items: None)]
        for p in self.p: p.start()

    def tearDown(self):
        for p in self.p: p.stop()
        state.guild_names.pop(G, None)
        state.local_playlist[:] = self.saved

    def test_lists_only_sent_when_changed(self):
        first = webcore.get_full_app_state(G, {})
        self.assertIn("playlist", first)
        for gone in ("favorites", "fav_rev", "favorites_count"):          # 最愛已拿掉：SSE 不再帶
            self.assertNotIn(gone, first)
        known = {"pl": first["pl_rev"]}
        again = webcore.get_full_app_state(G, known)
        self.assertNotIn("playlist", again)
        self.assertEqual(again["playlist_count"], 300)
        self.assertLess(len(json.dumps(again)), len(json.dumps(first)) / 5)
        state.local_playlist.append({"url": "https://youtu.be/new", "title": "新歌"})
        storage.save_local_playlist()
        self.assertEqual(len(webcore.get_full_app_state(G, known)["playlist"]), 301)

    def test_length_change_without_save_still_resent(self):
        first = webcore.get_full_app_state(G)
        known = {"pl": first["pl_rev"]}
        state.local_playlist.pop()
        self.assertEqual(len(webcore.get_full_app_state(G, known)["playlist"]), 299)

    def test_without_known_everything_is_sent(self):
        self.assertIn("playlist", webcore.get_full_app_state(G))     # /api 其他呼叫、重連的第一筆

    def test_stream_first_push_full_then_light(self):
        webapi.flask_app.config["LOGIN_DISABLED"] = True
        c = webapi.flask_app.test_client()
        ticks = iter([1, 2, 3])
        with mock.patch("musicbot.guildscope.allowed_guilds", return_value=None), \
                mock.patch.object(webapi, "get_full_app_state", wraps=webcore.get_full_app_state) as fs, \
                mock.patch("musicbot.webapi_player.wait_change", side_effect=lambda v, t: next(ticks, 99)):
            r = c.get(f"/api/progress-stream?guild_id={G}", buffered=False)
            it = iter(r.response)
            a = json.loads(next(it).decode()[len("data: "):])
            state.now_playing[G] = "換一首"                       # 讓狀態變，才會再推
            b = json.loads(next(it).decode()[len("data: "):])
            r.close()
        state.now_playing.pop(G, None)
        self.assertIn("playlist", a)
        self.assertNotIn("playlist", b)
        self.assertEqual(fs.call_args_list[1][0][1], {"pl": a["pl_rev"]})


class PanelAssets(unittest.TestCase):
    def test_no_external_fonts_or_cdn(self):
        for name in ("index.html", "members.html", "login.html"):
            html = (ROOT / "templates" / name).read_text(encoding="utf-8")
            for host in ("fonts.googleapis.com", "fonts.gstatic.com", "cdnjs.cloudflare.com"):
                self.assertNotIn(host, html, name)

    def test_every_used_icon_has_a_rule(self):
        css = (ROOT / "static" / "css" / "icons.css").read_text(encoding="utf-8")
        used = set()
        for f in ("templates/index.html", "templates/members.html", "templates/login.html", *app_js_paths(rel=True)):
            used |= set(re.findall(r"\bfa-([a-z0-9-]+)", (ROOT / f).read_text(encoding="utf-8")))
        generic = {"solid", "brands", "regular", "pulse", "spin", "fw", "lg", "xs", "sm", "xl", "2x", "3x"}
        missing = sorted(n for n in used - generic if not re.search(r"\.fa-" + re.escape(n) + r"[{:,.]", css))
        self.assertEqual(missing, [])

    def test_fonts_exist_and_are_small(self):
        css = (ROOT / "static" / "css" / "icons.css").read_text(encoding="utf-8")
        urls = re.findall(r"url\((/static/fonts/[^)?]+)\?v=[0-9a-f]+\)", css)
        self.assertEqual(sorted(os.path.basename(u) for u in urls), ["fa-brands-400.woff2", "fa-solid-900.woff2"])
        for u in urls:
            p = ROOT / u.lstrip("/")
            self.assertTrue(p.is_file(), u)
            self.assertLess(p.stat().st_size, 30_000)
            self.assertEqual(p.read_bytes()[:4], b"wOF2")

    def test_icon_and_style_versions_match(self):
        idx = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        mem = (ROOT / "templates" / "members.html").read_text(encoding="utf-8")
        v = re.findall(r"icons\.css\?v=([\w.-]+)", idx)
        self.assertEqual(v, re.findall(r"icons\.css\?v=([\w.-]+)", mem))
        self.assertEqual(set(v), set(re.findall(r"style\.css\?v=([\w.-]+)", idx)))

    def test_font_served_with_font_type(self):
        c = webapi.flask_app.test_client()
        r = c.get("/static/fonts/fa-solid-900.woff2?v=1", headers={"Host": HOST})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "font/woff2")
        self.assertIn("immutable", r.headers["Cache-Control"])
        r.close()

    def test_system_cjk_font_stack(self):
        css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
        m = re.search(r"--font:\s*([^;]+);", css)
        for name in ("PingFang TC", "Microsoft JhengHei", "Noto Sans TC"):
            self.assertIn(name, m.group(1))
        self.assertNotIn("Inter", css)


class SharedModules(unittest.TestCase):
    def test_write_atomic(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "sub", "x.json")
            jsonfile.write_atomic(p, {"a": "中文"}, private=True)
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"a": "中文"})
            self.assertFalse(os.path.exists(p + ".tmp"))
            if os.name == "posix":
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            with mock.patch("json.dump", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    jsonfile.write_atomic(p, {"b": 1})
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"a": "中文"})          # 寫到一半失敗：原檔不變
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_one_taipei_timezone(self):
        from musicbot import actionlog, selfcheck
        self.assertIs(actionlog.TZ, tz.TAIPEI)
        self.assertIs(selfcheck.TZ, tz.TAIPEI)
        self.assertEqual(tz.taipei(0).strftime("%Y-%m-%d %H:%M"), "1970-01-01 08:00")
        from musicbot import alerts
        self.assertEqual(alerts.taipei_time(0), "1970-01-01 08:00（台北時間）")

    def test_owner_id(self):
        owner._warned.clear()
        with mock.patch.dict(os.environ, {"OWNER_ID": "439430754368225280"}):
            self.assertEqual(owner.configured_id(), "439430754368225280")
            self.assertEqual(owner.owner_id(mock.Mock(application=None)), "439430754368225280")
        with mock.patch.dict(os.environ, {"OWNER_ID": "my name"}), self.assertLogs("musicbot", "WARNING") as cm:
            self.assertIsNone(owner.configured_id())
            self.assertIsNone(owner.configured_id())            # 只警告一次
        self.assertEqual(len(cm.output), 1)
        app = mock.Mock(team=None, owner=mock.Mock(id=777))
        with mock.patch.dict(os.environ, {"OWNER_ID": ""}):
            self.assertEqual(owner.owner_id(mock.Mock(application=app)), "777")
            team = mock.Mock(team=mock.Mock(owner=mock.Mock(id=888)))
            self.assertEqual(owner.owner_id(mock.Mock(application=team)), "888")
            cl = mock.Mock(application=None, application_info=mock.AsyncMock(return_value=app))
            self.assertEqual(asyncio.run(owner.resolve(cl)), "777")

    def test_alerts_uses_owner_module(self):
        from musicbot import alerts
        alerts._owner_id[0] = None
        try:
            with mock.patch.dict(os.environ, {"OWNER_ID": "123456789012345678"}):
                self.assertEqual(asyncio.run(alerts._resolve_owner()), 123456789012345678)
        finally:
            alerts._owner_id[0] = None


class Cleanup(unittest.TestCase):
    def test_dead_code_removed(self):
        self.assertFalse(hasattr(commands, "_field_lines"))
        self.assertFalse(hasattr(discordauth, "delete_user_sessions"))
        src = (ROOT / "musicbot" / "player.py").read_text(encoding="utf-8")
        self.assertNotIn("import random\n", src)
        self.assertNotIn("format_time", src)

    def test_play_next_split_into_steps(self):
        import inspect
        for name in ("_resolve_output_channel", "_stop_if_sleep_due", "_cancel_progress", "_refill_or_finish",
                     "_ensure_voice_client", "_take_next_song", "_open_stream", "_start_playback",
                     "_post_now_playing_panel", "_on_play_error"):
            self.assertTrue(callable(getattr(player, name)), name)
        self.assertLess(len(inspect.getsource(player._play_next_locked).splitlines()), 60)


if __name__ == "__main__":
    unittest.main()
