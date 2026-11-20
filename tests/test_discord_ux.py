"""Discord 指令使用體驗：回覆內容、連點防護、私訊、錯誤回覆、佇列分頁、面板文字（假的 interaction，不連 Discord）"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord  # noqa: E402
from discord import app_commands  # noqa: E402

from musicbot import commands, views, player, state, discord_ux, mylist_cmd, userlists, extmembers  # noqa: E402
from musicbot import actionlog as _actionlog  # noqa: E402
from musicbot.bot import tree  # noqa: E402

G = 515151
U = "https://www.youtube.com/watch?v=%011d"


def inter(in_voice=True, name="阿明"):
    it = mock.MagicMock()
    done = {"v": False}
    order = []
    async def send_message(*a, **k): done["v"] = True; order.append(("send", a, k))
    async def defer(*a, **k): done["v"] = True; order.append(("defer", a, k))
    async def followup(*a, **k): order.append(("followup", a, k))
    async def edit_message(*a, **k): done["v"] = True; order.append(("edit", a, k))
    it.response.send_message = mock.AsyncMock(side_effect=send_message)
    it.response.defer = mock.AsyncMock(side_effect=defer)
    it.response.edit_message = mock.AsyncMock(side_effect=edit_message)
    it.response.is_done = lambda: done["v"]
    it.followup.send = mock.AsyncMock(side_effect=followup)
    it.original_response = mock.AsyncMock(return_value=mock.MagicMock())
    it.edit_original_response = mock.AsyncMock()
    it.user.id = 111
    it.guild.id = G
    it.guild_id = G
    it.guild.voice_client = None
    it.user.display_name = name
    sent = mock.MagicMock()
    sent.channel.mention = "#指令頻道"
    it.channel.send = mock.AsyncMock(return_value=sent)
    it.channel.mention = "#指令頻道"
    if in_voice:
        it.user.voice.channel = mock.MagicMock()
        it.user.voice.channel.name = "語音一"
    else:
        it.user.voice = None
    it._order = order
    return it


def texts(it):
    """這個 interaction 收到的所有回覆文字"""
    out = []
    for kind, a, k in it._order:
        if a:
            out.append(str(a[0]))
        if k.get("content"):
            out.append(str(k["content"]))
    return out


def playing_vc(it, playing=True, paused=False):
    vc = mock.MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = playing
    vc.is_paused.return_value = paused
    vc.channel = it.user.voice.channel if it.user.voice else mock.MagicMock()
    it.guild.voice_client = vc
    return vc


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 10))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _actionlog._reset_for_tests(os.path.join(self.tmp, "a.json"))
        discord_ux.reset_for_tests()
        self.out = mock.MagicMock()
        sent = mock.MagicMock()
        sent.channel.mention = "#音樂"
        self.out.send = mock.AsyncMock(return_value=sent)
        self.patches = [mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=self.out))]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        for d in (state.queues, state.now_playing, state.now_playing_url, state.song_durations, state.song_start_times,
                  state.paused_position, state.control_panels, state.volumes):
            d.pop(G, None)
        discord_ux.reset_for_tests()
        _actionlog._reset_for_tests()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def public(self):
        return [str(c.args[0]) if c.args else "" for c in self.out.send.await_args_list]


class Helpers(Base):
    def test_md_escapes_markdown_and_truncates(self):
        self.assertEqual(discord_ux.md("**a**_b_~c~|d|"), r"\*\*a\*\*\_b\_\~c\~\|d\|")
        self.assertEqual(discord_ux.md("x" * 50, 10), "x" * 9 + "…")
        self.assertEqual(discord_ux.md("a\nb"), "a b")

    def test_panel_text_states(self):
        self.assertEqual(discord_ux.panel_text(G), "⏹ 目前沒有在播放")
        state.now_playing[G] = "直播*中"
        state.song_durations[G] = 0
        t = discord_ux.panel_text(G, elapsed=0)
        self.assertIn(r"正在播放：**直播\*中**", t)
        self.assertIn("🔴 直播", t)            # 跟網頁一樣：長度未知 → 直播
        state.song_durations[G] = 200
        state.paused_position[G] = 100
        t = discord_ux.panel_text(G)
        self.assertTrue(t.startswith("⏸"))
        self.assertIn("`1:40 / 3:20`", t)
        self.assertIn("\u2588" * 7, t)

    def test_queue_summary_matches_web(self):
        with mock.patch.dict(state.known_durations, {U % 1: 120, U % 2: 3600}, clear=False):
            self.assertEqual(discord_ux.queue_summary([(U % 1, "a"), (U % 2, "b")]), "2 首・1 小時 2 分鐘")
            self.assertEqual(discord_ux.queue_summary([(U % 1, "a"), (U % 99, "live")]), "2 首・2 分鐘以上")
            self.assertEqual(discord_ux.queue_summary([(U % 98, "x")]), "1 首")

    def test_claim_skip_window(self):
        self.assertTrue(discord_ux.claim_skip(G, U % 1))
        self.assertFalse(discord_ux.claim_skip(G, U % 1))      # 連點：同一首
        self.assertTrue(discord_ux.claim_skip(G, U % 2))       # 已經換到下一首：照常跳
        with mock.patch.object(discord_ux.time, "monotonic", return_value=discord_ux.time.monotonic() + 5):
            self.assertTrue(discord_ux.claim_skip(G, U % 2))   # 隔夠久：照常跳


class Metadata(unittest.TestCase):
    def test_every_command_is_guild_only(self):
        for c in tree.get_commands():
            self.assertTrue(c.guild_only, f"/{c.name} 在私訊裡會壞掉，要 guild_only")

    def test_volume_range_enforced_by_discord(self):
        p = commands.volume.parameters[0]
        self.assertEqual((p.min_value, p.max_value), (0, 200))

    def test_descriptions_are_chinese_and_short(self):
        for c in tree.get_commands():
            subs = list(getattr(c, "commands", [])) + [c]
            for s in subs:
                self.assertTrue(1 <= len(s.description) <= 100, s.name)
                self.assertRegex(s.description, r"[\u4e00-\u9fff]", s.name)

    def test_modal_says_keywords_work(self):
        m = views.MusicLinkModal()
        self.assertEqual(m.title, "點歌")
        self.assertIn("歌名", m.link.label)


class Skip(Base):
    def setUp(self):
        super().setUp()
        state.now_playing[G] = "好歌*一"
        state.now_playing_url[G] = U % 7

    def test_slash_skip_double_only_once(self):
        fade = mock.AsyncMock()
        with mock.patch.object(commands.audio, "fade_out_stop", fade):
            a, b = inter(), inter("小花")
            playing_vc(a); playing_vc(b)
            run(commands.skip.callback(a))
            run(commands.skip.callback(b))
        self.assertEqual(fade.await_count, 1)
        self.assertIn(r"⏭️ 已跳過：**好歌\*一**", texts(a))
        self.assertIn("剛剛已經跳過", texts(b)[0])
        self.assertEqual(self.public(), [r"⏭️ **阿明** 跳過了 **好歌\*一**"])

    def test_button_skip_double_only_once(self):
        fade = mock.AsyncMock()
        with mock.patch("musicbot.audio.fade_out_stop", fade):
            for _ in range(2):
                it = inter(); playing_vc(it)
                run(views.MusicControlView(G).skip_song.callback(it))
        self.assertEqual(fade.await_count, 1)
        self.assertIn("剛剛已經跳過", texts(it)[0])

    def test_slash_and_button_share_guard(self):
        with mock.patch("musicbot.audio.fade_out_stop", mock.AsyncMock()) as fade:
            it = inter(); playing_vc(it)
            run(commands.skip.callback(it))
            it2 = inter(); playing_vc(it2)
            run(views.MusicControlView(G).skip_song.callback(it2))
        self.assertEqual(fade.await_count, 1)


class StopVolumeQueuePanel(Base):
    def test_stop_with_nothing_to_stop(self):
        it = inter()
        with mock.patch("musicbot.player.stop_playback", mock.AsyncMock()) as sp:
            run(commands.stop.callback(it))
        sp.assert_not_awaited()
        self.assertEqual(texts(it), ["目前沒有在播放，佇列也是空的"])
        self.assertEqual(self.public(), [])

    def test_stop_while_playing(self):
        it = inter(); playing_vc(it)
        state.now_playing[G] = "歌"
        with mock.patch("musicbot.player.stop_playback", mock.AsyncMock()) as sp:
            run(commands.stop.callback(it))
        sp.assert_awaited_once()
        self.assertIn("已停止", texts(it)[0])

    def _stop_button(self, it):
        run(views.MusicControlView(G).stop_bot.callback(it))
        kw = it.response.send_message.await_args.kwargs
        return it.response.send_message.await_args.args[0], kw.get("view"), kw

    def test_stop_button_asks_first(self):
        state.queues[G] = [(U % i, "x") for i in range(4)]
        state.now_playing[G] = "歌"
        it = inter(); playing_vc(it)
        with mock.patch("musicbot.player.stop_playback", mock.AsyncMock()) as sp:
            text, view, kw = self._stop_button(it)
            self.assertEqual(text, "確定要停止並清空佇列（4 首）嗎？")
            self.assertTrue(kw["ephemeral"])
            self.assertEqual(view.timeout, 30)
            self.assertEqual(view.confirm.style, discord.ButtonStyle.danger)
            sp.assert_not_awaited()                      # 還沒確認：什麼都不做
            c1, c2 = inter(), inter()
            run(view.confirm.callback(c1))
            run(view.confirm.callback(c2))               # 連按兩下
        sp.assert_awaited_once()
        self.assertEqual(c1.response.edit_message.await_args.kwargs, {"content": "⏹ 已停止並清空佇列（仍在語音頻道）", "view": None})
        c2.response.edit_message.assert_not_awaited()

    def test_stop_button_cancel_and_timeout(self):
        state.now_playing[G] = "歌"
        it = inter(); playing_vc(it)
        with mock.patch("musicbot.player.stop_playback", mock.AsyncMock()) as sp:
            _, view, _ = self._stop_button(it)
            c = inter()
            run(view.cancel.callback(c))
            run(view.confirm.callback(inter()))          # 取消後再按確定：無效
            self.assertEqual(c.response.edit_message.await_args.kwargs["content"], "已取消，繼續播放")
            _, view2, _ = self._stop_button(inter())
            run(view2.on_timeout())
            run(view2.confirm.callback(inter()))         # 逾時後再按確定：無效
        sp.assert_not_awaited()
        view2.message_interaction.edit_original_response.assert_awaited_once_with(content="已逾時，沒有停止", view=None)

    def test_stop_confirm_only_presser(self):
        state.now_playing[G] = "歌"
        it = inter(); playing_vc(it)
        _, view, _ = self._stop_button(it)
        other = inter("小花"); other.user.id = 222
        self.assertFalse(run(view.interaction_check(other)))
        self.assertIn("只有按 ⏹ 的人", texts(other)[0])
        self.assertTrue(run(view.interaction_check(inter())))

    def test_stop_button_with_nothing(self):
        it = inter(); playing_vc(it, playing=False)
        with mock.patch("musicbot.player.stop_playback", mock.AsyncMock()) as sp:
            run(views.MusicControlView(G).stop_bot.callback(it))
        sp.assert_not_awaited()

    def test_volume_replies_before_announce_and_survives_send_failure(self):
        self.out.send = mock.AsyncMock(side_effect=discord.Forbidden(mock.MagicMock(status=403), "no"))
        it = inter()
        run(commands.volume.callback(it, 80))
        self.assertEqual(texts(it), ["🔊 音量已設為 **80**%"])
        self.assertEqual(state.volumes[G], 0.8)

    def test_volume_out_of_range_direct_call(self):
        it = inter()
        run(commands.volume.callback(it, 500))
        self.assertIn("0 到 200", texts(it)[0])

    def test_queue_paging_and_summary(self):
        state.queues[G] = [(U % i, f"歌_{i}") for i in range(25)]
        state.now_playing[G] = "現在*播"
        it = inter()
        with mock.patch.dict(state.known_durations, {U % 0: 65}, clear=False):
            run(commands.queue.callback(it))
        self.assertEqual(self.public(), [])                       # 不再公開發到音樂頻道
        kw = it.response.send_message.await_args.kwargs
        self.assertTrue(kw["ephemeral"])                          # 只有問的人看得到
        e = kw["embed"]
        self.assertIn(r"現在\*播", e.description)
        self.assertEqual(e.fields[0].name, "即將播放（25 首・1 分鐘以上）")
        self.assertEqual(len(e.fields[0].value.splitlines()), 10)
        self.assertIn(r"歌\_0 `1:05`", e.fields[0].value)
        self.assertEqual(e.footer.text, "第 1／3 頁")
        pager = kw["view"]
        self.assertEqual(pager.timeout, 300)
        self.assertIs(pager.message, it.original_response.return_value)   # 逾時時收起按鈕用
        self.assertTrue(pager.prev.disabled)
        self.assertFalse(pager.next.disabled)
        click = inter()
        run(pager.next.callback(click))
        e2 = click.response.edit_message.await_args.kwargs["embed"]
        self.assertIn("`11.`", e2.fields[0].value)
        self.assertEqual(e2.footer.text, "第 2／3 頁")
        for t in texts(it):
            self.assertNotIn("已發到", t)

    def test_queue_pager_timeout_removes_buttons(self):
        state.queues[G] = [(U % i, "x") for i in range(25)]
        it = inter()
        it.original_response = mock.AsyncMock(return_value=mock.MagicMock(edit=mock.AsyncMock()))
        run(commands.queue.callback(it))
        pager = it.response.send_message.await_args.kwargs["view"]
        run(pager.on_timeout())
        pager.message.edit.assert_awaited_once_with(view=None)

    def test_queue_short_has_no_pager(self):
        state.queues[G] = [(U % 1, "一首")]
        it = inter()
        run(commands.queue.callback(it))
        kw = it.response.send_message.await_args.kwargs
        self.assertNotIn("view", kw)
        self.assertTrue(kw["ephemeral"])
        self.assertEqual(self.public(), [])

    def test_queue_empty(self):
        it = inter()
        run(commands.queue.callback(it))
        self.assertEqual(texts(it), ["佇列是空的"])

    def test_panel_defers_first_and_shows_live(self):
        it = inter(); playing_vc(it)
        state.now_playing[G] = "直播台"
        state.song_durations[G] = 0
        upd = mock.AsyncMock()
        with mock.patch.object(commands, "_update_panel", upd):
            run(commands.panel.callback(it))
        self.assertEqual(it._order[0][0], "defer")
        content = self.out.send.await_args.args[0]
        self.assertIn("🔴 直播", content)
        self.assertIs(state.control_panels[G], self.out.send.return_value)
        self.assertEqual(texts(it)[-1], "✅ 控制面板已發到 #音樂")

    def test_panel_nothing_playing(self):
        it = inter(); playing_vc(it, playing=False)
        with mock.patch.object(commands, "_update_panel", mock.AsyncMock()), \
             mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=None)):
            run(commands.panel.callback(it))
        self.assertEqual(it.channel.send.await_args.args[0], "⏹ 目前沒有在播放")

    def test_panel_send_fails(self):
        it = inter(); playing_vc(it)
        self.out.send = mock.AsyncMock(side_effect=RuntimeError("no perms"))
        with mock.patch.object(commands, "_update_panel", mock.AsyncMock()):
            run(commands.panel.callback(it))
        self.assertIn("發不出去", texts(it)[-1])

    def test_pause_button_updates_panel_text_now(self):
        it = inter(); vc = playing_vc(it)
        state.now_playing[G] = "歌"
        state.song_durations[G] = 100
        state.song_start_times[G] = discord_ux.time.time() - 30
        run(views.MusicControlView(G).toggle_pause.callback(it))
        vc.pause.assert_called_once()
        self.assertTrue(it.response.edit_message.await_args.kwargs["content"].startswith("⏸ 正在播放"))

    def test_join_when_already_there(self):
        it = inter(); vc = playing_vc(it)
        with mock.patch.object(player, "connect_voice", mock.AsyncMock(return_value=vc)):
            run(commands.join.callback(it))
        self.assertTrue(texts(it)[-1].startswith("✅ 已經在"))


class Help(unittest.TestCase):
    def _names(self):
        out = []
        for c in tree.get_commands():
            if hasattr(c, "commands"):
                out += [f"{c.name} {s.name}" for s in c.commands]
            else:
                out.append(c.name)
        return out

    def test_help_lists_every_command_exactly(self):
        listed = [n for n, _ in commands.HELP_COMMANDS]
        self.assertEqual(sorted(listed), sorted(self._names()))     # 新增 / 改名指令沒更新說明就會失敗
        self.assertEqual(len(set(listed)), len(listed))

    def test_help_lists_every_panel_button(self):
        labels = [i.label for i in views.MusicControlView(G).children]
        self.assertEqual(sorted(e for e, _ in commands.HELP_BUTTONS), sorted(labels))

    def test_help_reply(self):
        it = inter()
        run(commands.help_cmd.callback(it))
        kw = it.response.send_message.await_args.kwargs
        self.assertTrue(kw["ephemeral"])
        e = kw["embed"]
        for f in e.fields:
            self.assertLessEqual(len(f.value), 1024)
        body = "\n".join(f.value for f in e.fields)
        self.assertIn("`/play`", body)
        self.assertIn("`/mylist add`", body)
        self.assertIn("會先問你確定", body)
        self.assertIn(commands.PUBLIC_URL.rstrip("/"), body)
        self.assertLessEqual(len(e), 6000)


class PlayReplies(Base):
    def _play(self, info, vc_playing=False, existing=0):
        class YDL:
            def __init__(self, o): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def extract_info(self, q, download=False): return info
        state.queues[G] = [(U % (900 + i), "前面") for i in range(existing)]
        it = inter()
        vc = playing_vc(it, playing=vc_playing)
        with mock.patch.object(player.yt_dlp, "YoutubeDL", YDL), \
             mock.patch.object(player, "play_next", mock.AsyncMock()):
            run(player._process_and_play(it, "https://www.youtube.com/watch?v=x", G, vc))
        return it

    def test_single_song_idle_says_playing_now(self):
        it = self._play({"id": "A" * 11, "title": "新*歌", "webpage_url": U % 1})
        self.assertEqual(texts(it)[-1], r"▶ 馬上播放：**新\*歌**")
        self.assertEqual(self.public(), [r"🎵 **阿明** 點了 **新\*歌**"])

    def test_single_song_queued_says_position(self):
        it = self._play({"id": "A" * 11, "title": "新歌", "webpage_url": U % 1}, vc_playing=True, existing=2)
        self.assertEqual(texts(it)[-1], "✅ 已加入佇列第 3 首：**新歌**")

    def test_playlist_count(self):
        it = self._play({"title": "清單", "entries": [{"id": "B%010d" % i, "url": "B%010d" % i, "title": f"s{i}"} for i in range(5)]},
                        vc_playing=True)
        self.assertEqual(texts(it)[-1], "✅ 已將 **5** 首歌加入佇列")


class Voice(Base):
    def test_no_permission_precheck(self):
        it = inter()
        perms = mock.MagicMock(connect=False, speak=True)
        it.user.voice.channel.permissions_for.return_value = perms
        with mock.patch.object(player, "connect_voice", mock.AsyncMock()) as cv:
            self.assertIsNone(run(player.get_voice_client(it)))
        cv.assert_not_awaited()
        self.assertIn("沒有加入語音頻道「語音一」的權限", texts(it)[-1])

    def test_full_channel_precheck(self):
        it = inter()
        ch = it.user.voice.channel
        ch.permissions_for.return_value = mock.MagicMock(connect=True, speak=True, move_members=False)
        ch.user_limit = 2
        ch.members = [object(), object()]
        with mock.patch.object(player, "connect_voice", mock.AsyncMock()) as cv:
            run(player.get_voice_client(it))
        cv.assert_not_awaited()
        self.assertIn("已經滿了（上限 2 人）", texts(it)[-1])

    def test_connect_error_is_readable(self):
        it = inter()
        err = asyncio.TimeoutError()
        with mock.patch.object(player, "connect_voice", mock.AsyncMock(side_effect=err)):
            run(player.get_voice_client(it))
        self.assertEqual(texts(it)[-1], "加入語音頻道「語音一」逾時，請再試一次")


class Errors(Base):
    def test_tree_error_handler_no_dm(self):
        it = inter()
        run(commands.on_app_command_error(it, app_commands.NoPrivateMessage()))
        self.assertEqual(texts(it), ["這個指令只能在伺服器裡使用"])

    def test_tree_error_handler_generic_after_defer(self):
        it = inter()
        run(it.response.defer())
        err = app_commands.CommandInvokeError(commands.skip, RuntimeError("boom"))
        with self.assertLogs("musicbot", level="ERROR"):
            run(commands.on_app_command_error(it, err))
        self.assertEqual(it._order[-1][0], "followup")
        self.assertEqual(texts(it)[-1], discord_ux.GENERIC_ERROR)

    def test_tree_has_error_handler(self):
        it = inter()      # 實際經過 CommandTree 的錯誤處理（有註冊 @tree.error 才會回覆）
        run(tree.on_error(it, app_commands.NoPrivateMessage()))
        self.assertEqual(texts(it), ["這個指令只能在伺服器裡使用"])

    def test_view_on_error_replies(self):
        it = inter()
        v = views.MusicControlView(G)
        with self.assertLogs("musicbot", level="ERROR"):
            run(v.on_error(it, RuntimeError("x"), v.skip_song))
        self.assertEqual(texts(it), [discord_ux.GENERIC_ERROR])


class MyListAdd(Base):
    def setUp(self):
        super().setUp()
        self.patches += [mock.patch.object(userlists, "FILE", os.path.join(self.tmp, "ul.json")),
                         mock.patch.object(extmembers, "FILE", os.path.join(self.tmp, "m.json"))]
        for p in self.patches[1:]: p.start()
        userlists._reset_cache()
        self.k = "k1"
        userlists.create(self.k, "週末*")

    def tearDown(self):
        userlists._reset_cache()
        super().tearDown()

    def test_add_defers_before_title_lookup_and_names_song(self):
        it = inter()
        seen = {}
        def lookup(url):
            seen["deferred_first"] = it.response.defer.await_count == 1
            return "查到*的歌"
        with mock.patch.object(mylist_cmd, "_key", return_value=self.k), \
             mock.patch("musicbot.extapi._lookup_title", lookup):
            run(mylist_cmd.add.callback(it, "週末*", "https://www.youtube.com/watch?v=CCCCCCCCCCC"))
        self.assertTrue(seen["deferred_first"])
        self.assertEqual(texts(it)[-1], r"✅ 已把 **查到\*的歌** 加入「週末\*」")
        it2 = inter()
        with mock.patch.object(mylist_cmd, "_key", return_value=self.k), \
             mock.patch("musicbot.extapi._lookup_title", lambda u: "查到的歌"):
            run(mylist_cmd.add.callback(it2, "週末*", "https://youtu.be/CCCCCCCCCCC"))
        self.assertIn("裡已經有", texts(it2)[-1])

    def test_add_bad_url(self):
        it = inter()
        with mock.patch.object(mylist_cmd, "_key", return_value=self.k), \
             mock.patch("musicbot.extapi._lookup_title", mock.Mock()) as lk:
            run(mylist_cmd.add.callback(it, "週末*", "not a url"))
        lk.assert_not_called()
        self.assertIn("網址格式不正確", texts(it)[-1])


if __name__ == "__main__":
    unittest.main()
