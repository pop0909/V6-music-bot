"""操作紀錄（點歌 / 跳過 / 刪歌）：儲存、7 天清除、上限、每條路徑的「誰 / 從哪裡」、/api/actions 權限。"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import actionlog, state, webcore, webapi, extapi, extmembers, userlists, userlistapi, webauth, player, views, commands  # noqa: E402

G = 8181
G2 = 8282
OWN = "o" * 40
HOST = "musbot.kdns.fr"
U = "https://www.youtube.com/watch?v=%011d"


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.file = actionlog._reset_for_tests(os.path.join(self.tmp, "action_log.json"))

    def tearDown(self):
        actionlog._reset_for_tests()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_and_recent_newest_first(self):
        actionlog.record(G, "add", ("小明", "ext"), "歌 A", url=U % 1)
        actionlog.record(G, "skip", ("阿華", "discord"), "歌 A")
        actionlog.record(G, "remove", ("擁有者", "web"), "歌 B", url=U % 2)
        items = actionlog.recent(G)
        self.assertEqual([i["act"] for i in items], ["remove", "skip", "add"])
        self.assertEqual(items[0]["who"], "擁有者")
        self.assertEqual(items[1]["src"], "discord")
        self.assertEqual(items[2]["url"], U % 1)
        self.assertRegex(items[0]["time"], r"^\d\d/\d\d \d\d:\d\d:\d\d$")

    def test_time_is_taipei(self):
        ts = 1794585600.0      # 2026-11-13 16:00:00 UTC = 11/14 00:00:00 台北
        self.assertEqual(actionlog.fmt_time(ts), "11/14 00:00:00")

    def test_per_guild(self):
        actionlog.record(G, "add", ("a", "web"), "x")
        actionlog.record(G2, "add", ("b", "web"), "y")
        self.assertEqual([i["title"] for i in actionlog.recent(G)], ["x"])
        self.assertEqual([i["title"] for i in actionlog.recent(G2)], ["y"])

    def test_no_actor_or_bad_action_not_logged(self):
        self.assertIsNone(actionlog.record(G, "add", None, "x"))
        self.assertIsNone(actionlog.record(G, "stop", ("a", "web"), "x"))
        self.assertIsNone(actionlog.record(0, "add", ("a", "web"), "x"))
        self.assertEqual(actionlog.recent(G), [])
        self.assertFalse(os.path.exists(self.file))

    def test_unknown_source_falls_back_to_web_and_clips(self):
        e = actionlog.record(G, "add", ("名" * 100, "evil"), "t\n" * 500)
        self.assertEqual(e["src"], "web")
        self.assertEqual(len(e["who"]), actionlog.WHO_MAX)
        self.assertLessEqual(len(e["title"]), actionlog.TITLE_MAX)
        self.assertNotIn("\n", e["title"])

    def test_atomic_file_written_and_reloaded(self):
        actionlog.record(G, "add", ("a", "web"), "x")
        self.assertTrue(os.path.exists(self.file))
        self.assertFalse(os.path.exists(self.file + ".tmp"))
        with open(self.file, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["guilds"][str(G)][0]["title"], "x")
        if os.name == "posix":
            self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        actionlog._reset_for_tests(self.file)       # 模擬重啟
        self.assertEqual(actionlog.recent(G)[0]["title"], "x")

    def test_old_entries_pruned_on_load_and_write(self):
        old = time.time() - 8 * 86400
        fresh = time.time() - 6 * 86400
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "guilds": {str(G): [
                {"ts": old, "act": "add", "who": "a", "src": "web", "title": "old"},
                {"ts": fresh, "act": "add", "who": "a", "src": "web", "title": "fresh"}],
                str(G2): [{"ts": old, "act": "skip", "who": "a", "src": "web", "title": "old2"}]}}, f)
        actionlog.prune()                          # 啟動時
        self.assertEqual([i["title"] for i in actionlog.recent(G)], ["fresh"])
        self.assertEqual(actionlog.recent(G2), [])
        with open(self.file, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertNotIn(str(G2), raw["guilds"])   # 檔案也已經清掉
        # 寫入時也會清：把 fresh 變成過期，再記一筆
        with mock.patch.object(actionlog.time, "time", return_value=time.time() + 2 * 86400):
            actionlog.record(G, "add", ("b", "web"), "new")
        self.assertEqual([i["title"] for i in actionlog.recent(G)], ["new"])

    def test_bounded_per_guild(self):
        with mock.patch.object(actionlog, "MAX_PER_GUILD", 5):
            for i in range(12):
                actionlog.record(G, "add", ("a", "web"), f"s{i}")
            items = actionlog.recent(G, 100)
        self.assertEqual([i["title"] for i in items], ["s11", "s10", "s9", "s8", "s7"])

    def test_corrupt_file_starts_empty(self):
        with open(self.file, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(actionlog.recent(G), [])
        actionlog.record(G, "add", ("a", "web"), "x")
        self.assertEqual(len(actionlog.recent(G)), 1)

    def test_write_failure_does_not_raise(self):
        actionlog._reset_for_tests(os.path.join(self.tmp, "nope", "\0bad", "x.json"))
        self.assertIsNotNone(actionlog.record(G, "add", ("a", "web"), "x"))   # 記憶體有、寫檔失敗只記 log

    def test_record_add_bulk_is_one_entry(self):
        actionlog.record_add(G, ("a", "web"), [(U % i, f"s{i}") for i in range(37)], label="匯入：清單")
        actionlog.record_add(G, ("a", "web"), [(U % 99, "one")])
        actionlog.record_add(G, ("a", "web"), [])
        items = actionlog.recent(G)
        self.assertEqual(len(items), 2)
        self.assertEqual((items[1]["n"], items[1]["detail"], items[1]["title"]), (37, "匯入：清單", "s0"))
        self.assertNotIn("n", items[0])
        self.assertEqual(items[0]["url"], U % 99)

    def test_recent_limit(self):
        for i in range(30):
            actionlog.record(G, "add", ("a", "web"), f"s{i}")
        self.assertEqual(len(actionlog.recent(G, 10)), 10)
        self.assertEqual(actionlog.recent(G, "abc")[0]["title"], "s29")


class Paths(unittest.TestCase):
    """每條路徑記下正確的人和來源"""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        actionlog._reset_for_tests(os.path.join(self.tmp, "action_log.json"))
        self.p = [mock.patch.object(extmembers, "FILE", os.path.join(self.tmp, "m.json")),
                  mock.patch.object(userlists, "FILE", os.path.join(self.tmp, "ul.json")),
                  mock.patch.object(webauth, "SECRET_FILE", os.path.join(self.tmp, "panel_secret")),
                  mock.patch.dict(os.environ, {"EXT_TOKEN": OWN, "PANEL_SECRET": "", "QUEUE_MAX": "500"}),
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
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, key, c=None):
        c = c or self.c
        r = c.post("/login", data={"key": key}, headers={"Host": HOST, "CF-Connecting-IP": "9.9.9.9"})
        self.assertIn(r.status_code, (302, 303))
        return c

    def post(self, path, json_, c=None):
        return (c or self.c).post(path, json=json_, headers={"Host": HOST})

    def last(self):
        items = actionlog.recent(G)
        return items[0] if items else None

    # ---- 網頁面板 ----
    def test_panel_add_owner_and_member(self):
        self.login(OWN)
        r = self.post("/api/queue/add", {"guild_id": G, "url": U % 1, "title": "歌一", "no_play": True})
        self.assertEqual(r.get_json()["status"], "added")
        e = self.last()
        self.assertEqual((e["act"], e["who"], e["src"], e["title"]), ("add", "擁有者", "web", "歌一"))
        _, tok = extmembers.create("小明")
        c2 = self.login(tok, webapi.flask_app.test_client())
        self.post("/api/queue/add", {"guild_id": G, "url": U % 2, "title": "歌二", "no_play": True}, c=c2)
        self.assertEqual((self.last()["who"], self.last()["src"]), ("小明", "web"))

    def test_panel_duplicate_or_full_not_logged(self):
        self.login(OWN)
        self.post("/api/queue/add", {"guild_id": G, "url": U % 1, "title": "歌一", "no_play": True})
        self.post("/api/queue/add", {"guild_id": G, "url": U % 1, "title": "歌一", "no_play": True})
        self.assertEqual(len(actionlog.recent(G)), 1)

    def test_panel_batch_is_one_entry(self):
        self.login(OWN)
        songs = [{"url": U % i, "title": f"s{i}"} for i in range(25)]
        j = self.post("/api/queue/add-batch", {"guild_id": G, "songs": songs}).get_json()
        self.assertEqual(j["count"], 25)
        items = actionlog.recent(G)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["n"], items[0]["who"]), (25, "擁有者"))

    def test_panel_play_all_local_playlist(self):
        self.login(OWN)
        with mock.patch.object(webapi, "local_playlist", [{"url": U % i, "title": f"s{i}"} for i in range(4)]):
            self.post("/api/playlist/play-all", {"guild_id": G})
        e = self.last()
        self.assertEqual((e["act"], e["n"], e["detail"]), ("add", 4, "本地歌單"))

    def test_panel_playlist_play_single(self):
        self.login(OWN)
        with mock.patch.object(webapi, "local_playlist", [{"url": U % 7, "title": "七"}]):
            self.post("/api/playlist/play", {"guild_id": G, "index": 0})
        self.assertEqual((self.last()["title"], self.last()["src"]), ("七", "web"))

    def test_panel_remove(self):
        state.queues[G] = [(U % 1, "甲"), (U % 2, "乙")]
        _, tok = extmembers.create("阿華")
        self.login(tok)
        self.post("/api/queue/remove", {"guild_id": G, "index": 1})
        e = self.last()
        self.assertEqual((e["act"], e["who"], e["src"], e["title"]), ("remove", "阿華", "web", "乙"))
        self.post("/api/queue/remove", {"guild_id": G, "index": 9})       # 無效索引不記
        self.assertEqual(len(actionlog.recent(G)), 1)

    def _vc(self, playing=True):
        vc = mock.MagicMock()
        vc.is_playing.return_value = playing
        vc.is_paused.return_value = False
        return vc

    def test_panel_skip(self):
        self.login(OWN)
        vc = self._vc()
        state.now_playing[G] = "正在播"
        state.now_playing_url[G] = U % 5
        with mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=vc)), \
             mock.patch.object(webcore, "_spawn", lambda coro: coro.close()):
            self.post("/api/skip", {"guild_id": G})
        e = self.last()
        self.assertEqual((e["act"], e["who"], e["src"], e["title"]), ("skip", "擁有者", "web", "正在播"))

    def test_panel_skip_when_idle_not_logged(self):
        self.login(OWN)
        with mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=self._vc(False))), \
             mock.patch.object(webcore, "_spawn", lambda coro: coro.close()):
            self.post("/api/skip", {"guild_id": G})
        self.assertEqual(actionlog.recent(G), [])

    def test_panel_play_index_logs_skip_of_current(self):
        self.login(OWN)
        state.queues[G] = [(U % 1, "甲"), (U % 2, "乙")]
        state.now_playing[G] = "正在播"
        with mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=self._vc())), \
             mock.patch.object(webcore, "_spawn", lambda coro: coro.close()):
            self.post("/api/queue/play", {"guild_id": G, "index": 1})
        e = self.last()
        self.assertEqual((e["act"], e["title"], e["detail"]), ("skip", "正在播", "改播：乙"))

    def test_clear_and_stop_not_logged(self):
        """使用者只選了點歌 / 跳過 / 刪歌，清空佇列不算"""
        self.login(OWN)
        state.queues[G] = [(U % 1, "甲")]
        self.post("/api/queue/clear", {"guild_id": G})
        self.assertEqual(actionlog.recent(G), [])

    def test_panel_import_job_one_entry(self):
        self.login(OWN)

        def fake_start(url, on_done, **kw):
            on_done("我的清單", [{"url": U % i, "title": f"s{i}"} for i in range(12)])
            return "job1"
        with mock.patch.object(webapi.importer, "start_job", fake_start):
            self.post("/api/import", {"guild_id": G, "url": "https://www.youtube.com/playlist?list=PLx"})
        e = self.last()
        self.assertEqual((e["n"], e["detail"], e["who"], e["src"]), (12, "匯入：我的清單", "擁有者", "web"))

    # ---- Chrome 擴充功能 ----
    def test_extension_add_member(self):
        _, tok = extmembers.create("小美")
        r = self.c.post("/api/ext/add", json={"url": "https://youtu.be/AAAAAAAAAAA", "title": "擴充"},
                        headers={"Host": HOST, "X-Ext-Token": tok})
        self.assertEqual(r.get_json()["status"], "added")
        e = self.last()
        self.assertEqual((e["act"], e["who"], e["src"], e["title"]), ("add", "小美", "ext", "擴充"))

    def test_extension_add_owner(self):
        self.c.post("/api/ext/add", json={"url": "https://youtu.be/AAAAAAAAAAA"}, headers={"Host": HOST, "X-Ext-Token": OWN})
        self.assertEqual((self.last()["who"], self.last()["src"], self.last()["title"]), ("擁有者", "ext", "查到的"))

    def test_ext_endpoint_via_panel_cookie_is_web(self):
        self.login(OWN)
        self.c.post("/api/ext/add", json={"url": "https://youtu.be/AAAAAAAAAAA", "title": "x"}, headers={"Host": HOST})
        self.assertEqual(self.last()["src"], "web")

    def test_extension_personal_list_play_one_entry(self):
        _, tok = extmembers.create("小美")
        k = extmembers.list_members()[0]["id"]
        pl = userlists.create(k, "通勤", [{"url": U % i, "title": f"s{i}"} for i in range(6)])
        r = self.c.post(f"/api/ext/lists/{pl['id']}", json={"action": "play"}, headers={"Host": HOST, "X-Ext-Token": tok})
        self.assertEqual(r.get_json()["count"], 6)
        e = self.last()
        self.assertEqual((e["n"], e["who"], e["src"], e["detail"]), (6, "小美", "ext", "歌單：通勤"))

    # ---- /api/actions ----
    def test_api_actions_visible_to_any_key_but_not_anonymous(self):
        actionlog.record(G, "add", ("a", "web"), "x")
        r = self.c.get(f"/api/actions?guild_id={G}", headers={"Host": HOST})
        self.assertEqual(r.status_code, 401)
        _, tok = extmembers.create("朋友")
        self.login(tok)
        j = self.c.get(f"/api/actions?guild_id={G}", headers={"Host": HOST}).get_json()
        self.assertEqual((j["status"], j["keep_days"], j["items"][0]["title"]), ("ok", 7, "x"))
        self.assertIn("time", j["items"][0])


def interaction(name="阿明"):
    it = mock.MagicMock()
    it.guild_id = G
    it.guild.id = G
    it.user.display_name = name
    it.response.send_message = mock.AsyncMock()
    it.response.defer = mock.AsyncMock()
    it.response.is_done = lambda: True
    it.followup.send = mock.AsyncMock()
    return it


class DiscordPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        actionlog._reset_for_tests(os.path.join(self.tmp, "action_log.json"))
        state.queues[G] = []
        state.now_playing[G] = "正在播"
        state.now_playing_url[G] = U % 5

    def tearDown(self):
        for d in (state.queues, state.now_playing, state.now_playing_url, state.song_meta, state.random_loop_guilds):
            d.pop(G, None)
        actionlog._reset_for_tests()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def vc(self, playing=True):
        vc = mock.MagicMock()
        vc.is_playing.return_value = playing
        vc.is_paused.return_value = False
        return vc

    def last(self):
        return actionlog.recent(G)[0]

    def test_slash_skip(self):
        it = interaction()
        it.guild.voice_client = self.vc()
        with mock.patch.object(commands.audio, "fade_out_stop", mock.AsyncMock()), \
             mock.patch.object(commands, "_get_output_channel", mock.AsyncMock(return_value=None)):
            asyncio.run(commands.skip.callback(it))
        e = self.last()
        self.assertEqual((e["act"], e["who"], e["src"], e["title"]), ("skip", "阿明", "discord", "正在播"))

    def test_button_skip(self):
        it = interaction("小花")
        it.guild.voice_client = self.vc()
        view = views.MusicControlView(G)
        with mock.patch("musicbot.audio.fade_out_stop", mock.AsyncMock()):
            asyncio.run(view.skip_song.callback(it))
        self.assertEqual((self.last()["who"], self.last()["src"]), ("小花", "discord"))

    def test_button_skip_nothing_playing_not_logged(self):
        it = interaction()
        it.guild.voice_client = self.vc(False)
        asyncio.run(views.MusicControlView(G).skip_song.callback(it))
        self.assertEqual(actionlog.recent(G), [])

    def _play(self, info):
        class YDL:
            def __init__(self, o): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def extract_info(self, q, download=False): return info
        it = interaction()
        with mock.patch.object(player.yt_dlp, "YoutubeDL", YDL), \
             mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=None)), \
             mock.patch.object(player, "play_next", mock.AsyncMock()):
            asyncio.run(player._process_and_play(it, "q", G, self.vc()))

    def test_play_single(self):
        self._play({"id": "AAAAAAAAAAA", "title": "單曲", "webpage_url": U % 1})
        e = self.last()
        self.assertEqual((e["act"], e["who"], e["src"], e["title"]), ("add", "阿明", "discord", "單曲"))
        self.assertNotIn("n", e)

    def test_play_youtube_playlist_one_entry(self):
        self._play({"title": "大清單", "entries": [{"id": "PL%09d" % i, "url": "PL%09d" % i, "title": f"s{i}"} for i in range(40)]})
        items = actionlog.recent(G)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["n"], items[0]["detail"]), (40, "清單：大清單"))

    def test_import_command_one_entry(self):
        it = interaction()
        vc = self.vc()
        with mock.patch.object(commands, "get_voice_client", mock.AsyncMock(return_value=vc)), \
             mock.patch("musicbot.importer.resolve", lambda url, progress=None: ("清單名", [{"url": U % i, "title": f"s{i}"} for i in range(10, 19)], 0)), \
             mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=None)), \
             mock.patch.object(webcore._prefetch, "ensure", mock.Mock()), \
             mock.patch.object(commands, "_get_output_channel", mock.AsyncMock(return_value=None)):
            asyncio.run(commands.import_cmd.callback(it, "https://www.youtube.com/playlist?list=PLx"))
        e = self.last()
        self.assertEqual((e["n"], e["detail"], e["src"], e["who"]), (9, "匯入：清單名", "discord", "阿明"))

    def test_favorite_random_button(self):
        it = interaction()
        vc = self.vc()
        vc.is_connected.return_value = True
        it.guild.voice_client = vc
        with mock.patch.object(views, "load_favorites", return_value=[{"url": U % 3, "title": "最愛歌"}]):
            asyncio.run(views.MusicControlView(G).fav_play.callback(it))
        e = self.last()
        self.assertEqual((e["act"], e["title"], e["detail"], e["src"]), ("add", "最愛歌", "隨機最愛", "discord"))

    def test_autoplay_and_internal_adds_not_logged(self):
        with mock.patch.object(webcore._prefetch, "ensure", mock.Mock()), \
             mock.patch.object(webcore, "ensure_voice_connected", mock.AsyncMock(return_value=None)):
            asyncio.run(webcore.web_import_songs(G, [{"url": U % 1, "title": "x"}]))     # actor 沒給（內部呼叫）
        self.assertEqual(actionlog.recent(G), [])


if __name__ == "__main__":
    unittest.main()
