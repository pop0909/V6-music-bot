"""換行格式：.gitattributes 規定 LF；程式 / 範本 / 靜態檔不能有 CR（部署到 Linux 伺服器時 md5 比對才一致）
執行：python -m unittest discover -s tests
"""
import os
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DIRS = ("musicbot", "templates", "static", "tools", "tests", "deploy", "chrome-extension")
EXTS = (".py", ".html", ".js", ".css", ".json", ".sh", ".md", ".txt", ".in")


class LineEndings(unittest.TestCase):
    def test_gitattributes_lf(self):
        with open(os.path.join(ROOT, ".gitattributes"), encoding="utf-8") as f:
            lines = [ln.split("#")[0].split() for ln in f]
        self.assertIn(["*", "text=auto", "eol=lf"], lines)
        self.assertIn(["*.png", "binary"], lines)

    def test_no_crlf(self):
        bad = []
        for d in DIRS:
            for root, dirs, files in os.walk(os.path.join(ROOT, d)):
                dirs[:] = [x for x in dirs if x not in ("__pycache__", "node_modules")]
                for name in files:
                    if name.endswith(EXTS):
                        p = os.path.join(root, name)
                        with open(p, "rb") as f:
                            if b"\r" in f.read():
                                bad.append(os.path.relpath(p, ROOT))
        for name in ("main.py", "requirements.txt"):
            p = os.path.join(ROOT, name)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    if b"\r" in f.read():
                        bad.append(name)
        self.assertEqual(bad, [], "這些檔案有 Windows 換行（CRLF）；請改成 LF（git add --renormalize . 後重新取出）")


if __name__ == "__main__":
    unittest.main()
