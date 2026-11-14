"""正式網頁伺服器（waitress）：真的開一個 waitress 打請求，確認
- Cloudflare 標頭（CF-Connecting-IP、X-Forwarded-Proto）原封不動傳到程式 → 輸錯鎖定、secure cookie 照舊
- SSE 每一筆立刻送出（不會被緩衝），斷線後名額會還回來；名額滿了回 503，一般請求不受影響
- 存取紀錄照舊寫到 "werkzeug" logger（本機雜訊過濾仍有效）
執行：python -m unittest discover -s tests
"""
import http.client
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from flask import Flask, Response, request, jsonify  # noqa: E402
from musicbot import webserver, webapi, webauth, extmembers, userlists  # noqa: E402

try:
    import waitress  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

OWN = "w" * 40


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, app):
        self.port = free_port()
        self.srv = webserver.create(app, "127.0.0.1", self.port)
        self.t = threading.Thread(target=self.srv.run, daemon=True)
        self.t.start()

    def conn(self, timeout=10):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def close(self):
        try:
            self.srv.task_dispatcher.shutdown(timeout=3)
        except Exception:
            pass
        self.srv.close()


def probe_app():
    app = Flask("probe")
    hold = threading.Event()

    @app.route("/hdr")
    def hdr():
        return jsonify(cf=request.headers.get("CF-Connecting-IP"), proto=request.headers.get("X-Forwarded-Proto"),
                       xff=request.headers.get("X-Forwarded-For"), remote=request.remote_addr, https=webauth.is_https(request),
                       ip=webauth.client_ip(request))

    @app.route("/sse")
    def sse():
        release = webserver.sse_acquire()
        if release is None:
            return jsonify(error="busy"), 503

        def gen():
            yield "data: first\n\n"
            while not hold.wait(0.05):
                yield ": ping\n\n"
            yield "data: second\n\n"
        r = Response(gen(), mimetype="text/event-stream")
        r.call_on_close(release)
        return r

    @app.route("/ok")
    def ok():
        return "ok"
    app.hold = hold
    return app


@unittest.skipUnless(HAVE, "沒裝 waitress")
class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.app = probe_app()
        self.s = Server(self.app)

    def tearDown(self):
        self.app.hold.set()
        self.s.close()

    def get_json(self, path, headers=None):
        c = self.s.conn()
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        import json
        out = json.loads(r.read())
        c.close()
        return out

    def test_cloudflare_headers_pass_through(self):
        d = self.get_json("/hdr", {"CF-Connecting-IP": "203.0.113.9", "X-Forwarded-Proto": "https",
                                   "X-Forwarded-For": "198.51.100.1, 10.0.0.1"})
        self.assertEqual(d["cf"], "203.0.113.9")
        self.assertEqual(d["proto"], "https")
        self.assertEqual(d["xff"], "198.51.100.1, 10.0.0.1")
        self.assertTrue(d["https"])
        self.assertEqual(d["ip"], "203.0.113.9")
        self.assertEqual(d["remote"], "127.0.0.1")

    def test_xff_fallback_and_plain(self):
        self.assertEqual(self.get_json("/hdr", {"X-Forwarded-For": "198.51.100.7"})["ip"], "198.51.100.7")
        d = self.get_json("/hdr")
        self.assertEqual(d["ip"], "127.0.0.1")
        self.assertFalse(d["https"])

    def test_sse_streams_immediately_and_releases_slot(self):
        before = webserver.sse_active()
        c = self.s.conn()
        c.request("GET", "/sse")
        r = c.getresponse()
        t0 = time.monotonic()
        self.assertEqual(r.readline(), b"data: first\n")
        self.assertLess(time.monotonic() - t0, 2)          # 沒有被緩衝住
        self.assertEqual(webserver.sse_active(), before + 1)
        r.close()
        c.close()                                           # 客戶端斷線 → 下一次寫入失敗 → close() → 還名額
        for _ in range(100):
            if webserver.sse_active() == before:
                break
            time.sleep(0.05)
        self.assertEqual(webserver.sse_active(), before)

    def test_sse_cap_returns_503_and_normal_requests_still_served(self):
        conns = []
        with mock.patch.object(webserver, "sse_max", return_value=webserver.sse_active() + 3):
            for _ in range(3):
                c = self.s.conn()
                c.request("GET", "/sse")
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                r.readline()
                conns.append((c, r))
            c = self.s.conn()
            c.request("GET", "/sse")
            r = c.getresponse()
            self.assertEqual(r.status, 503)
            r.read()
            c.close()
            # SSE 佔著執行緒時，一般請求照樣馬上回應
            t0 = time.monotonic()
            c = self.s.conn()
            c.request("GET", "/ok")
            self.assertEqual(c.getresponse().read(), b"ok")
            c.close()
            self.assertLess(time.monotonic() - t0, 2)
        for c, _ in conns:
            c.close()

    def test_access_log_format_kept(self):
        logger = logging.getLogger("werkzeug")
        recs = []
        h = logging.Handler()
        h.emit = recs.append
        logger.addHandler(h)
        old = logger.level
        logger.setLevel(logging.INFO)
        try:
            c = self.s.conn()
            c.request("GET", "/ok?x=1")
            c.getresponse().read()
            c.close()
            time.sleep(0.1)
        finally:
            logger.removeHandler(h)
            logger.setLevel(old)
        msgs = [r.getMessage() for r in recs]
        self.assertTrue(any('"GET /ok?x=1 HTTP/1.1" 200 -' in m and m.startswith("127.0.0.1 - - [") for m in msgs), msgs)

    def test_large_body_accepted(self):
        app = self.app

        @app.route("/big", methods=["POST"])
        def big():
            return jsonify(n=len(request.get_data()))
        body = b"x" * (3 * 1024 * 1024)
        c = self.s.conn()
        c.request("POST", "/big", body=body, headers={"Content-Type": "application/octet-stream"})
        import json
        self.assertEqual(json.loads(c.getresponse().read())["n"], len(body))
        c.close()


@unittest.skipUnless(HAVE, "沒裝 waitress")
class RealPanelTest(unittest.TestCase):
    """真正的面板 app 跑在 waitress 上：輸錯鎖定看 CF-Connecting-IP、https 登入發 secure cookie、SSE 會推資料"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = [mock.patch.object(extmembers, "FILE", os.path.join(self.tmp, "m.json")),
                  mock.patch.object(userlists, "FILE", os.path.join(self.tmp, "ul.json")),
                  mock.patch.object(webauth, "SECRET_FILE", os.path.join(self.tmp, "panel_secret")),
                  mock.patch.dict(os.environ, {"EXT_TOKEN": OWN, "PANEL_SECRET": ""})]
        for p in self.p:
            p.start()
        extmembers._cache.update(mtime=None, members=[])
        userlists._reset_cache()
        webauth.reset_all()
        self._ld = webapi.flask_app.config.get("LOGIN_DISABLED")
        webapi.flask_app.config["LOGIN_DISABLED"] = False
        self.s = Server(webapi.flask_app)

    def tearDown(self):
        self.s.close()
        webapi.flask_app.config["LOGIN_DISABLED"] = self._ld
        for p in reversed(self.p):
            p.stop()
        extmembers._cache.update(mtime=None, members=[])
        userlists._reset_cache()
        webauth.reset_all()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post_login(self, key, ip, https=False):
        import urllib.parse
        h = {"Host": "localhost", "CF-Connecting-IP": ip, "Content-Type": "application/x-www-form-urlencoded"}
        if https:
            h["X-Forwarded-Proto"] = "https"
        c = self.s.conn()
        c.request("POST", "/login", body=urllib.parse.urlencode({"key": key}), headers=h)
        r = c.getresponse()
        r.read()
        c.close()
        return r

    def test_lockout_keyed_on_cf_ip(self):
        for _ in range(5):
            self.post_login("wrong-key-xxxxxxxx", "203.0.113.50")
        self.assertGreater(webauth.locked_for("203.0.113.50"), 0)
        self.assertEqual(webauth.locked_for("127.0.0.1"), 0)

    def test_https_login_sets_secure_cookie_and_sse_works(self):
        r = self.post_login(OWN, "203.0.113.51", https=True)
        cookie = r.getheader("Set-Cookie") or ""
        self.assertIn("Secure", cookie)
        r2 = self.post_login(OWN, "203.0.113.52", https=False)
        self.assertNotIn("Secure", r2.getheader("Set-Cookie") or "")
        val = cookie.split(";", 1)[0]
        before = webserver.sse_active()
        c = self.s.conn()
        c.request("GET", "/api/progress-stream?guild_id=0", headers={"Host": "localhost", "Cookie": val})
        resp = c.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type", "").startswith("text/event-stream"))
        line = resp.readline()
        self.assertTrue(line.startswith(b"data: "), line)
        self.assertEqual(webserver.sse_active(), before + 1)
        resp.close()
        c.close()
        t0 = time.monotonic()
        for _ in range(100):                 # waitress 會發現斷線，SSE 迴圈約 1 秒內結束
            if webserver.sse_active() == before:
                break
            time.sleep(0.05)
        self.assertLess(time.monotonic() - t0, 4)
        self.assertEqual(webserver.sse_active(), before)


class ConfigTest(unittest.TestCase):
    def test_threads_and_sse_cap(self):
        with mock.patch.dict(os.environ, {"WEB_THREADS": ""}):
            self.assertEqual(webserver.threads(), 24)
            self.assertEqual(webserver.sse_max(), 16)
        with mock.patch.dict(os.environ, {"WEB_THREADS": "abc"}):
            self.assertEqual(webserver.threads(), 24)
        with mock.patch.dict(os.environ, {"WEB_THREADS": "2"}):
            self.assertEqual(webserver.threads(), webserver.RESERVED + 4)
        with mock.patch.dict(os.environ, {"WEB_THREADS": "999"}):
            self.assertEqual(webserver.threads(), 64)

    def test_release_only_once(self):
        before = webserver.sse_active()
        rel = webserver.sse_acquire()
        self.assertEqual(webserver.sse_active(), before + 1)
        rel(); rel()
        self.assertEqual(webserver.sse_active(), before)

    def test_fallback_to_flask_when_waitress_missing(self):
        app = mock.Mock()
        with mock.patch.object(webserver, "create", side_effect=ImportError("no waitress")):
            webserver.serve(app, "127.0.0.1", 1)
        app.run.assert_called_once()

    def test_start_flask_uses_webserver(self):
        with mock.patch.object(webserver, "serve") as s:
            webapi.start_flask()
        s.assert_called_once()
        self.assertIs(s.call_args[0][0], webapi.flask_app)

    def test_requirements_list_waitress(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as f:
            self.assertRegex(f.read(), r"(?m)^waitress")


if __name__ == "__main__":
    unittest.main()
