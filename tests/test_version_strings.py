"""版本字樣：啟動訊息是 V6，專案裡不再有使用者看得到的「Music Bot V5」等舊字樣。"""
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEXT = {".py", ".js", ".css", ".html", ".json", ".md", ".txt", ".sh", ".in", ".service", ".timer", ".ps1", ".bat"}


def tracked():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.split("\n")
        files = [ROOT / f for f in out if f]
    except Exception:
        files = []
    if not files:
        files = [p for p in ROOT.rglob("*") if ".git" not in p.parts and "__pycache__" not in p.parts]
    return [p for p in files if p.is_file() and (p.suffix in TEXT or p.name.endswith(".in")) and p.name != pathlib.Path(__file__).name]


class VersionStringTest(unittest.TestCase):
    def test_startup_banner_is_v6(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('log.info("Music Bot V6 啟動中...")', src)

    def test_no_stale_v5(self):
        pat = re.compile(r"\bV5\b")
        hits = []
        for p in tracked():
            try:
                txt = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(txt.splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{p.relative_to(ROOT)}:{i}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
