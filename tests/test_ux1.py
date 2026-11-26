"""T-ux1：載入前不顯示假狀態、清空佇列要確認、跑馬燈／焦點框／歌單名稱上限、
模式說明、窄視窗展開畫面、觸控與鍵盤時顯示佇列按鈕。
執行：python -m unittest discover -s tests
"""
import os
import re
import tempfile
import unittest

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import sys
sys.path.insert(0, ROOT)

from musicbot import userlists  # noqa: E402
from appjs import read_app_js  # noqa: E402


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


class LoadingState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html, cls.js, cls.css = read("templates", "index.html"), read_app_js(), read("static", "css", "style.css")

    def test_html_starts_neutral(self):
        self.assertIn('class="ui-loading"', self.html)
        self.assertIn('id="statusText">載入中…<', self.html)
        self.assertIn('id="playlistCount">載入中…<', self.html)
        self.assertIn('id="volumeValue">—<', self.html)
        vol = re.search(r'<input[^>]*id="volumeSlider"[^>]*>', self.html).group(0)
        self.assertIn("disabled", vol)
        self.assertIn('aria-disabled="true"', vol)
        self.assertNotIn('id="statusText">就緒<', self.html)
        self.assertNotIn('id="playlistCount">0 首<', self.html)
        self.assertNotIn('id="volumeValue">100%<', self.html)

    def test_js_waits_for_real_volume(self):
        self.assertIn("function applyKnownVolume(", self.js)
        self.assertIn("function markUiReady(", self.js)
        self.assertIn("volKnown", self.js)
        self.assertIn("classList.remove('ui-loading')", self.js)
        self.assertIn("if (!volKnown)", self.js)
        self.assertNotRegex(self.js, r"state\.volume = data\.volume \|\| 100")
        self.assertIn("el.playlistCount.textContent = '載入中…'", self.js)

    def test_css_hides_slider_until_ready(self):
        self.assertIn("#volumeSlider:disabled { visibility: hidden; }", self.css)
        self.assertIn(".volume-row:has(#volumeSlider:disabled) { pointer-events: none; }", self.css)
        self.assertIn("el.statusText.textContent === '載入中…'", read_app_js())


class ClearQueueConfirm(unittest.TestCase):
    def test_uses_panel_dialog_and_disables_when_empty(self):
        js, html = read_app_js(), read("templates", "index.html")
        self.assertIn("title: '清空佇列'", js)
        self.assertIn("清空佇列？將移除 ${n} 首", js)
        self.assertIn("askDialog({", js)
        self.assertIn("function syncQueueClearBtn(", js)
        self.assertIn("b.disabled = n === 0", js)
        btn = re.search(r'<button[^>]*id="npBtnQueueClear"[^>]*>', html).group(0)
        self.assertIn("disabled", btn)
        self.assertNotRegex(read("static", "js", "app", "07-tabs-init.js"), r"(?<![\w.])confirm\(")
        css = read("static", "css", "style.css")
        self.assertGreater(int(__import__("re").search(r"\.modal-overlay \{[^}]*z-index: (\d+)", css).group(1)), 60)


class Marquee(unittest.TestCase):
    def test_mask_and_segment_padding_align(self):
        css, js = read("static", "css", "style.css"), read("static", "js", "app", "03-artwork.js")
        self.assertIn("function checkMarquee(", js)
        self.assertIn("need > w + 1", js)
        self.assertIn(".song-title.marquee, .pb-title.marquee", css)
        self.assertIn("transparent 0, #000 1em, #000 calc(100% - 1em), transparent", css)
        self.assertIn("padding-left: 1em", css)
        self.assertIn(".np-view .song-title.marquee .marquee-inner", css)
        self.assertIn("animation: marquee var(--mq-dur, 16s)", css)


class FocusRing(unittest.TestCase):
    def test_focus_visible_not_mouse(self):
        css = read("static", "css", "style.css")
        self.assertIn("button:focus-visible", css[css.index("ux1（2026-11-26）"):])
        self.assertIn(".tab-btn:focus-visible", css)
        self.assertIn(".item-btn:focus-visible", css)
        self.assertIn(".volume-slider:focus-visible", css)
        self.assertIn(".list-item:focus-visible", css)
        self.assertIn("outline: 2px solid var(--gold)", css)


class PlaylistNameLimit(unittest.TestCase):
    def setUp(self):
        self._prev = userlists.FILE
        self.td = tempfile.mkdtemp()
        userlists.FILE = os.path.join(self.td, "p.json")
        userlists._cache["data"] = None
        userlists._cache["path"] = None

    def tearDown(self):
        userlists.FILE = self._prev
        userlists._cache["data"] = None
        userlists._cache["path"] = None

    def test_constants_and_client(self):
        self.assertEqual(userlists.NAME_MAX, 40)
        js, html = read("static", "js", "app", "11-mylist.js"), read("templates", "index.html")
        self.assertIn("maxLength: 40", js)
        self.assertIn("function nameLimitMsg(", js)
        self.assertIn("id=\"askCount\"", html)
        self.assertIn("function syncCount(", js)
        self.assertIn('title="重新命名"', js)
        self.assertIn("重新命名</button>", js)
        self.assertNotIn('title="改名"', js)
        css = read("static", "css", "style.css")
        self.assertIn("text-overflow: ellipsis", css)

    def test_create_rename_reject_too_long(self):
        ok = userlists.create("a", "啊" * 40)
        self.assertEqual(len(ok["name"]), 40)
        with self.assertRaisesRegex(ValueError, "最多 40 個字"):
            userlists.create("a", "啊" * 41)
        with self.assertRaisesRegex(ValueError, "最多 40 個字"):
            userlists.rename("a", ok["id"], "x" * 41)
        userlists.rename("a", ok["id"], "短名")
        self.assertEqual(userlists.get_playlist("a", ok["id"])["name"], "短名")

    def test_does_not_silently_truncate_user_names(self):
        with self.assertRaises(ValueError):
            userlists._require_name("n" * 41)
        self.assertEqual(len(userlists._clean_name("n" * 80, limit=40)), 40)

    def test_whitespace_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "請輸入歌單名稱"):
            userlists.create("a", "   ")


class ModeDescriptions(unittest.TestCase):
    def test_each_option_has_accurate_one_liner(self):
        html = read("templates", "index.html")
        menu = html[html.index('id="modeMenu"'):html.index('id="audioPlayer"')]
        expect = {
            "循環": "單曲重複這首；全部則播過的歌回到佇列末端",
            "隨機": "佇列播完後，從本地歌單隨機接著播",
            "推薦": "佇列播完後，自動接相關歌曲",
            "平衡": "讓每首歌音量差不多，切換後立即生效",
            "睡眠": "時間到或播完這首後，停止播放並離開語音",
        }
        for label, desc in expect.items():
            self.assertIn(f'<span class="ctrl-label">{label}</span>', menu, label)
            self.assertIn(f'<span class="mm-desc">{desc}</span>', menu, label)
        css = read("static", "css", "style.css")
        self.assertIn(".mode-menu .mm-desc {", css)
        # 說明來自程式實際行為，不是隨便寫的
        src = read("musicbot", "player.py") + read("musicbot", "player_sleep.py") + read("musicbot", "audio.py")
        self.assertIn("全部循環：播過的歌接回佇列尾端", src)
        self.assertIn("隨機循環抽一首", src)
        self.assertIn("自動推薦接歌", src)
        self.assertIn("停止播放、清佇列、離開語音", src)
        self.assertIn("音量平衡", read("musicbot", "audio.py")[:80])


class NarrowLayout(unittest.TestCase):
    def test_badge_and_stage_not_clipped(self):
        css = read("static", "css", "style.css")
        ux = css[css.index("ux1（2026-11-26）"):]
        self.assertIn(".queue-badge-pos { display: inline-flex !important;", ux)
        self.assertIn("@media (max-width: 400px)", ux)
        self.assertIn("@media (min-width: 761px) and (max-height: 620px)", ux)
        self.assertIn("overflow-y: auto", ux)
        self.assertIn(".np-stage { overflow: visible; }", ux)


class TouchQueueButtons(unittest.TestCase):
    def test_shown_on_coarse_pointer_and_focus(self):
        css = read("static", "css", "style.css")
        ux = css[css.index("ux1（2026-11-26）"):]
        self.assertIn("@media (hover: none), (pointer: coarse)", ux)
        self.assertIn(".list-item:focus-within .item-actions", ux)
        self.assertIn("min-width: 36px", ux)
        self.assertIn("min-height: 36px", ux)


class CacheToken(unittest.TestCase):
    def test_new_unique_version(self):
        html = read("templates", "index.html")
        self.assertIn("?v=20261126t11b", html)
        self.assertNotIn("20261122hard1", html)


if __name__ == "__main__":
    unittest.main()
