"""搜尋結果暫存：記憶體內、LRU 上限、1 小時過期、失敗 / 空結果 / 網址不存；面板搜尋、/play、Spotify 匯入都會用到
執行：python -m unittest discover -s tests
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import searchcache, webapi, webapi_library, player_voice, importer  # noqa: E402


def entry(i):
    return {"id": f"vid{i:07d}x", "title": f"歌 {i}", "duration": 200 + i, "url": f"https://www.youtube.com/watch?v=vid{i:07d}x"}


# 真實 yt-dlp「ytsearch5:tuki.」扁平結果的第一筆：頻道，不是影片
CHANNEL = {"_type": "url", "ie_key": "YoutubeTab", "id": "UCp74ruKv2sUiI2HqEhH8pRw",
           "url": "https://www.youtube.com/channel/UCp74ruKv2sUiI2HqEhH8pRw", "title": "tuki.(17)", "duration": None}
PLAYLIST = {"_type": "url", "ie_key": "YoutubeTab", "id": "PLabcdefghijklmnopqrstuvwxyz012345",
            "url": "https://www.youtube.com/playlist?list=PLabcdefghijklmnopqrstuvwxyz012345", "title": "合輯"}


def video(i):
    return dict(entry(i), _type="url", ie_key="Youtube")


class FakeYDL:
    calls = []
    result = None
    error = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, q, download=False):
        FakeYDL.calls.append(q)
        if FakeYDL.error:
            raise FakeYDL.error
        return FakeYDL.result


class Base(unittest.TestCase):
    def setUp(self):
        searchcache.clear()
        FakeYDL.calls, FakeYDL.result, FakeYDL.error = [], None, None


class CoreTest(Base):
    def test_normalize(self):
        self.assertEqual(searchcache.normalize("  Ｃｈｅｎ   LI  "), "chen li")
        searchcache.put("k", "Hello  World", [1])
        self.assertEqual(searchcache.get("k", "hello world"), [1])
        self.assertIsNone(searchcache.get("other", "hello world"))   # 種類不同不共用

    def test_empty_and_url_not_stored(self):
        searchcache.put("k", "abc", [])
        searchcache.put("k", "abc", None)
        searchcache.put("k", "", [1])
        searchcache.put("k", "https://www.youtube.com/watch?v=abc", [1])
        searchcache.put("k", "youtu.be/abc", [1])
        self.assertEqual(searchcache.size(), 0)

    def test_ttl(self):
        with mock.patch.object(searchcache.time, "monotonic", return_value=1000.0):
            searchcache.put("k", "q", [1])
        with mock.patch.object(searchcache.time, "monotonic", return_value=1000.0 + searchcache.TTL - 1):
            self.assertEqual(searchcache.get("k", "q"), [1])
        with mock.patch.object(searchcache.time, "monotonic", return_value=1000.0 + searchcache.TTL + 1):
            self.assertIsNone(searchcache.get("k", "q"))
        self.assertEqual(searchcache.size(), 0)

    def test_lru_bound(self):
        with mock.patch.object(searchcache, "MAX_ENTRIES", 3):
            for q in "abc":
                searchcache.put("k", q, [q])
            searchcache.get("k", "a")          # a 最近用過 → 留下，b 被擠掉
            searchcache.put("k", "d", ["d"])
            self.assertEqual(searchcache.size(), 3)
            self.assertIsNone(searchcache.get("k", "b"))
            self.assertEqual(searchcache.get("k", "a"), ["a"])

    def test_returns_copy(self):
        searchcache.put("k", "q", [{"t": 1}])
        searchcache.get("k", "q")[0]["t"] = 99
        self.assertEqual(searchcache.get("k", "q"), [{"t": 1}])


class PanelSearchTest(Base):
    def setUp(self):
        super().setUp()
        self._ld = webapi.flask_app.config.get("LOGIN_DISABLED")
        webapi.flask_app.config["LOGIN_DISABLED"] = True
        self.c = webapi.flask_app.test_client()
        self.p = mock.patch.object(webapi_library.yt_dlp, "YoutubeDL", FakeYDL)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        webapi.flask_app.config["LOGIN_DISABLED"] = self._ld

    def search(self, q):
        return self.c.post("/api/search", json={"query": q}, headers={"Host": "localhost"})

    def test_second_search_hits_cache(self):
        FakeYDL.result = {"entries": [entry(1), entry(2)]}
        r1 = self.search("陳粒 走马").get_json()
        r2 = self.search("  陳粒   走马 ").get_json()
        self.assertEqual(len(FakeYDL.calls), 1)
        self.assertEqual(r1["results"], r2["results"])
        self.assertTrue(r2.get("cached"))
        self.assertEqual(r2["count"], 2)

    def test_channels_and_playlists_filtered_still_five(self):
        FakeYDL.result = {"entries": [CHANNEL, video(1), PLAYLIST, video(2), video(3), None, video(4), video(5), video(6)]}
        r = self.search("tuki.").get_json()
        self.assertEqual(FakeYDL.calls, ["ytsearch8:tuki."])     # 多抓幾筆，濾掉後還有 5 首
        self.assertEqual([x["url"] for x in r["results"]], [entry(i)["url"] for i in range(1, 6)])
        self.assertTrue(all(x["duration"] for x in r["results"]))
        self.assertNotIn("UCp74", str(searchcache.get("panel5v", "tuki.")))

    def test_old_cache_kind_ignored(self):
        searchcache.put("panel5", "tuki.", [{"url": "https://www.youtube.com/watch?v=UCp74ruKv2sUiI2HqEhH8pRw", "title": "tuki.(17)"}])
        FakeYDL.result = {"entries": [CHANNEL, video(1)]}
        r = self.search("tuki.").get_json()
        self.assertEqual(len(FakeYDL.calls), 1)
        self.assertEqual([x["title"] for x in r["results"]], ["歌 1"])

    def test_missing_duration_is_zero(self):
        FakeYDL.result = {"entries": [dict(video(1), duration=None)]}
        self.assertEqual(self.search("x").get_json()["results"][0]["duration"], 0)

    def test_failure_and_empty_not_cached(self):
        FakeYDL.error = RuntimeError("boom")
        self.assertEqual(self.search("x").status_code, 500)
        FakeYDL.error = None
        FakeYDL.result = {"entries": []}
        self.search("x")
        FakeYDL.result = {"entries": [entry(3)]}
        r = self.search("x").get_json()
        self.assertEqual(len(FakeYDL.calls), 3)
        self.assertEqual(r["count"], 1)


class PlayLookupTest(Base):
    def setUp(self):
        super().setUp()
        self.p = mock.patch.object(player_voice.yt_dlp, "YoutubeDL", FakeYDL)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def lookup(self, q):
        return asyncio.run(player_voice._lookup(q))

    def test_text_query_cached(self):
        FakeYDL.result = {"extractor_key": "YoutubeSearch", "title": "q", "entries": [dict(entry(1), _type="url", description="d"), entry(2)]}
        a = self.lookup("陳粒 光")
        b = self.lookup("陳粒 光")
        self.assertEqual(FakeYDL.calls, ["ytsearch3:陳粒 光"])
        want = [{"id": entry(1)["id"], "url": entry(1)["url"], "title": "歌 1", "duration": 201}]
        self.assertEqual(a["entries"], want)     # 打字點歌只加第一首
        self.assertEqual(b["entries"], want)

    def test_channel_first_is_skipped(self):
        # 「tuki.」：YouTube 把頻道排第一 → 以前 ytsearch1 會把頻道網址塞進隊列
        FakeYDL.result = {"extractor_key": "YoutubeSearch", "entries": [CHANNEL, PLAYLIST, video(7), video(8)]}
        r = self.lookup("tuki.")
        self.assertEqual([e["id"] for e in r["entries"]], [entry(7)["id"]])
        self.assertEqual(searchcache.get("play1v", "tuki."), r["entries"])

    def test_only_channels_means_not_found(self):
        FakeYDL.result = {"extractor_key": "YoutubeSearch", "entries": [CHANNEL]}
        self.assertIsNone(self.lookup("tuki."))
        self.assertEqual(searchcache.size(), 0)

    def test_old_cache_kind_ignored(self):
        searchcache.put("play", "tuki.", [CHANNEL])          # 舊版存的（可能是頻道）
        FakeYDL.result = {"extractor_key": "YoutubeSearch", "entries": [CHANNEL, video(3)]}
        self.assertEqual(self.lookup("tuki.")["entries"][0]["id"], entry(3)["id"])
        self.assertEqual(len(FakeYDL.calls), 1)

    def test_url_never_cached(self):
        FakeYDL.result = {"extractor_key": "Youtube", "id": "abc", "url": "https://rr1---sn.googlevideo.com/x", "title": "t"}
        self.lookup("https://www.youtube.com/watch?v=abc")
        self.lookup("https://www.youtube.com/watch?v=abc")
        self.assertEqual(len(FakeYDL.calls), 2)
        self.assertEqual(searchcache.size(), 0)

    def test_non_search_result_or_empty_not_cached(self):
        FakeYDL.result = {"extractor_key": "Generic", "entries": [entry(1)]}
        self.lookup("weird")
        FakeYDL.result = {"extractor_key": "YoutubeSearch", "entries": []}
        self.lookup("nothing")
        FakeYDL.result = None
        self.lookup("none")
        self.assertEqual(searchcache.size(), 0)

    def test_failure_not_cached(self):
        FakeYDL.error = RuntimeError("bot check")
        with self.assertRaises(RuntimeError):
            self.lookup("x")
        self.assertEqual(searchcache.size(), 0)


class SpotifyImportTest(Base):
    def test_search_one_cached(self):
        FakeYDL.result = {"entries": [entry(5)]}
        with mock.patch.object(importer.yt_dlp, "YoutubeDL", FakeYDL):
            a = importer._yt_search_one("光", "陳粒")
            b = importer._yt_search_one("光", "陳粒")
            FakeYDL.result = {"entries": []}
            self.assertIsNone(importer._yt_search_one("沒有", "人"))
            importer._yt_search_one("沒有", "人")
        self.assertEqual(a, b)
        self.assertEqual(len(FakeYDL.calls), 3)
        self.assertEqual(FakeYDL.calls[0], "ytsearch3:光 陳粒")

    def test_search_one_skips_channel(self):
        FakeYDL.result = {"entries": [CHANNEL, video(9)]}
        with mock.patch.object(importer.yt_dlp, "YoutubeDL", FakeYDL):
            self.assertEqual(importer._yt_search_one("tuki.", "")["url"], entry(9)["url"])
            FakeYDL.result = {"entries": [CHANNEL, PLAYLIST]}
            self.assertIsNone(importer._yt_search_one("只有頻道", ""))


class VideoEntryTest(unittest.TestCase):
    def test_is_video_entry(self):
        from musicbot import ytdl
        self.assertTrue(ytdl.is_video_entry(video(1)))
        self.assertTrue(ytdl.is_video_entry({"id": "5ZdJlEYRTMw"}))                       # 沒寫 ie_key 也算
        self.assertTrue(ytdl.is_video_entry({"id": "a-b_c-d_e-f", "ie_key": "Youtube", "url": "https://www.youtube.com/shorts/a-b_c-d_e-f"}))
        for bad in (CHANNEL, PLAYLIST, None, "x", {}, {"id": "short"}, {"id": "5ZdJlEYRTMw", "_type": "playlist"},
                    {"id": "5ZdJlEYRTMw", "ie_key": "YoutubeTab"}, {"id": "abc def ghi"}):
            self.assertFalse(ytdl.is_video_entry(bad), bad)
        self.assertEqual(ytdl.video_entries({"entries": [CHANNEL, video(1), video(2)]}, 1), [video(1)])
        self.assertEqual(ytdl.video_entries(None), [])


if __name__ == "__main__":
    unittest.main()
