"""本地分段代理（避開 YouTube 對 Range: bytes=0- 的限速）"""
import http.server
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import unittest
import urllib.request
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import rangeproxy  # noqa: E402

DATA = bytes(range(256)) * 40000          # ~10MB


class _Up(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a): pass

    def do_GET(self):
        h = self.headers.get("Range", "")
        _Up.seen.append(h)
        a, _, b = h[6:].partition("-")
        a = int(a); b = int(b) if b else len(DATA) - 1
        body = DATA[a:b + 1]
        self.send_response(206)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", f"bytes {a}-{b}/{len(DATA)}")
        self.end_headers()
        self.wfile.write(body)


class _S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class RangeProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = _S(("127.0.0.1", 0), _Up)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.up_url = f"http://127.0.0.1:{cls.up.server_address[1]}/videoplayback?clen={len(DATA)}"

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()

    def setUp(self):
        _Up.seen.clear()

    def local(self):
        with mock.patch.object(rangeproxy, "eligible", return_value=len(DATA)):
            return rangeproxy.input_url(self.up_url)

    def get(self, url, rng=None):
        req = urllib.request.Request(url, headers={"Range": rng} if rng else {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers, r.read()

    def test_eligibility(self):
        g = "https://rr1---sn-x.googlevideo.com/videoplayback?itag=251&clen=31255908&dur=2270"
        self.assertEqual(rangeproxy.eligible(g), 31255908)
        self.assertIsNone(rangeproxy.eligible("https://rr1---sn-x.googlevideo.com/videoplayback?itag=251"))   # 沒 clen
        self.assertIsNone(rangeproxy.eligible("https://manifest.googlevideo.com/api/manifest/hls_playlist/x/index.m3u8"))
        self.assertIsNone(rangeproxy.eligible("https://rr1---sn-x.googlevideo.com/videoplayback?clen=5&live=1"))
        self.assertIsNone(rangeproxy.eligible("https://example.com/a.webm?clen=5"))
        self.assertEqual(rangeproxy.input_url("https://example.com/a.mp3"), "https://example.com/a.mp3")

    def test_open_ended_range_is_split_into_bounded_chunks(self):
        st, h, body = self.get(self.local(), "bytes=0-")
        self.assertEqual(st, 206)
        self.assertEqual(body, DATA)
        self.assertEqual(h["Content-Range"], f"bytes 0-{len(DATA) - 1}/{len(DATA)}")
        self.assertTrue(_Up.seen and all(r.split("-")[1] for r in _Up.seen))      # 上游每次都有結尾
        sizes = [int(r[6:].split("-")[1]) - int(r[6:].split("-")[0]) + 1 for r in _Up.seen]
        self.assertLessEqual(max(sizes), rangeproxy.CHUNK)

    def test_seek_range_and_no_range(self):
        u = self.local()
        st, h, body = self.get(u, "bytes=5000000-5000099")
        self.assertEqual((st, body), (206, DATA[5000000:5000100]))
        st, h, body = self.get(u, "bytes=9999000-")
        self.assertEqual(body, DATA[9999000:])
        st, h, body = self.get(u)
        self.assertEqual((st, len(body)), (200, len(DATA)))

    def test_bad_range_and_unknown_token(self):
        u = self.local()
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.get(u, f"bytes={len(DATA) + 5}-")
        self.assertEqual(c.exception.code, 416)
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.get(u.rsplit("/", 1)[0] + "/nope")
        self.assertEqual(c.exception.code, 404)

    def test_old_tokens_expire(self):
        u = self.local()
        tok = u.rsplit("/", 1)[1]
        with rangeproxy._lock:
            rangeproxy._streams[tok]["ts"] -= rangeproxy.TTL + 1
        self.local()                                     # 新註冊時順便清掉過期的
        self.assertNotIn(tok, rangeproxy._streams)

    def test_client_disconnect_is_quiet(self):
        import sys
        srv = rangeproxy._Server.__new__(rangeproxy._Server)
        with mock.patch.object(rangeproxy.log, "warning") as w:
            try:
                raise ConnectionResetError(104, "reset")
            except ConnectionResetError:
                srv.handle_error(None, None)
            w.assert_not_called()
            try:
                raise ValueError("x")
            except ValueError:
                srv.handle_error(None, None)
            w.assert_called_once()

    def test_player_uses_proxy_for_ffmpeg_input(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "musicbot", "player.py"), encoding="utf-8").read()
        self.assertEqual(src.count("rangeproxy.input_url("), 2)          # 起播 + 跳轉（預先解析共用起播路徑）


@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
class RangeProxyFfmpeg(unittest.TestCase):
    """真的讓 FFmpeg 透過代理讀一個 webm/opus 檔：從頭讀、-ss 跳轉都要正常"""
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.d = tempfile.mkdtemp()
        cls.f = os.path.join(cls.d, "a.webm")
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=30",
                        "-c:a", "libopus", "-b:a", "96k", cls.f], check=True)
        cls.data = open(cls.f, "rb").read()
        data = cls.data

        class H(_Up):
            def do_GET(self):
                h = self.headers.get("Range", "")
                a, _, b = h[6:].partition("-")
                a = int(a); b = int(b) if b else len(data) - 1
                body = data[a:b + 1]
                self.send_response(206); self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}"); self.end_headers()
                self.wfile.write(body)
        cls.up = _S(("127.0.0.1", 0), H)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.up.server_address[1]}/v?clen={len(data)}"

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown(); shutil.rmtree(cls.d, ignore_errors=True)

    def decode(self, *pre):
        with mock.patch.object(rangeproxy, "eligible", return_value=len(self.data)):
            u = rangeproxy.input_url(self.url)
        p = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", *pre, "-reconnect", "1", "-reconnect_streamed", "1",
                            "-multiple_requests", "1", "-i", u, "-vn", "-f", "s16le", "-ar", "48000", "-ac", "2", "-"],
                           capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        return len(p.stdout) / (48000 * 4)

    def test_full_and_seek(self):
        self.assertAlmostEqual(self.decode(), 30, delta=0.5)
        self.assertAlmostEqual(self.decode("-ss", "20"), 10, delta=0.5)


if __name__ == "__main__":
    unittest.main()
