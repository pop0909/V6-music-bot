"""Audit 5：歌單 / 最愛 CRUD 驗證與連點防誤刪、yt-dlp 更新訊息、成員頁手機版、擴充功能 content.js。"""
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from musicbot import webapi, state, storage, sysinfo  # noqa: E402

G = 88001
U = "https://www.youtube.com/watch?v=%011d"


class PlaylistCrud(unittest.TestCase):
    def setUp(self):
        self.saved = list(state.local_playlist)
        state.local_playlist[:] = [{"url": U % i, "title": "s%d" % i} for i in range(3)]
        self.p = [mock.patch.object(webapi, "save_local_playlist", lambda: True),
                  mock.patch.object(webapi, "save_favorites", lambda g: None)]
        for p in self.p: p.start()
        self.c = webapi.flask_app.test_client()

    def tearDown(self):
        for p in self.p: p.stop()
        state.local_playlist[:] = self.saved
        storage.favorites_cache.pop(G, None)

    def test_add_rejects_bad_url_and_caps_title(self):
        self.assertEqual(self.c.post("/api/playlist/add", json={"url": "javascript:alert(1)"}).status_code, 400)
        r = self.c.post("/api/playlist/add", json={"url": U % 9, "title": "x" * 999})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(state.local_playlist[-1]["title"]), 200)
        self.assertEqual(self.c.post("/api/playlist/add", json={"url": 5}).status_code, 400)

    def test_double_delete_does_not_remove_next(self):
        for _ in range(2):
            r = self.c.post("/api/playlist/delete", json={"index": 0, "expect_url": U % 0})
        self.assertEqual(r.status_code, 409)
        self.assertEqual([s["url"] for s in state.local_playlist], [U % 1, U % 2])

    def test_shifted_index_finds_by_url(self):
        self.c.post("/api/playlist/delete", json={"index": 0, "expect_url": U % 2})
        self.assertEqual([s["url"] for s in state.local_playlist], [U % 0, U % 1])

    def test_legacy_index_only_still_works(self):
        self.assertEqual(self.c.post("/api/playlist/delete", json={"index": 9}).status_code, 400)
        self.assertEqual(self.c.post("/api/playlist/delete", json={"index": 0}).status_code, 200)

    def test_edit_stale_and_bad_url(self):
        self.assertEqual(self.c.post("/api/playlist/edit", json={"index": 0, "url": "ftp://x", "title": "t"}).status_code, 400)
        r = self.c.post("/api/playlist/edit", json={"index": 0, "url": U % 7, "title": "t", "expect_url": U % 99})
        self.assertEqual(r.status_code, 409)

    def test_favorites(self):
        storage.favorites_cache[G] = [{"url": U % i, "title": "f"} for i in range(2)]
        self.assertEqual(self.c.post("/api/favorites/add", json={"guild_id": G, "url": "nope"}).status_code, 400)
        for _ in range(2):
            r = self.c.post("/api/favorites/remove", json={"guild_id": G, "index": 0, "expect_url": U % 0})
        self.assertEqual(r.status_code, 409)
        self.assertEqual([f["url"] for f in storage.favorites_cache[G]], [U % 1])


class UpdateMsg(unittest.TestCase):
    def test_non_systemd_says_restart_needed(self):
        runs = [mock.Mock(returncode=0, stdout="", stderr=""), mock.Mock(stdout="2099.1.1\n")]
        with mock.patch.object(sysinfo.subprocess, "run", side_effect=runs), \
             mock.patch.object(sysinfo, "_ytdlp_version", return_value="2025.1.1"), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            sysinfo._do_update()
        self.assertIn("手動重新啟動", sysinfo._update["result"]["message"])


class Frontend(unittest.TestCase):
    def test_members_table_scrolls_on_mobile(self):
        html = open(os.path.join(ROOT, "templates", "members.html"), encoding="utf-8").read()
        self.assertIn('class="tbl-wrap"', html)

    def test_app_js_sends_expect_url(self):
        js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        self.assertIn("API.playlistDelete(i, song.url)", js)
        self.assertIn("API.favRemove(i, null, song.url)", js)

    @unittest.skipUnless(shutil.which("node") and os.path.isfile(os.path.join(ROOT, "chrome-extension", "test_content.js")), "需要 node")
    def test_extension_content_js(self):
        r = subprocess.run(["node", os.path.join(ROOT, "chrome-extension", "test_content.js")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
