"""T-12 第 1 項：點歌到開播更快、跳過時也立刻預熱。
執行：python -m unittest tests.test_t12_start
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from musicbot import prewarm, webcore, views, commands, player_voice, player  # noqa: E402


class SourceHooks(unittest.TestCase):
    def test_web_skip_arms_before_voice(self):
        src = Path(ROOT, "musicbot", "webcore.py").read_text(encoding="utf-8")
        i = src.index("async def web_skip")
        body = src[i:src.index("\nasync def ", i + 10)]
        self.assertLess(body.index("_arm_upcoming(gid)"), body.index("ensure_voice_connected"))
        self.assertIn("與淡出同時預熱", body)

    def test_web_play_index_arms_target(self):
        src = Path(ROOT, "musicbot", "webcore.py").read_text(encoding="utf-8")
        i = src.index("async def web_play_index")
        body = src[i:src.index("\nasync def ", i + 10)]
        self.assertIn("_arm_upcoming(gid, song[0])", body)
        self.assertLess(body.index("_arm_upcoming"), body.index("ensure_voice_connected"))

    def test_idle_add_arms_first_song(self):
        src = Path(ROOT, "musicbot", "webcore.py").read_text(encoding="utf-8")
        self.assertIn("warm=auto_play and position == 1", src)
        self.assertIn("_arm_upcoming(gid, added[0][0] if added else None, warm=bool(added))", src)

    def test_discord_skip_and_play_arm(self):
        vs = Path(ROOT, "musicbot", "views.py").read_text(encoding="utf-8")
        cs = Path(ROOT, "musicbot", "commands.py").read_text(encoding="utf-8")
        pv = Path(ROOT, "musicbot", "player_voice.py").read_text(encoding="utf-8")
        self.assertIn("prewarm.arm_upcoming(gid, nxt[0] if nxt else None)", vs)
        self.assertIn("prewarm.arm_upcoming(gid, nxt[0] if nxt else None)", cs)
        self.assertIn("prewarm.arm_upcoming(gid, added_songs[0][0])", pv)
        # 預熱在淡出之前就開始（不要對到函式開頭的 import fade_out_stop）
        i = vs.index("async def skip_song")
        body = vs[i:vs.index("async def stop_bot", i)]
        self.assertLess(body.index("arm_upcoming"), body.index("await fade_out_stop"))

    def test_open_stream_logs_timings(self):
        src = Path(ROOT, "musicbot", "player.py").read_text(encoding="utf-8")
        self.assertIn('[開播] {guild_id} 解析 {resolve_ms:.0f} ms、開啟 {prime_ms:.0f} ms、合計 {total_ms:.0f} ms（{src}）', src)
        self.assertIn('快取命中', src)
        self.assertIn('現場解析', src)

    def test_ensure_idle_doc_and_arm_helper(self):
        self.assertTrue(callable(prewarm.prepare))
        self.assertTrue(callable(prewarm.arm_upcoming))
        self.assertTrue(callable(webcore._arm_upcoming))


if __name__ == "__main__":
    unittest.main()
