"""T-13：Chrome 擴充功能 1.3.0（B 站可加歌、popup 播放控制、清單匯入、快捷鍵）。"""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import webapi, extapi, state, importer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "chrome-extension"
G = 5151
TOK = "t" * 40
H = {"X-Ext-Token": TOK, "Host": "musbot.kdns.fr"}
BV = "https://www.bilibili.com/video/BV1rTy2zFE7f"
YT = "https://www.youtube.com/watch?v=BBBBBBBBBBB"


def fake_run_async(coro):
    import asyncio
    return asyncio.run(coro)


def src(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


class NormalizeMedia(unittest.TestCase):
    def test_youtube_unchanged(self):
        n = extapi.normalize_media_url
        self.assertEqual(n("https://youtu.be/BBBBBBBBBBB?t=3"), YT)
        self.assertEqual(n("https://www.youtube.com/shorts/BBBBBBBBBBB"), YT)
        self.assertIsNone(n("https://www.youtube.com/playlist?list=PLxxxx"))
        self.assertIsNone(n("https://example.com/x"))

    def test_bilibili_video(self):
        n = extapi.normalize_media_url
        self.assertEqual(n(BV + "?spm_id_from=x"), BV)
        self.assertEqual(n(BV + "?p=3"), BV + "?p=3")
        self.assertIsNone(n("https://www.bilibili.com/"))

    def test_bili_short_uses_resolve_short(self):
        with mock.patch("musicbot.bilibili.resolve_short", return_value=BV) as rs:
            self.assertEqual(extapi.normalize_media_url("https://b23.tv/abcdef"), BV)
            rs.assert_called()
        with mock.patch("musicbot.bilibili.resolve_short", return_value=None):
            self.assertIsNone(extapi.normalize_media_url("https://b23.tv/abcdef"))


class ExtApi13(unittest.TestCase):
    def setUp(self):
        self.c = webapi.flask_app.test_client()
        self.env = mock.patch.dict(os.environ, {"EXT_TOKEN": TOK})
        self.env.start()
        state.queues[G] = [("https://www.youtube.com/watch?v=AAAAAAAAAAA", "A")]
        self.p = [mock.patch.object(extapi, "_pick_guild", return_value=G),
                  mock.patch.object(extapi, "run_async", fake_run_async),
                  mock.patch.object(extapi, "_lookup_title", return_value="Looked Up")]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.env.stop()
        for d in (state.queues, state.song_start_times, state.song_durations,
                  state.paused_position, state.now_playing, state.now_playing_url,
                  state.now_playing_thumbnail, state.volumes, state.voice_clients,
                  state.current_voice_channel, state.guild_names):
            d.pop(G, None)

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

    def test_add_bilibili(self):
        r = self.add(url=BV + "?spm=1", title="洛天依")
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(j["status"], "added")
        self.assertEqual(state.queues[G][-1], (BV, "洛天依"))

    def test_add_still_rejects_foreign(self):
        r = self.add(url="https://example.com/x")
        self.assertEqual(r.status_code, 400)
        self.assertIn("B 站", r.get_json()["message"])

    def test_status_includes_progress_and_volume(self):
        vc = mock.Mock()
        vc.is_connected.return_value = True
        vc.is_playing.return_value = True
        vc.is_paused.return_value = False
        state.voice_clients[G] = vc
        state.now_playing[G] = "正在播"
        state.now_playing_url[G] = YT
        state.now_playing_thumbnail[G] = "https://i.ytimg.com/vi/BBBBBBBBBBB/hqdefault.jpg"
        state.song_start_times[G] = time.time() - 15
        state.song_durations[G] = 180
        state.volumes[G] = 0.8
        state.guild_names[G] = "測試服"
        j = self.c.get("/api/ext/status", headers=H).get_json()
        self.assertEqual(j["status"], "ok")
        self.assertEqual(j["current_title"], "正在播")
        self.assertAlmostEqual(j["elapsed"], 15, delta=0.5)
        self.assertEqual(j["elapsed_str"], "0:15")
        self.assertEqual(j["duration"], 180)
        self.assertEqual(j["duration_str"], "3:00")
        self.assertEqual(j["volume"], 80)
        self.assertTrue(j["thumbnail"].endswith("hqdefault.jpg"))
        self.assertTrue(j["is_playing"])
        self.assertIn("queue_length", j)

    def test_control_requires_token(self):
        r = self.c.post("/api/ext/control", json={"action": "skip"}, headers={"Host": "musbot.kdns.fr"})
        self.assertEqual(r.status_code, 401)

    def test_control_toggle_skip_volume(self):
        with mock.patch.object(extapi, "web_toggle_play", mock.AsyncMock(return_value={"status": "paused", "title": "A"})):
            j = self.c.post("/api/ext/control", json={"action": "toggle"}, headers=H).get_json()
        self.assertEqual(j["status"], "paused")
        with mock.patch.object(extapi, "web_skip", mock.AsyncMock(return_value={"status": "skipped", "accepted": True})):
            j = self.c.post("/api/ext/control", json={"action": "skip"}, headers=H).get_json()
        self.assertEqual(j["status"], "skipped")
        j = self.c.post("/api/ext/control", json={"action": "volume", "level": 50}, headers=H).get_json()
        self.assertEqual(j["status"], "ok")
        self.assertEqual(j["volume"], 50)
        self.assertEqual(state.volumes[G], 0.5)
        j = self.c.post("/api/ext/control", json={"action": "dance"}, headers=H).get_json()
        self.assertEqual(j["status"], "error")

    def test_import_rejects_single_video_and_foreign(self):
        r = self.c.post("/api/ext/import", json={"url": YT}, headers=H)
        self.assertEqual(r.status_code, 400)
        self.assertIn("單一影片", r.get_json()["message"])
        r = self.c.post("/api/ext/import", json={"url": "https://example.com/list"}, headers=H)
        self.assertEqual(r.status_code, 400)

    def test_import_starts_job_and_hides_other_users(self):
        with mock.patch.object(importer, "start_job", return_value="abc123def456") as st:
            r = self.c.post("/api/ext/import", json={"url": "https://www.youtube.com/playlist?list=PLxxxxYYYY"}, headers=H)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.get_json()["job"], "abc123def456")
        self.assertEqual(st.call_args.kwargs.get("meta", {}).get("owner_key") or st.call_args[1].get("meta", {}).get("owner_key"), "owner")
        with mock.patch.object(importer, "get_job", return_value={"id": "abc123def456", "status": "running", "owner_key": "someone-else"}):
            r = self.c.get("/api/ext/import/abc123def456", headers=H)
        self.assertEqual(r.status_code, 404)
        with mock.patch.object(importer, "get_job", return_value={"id": "abc123def456", "status": "done", "added": 3, "owner_key": "owner"}):
            r = self.c.get("/api/ext/import/abc123def456", headers=H)
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["job"]["added"], 3)
        self.assertNotIn("owner_key", j["job"])


class ImportableAndBili(unittest.TestCase):
    def test_is_importable_list(self):
        self.assertTrue(extapi._is_importable_list("https://www.youtube.com/playlist?list=PL1"))
        self.assertTrue(extapi._is_importable_list("https://open.spotify.com/playlist/abc"))
        self.assertFalse(extapi._is_importable_list(YT))
        self.assertFalse(extapi._is_importable_list(BV))
        mid, sid = 123456, 99
        u = f"https://space.bilibili.com/{mid}/channel/collectiondetail?sid={sid}"
        self.assertTrue(extapi._is_importable_list(u))

    def test_importer_bili_collection(self):
        songs = [{"url": BV, "title": "一", "duration": 10}] * 2
        fake = {"kind": "season", "title": "合集名", "songs": songs}
        with mock.patch("musicbot.bilibili.parse_resource", return_value={"kind": "season", "mid": 1, "season_id": 2}), \
             mock.patch("musicbot.bilibili.resolve_list", return_value=fake):
            name, out, skipped = importer.resolve("https://space.bilibili.com/1/channel/collectiondetail?sid=2")
        self.assertEqual(name, "合集名")
        self.assertEqual(len(out), 2)
        self.assertEqual(skipped, 0)
        self.assertEqual(out[0]["url"], BV)


class ManifestAndScripts(unittest.TestCase):
    def test_version_permissions_and_commands(self):
        man = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(man["version"], "1.3.0")
        self.assertEqual(set(man["permissions"]), {"storage", "contextMenus", "activeTab", "scripting"})
        hosts = set(man["host_permissions"])
        self.assertEqual(hosts, {
            "https://musbot.kdns.fr/*",
            "https://www.bilibili.com/*",
            "https://m.bilibili.com/*",
            "https://b23.tv/*",
        })
        self.assertIn("add-current", man["commands"])
        self.assertIn("skip-song", man["commands"])
        self.assertIn("Alt+P", man["commands"]["add-current"]["suggested_key"]["default"])
        self.assertIn("space.bilibili.com/*", " ".join(sum((cs.get("matches") or [] for cs in man["content_scripts"]), [])))

    def test_scripts_wire_new_features(self):
        api = src("chrome-extension", "api.js")
        self.assertIn("function videoFrom(", api)
        self.assertIn("function listFrom(", api)
        bg = src("chrome-extension", "background.js")
        self.assertIn("commands.onCommand", bg)
        self.assertIn("add-current", bg)
        self.assertIn("skip-song", bg)
        self.assertIn("加入整個清單", bg)
        self.assertIn("'/api/ext/control'", bg)
        self.assertIn("'/api/ext/import'", bg)
        pop = src("chrome-extension", "popup.js")
        self.assertIn("control('toggle')", pop)
        self.assertIn("control('skip')", pop)
        self.assertIn("control('volume'", pop)
        self.assertIn("importStatus", pop)
        html = src("chrome-extension", "popup.html")
        self.assertIn('id="toggle"', html)
        self.assertIn('id="vol"', html)
        self.assertIn("Alt+P", html)
        content = src("chrome-extension", "content.js")
        self.assertIn("function currentList(", content)
        self.assertIn("加入整個清單", content)
        self.assertIn("已加入", content)
        self.assertIn("加入中…", content)


if __name__ == "__main__":
    unittest.main()
