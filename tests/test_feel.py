"""操作手感修正（原始碼層級的保險；實際行為由 tools/smoke_ui.py 的 feel_checks 在瀏覽器裡驗）。
執行：python -m unittest discover -s tests
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def src(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def func(js, name):
    i = js.index(name)
    j = js.index("\n}\n", i)
    return js[i:j]


class DoubleSubmit(unittest.TestCase):
    def setUp(self):
        self.js = src("static", "js", "app.js")

    def test_url_enter_twice(self):
        body = func(self.js, "async function doSearch(")
        self.assertIn("if (_resolving) return;", body)
        self.assertIn("addFromUrl(query)", body)

    def test_fav_guard_and_instant_feedback(self):
        body = func(self.js, "async function addCurrentToFavorites(")
        self.assertIn("if (_favAdding) return;", body)
        self.assertIn("fav-busy", body)
        self.assertLess(body.index("fav-busy"), body.index("await API.favAdd"))   # 先有反應才等伺服器

    def test_modal_confirm_disabled_while_saving(self):
        i = self.js.index("$('modalConfirm').addEventListener")
        block = self.js[i:i + 700]
        self.assertIn("if (btn.disabled) return;", block)
        self.assertIn("btn.disabled = true;", block)
        self.assertIn("finally { btn.disabled = false; }", block)

    def test_list_add_buttons_guarded(self):
        # 搜尋結果、首頁卡片、最愛 / 本地歌單的「加入佇列」：送出中再按不會重送
        self.assertIn("if (div.classList.contains('is-adding')) return;", self.js)
        self.assertIn("if (card.classList.contains('is-adding')) return;", self.js)
        self.assertGreaterEqual(self.js.count("const b = ev.currentTarget; if (b.disabled) return; b.disabled = true;   // 連點兩下只加一次"), 2)


class StateHold(unittest.TestCase):
    def test_shuffle_not_bounced_by_sse(self):
        js = src("static", "js", "app.js")
        self.assertIn("data.shuffle_mode !== undefined && performance.now() > _shuffleHoldUntil", js)
        i = js.index("el.btnShuffle.addEventListener('click'")
        self.assertIn("_shuffleHoldUntil = performance.now() + 3000;", js[i:i + 600])


class FocusAndToasts(unittest.TestCase):
    def test_focus_leaves_closed_np_view(self):
        body = func(src("static", "js", "app.js"), "function setNpOpen(")
        self.assertIn("!open && npView.contains(document.activeElement)", body)
        self.assertIn("focus({ preventScroll: true })", body)

    def test_toast_cap(self):
        body = func(src("static", "js", "app.js"), "function showToast(")
        self.assertIn("const maxShown = innerWidth < 600 ? 3 : 4;", body)


class TouchTargets(unittest.TestCase):
    def setUp(self):
        css = src("static", "css", "style.css")
        i = css.index("@media (pointer: coarse) {\n    .item-btn, .btn-search")
        self.block = css[i:css.index("\n}\n", i)]
        self.css = css

    def test_hit_area_rules(self):
        for sel in (".item-btn::before", ".btn-search::before", ".btn-sm::before", ".tab-btn::before",
                    ".np-close::before", ".hdr-btn::before", ".cdd-btn::before", ".drag-handle::before"):
            self.assertIn(sel, self.block)
        self.assertIn(".item-actions { gap: 8px; }", self.block)
        self.assertIn(".volume-slider { height: 43px;", self.block)

    def test_buttons_not_clipping_their_hit_area(self):
        # 按鈕本身 overflow: hidden 會把往外延伸的點擊範圍裁掉；漣漪改放在 .fx-ripple-clip
        self.assertNotRegex(self.css, r"\.btn-sm, \.tab-btn, \.btn-search \{[^}]*overflow: hidden")
        self.assertIn(".fx-ripple-clip {", self.css)
        self.assertIn("fx-ripple-clip", src("static", "js", "app.js"))

    def test_volume_fill_keeps_clip(self):
        js = src("static", "js", "app.js")
        self.assertIn("el.volumeSlider.style.backgroundImage =", js)
        self.assertNotRegex(js, r"volumeSlider\.style\.background\s*=")

    def test_volume_track_ends_round(self):
        # 加大的觸控高度只畫中間 5px；圓角要換算到那 5px，不然兩端會變尖
        line = next(l for l in self.block.splitlines() if ".volume-slider { height: 43px;" in l)
        self.assertIn("border-radius: 2.5px / 21.5px", line)


class Feel2(unittest.TestCase):
    """第二輪：跳過防連點、手機播放列上緣展開、錯誤提示停久一點"""
    def setUp(self):
        self.js = src("static", "js", "app.js")
        self.css = src("static", "css", "style.css")

    def test_skip_guard(self):
        i = self.js.index("function skipSong()")
        body = self.js[i:self.js.index("\n}\n", i)]
        self.assertIn("if (_skipBusy || now < _skipHoldUntil) return;", body)
        self.assertIn("_skipHoldUntil = now + 700;", body)
        self.assertIn(".finally(", body)          # 伺服器回應後解除
        self.assertIn("setTimeout(() => { _skipBusy = false; }, 5000)", body)   # 沒回應也最多擋 5 秒
        # 樂觀更新仍在送出之前
        self.assertLess(body.index("showToast('⏭️ 已跳過'"), body.index("API.skip()"))

    def test_all_skip_paths_use_guard(self):
        # 按鈕、快捷鍵都走 skipSong，沒有別處直接呼叫 API.skip
        self.assertEqual(self.js.count("API.skip("), 1)
        self.assertIn("el.btnSkip.addEventListener('click', skipSong);", self.js)

    def test_error_toast_longer(self):
        self.assertIn("(type === 'error' ? 5000 : key ? 1400 : 2500)", self.js)

    def test_minibar_top_edge_expands_on_touch(self):
        i = self.css.index("@media (max-width: 760px) and (pointer: coarse) {\n    html:not(.np-open) .player-bar .progress-bar")
        block = self.css[i:self.css.index("\n}\n", i)]
        self.assertIn("html:not(.np-open) .player-bar .progress-bar { pointer-events: none; }", block)
        self.assertIn("pointer-events: auto; top: -12px; bottom: calc(100% + 2px);", block)
        # 播放列點擊展開時會略過進度條本身；pointer-events: none 讓上緣的點擊落到播放列
        self.assertIn("'button, input, label, select, a, .progress-bar, .volume-row, .pb-extras'", self.js)


if __name__ == "__main__":
    unittest.main()
