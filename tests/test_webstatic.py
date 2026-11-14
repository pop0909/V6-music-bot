"""面板載入速度：靜態檔快取標頭、gzip、版本號
執行：python -m unittest discover -s tests
"""
import gzip
import os
import re
import sys
import unittest

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import webapi, webstatic  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
H = {"Host": "localhost"}


def read(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read()


class StaticCacheTest(unittest.TestCase):
    def setUp(self):
        self.c = webapi.flask_app.test_client()

    def get(self, url, **headers):
        r = self.c.get(url, headers={**H, **headers})
        r.get_data()
        r.close()
        return r

    def test_versioned_long_cache(self):
        for url in ("/static/js/app.js?v=x", "/static/css/style.css?v=x"):
            r = self.get(url)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["Cache-Control"], webstatic.LONG)

    def test_unversioned_short_cache(self):
        r = self.get("/static/js/app.js")
        self.assertEqual(r.headers["Cache-Control"], webstatic.SHORT)

    def test_html_no_cache(self):
        r = self.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-cache")

    def test_api_still_no_store(self):
        r = self.get("/api/status")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_gzip_roundtrip(self):
        for url, rel in (("/static/js/app.js?v=x", "static/js/app.js"),
                         ("/static/css/style.css?v=x", "static/css/style.css")):
            r = self.get(url, **{"Accept-Encoding": "gzip, deflate, br"})
            self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
            self.assertIn("Accept-Encoding", r.headers.get("Vary", ""))
            self.assertEqual(int(r.headers["Content-Length"]), len(r.data))
            self.assertEqual(gzip.decompress(r.data), read(rel))
            self.assertLess(len(r.data), len(read(rel)) * 0.5)

    def test_no_gzip_without_accept(self):
        for ae in (None, "identity", "gzip;q=0, br"):
            r = self.get("/static/js/app.js?v=x", **({"Accept-Encoding": ae} if ae else {}))
            self.assertIsNone(r.headers.get("Content-Encoding"), ae)
            self.assertEqual(r.data, read("static/js/app.js"))

    def test_gzip_etag_304(self):
        r = self.get("/static/js/app.js?v=x", **{"Accept-Encoding": "gzip"})
        et = r.headers["ETag"]
        self.assertTrue(et.endswith('-gz"'))
        r2 = self.get("/static/js/app.js?v=x", **{"Accept-Encoding": "gzip", "If-None-Match": et})
        self.assertEqual(r2.status_code, 304)
        self.assertEqual(r2.data, b"")

    def test_range_not_gzipped(self):
        r = self.get("/static/js/app.js?v=x", **{"Accept-Encoding": "gzip", "Range": "bytes=0-99"})
        self.assertEqual(r.status_code, 206)
        self.assertIsNone(r.headers.get("Content-Encoding"))
        self.assertEqual(r.data, read("static/js/app.js")[:100])

    def test_gzip_cache_follows_mtime(self):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.js")
        with open(p, "w") as f:
            f.write("a" * 5000)
        g1 = webstatic.gzipped(p)
        with open(p, "w") as f:
            f.write("b" * 6000)
        os.utime(p, ns=(1, 1))
        self.assertEqual(gzip.decompress(webstatic.gzipped(p)), b"b" * 6000)
        self.assertNotEqual(g1, webstatic.gzipped(p))

    def test_traversal_ignored(self):
        self.assertIsNone(webstatic._static_file("/static/../musicbot/webapi.py"))
        r = self.get("/static/../.env", **{"Accept-Encoding": "gzip"})
        self.assertIn(r.status_code, (400, 404))


class VersionTest(unittest.TestCase):
    def test_index_versions_bumped_together(self):
        html = read("templates/index.html").decode("utf-8")
        vs = re.findall(r'/static/(?:js/app\.js|css/style\.css)\?v=([\w.-]+)', html)
        self.assertEqual(len(vs), 2)
        self.assertEqual(vs[0], vs[1])          # 兩個檔一起換版號


if __name__ == "__main__":
    unittest.main()
