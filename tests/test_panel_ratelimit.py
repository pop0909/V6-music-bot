"""控制面板：停止時的 KeyError 競態、內容沒變不 PATCH、超過 55 分鐘重發（避開 Discord 舊訊息編輯限速）"""
import asyncio
import datetime as dt
import os
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import player, state  # noqa: E402

G = 4242
_REAL_SLEEP = asyncio.sleep


class Msg:
    def __init__(self, age=0, channel=None):
        self.created_at = dt.datetime.fromtimestamp(time.time() - age, dt.timezone.utc)
        self.channel = channel
        self.edits = []
        self.deleted = False

    async def edit(self, content=None, view=None):
        self.edits.append(content)
        await _REAL_SLEEP(0)

    async def delete(self):
        self.deleted = True


class Chan:
    def __init__(self):
        self.sent = []

    async def send(self, content, view=None):
        m = Msg(0, self); m.edits.append(content); self.sent.append(m)
        return m


class PanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for d in (state.control_panels, state.now_playing, state.song_start_times, state.song_durations,
                  state.paused_position, state.progress_versions, state.progress_tasks):
            d.pop(G, None)

    async def test_update_panel_remove_race_no_keyerror(self):
        m = Msg()
        state.control_panels[G] = m
        a = asyncio.create_task(player._update_panel(G, "⏹", remove=True))
        b = asyncio.create_task(player._update_panel(G, "⏹", remove=True))
        await asyncio.gather(a, b)          # 舊版：第二個 del 會 KeyError
        self.assertNotIn(G, state.control_panels)

    async def test_update_panel_does_not_remove_newer_panel(self):
        old, new = Msg(), Msg()
        state.control_panels[G] = old

        async def swap(content=None, view=None):
            state.control_panels[G] = new   # 編輯期間換歌發了新面板
        old.edit = swap
        await player._update_panel(G, "⏹", remove=True)
        self.assertIs(state.control_panels[G], new)

    async def _run_updater(self, ticks, panel, paused=False):
        ch = Chan()
        panel.channel = ch
        state.control_panels[G] = panel
        state.now_playing[G] = "歌"
        state.song_durations[G] = 300
        if paused:
            state.paused_position[G] = 42
        else:
            state.song_start_times[G] = time.time() - 10
        state.progress_versions[G] = 1
        n = {"i": 0}
        real_sleep = asyncio.sleep

        async def fake_sleep(_):
            n["i"] += 1
            if n["i"] > ticks:
                state.progress_versions[G] = 2
            await real_sleep(0)
        with mock.patch.object(player.asyncio, "sleep", fake_sleep), \
             mock.patch.object(player.prefetch, "ensure", lambda g: None), \
             mock.patch("musicbot.views.MusicControlView", lambda g: object()):
            await player.progress_updater(G, ch, 1)
        return ch

    async def test_paused_unchanged_content_edits_once(self):
        m = Msg()
        await self._run_updater(6, m, paused=True)
        self.assertEqual(len(m.edits), 1)       # 暫停中文字不變 → 只 PATCH 一次

    async def test_old_panel_is_reposted_not_edited(self):
        m = Msg(age=player.PANEL_MAX_AGE + 5)
        ch = await self._run_updater(1, m)
        self.assertEqual(m.edits, [])
        self.assertTrue(m.deleted)
        self.assertEqual(len(ch.sent), 1)
        self.assertIs(state.control_panels[G], ch.sent[0])

    async def test_fresh_panel_still_updates_progress(self):
        m = Msg(age=60)
        state_ticks = 3
        await self._run_updater(state_ticks, m)
        self.assertGreaterEqual(len(m.edits), 1)
        self.assertFalse(m.deleted)

    def test_panel_age(self):
        self.assertAlmostEqual(player._panel_age(Msg(age=100)), 100, delta=2)
        self.assertEqual(player._panel_age(object()), 0.0)


if __name__ == "__main__":
    unittest.main()
