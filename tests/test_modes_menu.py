"""播放模式收進 ⋮（循環 / 隨機 / 推薦 / 平衡 / 睡眠）：原始碼層級的保險；實際行為由 tools/smoke_ui.py 的 mode_menu_checks 在瀏覽器裡驗。
執行：python -m unittest discover -s tests
"""
import os
import re
import unittest
from html.parser import HTMLParser

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MODE_IDS = ("btnLoop", "btnShuffle", "autoplayToggle", "normalizeToggle", "btnSleep")


def src(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def func(js, name):
    i = js.index(name)
    j = js.index("\n}\n", i)
    return js[i:j]


class _Tree(HTMLParser):
    """記下每個有 id 的元素的祖先 id 鏈"""
    VOID = {"input", "img", "br", "meta", "link", "hr", "source", "area", "col", "embed", "param", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack, self.anc, self.attrs = [], {}, {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.anc[a["id"]] = [x for x in self.stack if x]
            self.attrs[a["id"]] = a
        if tag not in self.VOID:
            self.stack.append(a.get("id"))

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()


class Markup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = src("templates", "index.html")
        cls.t = _Tree()
        cls.t.feed(cls.html)

    def test_five_modes_live_in_menu_not_bar(self):
        for i in MODE_IDS:
            self.assertIn("modeMenu", self.t.anc[i], i)
            self.assertNotIn("barExtras", self.t.anc[i], i)
            self.assertNotIn("playerBar", self.t.anc[i], i)
        self.assertNotIn("playerBar", self.t.anc["modeMenu"])   # body 底下（fixed，不被播放列 / 展開畫面裁切）

    def test_ids_unique(self):
        ids = re.findall(r'\bid="([^"]+)"', self.html)
        self.assertEqual(sorted(set(x for x in ids if ids.count(x) > 1)), [])

    def test_more_button_in_bar(self):
        self.assertIn("barExtras", self.t.anc["btnModes"])
        a = self.t.attrs["btnModes"]
        self.assertEqual(a.get("aria-haspopup"), "dialog")
        self.assertEqual(a.get("aria-expanded"), "false")
        self.assertEqual(a.get("aria-controls"), "modeMenu")
        self.assertTrue(a.get("aria-label"))
        self.assertIn("fa-ellipsis-vertical", self.html[self.html.index('id="btnModes"'):self.html.index("</button>", self.html.index('id="btnModes"'))])
        m = self.t.attrs["modeMenu"]
        self.assertEqual(m.get("role"), "dialog")
        self.assertIn("hidden", m)
        # 播放列剩：音量 | ⋮ | 停止
        bar = self.html[self.html.index('id="barExtras"'):self.html.index('id="btnExpand"')]
        self.assertLess(bar.index('id="volumeSlider"'), bar.index('id="btnModes"'))
        self.assertLess(bar.index('id="btnModes"'), bar.index('id="btnStop"'))

    def test_labels_and_state_text(self):
        menu = self.html[self.html.index('id="modeMenu"'):]
        menu = menu[:menu.index("</div>\n\n")]
        for label in ("循環", "隨機", "推薦", "平衡", "睡眠"):
            self.assertIn(f'<span class="ctrl-label"', menu)
            self.assertIn(f">{label}</span>", menu)
        for sid in ("loopState", "shuffleState", "autoplayState", "normalizeState", "sleepState"):
            self.assertIn(f'id="{sid}"', menu)
        # 原本的徽章 / 提示仍在（更新函式照舊寫它們）
        for i in ("loopIndicator", "sleepIndicator", "autoplayHint"):
            self.assertIn(f'id="{i}"', menu)

    def test_version_bumped(self):
        self.assertIn("style.css?v=20261121audit2", self.html)
        self.assertIn("app.js?v=20261121audit2", self.html)


class Script(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = src("static", "js", "app.js")

    def test_every_updater_syncs_menu(self):
        for name in ("function setShuffleUI(", "function setToggleUI(", "function updateLoopButton(", "function updateSleepIndicator("):
            self.assertIn("scheduleModeSync();", func(self.js, name), name)

    def test_sync_reads_optimistic_ui(self):
        body = func(self.js, "function syncModeMenu(")
        # 讀按鈕當下的樣子（含樂觀更新），不是伺服器狀態 → 選單 / 小圓點跟按鈕一起變
        for s in ("classList.contains('loop-one')", "classList.contains('active')", "classList.contains('on')"):
            self.assertIn(s, body)
        self.assertIn("btn.classList.toggle('has-on', on.length > 0)", body)
        self.assertIn("'播放模式：全部關閉'", body)

    def test_sync_flag_is_var(self):
        # setShuffleUI 等可能在宣告那行之前被呼叫：用 var 避免 TDZ
        self.assertRegex(self.js, r"\nvar _modeSyncQueued = false, _sleepShort = '';")

    def test_menu_behaviour(self):
        i = self.js.index("(function modeMenu()")
        body = self.js[i:self.js.index("})();", i)]
        self.assertIn("if (menu.hidden) open(e.detail === 0); else close(false);", body)   # 再按 ⋮ 收起；鍵盤打開聚焦第一項
        self.assertIn("document.addEventListener('pointerdown'", body)                        # 點外面收起
        self.assertIn("if (e.key === 'Escape') { stop(); close(true); return; }", body)       # Esc：收起回到 ⋮，不連帶收合播放畫面
        self.assertIn("}, true);", body)                                                       # capture：比全域快捷鍵先處理
        for k in ("'ArrowDown'", "'ArrowUp'", "'Home'", "'End'", "'Tab'"):
            self.assertIn(k, body)
        self.assertIn("btn.setAttribute('aria-expanded', 'true')", body)
        self.assertIn("btn.setAttribute('aria-expanded', 'false')", body)
        # 切換模式不會收起選單：選單裡的點擊沒有任何收起的處理
        self.assertNotIn("menu.addEventListener('click'", body)

    def test_menu_closes_when_button_moves(self):
        self.assertIn("window.closeModeMenu(false)", func(self.js, "function placeExtras("))
        self.assertIn("window.closeModeMenu(false)", func(self.js, "function setNpOpen("))

    def test_shortcuts_unchanged(self):
        self.assertIn("else if (k === 'l' || k === 'L') { if (!e.repeat && el.btnLoop) el.btnLoop.click(); }", self.js)
        self.assertIn("else if (k === 's' || k === 'S') { if (!e.repeat && el.btnShuffle) el.btnShuffle.click(); }", self.js)

    def test_guards_kept(self):
        # 樂觀更新 + 失敗退回 + SSE 舊值不蓋回（隨機不閃回）
        self.assertIn("_shuffleHoldUntil = performance.now() + 3000;", self.js)
        self.assertIn("_loopHoldUntil = performance.now() + 1500;", self.js)
        self.assertIn("autoplayBusy = true;", self.js)
        self.assertIn("normalizeBusy = true;", self.js)


class Style(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = src("static", "css", "style.css")

    def test_menu_rows_44px_and_fixed(self):
        i = self.css.index("播放模式 ⋮（循環")
        block = self.css[i:]
        self.assertIn("min-height: 44px", block)
        self.assertRegex(block, r"\.mode-menu \{\s*position: fixed;")
        self.assertIn(".mode-menu[hidden] { display: none; }", block)
        self.assertIn(".pb-modes.has-on .modes-dot { display: block; }", block)
        self.assertNotRegex(block, r"(animation|transition)\s*:")   # 只切換顯示：順暢優先，沒有動畫

    def test_np_extra_one_row_44(self):
        block = self.css[self.css.index("播放模式 ⋮（循環"):]
        self.assertIn(".np-extra .ctrl-btn { width: 44px; height: 44px;", block)

    def test_old_selectors_gone(self):
        for s in (".np-extra #btnLoop", ".np-extra #btnShuffle", ".pb-extras #btnLoop", ".pb-extras #btnSleep"):
            self.assertNotIn(s, self.css)


if __name__ == "__main__":
    unittest.main()
