"""靜態檔版本 ?v=：同一個值不能對應到不同內容的 style.css / app.js（舊值被重用 → 瀏覽器快取拿到舊檔）。
比對 git 歷史裡每個改過這些檔的 commit；沒有 git 就略過。"""
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAT = re.compile(r"/static/(?:css/style\.css|css/icons\.css|js/app\.js)\?v=([0-9A-Za-z_.-]+)")
FILES = ("static/css/style.css", "static/css/icons.css", "static/js/app.js")


def git(*args, inp=None):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, input=inp, timeout=60, encoding="utf-8")


class StaticVersionTest(unittest.TestCase):
    def test_css_and_js_share_one_version(self):
        vals = set(PAT.findall((ROOT / "templates" / "index.html").read_text(encoding="utf-8")))
        self.assertEqual(len(vals), 1, vals)

    def test_version_never_reused_for_different_content(self):
        try:
            log = git("log", "--format=%H", "--", "templates/index.html", *FILES)
        except Exception:
            self.skipTest("沒有 git")
        if log.returncode != 0 or not log.stdout.strip():
            self.skipTest("不是 git 工作目錄")
        cur_v = PAT.findall((ROOT / "templates" / "index.html").read_text(encoding="utf-8"))[0]
        cur = {}
        for f in FILES:
            r = git("hash-object", "--path", f, f)          # 套用跟 commit 一樣的換行轉換
            cur[f] = r.stdout.strip()
        bad = []
        for c in log.stdout.split():
            html = git("show", f"{c}:templates/index.html")
            if html.returncode != 0 or cur_v not in PAT.findall(html.stdout):
                continue
            for f in FILES:
                r = git("rev-parse", "-q", "--verify", f"{c}:{f}")
                old = r.stdout.strip() if r.returncode == 0 else ""
                if old and old != cur[f]:
                    bad.append(f"{c[:7]} 的 {f} 內容不同但也用 ?v={cur_v}")
        self.assertEqual(bad, [], "改了 CSS / JS 要換新的 ?v 值")


if __name__ == "__main__":
    unittest.main()
