"""cookies 到期提醒：只用假的 cookies 檔（fixture），不碰真的主檔。
剩 ≤7 天、≤2 天、已過期各私訊一次；重啟不重發；換新主檔重新計算；面板只給擁有者看到期日。"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import selfcheck, ytdl  # noqa: E402
from appjs import read_app_js  # noqa: E402

DAY = 86400
NOW = 1_800_000_000          # 2027-01-15 台北時間
FAKE = "FAKEVALUE"


def fixture(exp):
    """exp = {名稱: 到期 epoch}；外加不看的短效 cookie、工作階段 cookie、HttpOnly 格式"""
    lines = ["# Netscape HTTP Cookie File", "# https://curl.haxx.se/rfc/cookie_spec.html", ""]
    for i, (name, e) in enumerate(exp.items()):
        dom = ".youtube.com" if i % 2 == 0 else ".google.com"
        prefix = "#HttpOnly_" if name == "HSID" else ""
        lines.append(f"{prefix}{dom}\tTRUE\t/\tTRUE\t{e}\t{name}\t{FAKE}")
    lines.append(f".youtube.com\tTRUE\t/\tTRUE\t{NOW + 3600}\t__Secure-1PSIDTS\t{FAKE}")    # 短效輪替 → 不看
    lines.append(f".youtube.com\tTRUE\t/\tFALSE\t0\tYSC\t{FAKE}")                           # 工作階段 cookie
    return "\n".join(lines) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(selfcheck, "STATE_FILE", os.path.join(self.tmp, "selfcheck.json"))
        p.start(); self.addCleanup(p.stop)
        self.path = os.path.join(self.tmp, "cookies.txt")
        self.sent = []

    def write(self, exp):
        pathlib.Path(self.path).write_text(fixture(exp), encoding="utf-8")
        return self.path

    def send(self, ok=True):
        def f(text):
            if ok:
                self.sent.append(text)
            return ok
        return f

    def check(self, now, ok=True):
        return selfcheck.check_cookie_expiry(now=now, send=self.send(ok), path=self.path)


class ParseTest(Base):
    def test_earliest_login_cookie(self):
        self.write({"LOGIN_INFO": NOW + 300 * DAY, "SID": NOW + 20 * DAY, "HSID": NOW + 10 * DAY,
                    "__Secure-1PSID": NOW + 30 * DAY})
        info = selfcheck.cookie_expiry(self.path, now=NOW)
        self.assertEqual((info["name"], info["earliest"], info["days_left"], info["level"]), ("HSID", NOW + 10 * DAY, 10.0, None))

    def test_httponly_lines_are_cookies_not_comments(self):
        self.write({"LOGIN_INFO": NOW + DAY, "HSID": NOW + DAY})
        self.assertIn("HSID", ytdl.cookie_names(self.path))
        self.assertNotIn("Netscape HTTP Cookie File", " ".join(ytdl.cookie_names(self.path)))

    def test_same_name_two_domains_takes_earliest(self):
        txt = (f".youtube.com\tTRUE\t/\tTRUE\t{NOW + 9 * DAY}\tSID\tx\n.google.com\tTRUE\t/\tTRUE\t{NOW + 5 * DAY}\tSID\tx\n")
        pathlib.Path(self.path).write_text(txt, encoding="utf-8")
        self.assertEqual(ytdl.cookie_expiries(self.path, {"SID"}), {"SID": NOW + 5 * DAY})

    def test_none_when_no_login_cookies_or_unset(self):
        pathlib.Path(self.path).write_text(f".youtube.com\tTRUE\t/\tTRUE\t{NOW + DAY}\tVISITOR_INFO1_LIVE\tx\n", encoding="utf-8")
        self.assertIsNone(selfcheck.cookie_expiry(self.path, now=NOW))
        self.assertIsNone(selfcheck.cookie_expiry(os.path.join(self.tmp, "nope.txt"), now=NOW))
        with mock.patch.dict(os.environ, {"YTDLP_COOKIES": ""}):
            self.assertIsNone(selfcheck.cookie_expiry(now=NOW))

    def test_garbage_lines_ignored(self):
        pathlib.Path(self.path).write_text("garbage\n.youtube.com\tTRUE\t/\tTRUE\tnotanumber\tSID\tx\n\n", encoding="utf-8")
        self.assertIsNone(selfcheck.cookie_expiry(self.path, now=NOW))


class AlertTest(Base):
    def test_thresholds_once_each(self):
        self.write({"LOGIN_INFO": NOW + 400 * DAY, "SID": NOW + 10 * DAY})
        self.assertIsNone(self.check(NOW))                     # 剩 10 天：不提醒
        self.assertEqual(self.check(NOW + 3 * DAY + 1), 7)     # 剩 <7 天
        for h in range(1, 48):                                 # 之後每小時檢查都不重發
            self.assertIsNone(self.check(NOW + 3 * DAY + h * 3600))
        self.assertEqual(self.check(NOW + 8 * DAY + 1), 2)     # 剩 <2 天
        self.assertIsNone(self.check(NOW + 9 * DAY))
        self.assertEqual(self.check(NOW + 10 * DAY + 1), 0)    # 過期
        self.assertIsNone(self.check(NOW + 11 * DAY))
        self.assertEqual(len(self.sent), 3)
        self.assertIn("還剩約 7 天", self.sent[0])
        self.assertIn("SID", self.sent[0])
        self.assertIn("台北時間", self.sent[0])
        self.assertIn("已經過期", self.sent[2])
        for t in self.sent:
            self.assertNotIn(FAKE, t)                          # 不洩漏 cookie 值
            self.assertIn("Google 也可能更早", t)

    def test_jump_straight_to_severe_sends_one(self):
        self.write({"SID": NOW + DAY})
        self.assertEqual(self.check(NOW), 2)
        self.assertIsNone(self.check(NOW + 3600))              # 不再補發 7 天那則
        self.assertEqual(len(self.sent), 1)
        self.assertIn("還剩約 1 天", self.sent[0])

    def test_survives_restart_via_state_file(self):
        self.write({"SID": NOW + 5 * DAY})
        self.check(NOW)
        st = json.loads(pathlib.Path(selfcheck.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(st["cookie_expiry"], {"sig": NOW + 5 * DAY, "sent": [7]})
        self.assertIsNone(self.check(NOW + 60))                # 重啟 = 重新讀狀態檔
        self.assertEqual(len(self.sent), 1)

    def test_new_master_resets(self):
        self.write({"SID": NOW + 5 * DAY})
        self.check(NOW)
        self.write({"SID": NOW + 300 * DAY})                   # 重新匯出
        self.assertIsNone(self.check(NOW + DAY))
        self.write({"SID": NOW + 6 * DAY})                     # 又一份快過期的 → 重新提醒
        self.assertEqual(self.check(NOW + DAY), 7)
        self.assertEqual(len(self.sent), 2)

    def test_not_recorded_when_send_fails(self):
        self.write({"SID": NOW + 5 * DAY})
        self.assertIsNone(self.check(NOW, ok=False))
        self.assertEqual(self.check(NOW + 60), 7)

    def test_under_one_day_wording(self):
        self.write({"SID": NOW + 5 * 3600})
        self.check(NOW)
        self.assertIn("不到 1 天（約 5 小時）", self.sent[0])

    def test_startup_uses_alerts_with_force(self):
        self.write({"LOGIN_INFO": NOW + 400 * DAY, "SID": int(__import__("time").time()) + 3 * DAY})
        with mock.patch.dict(os.environ, {"YTDLP_COOKIES": self.path}), \
             mock.patch("musicbot.alerts.alert", return_value=True) as al:
            selfcheck.startup_cookie_check()
            selfcheck.startup_cookie_check()
        kinds = [c.args[0] for c in al.call_args_list]
        self.assertEqual(kinds, ["cookie_expiry:7"])
        self.assertTrue(al.call_args.kwargs.get("force"))


class PanelTest(Base):
    def test_status_includes_expiry_owner_only(self):
        import time as _t
        from musicbot import webapi, webauth
        self.write({"LOGIN_INFO": int(_t.time()) + 3 * DAY, "SID": int(_t.time()) + 30 * DAY})
        c = webapi.flask_app.test_client()
        ytdl.reset_cookie_runtime()
        self.addCleanup(ytdl.reset_cookie_runtime)
        with mock.patch.dict(os.environ, {"YTDLP_COOKIES": self.path}), \
             mock.patch.dict(webapi.flask_app.config, {"LOGIN_DISABLED": True}):      # = 擁有者
            r = c.get("/api/system", headers={"Host": "localhost"})
        self.assertEqual(r.status_code, 200, r.data[:200])
        ex = r.get_json()["cookies"]["expiry"]
        self.assertEqual((ex["name"], ex["level"]), ("LOGIN_INFO", 7))
        with mock.patch.dict(os.environ, {"YTDLP_COOKIES": self.path}), \
             mock.patch.dict(webapi.flask_app.config, {"LOGIN_DISABLED": False}), \
             mock.patch.object(webauth, "parse_cookie", return_value=({"id": "m1", "name": "y", "owner": False}, _t.time())):
            r = c.get("/api/system", headers={"Host": "localhost"})
        self.assertEqual(r.status_code, 200, r.data[:200])
        self.assertTrue(r.get_json()["cookies"]["configured"])
        self.assertNotIn("expiry", r.get_json()["cookies"])

    def test_frontend_shows_expiry(self):
        js = read_app_js()
        self.assertIn("ck.expiry", js)
        self.assertIn("快過期了", js)


if __name__ == "__main__":
    unittest.main()
