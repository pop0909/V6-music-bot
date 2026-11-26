"""Bilibili：網址白名單與正規化、公開 API 搜尋 / 解析（全部 stub 網路）、播放標頭、面板與插件。"""
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from musicbot import audio, bilibili, rangeproxy, utils, ytdl
from appjs import read_app_js  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
BV = "BV1rTy2zFE7f"
PAGE = f"https://www.bilibili.com/video/{BV}"
CDN = "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/00/00/1.m4s?deadline=1795678359&e=ok"


def _resp(payload, status=200, headers=None, url="https://api.bilibili.com/"):
    if isinstance(payload, (dict, list)):
        body = json.dumps(payload).encode()
    elif isinstance(payload, bytes):
        body = payload
    else:
        body = str(payload).encode()
    hdrs = headers or {"Content-Type": "application/json"}
    return status, hdrs, body, url


class ParseNormalize(unittest.TestCase):
    def test_hosts_allowed(self):
        ok = [
            f"https://www.bilibili.com/video/{BV}",
            f"https://m.bilibili.com/video/{BV}?p=2",
            "https://b23.tv/abcdef",
            "https://www.b23.tv/x",
            "http://bilibili.com/video/av170001",
            "www.bilibili.com/video/" + BV,
        ]
        bad = [
            "https://bilibili.com.evil.com/video/" + BV,
            "https://evilbilibili.com/video/" + BV,
            "https://bilibili.com@evil.com/video/" + BV,
            "https://bilivideo.com/x.m4s",
            "https://evil.com/x",
        ]
        for u in ok:
            self.assertTrue(utils.song_host_allowed(u), u)
        for u in bad:
            self.assertFalse(utils.song_host_allowed(u), u)

    def test_error_message_names_bilibili(self):
        self.assertIn("Bilibili", utils.URL_NOT_ALLOWED)
        self.assertIsNone(utils.song_url_error(PAGE))

    def test_normalize_bv_and_part(self):
        self.assertEqual(bilibili.normalize(PAGE + "?spm_id_from=x"), PAGE)
        self.assertEqual(bilibili.normalize(f"https://m.bilibili.com/video/{BV}?p=3&vd_source=1"), PAGE + "?p=3")
        self.assertEqual(bilibili.normalize(f"https://www.bilibili.com/video/{BV}?p=1"), PAGE)
        self.assertEqual(bilibili.normalize("https://www.bilibili.com/video/av170001"),
                         "https://www.bilibili.com/video/av170001")
        self.assertIsNone(bilibili.normalize("https://www.bilibili.com/"))
        self.assertIsNone(bilibili.normalize("https://b23.tv/abc"))  # 短網址要先轉址

    def test_is_page_and_cdn(self):
        self.assertTrue(bilibili.is_page(PAGE))
        self.assertTrue(bilibili.is_page("https://b23.tv/x"))
        self.assertFalse(bilibili.is_cdn_host(PAGE))
        self.assertTrue(bilibili.is_cdn_host(CDN))
        self.assertFalse(bilibili.is_page(CDN))


class ShortLink(unittest.TestCase):
    def test_follows_location_to_bv(self):
        def fake(url, headers=None, method="GET", max_bytes=2_000_000):
            raise AssertionError("resolve_short should use opener, not _http")
        dest = PAGE + "?p=2"

        class _Hdr(dict):
            def get(self, k, default=None):
                return dict.get(self, k, default)
        def open_fail(req, timeout=15):
            raise urllib.error.HTTPError(dest, 302, "Found", _Hdr({"Location": dest}), io.BytesIO())

        with mock.patch("urllib.request.build_opener") as bo:
            bo.return_value.open.side_effect = open_fail
            got = bilibili.resolve_short("https://b23.tv/xxxxxx")
        self.assertEqual(got, PAGE + "?p=2")


class ApiSearchAndView(unittest.TestCase):
    def tearDown(self):
        bilibili.reset_runtime()

    def _nav(self):
        return {"code": -101, "message": "账号未登录", "data": {
            "isLogin": False,
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/" + ("a" * 32) + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + ("b" * 32) + ".png",
            },
        }}

    def _video_hit(self):
        return {"bvid": BV, "title": "接下來會很瘋狂【<em class=\"keyword\">原神</em>】", "duration": "5:9",
                "pic": "//i0.hdslb.com/bfs/cover/x.jpg", "author": "UP主"}

    def test_search_parses_results_and_strips_em(self):
        payload = {"code": 0, "message": "OK", "data": {"result": [
            self._video_hit(),
            {"bvid": "", "title": "廣告", "duration": "0:10"},
            {"bvid": "BV1INVALID", "title": "第二首", "duration": "1:02"},  # 不是合法 BV
        ]}}
        seen = []

        def fake(url, headers=None, method="GET", max_bytes=2_000_000):
            seen.append(url)
            if "/x/web-interface/nav" in url:
                return _resp(self._nav())
            if "/wbi/search/type" in url:
                self.assertIn("w_rid=", url)
                self.assertIn("wts=", url)
                self.assertIn("keyword=", url)
                return _resp(payload)
            raise AssertionError("unexpected " + url)

        with mock.patch.object(bilibili, "_http", side_effect=fake):
            rows = bilibili.search("原神", 5)
        self.assertTrue(any("/wbi/search/type" in u for u in seen))
        self.assertFalse(any("/search/type?" in u and "/wbi/" not in u for u in seen))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], PAGE)
        self.assertEqual(rows[0]["title"], "接下來會很瘋狂【原神】")
        self.assertEqual(rows[0]["duration"], 5 * 60 + 9)
        self.assertEqual(rows[0]["thumbnail"], "https://i0.hdslb.com/bfs/cover/x.jpg")
        self.assertEqual(rows[0]["author"], "UP主")

    def test_search_falls_back_when_type_returns_html(self):
        html = b'<!DOCTYPE HTML><html lang="zh-cn"><title>\xe5\x87\xba\xe9\x94\x99\xe5\x95\x8a! - aba.bilibili.com</title></html>'
        grouped = {"code": 0, "message": "OK", "data": {"result": [
            {"result_type": "activity", "data": [{"title": "廣告"}]},
            {"result_type": "video", "data": [self._video_hit()]},
        ]}}
        seen = []

        def fake(url, headers=None, method="GET", max_bytes=2_000_000):
            seen.append(url)
            if "/x/web-interface/nav" in url:
                return _resp(self._nav())
            if "/wbi/search/type" in url:
                return _resp(html, headers={"Content-Type": "text/html; charset=utf-8"})
            if "/search/all/v2" in url:
                return _resp(grouped)
            raise AssertionError("unexpected " + url)

        with mock.patch.object(bilibili, "_http", side_effect=fake):
            rows = bilibili.search("原神", 5)
        self.assertTrue(any("/wbi/search/type" in u for u in seen))
        self.assertTrue(any("/search/all/v2" in u for u in seen))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "接下來會很瘋狂【原神】")

    def test_search_falls_back_to_search_all_if_wbi_code_fails(self):
        seen = []

        def fake(url, headers=None, method="GET", max_bytes=2_000_000):
            seen.append(url)
            if "/x/web-interface/nav" in url:
                return _resp(self._nav())
            if "/wbi/search/type" in url:
                return _resp({"code": -412, "message": "请求被拦截", "data": None})
            if "/search/all/v2" in url:
                return _resp({"code": -400, "message": "bad"}, headers={"Content-Type": "application/json"})
            if url.rstrip("/").endswith("/search/all") or "/search/all?" in url:
                return _resp({"code": 0, "data": {"result": [self._video_hit()]}})
            raise AssertionError("unexpected " + url)

        with mock.patch.object(bilibili, "_http", side_effect=fake):
            rows = bilibili.search("原神", 3)
        self.assertTrue(any(u.rstrip("/").endswith("/search/all") or "/search/all?" in u for u in seen))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], PAGE)

    def test_api_json_detects_html(self):
        html = b"<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01 Transitional//EN\"><html>sorry</html>"
        with mock.patch.object(bilibili, "_http", return_value=_resp(html, headers={"Content-Type": "text/html"})):
            with self.assertRaises(bilibili.BiliApiFormatError):
                bilibili._api_json("/x/web-interface/search/type", {"keyword": "x"})

    def test_resolve_multi_part_all_or_one(self):
        view = {"code": 0, "data": {
            "bvid": BV, "aid": 1, "title": "合集", "pic": "https://i0.hdslb.com/bfs/x.jpg", "duration": 90,
            "cid": 11, "pages": [
                {"cid": 11, "page": 1, "part": "開頭", "duration": 30},
                {"cid": 22, "page": 2, "part": "中段", "duration": 60},
            ],
        }}
        with mock.patch.object(bilibili, "_http", return_value=_resp(view)):
            all_parts = bilibili.resolve_for_queue(PAGE)
            one = bilibili.resolve_for_queue(PAGE + "?p=2")
        self.assertEqual(len(all_parts), 2)
        self.assertTrue(all_parts[1]["url"].endswith("?p=2"))
        self.assertIn("P2", all_parts[1]["title"])
        self.assertEqual(len(one), 1)
        self.assertEqual(one[0]["url"], PAGE + "?p=2")
        self.assertEqual(one[0]["duration"], 60)

    def test_extract_stream_sends_referer_and_probes_size(self):
        view = {"code": 0, "data": {
            "bvid": BV, "aid": 1, "title": "歌", "pic": "https://i0.hdslb.com/bfs/x.jpg",
            "duration": 12, "cid": 99, "pages": [{"cid": 99, "page": 1, "part": "歌", "duration": 12}],
        }}
        play = {"code": 0, "data": {"dash": {"audio": [
            {"id": 30216, "baseUrl": CDN, "bandwidth": 1000, "mime_type": "audio/mp4", "codecs": "mp4a.40.2"},
            {"id": 30280, "baseUrl": CDN + "&q=hi", "bandwidth": 140000, "mime_type": "audio/mp4", "codecs": "mp4a.40.2"},
        ]}}}
        calls = []

        def http(url, headers=None, method="GET", max_bytes=2_000_000):
            calls.append((url, dict(headers or {})))
            if "playurl" in url:
                return _resp(play, url=url)
            if "upos-" in url or "bilivideo.com" in url:
                return 206, {"Content-Range": "bytes 0-0/13159766", "Content-Type": "video/mp4"}, b"\x00", url
            return _resp(view, url=url)

        with mock.patch.object(bilibili, "_http", side_effect=http):
            res = bilibili.extract_stream(PAGE)
        self.assertEqual(res["title"], "歌")
        self.assertTrue(res["url"].startswith("https://upos-"))
        self.assertIn("&q=hi", res["url"])  # 選 bitrate 較高的
        self.assertEqual(res["headers"]["Referer"], "https://www.bilibili.com")
        self.assertEqual(res["filesize"], 13159766)
        self.assertEqual(res["mime"], "audio/mp4")
        # 問大小時帶了 Referer
        probe = [h for u, h in calls if "bilivideo.com" in u]
        self.assertTrue(probe)
        self.assertEqual(probe[0].get("Referer") or probe[0].get("referer"), "https://www.bilibili.com")


class PlaybackHeaders(unittest.TestCase):
    def test_ffmpeg_direct_includes_referer(self):
        s = audio.before_options(headers={"Referer": "https://www.bilibili.com", "User-Agent": "X"})
        self.assertIn("-headers", s)
        self.assertIn("Referer: https://www.bilibili.com", s)
        self.assertIn("User-Agent: X", s)
        self.assertIn("-reconnect 1", s)
        s2 = audio.before_options(seek=3.5, headers={"Referer": "https://www.bilibili.com"})
        self.assertTrue(s2.startswith("-ss 3.500"))

    def test_rangeproxy_forwards_referer_and_uses_size(self):
        self.assertEqual(rangeproxy.eligible(CDN), None)  # 沒有 contentlength
        got = rangeproxy.input_url(CDN, page=PAGE, headers=bilibili.stream_headers(), size=13159766, mime="audio/mp4")
        self.assertTrue(got.startswith("http://127.0.0.1:"))
        tok = got.rstrip("/").rsplit("/", 1)[-1]
        entry = rangeproxy._streams[tok]
        self.assertEqual(entry["size"], 13159766)
        self.assertEqual(entry["headers"]["Referer"], "https://www.bilibili.com")
        self.assertEqual(entry["mime"], "audio/mp4")
        # 清掉以免干擾其他測試
        rangeproxy._streams.pop(tok, None)

    def test_rangeproxy_fetch_sends_referer(self):
        captured = {}

        class _Resp:
            status = 206
            headers = {}
            def read(self, n):
                return b"x" * max(0, n)
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def urlopen(req, timeout=None):
            captured["headers"] = dict(req.header_items() if hasattr(req, "header_items") else [])
            # urllib Request stores headers in req.headers
            captured["headers"] = {k.title(): v for k, v in req.headers.items()}
            captured["url"] = req.full_url
            return _Resp()

        e = {"url": CDN, "headers": {"Referer": "https://www.bilibili.com", "User-Agent": "BotUA"},
             "size": 100, "rlock": __import__("threading").Lock(), "refreshed_at": 0, "url_ts": 0}
        out = io.BytesIO()
        # _GuardedWriter-like
        class W:
            def write(self, b):
                out.write(b)
                return len(b)
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            # 上游立刻結束 → 寫 0 bytes，不要真的重試太久：讓第一次就回
            rangeproxy._fetch_into(e, 0, 10, W())
        self.assertEqual(captured["headers"].get("Referer"), "https://www.bilibili.com")
        self.assertIn("Range", {k.title() for k in captured["headers"]})


class PanelAndExtension(unittest.TestCase):
    def test_search_source_toggle_in_ui(self):
        html = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
        self.assertIn('data-src="youtube"', html)
        self.assertIn('data-src="bilibili"', html)
        self.assertIn("?v=20261126t12b", html)
        js = read_app_js()
        self.assertIn("API.search(query, searchSource)", js)
        self.assertIn("panel5bili", open(os.path.join(ROOT, "musicbot", "webapi_library.py"), encoding="utf-8").read()
                      or "panel5bili")
        self.assertIn("source: source || 'youtube'", js)
        self.assertIn("mb_search_src", js)
        css = open(os.path.join(ROOT, "static", "css", "style.css"), encoding="utf-8").read()
        self.assertIn(".search-src-btn", css)

    def test_extension_manifest_and_scripts(self):
        man = json.loads(open(os.path.join(ROOT, "chrome-extension", "manifest.json"), encoding="utf-8").read())
        self.assertEqual(man["version"], "1.2.1")
        joined = " ".join(man.get("host_permissions") or [])
        self.assertIn("bilibili.com", joined)
        self.assertIn("b23.tv", joined)
        matches = " ".join(sum((cs.get("matches") or [] for cs in man["content_scripts"]), []))
        self.assertIn("bilibili.com", matches)
        content = open(os.path.join(ROOT, "chrome-extension", "content.js"), encoding="utf-8").read()
        self.assertIn("isBili", content)
        self.assertIn("www.bilibili.com/video/", content)
        bg = open(os.path.join(ROOT, "chrome-extension", "background.js"), encoding="utf-8").read()
        self.assertIn("BILI_LINKS", bg)
        pop = open(os.path.join(ROOT, "chrome-extension", "popup.js"), encoding="utf-8").read()
        self.assertIn("bilibili", pop)

    def test_prefetch_dispatches_to_bilibili(self):
        src = open(os.path.join(ROOT, "musicbot", "prefetch.py"), encoding="utf-8").read()
        self.assertIn("bilibili.extract_stream", src)
        self.assertIn("deadline", src)


if __name__ == "__main__":
    unittest.main()
