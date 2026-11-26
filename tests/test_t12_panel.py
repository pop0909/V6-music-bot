"""T-12 第 2 項：面板載入／SSE 佇列省略／特效降載。"""
import json
import os
import re
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)

from appjs import app_js_files, read_app_js  # noqa: E402
from musicbot import state, webcore, webapi, bot  # noqa: E402

G = 111
TOKEN = "20261126t12b"
U = "https://www.youtube.com/watch?v=T12Q%05d"


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class AssetAndEffects(unittest.TestCase):
    def test_token_and_defer_in_head(self):
        idx = _read("templates/index.html")
        mem = _read("templates/members.html")
        self.assertIn(f"icons.css?v={TOKEN}", idx)
        self.assertIn(f"style.css?v={TOKEN}", idx)
        self.assertIn(f"icons.css?v={TOKEN}", mem)
        self.assertLess(idx.index("01-core.js"), idx.index("</head>"))
        files = app_js_files()
        self.assertEqual(len(files), 11)
        for n in files:
            self.assertRegex(idx, rf'<script src="/static/js/app/{re.escape(n)}\?v={TOKEN}" defer></script>')
        self.assertNotIn("async", idx.split("</head>")[0])

    def test_list_flip_disabled(self):
        js = _read("static/js/app/08-effects.js")
        # 包裝函式直接呼叫原函式，不再對清單做 getBoundingClientRect
        m = re.search(r"function fxWrapList\(fn, getList\) \{.*?\n\}", js, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("return fn.apply(this, args);", body)
        self.assertNotIn("fxSnapshot", body)
        self.assertNotIn("getBoundingClientRect", re.sub(r"//.*", "", body))

    def test_decorative_animations_off_by_default(self):
        css = _read("static/css/style.css")
        self.assertIn("預設關閉持續性裝飾動畫", css)
        self.assertIn(".np-card.is-playing .album-art img { animation: none; will-change: auto; }", css)
        self.assertIn(".list-item { animation: none; }", css)


class SseQueueOmit(unittest.TestCase):
    def setUp(self):
        state.guild_names[G] = "測試"
        state.queues[G] = [(U % i, "歌 %d 標題再長一點" % i) for i in range(1, 81)]
        state.known_durations.clear()
        self._meta = dict(state.song_meta.get(G) or {})
        state.song_meta[G] = {}

    def tearDown(self):
        state.guild_names.pop(G, None)
        state.queues.pop(G, None)
        state.song_meta.pop(G, None)
        if self._meta:
            state.song_meta[G] = self._meta
        state.known_durations.clear()
        state.now_playing.pop(G, None)
        state.is_playing_state.pop(G, None)
        state.song_start_times.pop(G, None)

    def test_status_still_includes_queue(self):
        s = webcore.get_guild_status(G)
        self.assertEqual(len(s["queue"]), 80)
        self.assertIn("q_rev", s)
        self.assertEqual(s["queue_length"], 80)

    def test_full_state_omits_unchanged_queue(self):
        first = webcore.get_full_app_state(G, {})
        self.assertEqual(len(first["queue"]), 80)
        self.assertIn("q_rev", first)
        known = {"pl": first["pl_rev"], "q": first["q_rev"]}
        again = webcore.get_full_app_state(G, known)
        self.assertNotIn("queue", again)
        self.assertEqual(again["queue_length"], 80)
        self.assertEqual(again["q_rev"], first["q_rev"])
        self.assertLess(len(json.dumps(again)), 2000)
        self.assertLess(len(json.dumps(again)) * 8, len(json.dumps(first)))

    def test_queue_change_is_resent(self):
        first = webcore.get_full_app_state(G, {})
        known = {"pl": first["pl_rev"], "q": first["q_rev"]}
        state.queues[G].append((U % 99, "新加的歌"))
        nxt = webcore.get_full_app_state(G, known)
        self.assertEqual(len(nxt["queue"]), 81)
        self.assertNotEqual(nxt["q_rev"], first["q_rev"])

    def test_duration_change_is_resent_but_status_queue_always_complete(self):
        first = webcore.get_full_app_state(G, {})
        known = {"pl": first["pl_rev"], "q": first["q_rev"]}
        url = state.queues[G][0][0]
        state.remember_duration(url, 185)
        nxt = webcore.get_full_app_state(G, known)
        self.assertIn("queue", nxt)
        self.assertEqual(nxt["queue"][0]["duration"], 185)
        self.assertNotEqual(nxt["q_rev"], first["q_rev"])

    def test_without_known_includes_queue(self):
        self.assertIn("queue", webcore.get_full_app_state(G))

    def test_same_duration_does_not_need_new_rev(self):
        url = state.queues[G][0][0]
        state.remember_duration(url, 120)
        first = webcore.get_full_app_state(G, {})
        state.remember_duration(url, 120)   # 相同秒數只是刷新順序，版本不該變
        again = webcore.get_full_app_state(G, {"pl": first["pl_rev"], "q": first["q_rev"]})
        self.assertNotIn("queue", again)
        self.assertEqual(again["q_rev"], first["q_rev"])


class LivePartialAndCache(unittest.TestCase):
    def test_apply_live_state_ignores_missing_fields(self):
        js = _read("static/js/app/04-live.js")
        self.assertIn("if ('duration' in data) state.duration", js)
        self.assertIn("if ('is_playing' in data) state.isPlaying", js)
        self.assertIn("if ('is_paused' in data) state.isPaused", js)
        self.assertIn("'current_url' in data &&", js)
        art = _read("static/js/app/03-artwork.js")
        self.assertIn("('current_title' in data) ? data.current_title : state.currentTitle", art)
        self.assertIn("('duration' in data) ? data.duration : state.duration", art)

    def test_message_cache_is_capped(self):
        self.assertEqual(bot.client._connection.max_messages, 200)

    def test_stream_sends_progress_only_when_just_elapsed_changes(self):
        webapi.flask_app.config["LOGIN_DISABLED"] = True
        gid = G
        state.guild_names[gid] = "測試"
        state.queues[gid] = [(U % i, "歌 %d" % i) for i in range(1, 81)]
        state.is_playing_state[gid] = True
        state.is_paused_state[gid] = False
        state.now_playing[gid] = "歌 1"
        state.now_playing_url[gid] = U % 1
        state.song_start_times[gid] = time.time() - 10
        state.song_durations[gid] = 180
        n = {"i": 0}

        def wait_change(v, t):
            n["i"] += 1
            state.song_start_times[gid] = time.time() - 10 - n["i"]
            return v

        c = webapi.flask_app.test_client()
        try:
            with mock.patch("musicbot.guildscope.allowed_guilds", return_value=None), \
                    mock.patch("musicbot.idlesleep.all_asleep", return_value=False), \
                    mock.patch("musicbot.webapi_player.wait_change", side_effect=wait_change):
                r = c.get(f"/api/progress-stream?guild_id={gid}", buffered=False)
                it = iter(r.response)
                a = json.loads(next(it).decode()[len("data: "):])
                b = json.loads(next(it).decode()[len("data: "):])
                r.close()
        finally:
            state.guild_names.pop(gid, None)
            state.queues.pop(gid, None)
            state.is_playing_state.pop(gid, None)
            state.is_paused_state.pop(gid, None)
            state.now_playing.pop(gid, None)
            state.now_playing_url.pop(gid, None)
            state.song_start_times.pop(gid, None)
            state.song_durations.pop(gid, None)

        self.assertIn("queue", a)
        self.assertEqual(len(a["queue"]), 80)
        self.assertIn("elapsed", a)
        self.assertEqual(set(b.keys()), {"elapsed", "elapsed_str"})
        self.assertNotEqual(b["elapsed"], a["elapsed"])
        self.assertLess(len(json.dumps(b)), 80)
        self.assertLess(len(json.dumps(b)) * 8, len(json.dumps(a)))



if __name__ == "__main__":
    unittest.main()
