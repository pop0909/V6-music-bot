"""T-11 第 2 項：Discord /play 可選「來源」參數，關鍵字能搜 B 站。
執行：python -m unittest discover -s tests -p test_t11_play_source.py
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from musicbot.bot import tree  # noqa: E402
from musicbot import commands, player_voice, searchcache, utils  # noqa: E402


def _bili_row(i=1):
    return {
        "url": f"https://www.bilibili.com/video/BV1xx411c7m{i}",
        "title": f"B 站歌 {i}",
        "duration": 120 + i,
        "author": "UP主",
    }


class PlayCommandSource(unittest.TestCase):
    def setUp(self):
        self.cmd = tree.get_command("play")

    def test_optional_source_is_named_來源(self):
        params = {p.name: p for p in self.cmd.parameters}
        self.assertIn("query", params)
        self.assertIn("source", params)
        src = params["source"]
        self.assertEqual(src.display_name, "來源")
        self.assertFalse(src.required)
        self.assertIsNone(src.default)
        self.assertEqual([(c.name, c.value) for c in src.choices],
                         [("YouTube", "youtube"), ("Bilibili", "bilibili")])

    def test_payload_uses_來源_and_is_optional(self):
        opts = {o["name"]: o for o in self.cmd.to_dict(tree)["options"]}
        self.assertIn("query", opts)
        self.assertIn("來源", opts)
        self.assertTrue(opts["query"]["required"])
        self.assertFalse(opts["來源"]["required"])
        self.assertEqual([c["value"] for c in opts["來源"]["choices"]], ["youtube", "bilibili"])

    def test_still_guild_only(self):
        self.assertTrue(self.cmd.guild_only)

    def test_help_mentions_bilibili_source(self):
        desc = dict(commands.HELP_COMMANDS)["play"]
        self.assertIn("B 站", desc)
        self.assertIn("來源", desc)
        self.assertIn("YouTube", desc)


class PlaySourceNormalize(unittest.TestCase):
    def test_default_and_aliases(self):
        self.assertEqual(player_voice._play_source(None), "youtube")
        self.assertEqual(player_voice._play_source(""), "youtube")
        self.assertEqual(player_voice._play_source("youtube"), "youtube")
        self.assertEqual(player_voice._play_source("Bilibili"), "bilibili")
        self.assertEqual(player_voice._play_source("nope"), "youtube")
        choice = mock.Mock(value="bilibili")
        self.assertEqual(player_voice._play_source(choice), "bilibili")


class PlayBiliLookup(unittest.TestCase):
    def setUp(self):
        searchcache.clear()

    def lookup(self, q, source="youtube"):
        return asyncio.run(player_voice._lookup(q, source))

    def test_keyword_uses_bilibili_search_not_ytdlp(self):
        with mock.patch.object(player_voice, "yt_dlp") as ytdlp, \
                mock.patch("musicbot.bilibili.search", return_value=[_bili_row()]) as search:
            info = self.lookup("原神", "bilibili")
        search.assert_called_once_with("原神", 1)
        ytdlp.YoutubeDL.assert_not_called()
        self.assertEqual(info["entries"][0]["title"], "B 站歌 1")
        self.assertTrue(info["entries"][0]["url"].startswith("https://www.bilibili.com/video/"))

    def test_bili_keyword_is_cached(self):
        with mock.patch("musicbot.bilibili.search", return_value=[_bili_row()]) as search:
            a = self.lookup("原神", "bilibili")
            b = self.lookup("原神", "bilibili")
        self.assertEqual(search.call_count, 1)
        self.assertEqual(a["entries"], b["entries"])
        self.assertEqual(searchcache.get("play1bili", "原神"), a["entries"])
        self.assertIsNone(searchcache.get("play1v", "原神"))

    def test_bili_empty_means_not_found(self):
        with mock.patch("musicbot.bilibili.search", return_value=[]):
            self.assertIsNone(self.lookup("沒有這首歌", "bilibili"))
        self.assertEqual(searchcache.size(), 0)

    def test_youtube_keyword_unchanged(self):
        class YDL:
            def __init__(self, o): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def extract_info(self, q, download=False):
                self.q = q
                return {"extractor_key": "YoutubeSearch", "entries": [
                    {"id": "abcdefghijk", "title": "YT 歌", "duration": 10,
                     "url": "https://www.youtube.com/watch?v=abcdefghijk",
                     "ie_key": "Youtube", "_type": "url"},
                ]}
        inst = []
        def factory(opts):
            y = YDL(opts); inst.append(y); return y
        with mock.patch.object(player_voice.yt_dlp, "YoutubeDL", factory), \
                mock.patch("musicbot.bilibili.search") as search:
            info = self.lookup("陳粒", "youtube")
        search.assert_not_called()
        self.assertEqual(inst[0].q, "ytsearch3:陳粒")
        self.assertEqual(info["entries"][0]["title"], "YT 歌")

    def test_url_ignores_source(self):
        url = "https://www.youtube.com/watch?v=abcdefghijk"
        payload = {"extractor_key": "Youtube", "id": "abcdefghijk", "title": "單曲", "url": url}
        class YDL:
            def __init__(self, o): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def extract_info(self, q, download=False):
                self.q = q
                return payload
        inst = []
        with mock.patch.object(player_voice.yt_dlp, "YoutubeDL", lambda o: inst.append(YDL(o)) or inst[-1]), \
                mock.patch("musicbot.bilibili.search") as search, \
                mock.patch("musicbot.bilibili.is_page", return_value=False):
            info = self.lookup(url, "bilibili")
        search.assert_not_called()
        self.assertEqual(inst[0].q, url)
        self.assertEqual(info["title"], "單曲")

    def test_bili_url_still_uses_page_lookup(self):
        url = "https://www.bilibili.com/video/BV1xx411c7mD"
        shaped = {"id": url, "webpage_url": url, "title": "BV 影片", "duration": 30}
        with mock.patch("musicbot.bilibili.is_page", return_value=True), \
                mock.patch("musicbot.bilibili.lookup_ytdl_shape", return_value=shaped) as lk, \
                mock.patch("musicbot.bilibili.search") as search:
            info = self.lookup(url, "youtube")
        lk.assert_called_once_with(url)
        search.assert_not_called()
        self.assertEqual(info["title"], "BV 影片")


class PlayProcessSource(unittest.TestCase):
    def test_process_passes_source_to_lookup(self):
        it = mock.MagicMock()
        it.response.is_done.return_value = True
        it.followup.send = mock.AsyncMock()
        it.user.display_name = "u"
        seen = {}

        async def fake_lookup(q, source="youtube"):
            seen["q"], seen["source"] = q, source
            return {"entries": [{"url": "https://www.bilibili.com/video/BV1xx411c7mD", "title": "歌"}]}

        with mock.patch.object(player_voice, "_lookup", fake_lookup), \
                mock.patch.object(player_voice._p, "play_next", mock.AsyncMock()), \
                mock.patch("musicbot.actionlog.record_add"), \
                mock.patch("musicbot.discord_ux.announce", mock.AsyncMock()), \
                mock.patch.dict(player_voice.queues, {}, clear=False):
            from musicbot.state import queues
            queues[7] = []
            vc = mock.Mock()
            vc.is_playing.return_value = False
            vc.is_paused.return_value = False
            asyncio.run(player_voice._process_and_play(it, "原神", 7, vc, source="bilibili"))
        self.assertEqual(seen, {"q": "原神", "source": "bilibili"})
        self.assertTrue(it.followup.send.await_count >= 1)
        self.assertIn("歌", it.followup.send.await_args.args[0])

    def test_keyword_too_long_is_rejected(self):
        it = mock.MagicMock()
        it.response.is_done.return_value = True
        it.followup.send = mock.AsyncMock()
        with mock.patch.object(player_voice, "_lookup", mock.AsyncMock()) as lk:
            asyncio.run(player_voice._process_and_play(
                it, "字" * (utils.SEARCH_QUERY_MAX + 1), 7, mock.Mock()))
        lk.assert_not_called()
        msg = it.followup.send.await_args.args[0]
        self.assertIn(utils.SEARCH_QUERY_TOO_LONG, msg)


class SyncUnchanged(unittest.TestCase):
    def test_still_prefers_guild_sync(self):
        with open(os.path.join(ROOT, "musicbot", "bot.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("SYNC_GUILD_ID", src)
        self.assertIn("await tree.sync(guild=guild)", src)
        self.assertIn("已同步 {len(synced)} 個指令到測試伺服器", src)


if __name__ == "__main__":
    unittest.main()
