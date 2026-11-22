"""每日 yt-dlp 更新排程：台北凌晨 5 點；有人在聽就跳過；版本有變且沒人在聽才重啟 bot
- 實際執行 deploy/update-ytdlp.sh（假的 python / pip / systemctl + 本機假面板），不連外、不動真的 venv
- dcbot 用的 unit 檔要跟範本一致；擁有者金鑰的 /api/ext/status 會回 voice_guilds
執行：python -m unittest discover -s tests
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from musicbot import webapi, extapi, extmembers, state  # noqa: E402

SCRIPT = os.path.join(ROOT, "deploy", "update-ytdlp.sh")
SYSD = os.path.join(ROOT, "deploy", "systemd")
TOK = "o" * 40


def read(*p):
    with open(os.path.join(*p), encoding="utf-8") as f:
        return f.read()


class UnitFiles(unittest.TestCase):
    def render(self, name):
        t = read(SYSD, name + ".in")
        return t.replace("@APP_DIR@", "/root/v6").replace("@PYTHON@", "/root/v6/venv/bin/python").replace("@RUN_USER@", "root")

    def test_dcbot_units_match_templates(self):
        for name in ("musicbot-update.service", "musicbot-update.timer"):
            got = read(SYSD, "dcbot", name)
            first, rest = got.split("\n", 1)
            self.assertTrue(first.startswith("# 由 deploy/systemd/"), name)
            self.assertEqual(rest, self.render(name), f"{name}：改了範本要重新產生 deploy/systemd/dcbot/ 的檔")

    def test_timer_0500_taipei(self):
        t = read(SYSD, "dcbot", "musicbot-update.timer")
        self.assertIn("\nOnCalendar=*-*-* 05:00:00 Asia/Taipei\n", t)
        self.assertNotIn("RandomizedDelaySec", t)
        self.assertIn("Persistent=true", t)
        self.assertIn("WantedBy=timers.target", t)

    def test_service_reads_env_and_uses_venv(self):
        s = read(SYSD, "dcbot", "musicbot-update.service")
        self.assertIn("EnvironmentFile=-/root/v6/.env", s)
        self.assertIn("MUSICBOT_PYTHON=/root/v6/venv/bin/python", s)
        self.assertIn("MUSICBOT_SERVICE=musicbot.service", s)
        self.assertIn("ExecStart=/bin/bash /root/v6/deploy/update-ytdlp.sh --restart-if-changed", s)
        self.assertIn("Type=oneshot", s)
        self.assertNotIn("@", s)

    def test_script_executable_in_git(self):
        r = subprocess.run(["git", "ls-files", "-s", "deploy/update-ytdlp.sh"], cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout:
            self.skipTest("沒有 git")
        self.assertTrue(r.stdout.startswith("100755"), r.stdout)


class ExtStatusVoiceGuilds(unittest.TestCase):
    def setUp(self):
        self.c = webapi.flask_app.test_client()
        self.tmp = tempfile.mkdtemp()
        self.p = [mock.patch.dict(os.environ, {"EXT_TOKEN": TOK}), mock.patch.object(extapi, "_pick_guild", return_value=7),
                  mock.patch.object(extmembers, "FILE", os.path.join(self.tmp, "ext_members.json"))]
        for p in self.p: p.start()
        extmembers._cache.update(mtime=None, members=[])
        self.saved = dict(state.voice_clients)
        state.voice_clients.clear()

    def tearDown(self):
        state.voice_clients.clear()
        state.voice_clients.update(self.saved)
        for p in reversed(self.p): p.stop()
        extmembers._cache.update(mtime=None, members=[])
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, tok=TOK):
        return self.c.get("/api/ext/status", headers={"Host": "127.0.0.1:5000", "X-Ext-Token": tok}).get_json()

    def test_owner_sees_count(self):
        self.assertEqual(self.get()["voice_guilds"], 0)
        a, b, broken = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        a.is_connected.return_value = True
        b.is_connected.return_value = False
        broken.is_connected.side_effect = RuntimeError("x")      # 判斷不出來 → 算在用
        state.voice_clients.update({1: a, 2: b, 3: broken})
        self.assertEqual(self.get()["voice_guilds"], 2)

    def test_member_does_not_see_count(self):
        _, tok = extmembers.create("朋友")
        j = self.get(tok)
        self.assertEqual(j["status"], "ok")
        self.assertNotIn("voice_guilds", j)


class _Panel(BaseHTTPRequestHandler):
    replies = []          # 依序回應；用完就重複最後一個
    seen = []

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get("X-Ext-Token")))
        code, body = self.replies[min(len(self.seen) - 1, len(self.replies) - 1)]
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


IDLE = (200, {"status": "ok", "owner": True, "voice_guilds": 0})
BUSY = (200, {"status": "ok", "owner": True, "voice_guilds": 1})


@unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "需要 Linux 的 bash")
class UpdateScript(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="v6upd-")
        self.ver = os.path.join(self.d, "ver")
        self.calls = os.path.join(self.d, "calls")
        with open(self.ver, "w") as f:
            f.write("2026.11.01")
        # 假 python：-c 查版本讀檔；-m pip 寫 calls 並依 NEWVER 改版本；其他（bot_busy 的 stdin 腳本）交給真的 Python
        fake_py = os.path.join(self.d, "python")
        self._w(fake_py, f"""#!/bin/bash
if [ "$1" = "-c" ]; then cat "{self.ver}"; exit 0; fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then echo pip >> "{self.calls}"; [ -n "${{NEWVER:-}}" ] && echo "$NEWVER" > "{self.ver}"; exit 0; fi
exec "{sys.executable}" "$@"
""")
        bindir = os.path.join(self.d, "bin")
        os.makedirs(bindir)
        self._w(os.path.join(bindir, "systemctl"), f"""#!/bin/bash
echo "systemctl $*" >> "{self.calls}"
if [ "$1" = "is-active" ] || [ "$2" = "is-active" ]; then exit "${{ACTIVE_RC:-0}}"; fi
exit 0
""")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Panel)
        _Panel.seen = []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.env = dict(PATH=bindir + os.pathsep + os.environ.get("PATH", ""), MUSICBOT_PYTHON=fake_py,
                        MUSICBOT_DIR=self.d, MUSICBOT_USER="", WEB_PORT=str(self.srv.server_address[1]),
                        EXT_TOKEN=TOK, MUSICBOT_DEFER_STEP="0")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.d, ignore_errors=True)

    def _w(self, p, txt):
        with open(p, "w") as f:
            f.write(txt)
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)

    def run_script(self, replies, **env):
        _Panel.replies = replies
        e = dict(self.env, **env)
        r = subprocess.run(["bash", SCRIPT, "--restart-if-changed"], env=e, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = []
        if os.path.exists(self.calls):
            with open(self.calls) as f:
                calls = f.read().split("\n")
        return r.stdout, [c for c in calls if c]

    def restarted(self, calls):
        return any("try-restart" in c for c in calls)

    def test_idle_and_changed_restarts(self):
        out, calls = self.run_script([IDLE], NEWVER="2026.11.20")
        self.assertIn("pip", calls)
        self.assertTrue(self.restarted(calls), out)
        self.assertTrue(all(p == "/api/ext/status" and t == TOK for p, t in _Panel.seen))
        self.assertEqual(len(_Panel.seen), 2)          # 更新前、重啟前各問一次

    def test_idle_unchanged_no_restart(self):
        out, calls = self.run_script([IDLE])
        self.assertIn("pip", calls)
        self.assertFalse(self.restarted(calls))
        self.assertIn("已是最新版", out)

    def test_busy_skips_everything(self):
        out, calls = self.run_script([BUSY], NEWVER="2026.11.20")
        self.assertNotIn("pip", calls)
        self.assertFalse(self.restarted(calls))
        self.assertIn("今天跳過", out)

    def test_busy_then_idle_with_defer(self):
        out, calls = self.run_script([BUSY, IDLE], NEWVER="2026.11.20", MUSICBOT_DEFER_MAX="2")
        self.assertIn("pip", calls)
        self.assertTrue(self.restarted(calls))

    def test_someone_starts_listening_during_update(self):
        out, calls = self.run_script([IDLE, BUSY], NEWVER="2026.11.20")
        self.assertIn("pip", calls)
        self.assertFalse(self.restarted(calls))
        self.assertIn("不重啟", out)

    def test_any_doubt_counts_as_busy(self):
        cases = {
            "no_token": dict(EXT_TOKEN=""),
            "http_401": dict(),
            "not_owner": dict(),
            "bad_json": dict(),
            "missing_field": dict(),
            "bool_field": dict(),
        }
        replies = {
            "no_token": [IDLE],
            "http_401": [(401, {"status": "error"})],
            "not_owner": [(200, {"status": "ok", "owner": False, "voice_guilds": 0})],
            "bad_json": [(200, b"<html>")],
            "missing_field": [(200, {"status": "ok", "owner": True})],
            "bool_field": [(200, {"status": "ok", "owner": True, "voice_guilds": False})],
        }
        for name, env in cases.items():
            with self.subTest(name):
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                out, calls = self.run_script(replies[name], NEWVER="2026.11.20", **env)
                self.assertNotIn("pip", calls, name)
                self.assertFalse(self.restarted(calls), name)

    def test_panel_down_counts_as_busy(self):
        out, calls = self.run_script([IDLE], NEWVER="2026.11.20", WEB_PORT="1")
        self.assertNotIn("pip", calls)
        self.assertFalse(self.restarted(calls))

    def test_service_stopped_updates_without_restart(self):
        out, calls = self.run_script([BUSY], NEWVER="2026.11.20", ACTIVE_RC="3")
        self.assertIn("pip", calls)
        self.assertFalse(self.restarted(calls))
        self.assertEqual(_Panel.seen, [])

    def test_quoted_token_from_env_file(self):
        out, calls = self.run_script([IDLE], EXT_TOKEN=f'"{TOK}"')
        self.assertEqual(_Panel.seen[0][1], TOK)


if __name__ == "__main__":
    unittest.main()
