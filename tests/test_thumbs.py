"""封面縮圖：依顯示大小挑 YouTube 縮圖解析度（app.js 的 thumbCandidates / loadThumb / measureBars）"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()


def _slice(start, end):
    a = JS.index(start)
    return JS[a:JS.index(end, a)]


class ThumbSource(unittest.TestCase):
    def test_uses_cors_jpg_not_webp(self):
        # yt-dlp 給的 vi_webp/maxresdefault.webp 沒有 Access-Control-Allow-Origin；加了 crossorigin 會載入失敗
        self.assertIn("https://i.ytimg.com/vi/${id}/${THUMB_TIERS[i][1]}.jpg", JS)
        for name in ("maxresdefault", "hq720", "sddefault", "hqdefault", "mqdefault"):
            self.assertIn(f"'{name}'", JS)
        self.assertIn("if (fallback && !id) list.push(fallback);", JS)

    def test_missing_size_placeholder_is_skipped(self):
        # 不存在的尺寸回 404 + 120x90 灰圖：onerror 或 naturalWidth <= 120 都要換下一張
        body = _slice("function loadThumb(", "\n}\n")
        self.assertIn("img.onerror = miss;", body)
        self.assertIn("img.naturalWidth <= 120", body)
        self.assertIn("_thumbMissing.add(src)", body)

    def test_each_place_sized_by_display(self):
        self.assertIn("thumbCandidates(url, thumbnail, Math.max(640, thumbNeed(el.albumArt) || 1280))", JS)   # 大封面
        self.assertIn("thumbCandidates(url, thumbnail, thumbNeed(el.barArt))", JS)                         # 底部小封面
        self.assertIn("thumbCandidates(it.url, it.thumbnail, need)", JS)                                   # 再聽一次卡片
        self.assertIn("thumbCandidates(r.url, r.thumbnail, thumbNeed(64))", JS)                           # 搜尋結果
        self.assertIn("thumbnail = thumbCandidates(url, thumbnail, 320)[0] || '';", JS)                   # 模糊背景
        self.assertNotIn('src="${escapeHtml(r.thumbnail)}"', JS)

    def test_bar_crop_measures_visible_area(self):
        body = _slice("function measureBars(", "\n}\n")
        self.assertIn("const W = 128, H = 72;", body)
        self.assertIn("ctx.drawImage(img, (nw - sw) / 2, (nh - sh) / 2, sw, sh, 0, 0, W, H);", body)
        self.assertIn("bx ? bx + 1 : 0", body)


@unittest.skipUnless(shutil.which("node"), "需要 node")
class ThumbCandidatesBehaviour(unittest.TestCase):
    def run_js(self, expr):
        code = "\n".join([
            "const window = { devicePixelRatio: 2 };",
            _slice("const THUMB_TIERS = [", "function loadThumb("),
            f"console.log(JSON.stringify({expr}));",
        ])
        r = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    U = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    W = "https://i.ytimg.com/vi_webp/dQw4w9WgXcQ/maxresdefault.webp"
    B = "https://i.ytimg.com/vi/dQw4w9WgXcQ/"

    def names(self, lst):
        return [re.sub(r".*/", "", u) for u in lst]

    def test_tiers_by_need(self):
        r = self.run_js(f"[1340, 1280, 700, 640, 500, 480, 300, 0, true, false].map(n => thumbCandidates({self.U!r}, {self.W!r}, n))")
        full = ["maxresdefault.jpg", "hq720.jpg", "sddefault.jpg", "hqdefault.jpg", "mqdefault.jpg"]
        self.assertEqual(self.names(r[0]), full)          # 比最大還大：從最大開始
        self.assertEqual(self.names(r[1]), full)
        self.assertEqual(self.names(r[2]), full)          # 700 > 640 → 要 1280 那階
        self.assertEqual(self.names(r[3]), full[2:])
        self.assertEqual(self.names(r[4]), full[2:])
        self.assertEqual(self.names(r[5]), full[3:])
        self.assertEqual(self.names(r[6]), full[4:])
        self.assertEqual(self.names(r[7]), full[4:])      # 量不到寬度 → 最小
        self.assertEqual(self.names(r[8]), full)          # 舊呼叫法 true = 大封面
        self.assertEqual(self.names(r[9]), full[4:])
        self.assertTrue(all(u.startswith(self.B) for lst in r for u in lst))

    def test_id_from_thumbnail_and_non_youtube(self):
        r = self.run_js("[thumbCandidates('', 'https://i.ytimg.com/vi_webp/abcdefghijk/maxresdefault.webp', 320),"
                        " thumbCandidates('https://open.spotify.com/track/1', 'https://i.scdn.co/image/x.jpg', 1280),"
                        " thumbCandidates('', '', 1280), thumbNeed(64), thumbNeed(0)]")
        self.assertEqual(r[0], ["https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg"])
        self.assertEqual(r[1], ["https://i.scdn.co/image/x.jpg"])
        self.assertEqual(r[2], [])
        self.assertEqual(r[3], 128)   # 64px × 2 倍螢幕
        self.assertEqual(r[4], 0)

    def test_known_missing_sizes_skipped(self):
        r = self.run_js(f"(_thumbMissing.add({self.B + 'maxresdefault.jpg'!r}), _thumbMissing.add({self.B + 'hq720.jpg'!r}),"
                        f" thumbCandidates({self.U!r}, '', 1280))")
        self.assertEqual(self.names(r), ["sddefault.jpg", "hqdefault.jpg", "mqdefault.jpg"])


if __name__ == "__main__":
    unittest.main()
