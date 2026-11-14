"""分段代理：上游中途 403 / 410 / 卡住 / 斷線時要能恢復，恢復不了就乾脆失敗（2026-11-05 後段靜音）"""
import http.client
import http.server
import os
import socketserver
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import rangeproxy  # noqa: E402

DATA = bytes(range(256)) * 30000          # ~7.3MB → 4 段
LATE = 4 * 1024 * 1024   # 這個位置之後算「後段」（剛好是第 3 段開頭）


class _Up(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = {}
    seen = []

    def log_message(self, *a): pass

    def do_GET(self):
        h = self.headers.get("Range", "")
        _Up.seen.append((self.path, h))
        a, _, b = h[6:].partition("-")
        a = int(a); b = int(b) if b else len(DATA) - 1
        m = _Up.mode
        fresh_n = 0
        if "fresh=" in self.path:
            try:
                fresh_n = int(self.path.split("fresh=")[1].split("&")[0])
            except ValueError:
                fresh_n = 1
        fresh = fresh_n >= 1
        # 冷啟動：前 N 代網址（含原始 fresh=0）一律 403，直到 resolve 升到夠新的一代
        cold = m.get("cold_403_gens")
        if cold is not None and a == 0 and fresh_n < cold:
            return self._err(403)
        if m.get("late_403") and a >= LATE and not fresh:
            return self._err(m.get("code", 403))
        if m.get("first_403") and m["first_403"] > 0:
            m["first_403"] -= 1
            return self._err(403)
        body = DATA[a:b + 1]
        self.send_response(206)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", f"bytes {a}-{b}/{len(DATA)}")
        self.end_headers()
        if m.get("stall_once") and a >= LATE:
            m["stall_once"] = False
            self.wfile.write(body[:1000]); self.wfile.flush()
            threading.Event().wait(3)        # 卡住不動（time.sleep 在測試裡被換掉了）
            self.close_connection = True
            return
        if m.get("drop_once") and a >= LATE:
            m["drop_once"] = False
            self.wfile.write(body[:5000]); self.wfile.flush()
            self.close_connection = True     # 宣告的長度沒送完就斷線
            return
        self.wfile.write(body)

    def _err(self, code):
        self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()


class _S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class RangeProxyRecovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = _S(("127.0.0.1", 0), _Up)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.up.server_address[1]}/videoplayback?clen={len(DATA)}"

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()

    def setUp(self):
        _Up.mode = {}
        _Up.seen.clear()
        self.resolved = []
        p = [mock.patch.object(rangeproxy, "eligible", side_effect=lambda u: len(DATA)),
             mock.patch.object(rangeproxy.time, "sleep", lambda s: None),
             mock.patch.object(rangeproxy, "_resolver", self.resolve)]
        for x in p:
            x.start(); self.addCleanup(x.stop)

    def resolve(self, page):
        self.resolved.append(page)
        return self.base + f"&fresh={len(self.resolved)}"

    def local(self, page="https://www.youtube.com/watch?v=NxN61sK-Y2Q"):
        return rangeproxy.input_url(self.base, page=page)

    def get(self, url, rng="bytes=0-"):
        req = urllib.request.Request(url, headers={"Range": rng})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()

    def test_late_403_refreshes_url_and_continues(self):
        _Up.mode = {"late_403": True}
        with self.assertLogs(rangeproxy.log, "INFO") as cm:
            st, body = self.get(self.local())
        self.assertEqual(body, DATA)
        self.assertEqual(self.resolved, ["https://www.youtube.com/watch?v=NxN61sK-Y2Q"])
        out = "\n".join(cm.output)
        self.assertIn("已重新解析並換上新網址", out)
        self.assertIn(f"上游 {LATE}-", out)   # 有記位元組範圍
        self.assertIn("重試成功", out)
        self.assertTrue(any("fresh=1" in p for p, _ in _Up.seen))

    def test_late_410_also_refreshes(self):
        _Up.mode = {"late_403": True, "code": 410}
        st, body = self.get(self.local())
        self.assertEqual(body, DATA)
        self.assertEqual(len(self.resolved), 1)

    def test_single_transient_403_retries_without_refresh(self):
        _Up.mode = {"first_403": 1}
        with self.assertLogs(rangeproxy.log, "INFO") as cm:
            st, body = self.get(self.local())
        self.assertEqual(body, DATA)
        self.assertEqual(self.resolved, [])
        self.assertIn("上游 0-2097151 重試成功", "\n".join(cm.output))

    def test_refresh_failure_fails_cleanly(self):
        _Up.mode = {"late_403": True}
        u = self.local()
        with mock.patch.object(rangeproxy, "_resolver", side_effect=RuntimeError("yt-dlp 壞了")), \
                self.assertLogs(rangeproxy.log, "INFO") as cm:
            with self.assertRaises(http.client.IncompleteRead):
                self.get(u)
        out = "\n".join(cm.output)
        self.assertIn("重新解析網址失敗", out)
        self.assertIn("放棄串流", out)
        # 之後的請求（FFmpeg -reconnect）直接 502 → FFmpeg 結束、播放器切歌，不會一直靜音
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.get(u, f"bytes={LATE}-")
        self.assertEqual(c.exception.code, 502)

    def test_refresh_with_different_size_is_rejected(self):
        _Up.mode = {"late_403": True}
        u = self.local()
        with mock.patch.object(rangeproxy, "eligible", side_effect=lambda x: 123 if "fresh" in x else len(DATA)), \
                self.assertLogs(rangeproxy.log, "INFO") as cm:
            with self.assertRaises(http.client.IncompleteRead):
                self.get(u)
        self.assertIn("檔案大小不同", "\n".join(cm.output))

    def test_no_page_cannot_refresh_but_fails_cleanly(self):
        _Up.mode = {"late_403": True}
        u = rangeproxy.input_url(self.base)
        with self.assertLogs(rangeproxy.log, "INFO") as cm:
            with self.assertRaises(http.client.IncompleteRead):
                self.get(u)
        self.assertIn("沒有可重新解析的歌曲網址", "\n".join(cm.output))

    def test_stalled_upstream_is_retried_from_offset(self):
        _Up.mode = {"stall_once": True}
        with mock.patch.object(rangeproxy, "READ_TIMEOUT", 1), self.assertLogs(rangeproxy.log, "INFO") as cm:
            st, body = self.get(self.local())
        self.assertEqual(body, DATA)
        out = "\n".join(cm.output)
        self.assertIn("讀取逾時", out)
        self.assertIn("重試成功", out)
        # 卡住的那段重要一次，其他段不重複
        starts = [int(h[6:].split("-")[0]) for _, h in _Up.seen]
        self.assertEqual(starts.count(LATE), 2)
        self.assertEqual(len(starts), 5)

    def test_slow_trickle_hits_chunk_deadline(self):
        _Up.mode = {}
        # RETRIES 提高後每次卡住仍會寫出一緩衝，次數夠多會湊滿；這裡壓回少量重試以確認會放棄
        with mock.patch.object(rangeproxy, "CHUNK_DEADLINE", -1), \
                mock.patch.object(rangeproxy, "RETRIES", 3), \
                self.assertLogs(rangeproxy.log, "WARNING") as cm:
            with self.assertRaises(http.client.IncompleteRead):
                self.get(self.local(), "bytes=0-300000")
        # 每次都「超時」→ 重試用完後放棄；有 log 不會默默卡住
        self.assertIn("秒內沒收完", "\n".join(cm.output))

    def test_slow_reader_is_not_mistaken_for_stall(self):
        """FFmpeg 照即時速度讀（代理寫不出去）不能被當成上游卡住"""
        class SlowOut:
            n = 0
            def write(self, b):
                SlowOut.n += len(b)
                rangeproxy.time.monotonic.__self__ if False else None
        e = {"url": self.base, "size": len(DATA), "page": None, "rlock": threading.Lock()}
        clock = [0.0]
        real = rangeproxy._GuardedWriter(SlowOut())
        orig_write = real.write
        def slow_write(b):
            clock[0] += 10.0          # 每寫一次「過了 10 秒」（被 FFmpeg 擋住）
            return orig_write(b)
        real.write = slow_write
        with mock.patch.object(rangeproxy.time, "monotonic", lambda: clock[0]):
            got = rangeproxy._fetch_into(e, 0, rangeproxy.CHUNK - 1, real)
        self.assertEqual(got, rangeproxy.CHUNK)

    def test_upstream_drop_midway_resumes(self):
        _Up.mode = {"drop_once": True}
        with self.assertLogs(rangeproxy.log, "INFO") as cm:
            st, body = self.get(self.local())
        self.assertEqual(body, DATA)
        self.assertIn("重試成功", "\n".join(cm.output), cm.output)

    def test_client_disconnect_is_not_retried(self):
        class Gone:
            failed = False
        w = rangeproxy._GuardedWriter(Gone())
        Gone.write = lambda self, b: (_ for _ in ()).throw(BrokenPipeError())
        e = {"url": self.base, "size": len(DATA), "page": None, "rlock": threading.Lock()}
        n = len(_Up.seen)
        with self.assertRaises(BrokenPipeError):
            rangeproxy._fetch_into(e, 0, 1000, w)
        self.assertEqual(len(_Up.seen) - n, 1)

    def test_cold_start_403_refreshes_twice_then_succeeds(self):
        """開播第一段：原始 + 第一次重解析都 403，第二次重解析才通（對應小半 22PF83zIW0k）"""
        _Up.mode = {"cold_403_gens": 2}   # fresh=0,1 → 403；fresh=2 → OK
        with self.assertLogs(rangeproxy.log, "INFO") as cm:
            st, body = self.get(self.local(), "bytes=0-2097151")
        self.assertEqual(body, DATA[:2097152])
        self.assertEqual(len(self.resolved), 2)
        out = "\n".join(cm.output)
        self.assertIn("已重新解析並換上新網址", out)
        self.assertIn("重試成功", out)
        self.assertTrue(any("fresh=2" in p for p, _ in _Up.seen))

    def test_cold_start_second_refresh_bypasses_min_gap(self):
        """上一輪剛換的網址又立刻 403 時，不可被 REFRESH_MIN_GAP 擋住沿用壞網址"""
        _Up.mode = {"cold_403_gens": 2}
        with mock.patch.object(rangeproxy, "REFRESH_MIN_GAP", 9999):
            st, body = self.get(self.local(), "bytes=0-1000")
        self.assertEqual(body, DATA[:1001])
        self.assertEqual(len(self.resolved), 2)

    def test_cold_start_exhausts_refreshes_fails_cleanly(self):
        """重解析用盡仍 403 → 放棄串流、後續 502，不要掛住"""
        _Up.mode = {"cold_403_gens": 99}
        u = self.local()
        with self.assertLogs(rangeproxy.log, "INFO") as cm:
            with self.assertRaises(http.client.IncompleteRead):
                self.get(u, "bytes=0-1000")
        out = "\n".join(cm.output)
        self.assertIn("放棄串流", out)
        self.assertGreaterEqual(len(self.resolved), rangeproxy.MAX_REFRESHES_COLD)
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.get(u, "bytes=0-1000")
        self.assertEqual(c.exception.code, 502)

    def test_player_passes_page_and_sets_resolver(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "musicbot", "player.py"), encoding="utf-8").read()
        self.assertIn("rangeproxy.set_resolver(", src)
        self.assertIn("prewarm.open_source(guild_id, flat_url, real_audio_url)", src)
        pw = open(os.path.join(os.path.dirname(__file__), "..", "musicbot", "prewarm.py"), encoding="utf-8").read()
        self.assertIn("rangeproxy.input_url(stream_url, page=page_url)", pw)
        self.assertIn("page=now_playing_url.get(gid)", src)
        self.assertIn("STREAM_OPEN_RETRIES", src)
        self.assertIn("開播重試", src)


class FfmpegOptions(unittest.TestCase):
    def test_rw_timeout_present(self):
        from musicbot import audio
        self.assertIn("-rw_timeout", audio.RECONNECT_OPTIONS)

    def test_prime_timeout_allows_multi_refresh(self):
        from musicbot import audio
        self.assertGreaterEqual(audio.PRIME_TIMEOUT, 30.0)


if __name__ == "__main__":
    unittest.main()
