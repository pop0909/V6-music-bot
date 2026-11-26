"""T-11 第 3 項：B 站合集與公開收藏夾（網址解析、分頁、上限、面板選擇）。
執行：python -m unittest tests.test_t11_bili_lists
"""
import json
from pathlib import Path
import os
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from appjs import read_app_js  # noqa: E402
from musicbot import bilibili, webapi  # noqa: E402

BV = "BV1vny2znEiR"
MID = 700232090
SID = 8986237
FID = 1193340201
SEASON_URL = f"https://space.bilibili.com/{MID}/channel/collectiondetail?sid={SID}"
LISTS_URL = f"https://space.bilibili.com/{MID}/lists/{SID}?type=season"
LIST_URL = f"https://www.bilibili.com/list/{MID}?sid={SID}"
FAV_URL = f"https://www.bilibili.com/medialist/detail/ml{FID}"
FAV_PLAY = f"https://www.bilibili.com/medialist/play/ml{FID}"
SPACE_FAV = f"https://space.bilibili.com/{MID}/favlist?fid={FID}"
VIDEO = f"https://www.bilibili.com/video/{BV}"


def _resp(payload, status=200, headers=None, url="https://api.bilibili.com/"):
    if isinstance(payload, (dict, list)):
        body = json.dumps(payload).encode()
    elif isinstance(payload, bytes):
        body = payload
    else:
        body = str(payload).encode()
    return status, headers or {"Content-Type": "application/json"}, body, url


def _archive(i, bvid=None, title=None):
    bvid = bvid or f"BV1{i:010d}"[:12].replace("BV1", "BV1")
    # legal-looking BV: BV1 + 10 chars from allowed set
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not bvid or not bilibili._BV_RE.fullmatch(bvid):
        core = "".join(alphabet[i % len(alphabet)] for _ in range(10))
        bvid = "BV1" + core[:10]
        # ensure unique: encode i into last chars
        suffix = alphabet[i % len(alphabet)] + alphabet[(i // len(alphabet)) % len(alphabet)]
        bvid = "BV1" + core[:8] + suffix
    return {"aid": 1000 + i, "bvid": bvid, "title": title or f"第 {i} 集", "duration": 60 + i, "pic": f"//i0.hdslb.com/{i}.jpg"}


# 固定合法 BV，避免隨機不合格
def _bv(n):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    body = []
    x = n
    for _ in range(10):
        body.append(alphabet[x % len(alphabet)])
        x //= len(alphabet)
    return "BV" + "".join(body)


def _archive_n(i, title=None):
    return {"aid": 1000 + i, "bvid": _bv(i), "title": title or f"第 {i} 集", "duration": 60 + i, "pic": f"//i0.hdslb.com/{i}.jpg"}


class ParseResource(unittest.TestCase):
    def test_season_url_shapes(self):
        for u in (
            SEASON_URL,
            LISTS_URL,
            LIST_URL,
            f"https://www.bilibili.com/list/{MID}?sid={SID}&spm_id_from=x",
            f"https://www.bilibili.com/medialist/play/{MID}?business=space_collection&business_id={SID}",
            f"https://space.bilibili.com/{MID}/favlist?fid={SID}&ftype=collect",
        ):
            info = bilibili.parse_resource(u)
            self.assertEqual(info["kind"], "season", u)
            self.assertEqual(info["mid"], MID, u)
            self.assertEqual(info["season_id"], SID, u)
            self.assertEqual(bilibili.canonical_resource(info), SEASON_URL)

    def test_fav_url_shapes(self):
        for u in (
            FAV_URL,
            FAV_PLAY,
            SPACE_FAV,
            f"https://www.bilibili.com/list/ml{FID}",
            f"https://m.bilibili.com/medialist/detail/ml{FID}?from=x",
        ):
            info = bilibili.parse_resource(u)
            self.assertEqual(info["kind"], "favlist", u)
            self.assertEqual(info["fid"], FID, u)
            self.assertEqual(bilibili.canonical_resource(info), FAV_URL)

    def test_video_in_list_url_keeps_bvid_and_season(self):
        u = f"{LIST_URL}&bvid={BV}"
        info = bilibili.parse_resource(u)
        self.assertEqual(info["kind"], "video")
        self.assertEqual(info["bvid"], BV)
        self.assertEqual(info["mid"], MID)
        self.assertEqual(info["season_id"], SID)

    def test_plain_video_and_rejects(self):
        v = bilibili.parse_resource(VIDEO)
        self.assertEqual(v["kind"], "video")
        self.assertEqual(v["bvid"], BV)
        self.assertEqual(bilibili.parse_resource("https://www.bilibili.com/bangumi/play/ss123")["kind"], "bangumi")
        self.assertEqual(bilibili.parse_resource("https://www.bilibili.com/cheese/play/ss1")["kind"], "bangumi")
        self.assertEqual(
            bilibili.parse_resource(f"https://space.bilibili.com/{MID}/channel/seriesdetail?sid=1")["kind"],
            "series",
        )
        self.assertEqual(
            bilibili.parse_resource(f"https://space.bilibili.com/{MID}/lists/99?type=series")["kind"],
            "series",
        )
        self.assertIsNone(bilibili.parse_resource("https://www.youtube.com/watch?v=AAAAAAAAAAA"))

    def test_short_link_canonicalizes_season(self):
        dest = LIST_URL

        class _Hdr(dict):
            def get(self, k, default=None):
                return dict.get(self, k, default)

        def open_fail(req, timeout=15):
            import io, urllib.error
            raise urllib.error.HTTPError(dest, 302, "Found", _Hdr({"Location": dest}), io.BytesIO())

        with mock.patch("urllib.request.build_opener") as bo:
            bo.return_value.open.side_effect = open_fail
            got = bilibili.resolve_short("https://b23.tv/xxxxxx")
        self.assertEqual(got, SEASON_URL)


class ResolveLists(unittest.TestCase):
    def tearDown(self):
        bilibili.reset_runtime()

    def test_season_pages_and_cap(self):
        seen = []

        def fake_api(path, params=None, signed=False, referer=None):
            seen.append((path, dict(params or {}), referer))
            self.assertIn("/seasons_archives_list", path)
            self.assertTrue(referer and "collectiondetail" in referer)
            pn = int(params["page_num"])
            total = 250
            start = (pn - 1) * 30
            archives = [_archive_n(start + i) for i in range(30) if start + i < total]
            return {"code": 0, "data": {
                "meta": {"season_id": SID, "name": "合集·示範", "mid": MID, "total": total},
                "archives": archives,
                "page": {"page_num": pn, "page_size": 30, "total": total},
            }}

        with mock.patch.object(bilibili, "_api_json", side_effect=fake_api):
            result = bilibili.resolve_list(SEASON_URL)
        self.assertEqual(result["kind"], "season")
        self.assertEqual(result["title"], "合集·示範")
        self.assertEqual(len(result["songs"]), 200)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total"], 250)
        self.assertEqual(result["songs"][0]["url"], "https://www.bilibili.com/video/" + _bv(0))
        self.assertTrue(result["songs"][0]["thumbnail"].startswith("https://"))
        # 200 / 30 = 7 頁（最後一頁只取 20）
        self.assertEqual(len(seen), 7)
        self.assertEqual(seen[0][1]["page_size"], 30)

        with mock.patch.object(bilibili, "_api_json", side_effect=fake_api):
            songs = bilibili.resolve_for_queue(LISTS_URL)
        self.assertEqual(len(songs), 200)

    def test_season_missing(self):
        with mock.patch.object(bilibili, "_api_json", return_value={"code": -404, "message": "啥都木有", "data": None}):
            with self.assertRaises(RuntimeError) as ctx:
                bilibili.resolve_list(SEASON_URL)
        self.assertIn("找不到這個合集", str(ctx.exception))

    def test_fav_public_and_skips_bangumi(self):
        payload = {"code": 0, "data": {
            "info": {"title": "！", "media_count": 3, "id": FID},
            "medias": [
                {"type": 2, "bvid": _bv(1), "title": "歌一", "duration": 10, "cover": "//i0.hdslb.com/a.jpg"},
                {"type": 24, "bvid": _bv(2), "title": "番劇", "duration": 20, "cover": "//x", "ogv": {"season_id": 1}},
                {"type": 2, "bvid": "", "title": "沒編號"},
                {"type": 2, "bvid": _bv(3), "title": "歌二", "duration": 11, "cover": "https://i0.hdslb.com/b.jpg"},
            ],
            "has_more": False,
        }}
        referers = []

        def fake(path, params=None, signed=False, referer=None):
            referers.append(referer)
            self.assertIn("/fav/resource/list", path)
            self.assertEqual(int(params["media_id"]), FID)
            return payload

        with mock.patch.object(bilibili, "_api_json", side_effect=fake):
            result = bilibili.resolve_list(FAV_URL)
        self.assertEqual(result["kind"], "favlist")
        self.assertEqual(result["title"], "！")
        self.assertEqual(len(result["songs"]), 2)
        self.assertEqual([s["title"] for s in result["songs"]], ["歌一", "歌二"])
        self.assertFalse(result["truncated"])
        self.assertTrue(referers[0] and f"ml{FID}" in referers[0])

    def test_fav_not_public(self):
        with mock.patch.object(bilibili, "_api_json", return_value={"code": 0, "data": None}):
            with self.assertRaises(RuntimeError) as ctx:
                bilibili.resolve_list(FAV_URL)
        self.assertIn("找不到這個收藏夾", str(ctx.exception))

    def test_fav_empty_usable(self):
        payload = {"code": 0, "data": {
            "info": {"title": "空的", "media_count": 1, "id": FID},
            "medias": [{"type": 24, "bvid": _bv(1), "title": "番劇", "ogv": {"season_id": 9}}],
            "has_more": False,
        }}
        with mock.patch.object(bilibili, "_api_json", return_value=payload):
            with self.assertRaises(RuntimeError) as ctx:
                bilibili.resolve_list(FAV_PLAY)
        self.assertIn("沒有可加入的影片", str(ctx.exception))

    def test_bangumi_and_series_messages(self):
        with self.assertRaises(RuntimeError) as c1:
            bilibili.resolve_list("https://www.bilibili.com/bangumi/play/ep1")
        self.assertEqual(str(c1.exception), "暫不支援番劇網址")
        with self.assertRaises(RuntimeError) as c2:
            bilibili.resolve_list(f"https://space.bilibili.com/{MID}/channel/seriesdetail?sid=1")
        self.assertIn("系列", str(c2.exception))

    def test_video_offers_collection_but_does_not_expand(self):
        view = {
            "bvid": BV, "aid": 1, "title": "這一首", "pic": "https://i0.hdslb.com/x.jpg",
            "duration": 12, "cid": 9,
            "pages": [{"cid": 9, "page": 1, "part": "這一首", "duration": 12}],
            "ugc_season": {"id": SID, "title": "合集·海綿寶寶", "mid": MID, "ep_count": 2},
        }
        with mock.patch.object(bilibili, "view", return_value=view):
            result = bilibili.resolve_list(VIDEO)
        self.assertEqual(result["kind"], "video")
        self.assertEqual(len(result["songs"]), 1)
        self.assertFalse(result["expanded"])
        self.assertEqual(result["collection"]["id"], SID)
        self.assertEqual(result["collection"]["count"], 2)
        self.assertEqual(result["collection"]["title"], "合集·海綿寶寶")
        # Discord / 一般解析：只加這首
        with mock.patch.object(bilibili, "view", return_value=view):
            songs = bilibili.resolve_for_queue(VIDEO)
        self.assertEqual(len(songs), 1)

    def test_expand_season_from_video(self):
        view = {
            "bvid": BV, "aid": 1, "title": "這一首", "pic": "https://i0.hdslb.com/x.jpg",
            "duration": 12, "cid": 9,
            "pages": [{"cid": 9, "page": 1, "part": "這一首", "duration": 12}],
            "ugc_season": {"id": SID, "title": "合集·海綿寶寶", "mid": MID, "ep_count": 2},
        }

        def fake(path, params=None, signed=False, referer=None):
            return {"code": 0, "data": {
                "meta": {"season_id": SID, "name": "合集·海綿寶寶", "mid": MID, "total": 2},
                "archives": [_archive_n(1, "A"), _archive_n(2, "B")],
                "page": {"page_num": 1, "page_size": 30, "total": 2},
            }}

        with mock.patch.object(bilibili, "view", return_value=view), \
             mock.patch.object(bilibili, "_api_json", side_effect=fake):
            result = bilibili.resolve_list(VIDEO, expand_season=True)
        self.assertEqual(result["kind"], "season")
        self.assertTrue(result["expanded"])
        self.assertEqual(len(result["songs"]), 2)
        self.assertEqual([s["title"] for s in result["songs"]], ["A", "B"])

    def test_lookup_ytdl_shape_uses_collection_title(self):
        def fake(path, params=None, signed=False, referer=None):
            return {"code": 0, "data": {
                "meta": {"season_id": SID, "name": "合集·示範", "mid": MID, "total": 2},
                "archives": [_archive_n(1, "A"), _archive_n(2, "B")],
                "page": {"page_num": 1, "page_size": 30, "total": 2},
            }}
        with mock.patch.object(bilibili, "_api_json", side_effect=fake):
            info = bilibili.lookup_ytdl_shape(SEASON_URL)
        self.assertEqual(info["title"], "合集·示範")
        self.assertEqual(len(info["entries"]), 2)
        self.assertEqual(info["entries"][0]["title"], "A")


class ResolveUrlApiAndUi(unittest.TestCase):
    def test_resolve_url_returns_collection_and_expand(self):
        ld = webapi.flask_app.config.get("LOGIN_DISABLED")
        webapi.flask_app.config["LOGIN_DISABLED"] = True
        video_result = {
            "kind": "video", "title": "這一首", "songs": [
                {"url": VIDEO, "title": "這一首", "duration": 12, "thumbnail": ""},
            ], "truncated": False, "total": 1,
            "collection": {"id": SID, "title": "合集·海綿寶寶", "count": 2, "mid": MID},
            "expanded": False,
        }
        season_result = {
            "kind": "season", "title": "合集·海綿寶寶", "songs": [
                {"url": VIDEO, "title": "A", "duration": 1, "thumbnail": ""},
                {"url": VIDEO + "x", "title": "B", "duration": 2, "thumbnail": ""},
            ], "truncated": False, "total": 2,
            "collection": {"id": SID, "title": "合集·海綿寶寶", "count": 2, "mid": MID},
            "expanded": True,
        }
        try:
            c = webapi.flask_app.test_client()
            with mock.patch.object(bilibili, "resolve_list", return_value=video_result) as rl:
                r = c.post("/api/resolve-url", json={"url": VIDEO}, headers={"Host": "localhost"})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
            body = r.get_json()
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["kind"], "video")
            self.assertEqual(body["collection"]["count"], 2)
            self.assertFalse(body["expanded"])
            self.assertEqual(rl.call_args.kwargs.get("expand_season"), False)

            with mock.patch.object(bilibili, "resolve_list", return_value=season_result) as rl2:
                r2 = c.post("/api/resolve-url",
                            json={"url": VIDEO, "expand_season": True},
                            headers={"Host": "localhost"})
            self.assertEqual(r2.get_json()["kind"], "season")
            self.assertTrue(r2.get_json()["expanded"])
            self.assertTrue(r2.get_json()["is_playlist"])
            self.assertEqual(r2.get_json()["playlist_title"], "合集·海綿寶寶")
            self.assertTrue(rl2.call_args.kwargs.get("expand_season"))
        finally:
            webapi.flask_app.config["LOGIN_DISABLED"] = ld

    def test_resolve_url_bangumi_message(self):
        ld = webapi.flask_app.config.get("LOGIN_DISABLED")
        webapi.flask_app.config["LOGIN_DISABLED"] = True
        try:
            c = webapi.flask_app.test_client()
            r = c.post("/api/resolve-url",
                       json={"url": "https://www.bilibili.com/bangumi/play/ss1"},
                       headers={"Host": "localhost"})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.get_json()["message"], "暫不支援番劇網址")
        finally:
            webapi.flask_app.config["LOGIN_DISABLED"] = ld

    def test_panel_js_asks_and_keeps_let_data(self):
        lists = Path(ROOT, "static", "js", "app", "05-lists.js").read_text(encoding="utf-8")
        core = Path(ROOT, "static", "js", "app", "01-core.js").read_text(encoding="utf-8")
        self.assertIn("resolveUrl: (u, extra) => api('POST', '/api/resolve-url', { url: u, ...(extra || {}) })", core)
        body = lists[lists.index("async function addFromUrl"):lists.index("async function doSearch")]
        self.assertIn("let data = await API.resolveUrl(query);", body)
        self.assertIn("這首歌在合集裡", body)
        self.assertIn("加入整份合集", body)
        self.assertIn("只加這首", body)
        self.assertIn("expand_season: true", body)
        self.assertIn("data.kind === 'favlist' ? '收藏夾'", body)
        self.assertIn("已加入前 ${batchResult.count} 首", body)
        self.assertIn("20261126t11b", Path(ROOT, "templates", "index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
