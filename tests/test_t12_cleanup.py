"""T-12 第 4 項：程式碼整理後行為不變。"""
import ast
import inspect
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _imported_names(rel):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    names.add(a.asname or a.name)
    return names


class LeftoverImports(unittest.TestCase):
    def test_split_web_modules_dropped_unused_imports(self):
        leftover = {
            "musicbot/extapi.py": {"get_active_guild"},
            "musicbot/userlistapi.py": {"song_host_allowed"},
            "musicbot/webapi_player.py": {
                "random_loop_guilds", "guild_names", "_set_active_guild", "get_active_guild",
            },
            "musicbot/webapi_queue.py": {
                "voice_clients", "guild_names", "song_host_allowed", "get_active_guild",
            },
        }
        for rel, names in leftover.items():
            imported = _imported_names(rel)
            self.assertFalse(names & imported, f"{rel} 仍引入未使用的 {names & imported}")


class PausePlayback(unittest.TestCase):
    def tearDown(self):
        from musicbot import state
        gid = getattr(self, "_gid", None)
        if gid is None:
            return
        for d in (state.song_start_times, state.paused_position,
                  state.is_playing_state, state.is_paused_state):
            d.pop(gid, None)

    def test_records_elapsed_and_flags(self):
        from musicbot import player, state
        gid = self._gid = 424201
        vc = mock.Mock()
        state.song_start_times[gid] = time.time() - 12.5
        state.is_playing_state[gid] = True
        state.is_paused_state[gid] = False
        player.pause_playback(gid, vc)
        vc.pause.assert_called_once_with()
        self.assertAlmostEqual(state.paused_position[gid], 12.5, delta=0.25)
        self.assertNotIn(gid, state.song_start_times)
        self.assertFalse(state.is_playing_state[gid])
        self.assertTrue(state.is_paused_state[gid])

    def test_views_and_web_use_the_same_helper(self):
        from musicbot import views, webcore
        self.assertIn("pause_playback", inspect.getsource(views.MusicControlView.toggle_pause))
        self.assertIn("pause_playback", inspect.getsource(webcore.web_toggle_play))
        self.assertNotIn("vc.pause()", inspect.getsource(views.MusicControlView.toggle_pause))


class QueueHelpers(unittest.TestCase):
    def test_add_and_batch_share_body_gid(self):
        from musicbot import webapi_queue
        self.assertIn("_body_gid", inspect.getsource(webapi_queue.api_queue_add))
        self.assertIn("_body_gid", inspect.getsource(webapi_queue.api_queue_add_batch))

    def test_body_gid_fallback_and_explicit(self):
        from musicbot import webapi_queue
        with mock.patch.object(webapi_queue.guildscope, "default_guild", return_value=99):
            self.assertEqual(webapi_queue._body_gid({}), 99)
            self.assertEqual(webapi_queue._body_gid({"guild_id": 0}), 99)
            self.assertEqual(webapi_queue._body_gid({"guild_id": 7}), 7)
        with mock.patch.object(webapi_queue.guildscope, "default_guild", return_value=0):
            self.assertEqual(webapi_queue._body_gid({}), 0)

    def test_reorder_uses_shared_prefetch_helper(self):
        from musicbot import webapi_queue
        src = inspect.getsource(webapi_queue.api_queue_reorder)
        self.assertIn("_ensure_prefetch", src)
        self.assertNotIn("prefetch.ensure", src)


class AutoplayPick(unittest.TestCase):
    def test_peek_and_take_share_ready_helper(self):
        from musicbot import prefetch
        self.assertIn("_ready_autoplay_pick", inspect.getsource(prefetch.peek_autoplay_pick))
        self.assertIn("_ready_autoplay_pick", inspect.getsource(prefetch.take_autoplay_pick))


class BilibiliHelpers(unittest.TestCase):
    def test_parse_and_parse_resource_share_page_url(self):
        from musicbot import bilibili
        self.assertIn("_page_url", inspect.getsource(bilibili.parse))
        self.assertIn("_page_url", inspect.getsource(bilibili.parse_resource))

    def test_page_url_accepts_video_rejects_short_and_foreign(self):
        from musicbot import bilibili
        u = bilibili._page_url("https://www.bilibili.com/video/BV1xx411c7mD?p=2")
        self.assertIsNotNone(u)
        self.assertEqual(u.path, "/video/BV1xx411c7mD")
        self.assertIsNone(bilibili._page_url("https://b23.tv/xxxx"))
        self.assertIsNone(bilibili._page_url("https://www.youtube.com/watch?v=AAAAAAAAAAA"))
        self.assertIsNone(bilibili._page_url(None))
        self.assertEqual(
            bilibili.parse("www.bilibili.com/video/BV1xx411c7mD?p=3")["p"], 3)

    def test_host_matches(self):
        from musicbot import bilibili
        self.assertTrue(bilibili._host_matches("www.bilibili.com", bilibili._HOSTS))
        self.assertTrue(bilibili._host_matches("b23.tv", bilibili._SHORT_HOSTS))
        self.assertFalse(bilibili._host_matches("youtube.com", bilibili._HOSTS))
        self.assertTrue(bilibili.is_host("https://www.bilibili.com/video/BV1xx411c7mD"))
        self.assertFalse(bilibili.is_host("https://www.youtube.com/watch?v=AAAAAAAAAAA"))


if __name__ == "__main__":
    unittest.main()
