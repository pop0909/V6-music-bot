import logging
import os
import unittest

os.environ.setdefault("DISCORD_TOKEN", "x")
from musicbot import webapi  # noqa: E402


def rec(m):
    return logging.LogRecord("werkzeug", 20, "", 0, "%s - - [%s] %s", ("127.0.0.1", "x", m), None)


class ForeignFilterTest(unittest.TestCase):
    def test_filters_root_post_flood_only(self):
        f = webapi._ForeignRequestFilter(5000)
        with self.assertLogs("musicbot", level="WARNING") as logs:
            res = [f.filter(rec(m)) for m in ['"POST / HTTP/1.1" 405 -'] * 50]
        self.assertEqual(res, [False] * 50)
        self.assertEqual(len(logs.output), 1)   # 只提示一次
        self.assertTrue(f.filter(rec('"GET / HTTP/1.1" 200 -')))
        self.assertTrue(f.filter(rec('"POST /api/skip HTTP/1.1" 404 -')))
        self.assertTrue(f.filter(rec('"GET /static/js/app.js HTTP/1.1" 200 -')))
        self.assertFalse(f.filter(rec('"{}POST / HTTP/1.1" 405 -')))


if __name__ == "__main__":
    unittest.main()
