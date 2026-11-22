"""最愛歌曲已拿掉：網頁、Discord 面板 / 說明、API、SSE 都不再有最愛；主畫面＝本地歌單 + 我的歌單。"""
import os
import re
import sys
import unittest

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from musicbot import views, commands, webapi, storage  # noqa: E402
from appjs import read_app_js  # noqa: E402

HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "css", "style.css"), encoding="utf-8").read()
JS = read_app_js()


class Web(unittest.TestCase):
    def test_no_favorites_ui(self):
        for s in ('data-tab="favorites"', 'id="tabFavorites"', 'id="favList"', 'id="btnBarFav"', "btnFavPlayAll",
                  "btnFavAddCurrent", "最愛"):
            self.assertNotIn(s, HTML, s)
        for s in ("/api/favorites", "favAdd", "favRemove", "getFavorites", "renderFav", "state.favorites",
                  "data.favorites", "_favKey", "homeDirty", "FV.home", "最愛"):
            self.assertNotIn(s, JS, s)
        for s in (".pb-fav", "fav-busy", "favPop"):
            self.assertNotIn(s, CSS, s)

    def test_layout_playlist_and_mylists(self):
        self.assertIn("return { homePanel: ['playlist'], sidePanel: ['mylists'] };", JS)
        self.assertIn("return { homePanel: ['playlist', 'mylists'], sidePanel: [] };", JS)
        self.assertIn("const tabPref = { homePanel: 'playlist', sidePanel: 'mylists' };", JS)
        tabs = re.findall(r'data-tab="(\w+)"', HTML)
        self.assertEqual(tabs, ["playlist", "mylists"])

    def test_wide_screen_mylists_renders_after_its_script_loads(self):
        # 寬螢幕一載入右側就是我的歌單：07 檔排版時 ML 還沒宣告（11 檔），不能直接呼叫
        self.assertIn("else if (btn.dataset.tab === 'mylists' && typeof ML !== 'undefined') ML.render();", JS)
        self.assertIn("if (isTabShown('mylists')) ML.render();", JS)

    def test_api_routes_gone(self):
        rules = {r.rule for r in webapi.flask_app.url_map.iter_rules()}
        self.assertFalse([r for r in rules if "favorite" in r], rules)

    def test_storage_has_no_favorites(self):
        for name in ("load_favorites", "save_favorites", "add_favorite_song", "favorites_cache", "favorites_rev"):
            self.assertFalse(hasattr(storage, name), name)


class Discord(unittest.TestCase):
    def test_panel_has_no_favorite_buttons(self):
        v = views.MusicControlView(None)
        ids = [i.custom_id for i in v.children]
        self.assertNotIn("mb:add_favorite", ids)
        self.assertNotIn("mb:fav_play", ids)
        labels = [i.label for i in v.children]
        self.assertNotIn("\u2764\ufe0f", labels)
        self.assertNotIn("\U0001f3b5", labels)
        # 兩排：控制 4 顆 / 隨機循環 + 音量 + 點歌 4 顆
        rows = {}
        for i in v.children:
            rows.setdefault(i.row or 0, []).append(i.label)
        self.assertEqual([len(rows[r]) for r in sorted(rows)], [4, 4])

    def test_help_has_no_favorites(self):
        e = commands.help_embed()
        text = "\n".join(f.value for f in e.fields)
        self.assertNotIn("最愛", text)
        self.assertEqual(len(commands.HELP_BUTTONS), 8)

    def test_old_buttons_still_answered(self):
        src = open(os.path.join(ROOT, "musicbot", "bot.py"), encoding="utf-8").read()
        self.assertIn("client.add_view(RetiredButtonsView())", src)


if __name__ == "__main__":
    unittest.main()
