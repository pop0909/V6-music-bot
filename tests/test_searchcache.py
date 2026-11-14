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
    return {"id": f"vid{i:08d}x", "title": f"歌 {i}", "duration": 200 + i, "url": f"https://www.youtube.com/watch?v=vid{i:08d}x"}


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
        FakeYDL.result = {"extractor_key": "YoutubeSearch", "title": "q", "entries": [dict(entry(1), _type="url", description="d")]}
        a = self.lookup("陳粒 光")
        b = self.lookup("陳粒 光")
        self.assertEqual(len(FakeYDL.calls), 1)
        self.assertIs(a, FakeYDL.result)
        self.assertEqual(b["entries"], [{"id": entry(1)["id"], "url": entry(1)["url"], "title": "歌 1", "duration": 201}])

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


if __name__ == "__main__":
    unittest.main()
