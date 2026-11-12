"""面板 UI 煙霧測試（Playwright）：預設略過，因為要開瀏覽器、約 4 分鐘。
要跑：RUN_UI_SMOKE=1 python -m unittest tests.test_ui_smoke（或直接 python tools/check.py）。
沒裝 Playwright 也會略過，不會讓一般測試失敗。"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import check  # noqa: E402


@unittest.skipUnless(os.getenv("RUN_UI_SMOKE") == "1", "UI 煙霧測試預設略過（設 RUN_UI_SMOKE=1 或用 tools/check.py）")
@unittest.skipUnless(check.playwright_available(), "沒裝 Playwright")
class UiSmoke(unittest.TestCase):
    def test_panel_ui_smoke(self):
        proc, base = check.start_sim()
        try:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "smoke_ui.py"), os.path.join(ROOT, "smoke_out")],
                               cwd=ROOT, env=dict(os.environ, SMOKE_BASE=base, PYTHONIOENCODING="utf-8"),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
        finally:
            check.stop_sim(proc)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
