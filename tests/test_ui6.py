"""面板介面：佇列拖曳 / 鍵盤排序、跑馬燈、封面主色、載入骨架、手機播放列、L / S 快捷鍵"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


JS = _read("static", "js", "app.js")
CSS = _read("static", "css", "style.css")
HTML = _read("templates", "index.html")


def _slice(start, end):
    a = JS.index(start)
    return JS[a:JS.index(end, a)]


class QueueReorder(unittest.TestCase):
    def test_handle_is_keyboard_and_touch_friendly(self):
        self.assertIn('class="drag-handle" tabindex="0" role="button"', JS)
        body = _slice("function bindQueueDrag(", "\nlet _qKeyBusy")
        for frag in ("'ArrowUp'", "'ArrowDown'", "'Home'", "'End'", "e.stopPropagation()", "commitQueueMove(list, div, items, from, to, true)"):
            self.assertIn(frag, body)
        self.assertIn(".drag-handle { touch-action: none;", CSS)   # 只拖把手：清單其他地方照常捲動

    def test_move_sends_expected_url_and_resyncs_on_conflict(self):
        body = _slice("async function commitQueueMove(", "\nlet _qRenderPending")
        self.assertIn("API.queueReorder(from, to, undefined, song && song.url)", body)   # 伺服器比對網址，被別人改過就 409
        self.assertIn("state._queueKey = undefined;", body)                             # 失敗：下次 SSE 照伺服器重畫
        self.assertIn("API.queuePlay(+div.dataset.index)", JS)                          # 搬過的列：播放 / 刪除用目前位置
        self.assertIn("API.queueRemove(+div.dataset.index)", JS)

    def test_autoscroll_keeps_going_at_edges(self):
        body = _slice("function dragFrame(", "\nasync function finishQueueDrag")
        self.assertIn("EDGE = 48", body)
        self.assertIn("requestAnimationFrame", body)


class Marquee(unittest.TestCase):
    def test_measures_overflow_instead_of_length(self):
        self.assertNotIn("title.length > 20", JS)
        body = _slice("function checkMarquee(", "\nfunction mqGap")
        self.assertIn("need > w + 1 && !_mqReduce.matches", body)
        self.assertIn("setMarquee(titleEl, title || '尚未播放');", JS)
        self.assertIn("setMarquee(el.barTitle, title || '尚未播放');", JS)

    def test_css_pause_and_reduced_motion(self):
        self.assertIn(".marquee:hover .marquee-inner { animation-play-state: paused; }", CSS)
        self.assertIn("@media (prefers-reduced-motion: reduce) { .marquee-inner { animation: none !important; } }", CSS)
        self.assertIn("animation: marquee var(--mq-dur, 16s) linear 1.6s infinite;", CSS)
        # 展開畫面關掉所有動畫（.np-view * { animation: none !important }），大歌名要例外才會捲
        self.assertIn(".np-view .song-title.marquee .marquee-inner { animation: marquee var(--mq-dur, 16s) linear 1.6s infinite !important; }", CSS)
        self.assertIn("@media (prefers-reduced-motion: reduce) { .np-view .song-title.marquee .marquee-inner { animation: none !important; } }", CSS)


class Skeleton(unittest.TestCase):
    def test_template_starts_with_skeletons(self):
        for lid in ("shelfRow", "playlistList", "favList", "npQueueList"):
            i = HTML.index(f'id="{lid}"')
            self.assertIn('<div class="skel-wrap" role="status" aria-busy="true" aria-label="載入中">', HTML[i:i + 200], lid)

    def test_lists_wait_for_first_push(self):
        for key, lst in (("_plKey", "el.playlistList"), ("_favKey", "el.favList")):
            self.assertIn(f"if (stillLoading('{key}')) {{ showSkeleton({lst}, 'row', 6); return; }}", JS)
        self.assertIn("if (stillLoading('_queueKey') && !(state.queue || []).length)", JS)
        self.assertEqual(JS.count("resetListCaches();"), 2)   # 手動換伺服器、自動跟隨
        self.assertIn("showSkeleton(el.shelfRow, 'card', 6)", JS)
        self.assertIn("el.searchResults.innerHTML = skelHTML('result', 5)", JS)
        self.assertIn(".skel-wrap { display: contents; }", CSS)


class MobileBar(unittest.TestCase):
    def test_touch_targets(self):
        i = CSS.index("/* ---- 手機底部播放列：按鍵 ≥ 44px")
        block = CSS[i:CSS.index("\n}\n", i)]
        self.assertIn(".player-bar .ctrl-btn { width: 46px; height: 46px;", block)
        self.assertIn(".player-bar .ctrl-play { width: 52px; height: 52px;", block)
        self.assertIn("env(safe-area-inset-bottom, 0px)", block)
        self.assertIn("setNpOpen(dy < 0);", JS)   # 往上滑展開、往下滑收合


class Shortcuts(unittest.TestCase):
    def test_l_and_s(self):
        self.assertIn("else if (k === 'l' || k === 'L') { if (!e.repeat && el.btnLoop) el.btnLoop.click(); }", JS)
        self.assertIn("else if (k === 's' || k === 'S') { if (!e.repeat && el.btnShuffle) el.btnShuffle.click(); }", JS)
        help_ = HTML[HTML.index('id="kbdHelp"'):HTML.index("</dl>", HTML.index('id="kbdHelp"'))]
        self.assertIn("<dt><kbd>L</kbd></dt>", help_)
        self.assertIn("<dt><kbd>S</kbd></dt>", help_)
        # 不能跟既有快捷鍵撞到
        keys = _slice("    const k = e.key;\n    let handled = true;", "    else handled = false;")
        self.assertEqual(keys.count("k === 'l'"), 1)
        self.assertEqual(keys.count("k === 's'"), 1)


class SearchBox(unittest.TestCase):
    def test_emptying_input_hides_results(self):
        self.assertIn("el.searchInput.addEventListener('input', _onSearchEmptied);", JS)
        self.assertIn("el.searchInput.addEventListener('search', _onSearchEmptied);", JS)   # type=search 的 ✕
        self.assertIn("if (!el.searchInput.value.trim() && el.searchResults.firstChild) clearSearchResults();", JS)

    def test_stale_search_response_dropped(self):
        body = _slice("async function doSearch(", "\n// ==================== 事件綁定")
        self.assertIn("const seq = ++_searchSeq;", body)
        self.assertIn("if (seq !== _searchSeq) return;", body)
        esc = _slice("// 搜尋結果：按 Esc 收起", "\n});")
        self.assertIn("clearSearchResults();", esc)


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TintAndSkeletonBehaviour(unittest.TestCase):
    def run_js(self, expr):
        code = "\n".join([_slice("function rgbToHsl(", "function applyCoverTint("), _slice("const SKEL_W", "function showSkeleton("),
                          f"console.log(JSON.stringify({expr}));"])
        r = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    PX = "(r, g, b, n) => { const a = []; for (let i = 0; i < n; i++) a.push(r, g, b, 255); return new Uint8ClampedArray(a); }"

    def test_dominant_hue(self):
        r = self.run_js(f"(() => {{ const px = {self.PX}; const mix = (a, b) => {{ const o = new Uint8ClampedArray(a.length + b.length); o.set(a); o.set(b, a.length); return o; }};"
                        " return [pickTint(px(220, 40, 40, 400)), pickTint(px(30, 90, 220, 400)), pickTint(px(128, 128, 128, 400)),"
                        " pickTint(px(5, 5, 5, 400)), pickTint(mix(px(120, 120, 120, 380), px(40, 200, 80, 120)))]; })()")
        red, blue, grey, black, green = r
        self.assertTrue(red["h"] < 10 or red["h"] > 350, red)
        self.assertTrue(200 < blue["h"] < 235, blue)
        self.assertIsNone(grey)    # 灰階封面：用主題色
        self.assertIsNone(black)
        self.assertTrue(120 < green["h"] < 150, green)   # 灰底上的綠色：挑鮮豔的那個
        for t in (red, blue, green):
            self.assertTrue(0.45 <= t["s"] <= 0.85)

    def test_play_icon_contrast_every_hue(self):
        # 每個色相都要：深色主題白色圖示對比 ≥ 3.2、淺色主題 ≥ 4.5
        r = self.run_js("Array.from({length: 24}, (_, i) => [i * 15, ...['dark', 'light'].map(th => 1.05 / (relLum(tintForTheme(i * 15, 0.85, th)) + 0.05))])")
        for h, dark, light in r:
            self.assertGreaterEqual(dark, 3.2, h)
            self.assertGreaterEqual(light, 4.5, h)

    def test_skeleton_markup(self):
        r = self.run_js("[skelHTML('row', 3), skelHTML('card', 2), skelHTML('result', 1)]")
        self.assertEqual(r[0].count('class="skel-row"'), 3)
        self.assertEqual(r[1].count('class="skel-card"'), 2)
        self.assertEqual(r[2].count('class="skel-result"'), 1)
        self.assertTrue(all(h.startswith('<div class="skel-wrap" role="status" aria-busy="true"') for h in r))


if __name__ == "__main__":
    unittest.main()
