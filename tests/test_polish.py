"""細節修正（文字 / 錯誤訊息 / 無障礙）。執行：python -m unittest discover -s tests

- yt-dlp 英文錯誤 → 中文，不把英文原文丟給使用者
- 面板 API 的 HTTP 錯誤、500 一律中文，例外細節只寫日誌
- 加入語音失敗原因中文化
- 擁有者私訊時間標台北時間；播放失敗寫伺服器名稱
- 擴充功能狀態分「休眠中」/「不在語音頻道」；錯誤訊息不顯示英文
- 面板：清單按鈕有名稱、直播不顯示 0:00、關起來的展開播放器 Tab 進不去
"""
import asyncio
import os
import re
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from musicbot import ytdl, webapi, webcore, alerts, extapi, idlesleep, state  # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")


def src(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


class PlainError(unittest.TestCase):
    CASES = {
        "ERROR: [youtube] abc: Private video. Sign in if you've been granted access": "私人影片",
        "ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader": "已被刪除",
        "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.": "年齡限制",
        "ERROR: The uploader has not made this video available in your country": "地區",
        "ERROR: [youtube] abc: This live event will begin in 3 hours.": "還沒開始",
        "ERROR: [youtube] abc: Join this channel to get access to members-only content": "會員限定",
        "ERROR: Unsupported URL: https://example.com/x": "不支援這個網址",
        "ERROR: unable to download video data: HTTP Error 403: Forbidden": "403",
        "Read timed out.": "逾時",
    }

    def test_known_cases_are_chinese(self):
        for raw, want in self.CASES.items():
            with self.subTest(raw=raw[:40]):
                out = ytdl.plain_error(raw)
                self.assertIn(want, out)
                self.assertNotIn("ERROR", out)

    def test_unknown_falls_back_to_chinese(self):
        out = ytdl.plain_error(RuntimeError("something weird happened xyz"))
        self.assertTrue(CJK.search(out))
        self.assertNotIn("xyz", out)

    def test_chinese_message_passes_through(self):
        self.assertEqual(ytdl.plain_error("佇列已滿（上限 500 首）"), "佇列已滿（上限 500 首）")

    def test_friendly_error_uses_mapping(self):
        with mock.patch.object(ytdl, "is_bot_check", return_value=False):
            self.assertEqual(ytdl.friendly_error("ERROR: Private video"), "這部影片是私人影片，無法播放")


class ApiErrors(unittest.TestCase):
    def setUp(self):
        self.c = webapi.flask_app.test_client()

    def test_http_errors_chinese(self):
        from werkzeug.exceptions import NotFound, MethodNotAllowed
        for exc, code in ((NotFound(), 404), (MethodNotAllowed(), 405)):
            with webapi.flask_app.test_request_context("/api/x"):
                resp, c = webapi.api_error_handler(exc)
            self.assertEqual(c, code)
            self.assertTrue(CJK.search(resp.get_json()["message"]))

    def test_500_hides_exception_text(self):
        with webapi.flask_app.test_request_context("/api/x"):
            with self.assertLogs(webapi.log, "ERROR"):
                resp, code = webapi.api_error_handler(KeyError("secret-detail-123"))
        self.assertEqual(code, 500)
        body = resp.get_json()
        self.assertNotIn("secret-detail-123", body["message"])
        self.assertTrue(CJK.search(body["message"]))

    def test_all_mapped_codes_chinese(self):
        for code, msg in webapi._HTTP_ZH.items():
            self.assertTrue(CJK.search(msg), code)


class JoinErrorText(unittest.TestCase):
    def test_cases(self):
        import discord
        resp = mock.Mock(status=403, reason="Forbidden")
        self.assertIn("權限", webcore.join_error_text(discord.Forbidden(resp, "Missing Permissions")))
        self.assertIn("逾時", webcore.join_error_text(asyncio.TimeoutError()))
        self.assertIn("已滿", webcore.join_error_text(Exception("Channel is full")))
        self.assertEqual(webcore.join_error_text(Exception("已經在這個頻道")), "已經在這個頻道")
        out = webcore.join_error_text(Exception("ClientException: weird english"))
        self.assertNotIn("weird", out)
        self.assertTrue(CJK.search(out))


class OwnerDm(unittest.TestCase):
    def test_taipei_time_label(self):
        # 2026-11-19 16:52 UTC = 2026-11-20 00:52 台北
        self.assertEqual(alerts.taipei_time(1795107120), "2026-11-20 00:52（台北時間）")

    def test_playback_failed_uses_guild_name(self):
        sent = []
        with mock.patch.object(alerts, "alert", lambda kind, msg, *a, **k: sent.append(msg)), \
                mock.patch.dict(state.guild_names, {4242: "測試群"}):
            alerts.playback_failed(4242, 3, "boom")
        self.assertIn("「測試群」", sent[0])
        self.assertNotIn("4242", sent[0])


class ExtStatusAsleep(unittest.TestCase):
    def test_asleep_only_when_really_sleeping(self):
        with mock.patch.object(idlesleep, "asleep", {77}):
            self.assertTrue(extapi._is_asleep(77))
            self.assertFalse(extapi._is_asleep(78))
            self.assertFalse(extapi._is_asleep(None))

    def test_extension_texts(self):
        api = src("chrome-extension", "api.js")
        self.assertNotIn("伺服器回應 ${r.status}", api)
        self.assertNotIn("e.message || e", api)
        pop = src("chrome-extension", "popup.js")
        self.assertIn("s.asleep", pop)
        self.assertIn("不在語音頻道", pop)


class PanelSource(unittest.TestCase):
    def test_list_buttons_have_names(self):
        js = src("static", "js", "app.js")
        for label in ("現在播放這首", "從佇列移除", "從最愛移除", "從歌單刪除"):
            self.assertIn(f'aria-label="{label}"', js)

    def test_live_and_errors(self):
        js = src("static", "js", "app.js")
        self.assertIn("function totalText(", js)
        self.assertIn("'直播'", js)
        self.assertIn("function httpErrorText(", js)
        self.assertIn("連不到機器人，請檢查網路後再試", js)

    def test_kicker_chinese(self):
        self.assertNotIn("NOW PLAYING", src("templates", "index.html"))
        self.assertIn("正在播放", src("templates", "index.html"))

    def test_no_english_bot_in_user_text(self):
        for p in (("musicbot", "webapi_player.py"), ("musicbot", "webapi_queue.py"), ("musicbot", "webcore.py")):
            for m in re.finditer(r'"message":\s*f?"([^"]*)"', src(*p)):
                self.assertNotRegex(m.group(1), r"\b[Bb]ot\b", p)
