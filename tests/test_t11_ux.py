"""T-11 第 1 項：面板搜尋、切歌提示、匯入數量與錯誤用語。
執行：python -m unittest tests.test_t11_ux
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from appjs import read_app_js  # noqa: E402
from musicbot import utils, ytdl, webapi, webapi_library, webcore  # noqa: E402


def src(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


class SearchUx(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = read_app_js()
        cls.css = src("static", "css", "style.css")
        cls.html = src("templates", "index.html")
        cls.lists = src("static", "js", "app", "05-lists.js")

    def test_add_from_search_keeps_results(self):
        body = self.lists[self.lists.index("div.addEventListener('click', async () => {"):]
        body = body[:body.index("\n        });")]
        self.assertIn("toastAdded(res, r.title)", body)
        self.assertNotIn("el.searchInput.value = ''", body)
        self.assertNotIn("el.searchResults.innerHTML = ''", body)

    def test_results_show_channel_or_up(self):
        self.assertIn("const channel = r.channel || r.author || r.uploader || ''", self.lists)
        self.assertIn('class="result-channel"', self.lists)
        self.assertIn(".result-channel {", self.css)
        self.assertIn(".result-meta {", self.css)

    def test_search_shows_loading_text(self):
        self.assertIn("搜尋中…", self.lists)
        self.assertIn("class=\"search-loading\"", self.lists)
        self.assertIn(".search-loading-text", self.css)

    def test_keyword_length_capped_client_and_server(self):
        self.assertIn("SEARCH_QUERY_MAX = 200", self.js)
        self.assertEqual(utils.SEARCH_QUERY_MAX, 200)
        self.assertIn("[...query].length > SEARCH_QUERY_MAX", self.lists)
        self.assertIn("len(query) > SEARCH_QUERY_MAX", src("musicbot", "webapi_library.py"))
        self.assertIn('maxlength="2048"', self.html)  # 網址可比關鍵字長，硬上限跟 MAX_URL_LEN 對齊

    def test_search_input_maxlength_matches_url_cap(self):
        self.assertEqual(utils.MAX_URL_LEN, 2048)


class PlayThisToast(unittest.TestCase):
    def test_waits_until_current_url_before_confirming(self):
        js = src("static", "js", "app", "05-lists.js")
        i = js.index("[data-action=\"play\"]').addEventListener('click'")
        body = js[i:i + 1200]
        self.assertIn("切換中…", body)
        self.assertIn("waitUntilCurrent(song.url", body)
        self.assertIn("▶ 正在播放：", body)
        self.assertNotIn("▶ 播放：", body)


class ImportCountAndErrors(unittest.TestCase):
    def test_web_import_records_added_before_voice(self):
        src_py = src("musicbot", "webcore.py")
        i = src_py.index("async def web_import_songs")
        body = src_py[i:src_py.index("\n    return count\n", i)]
        self.assertIn('stats["added"] = count', body)
        self.assertLess(body.index('stats["added"] = count'), body.index("ensure_voice_connected"))

    def test_import_done_falls_back_to_stats_added(self):
        q = src("musicbot", "webapi_queue.py")
        self.assertIn('added = stats.get("added", 0)', q)

    def test_import_toast_uses_numeric_added(self):
        js = src("static", "js", "app", "05-lists.js")
        self.assertIn("const n = Number(j.added);", js)
        self.assertIn("加入 ${added} 首", js)

    def test_search_api_rejects_long_keyword(self):
        ld = webapi.flask_app.config.get("LOGIN_DISABLED")
        webapi.flask_app.config["LOGIN_DISABLED"] = True
        try:
            c = webapi.flask_app.test_client()
            r = c.post("/api/search", json={"query": "字" * (utils.SEARCH_QUERY_MAX + 1)},
                       headers={"Host": "localhost"})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.get_json()["code"], "query_too_long")
            self.assertEqual(r.get_json()["message"], utils.SEARCH_QUERY_TOO_LONG)
        finally:
            webapi.flask_app.config["LOGIN_DISABLED"] = ld

    def test_import_api_uses_unified_url_error(self):
        ld = webapi.flask_app.config.get("LOGIN_DISABLED")
        webapi.flask_app.config["LOGIN_DISABLED"] = True
        try:
            c = webapi.flask_app.test_client()
            r = c.post("/api/import", json={"guild_id": 1, "url": "https://evil.example.com/x?list=PL1"},
                       headers={"Host": "localhost"})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.get_json()["message"], utils.URL_NOT_ALLOWED)
            r2 = c.post("/api/import", json={"guild_id": 1, "url": "not-a-url"},
                        headers={"Host": "localhost"})
            self.assertEqual(r2.status_code, 400)
            self.assertIn("網址格式不正確", r2.get_json()["message"])
        finally:
            webapi.flask_app.config["LOGIN_DISABLED"] = ld

    def test_ytdl_unsupported_url_uses_same_wording(self):
        self.assertEqual(ytdl.plain_error("ERROR: Unsupported URL: https://example.com/x"),
                         utils.URL_NOT_ALLOWED)


class UrlDialogStaysOpen(unittest.TestCase):
    def test_save_modal_returns_before_close_on_error(self):
        js = src("static", "js", "app", "05-lists.js")
        i = js.index("async function saveModal(")
        body = js[i:js.index("\n}", i)]
        self.assertIn("if (urlErr) { showToast(urlErr, 'error'); return; }", body)
        self.assertIn("if (!r || r.status === 'error')", body)
        close_at = body.index("$('editModal').classList.remove('active')")
        err_at = body.index("if (!r || r.status === 'error')")
        self.assertLess(err_at, close_at)

    def test_ask_dialog_submit_keeps_open_on_error(self):
        ml = src("static", "js", "app", "11-mylist.js")
        body = ml[ml.index("const askDialog"):ml.index("const ML = ")]
        self.assertIn("opts.submit", body)
        self.assertIn("if (err) { setErr(String(err)); input.focus(); return; }", body)
        self.assertIn("validate: (u) => songUrlError(u)", ml)
        self.assertIn("submit: async (u) =>", ml)

    def test_add_from_url_keeps_input_and_shows_message(self):
        js = src("static", "js", "app", "05-lists.js")
        i = js.index("async function addFromUrl(")
        body = js[i:js.index("\nasync function doSearch(", i)]
        self.assertIn("保留輸入的網址，不關結果區", body)
        self.assertNotIn("解析失敗：", body)


class SearchChannelApi(unittest.TestCase):
    def test_js_and_python_caps_match(self):
        js = src("static", "js", "app", "01-core.js")
        self.assertIn(f"const SEARCH_QUERY_MAX = {utils.SEARCH_QUERY_MAX};", js)
        self.assertIn(f"const URL_NOT_ALLOWED = '{utils.URL_NOT_ALLOWED}';", js)


if __name__ == "__main__":
    unittest.main()
