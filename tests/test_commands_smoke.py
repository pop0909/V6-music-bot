"""Discord 指令煙霧測試：每個 slash 指令在各種狀態 / 奇怪參數下都不能丟例外，且一定要回覆使用者。
執行：python -m unittest discover -s tests"""
import asyncio
import inspect
import os
import shutil
import sys
import tempfile
import typing
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from musicbot import commands, mylist_cmd, userlists, extmembers, player, state  # noqa: E402

from musicbot import actionlog as _actionlog  # noqa: E402
_actionlog._reset_for_tests()   # 操作紀錄寫到暫存資料夾，不碰專案的 data/
from musicbot.bot import tree  # noqa: E402

GID = 424242


def make_interaction(in_voice=False, linked=False):
    it = mock.MagicMock()
    done = {"v": False}
    async def send_message(*a, **k): done["v"] = True
    async def defer(*a, **k): done["v"] = True
    it.response.send_message = mock.AsyncMock(side_effect=send_message)
    it.response.defer = mock.AsyncMock(side_effect=defer)
    it.response.send_modal = mock.AsyncMock(side_effect=send_message)
    it.response.is_done = lambda: done["v"]
    it.followup.send = mock.AsyncMock()
    it.edit_original_response = mock.AsyncMock()
    it.original_response = mock.AsyncMock()
    it.guild.id = GID
    it.guild.name = "測試"
    it.guild_id = GID
    it.guild.voice_client = None
    it.channel.send = mock.AsyncMock()
    it.user.id = 123456789012345678 if linked else 999999999999999999
    it.user.display_name = "u"
    if in_voice:
        it.user.voice.channel = mock.MagicMock(id=5, name="ch")
    else:
        it.user.voice = None
    return it


def replied(it):
    return (it.response.send_message.await_count + it.response.defer.await_count + it.followup.send.await_count
            + it.edit_original_response.await_count + it.response.send_modal.await_count) > 0


def all_commands():
    out = []
    for c in tree.get_commands():
        if hasattr(c, "commands"):
            out += [(f"{c.name} {s.name}", s) for s in c.commands]
        else:
            out.append((c.name, c))
    return out


def arg_sets(cmd):
    """每個參數給幾組奇怪的值"""
    params = [p for p in inspect.signature(cmd.callback).parameters.values()][1:]
    vals = {}
    for p in params:
        ann = p.annotation
        if typing.get_origin(ann) is typing.Union:
            ann = [a for a in typing.get_args(ann) if a is not type(None)][0]
        if ann is int:
            vals[p.name] = [0, -5, 10 ** 6]
        elif ann is bool:
            vals[p.name] = [True]
        else:
            vals[p.name] = ["", "x" * 600, "https://open.spotify.com/playlist/abc", "我的歌單"]
    sets = [{}]
    for name, vs in vals.items():
        sets = [dict(s, **{name: v}) for s in sets for v in vs[:3]]
    return sets[:12] or [{}]


class CommandSmoke(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = [mock.patch.object(userlists, "FILE", os.path.join(self.tmp, "ul.json")),
                  mock.patch.object(extmembers, "FILE", os.path.join(self.tmp, "m.json")),
                  mock.patch.object(player, "connect_voice", mock.AsyncMock(side_effect=RuntimeError("無法連線（測試）"))),
                  mock.patch("musicbot.importer._spotify_tracks", lambda url: ("清單", [])),
                  mock.patch("yt_dlp.YoutubeDL", side_effect=RuntimeError("網路被測試擋下"))]
        for p in self.p: p.start()
        userlists._reset_cache()

    def tearDown(self):
        for p in self.p: p.stop()
        userlists._reset_cache()
        for d in (state.queues, state.now_playing):
            d.pop(GID, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_command_replies_without_crashing(self):
        cmds = all_commands()
        self.assertGreaterEqual(len(cmds), 14)
        failures = []
        for name, cmd in cmds:
            for in_voice in (False, True):
                for args in arg_sets(cmd):
                    it = make_interaction(in_voice=in_voice)
                    try:
                        asyncio.run(asyncio.wait_for(cmd.callback(it, **args), 10))
                    except Exception as e:
                        failures.append(f"/{name} voice={in_voice} {list(args.values())!r:.60}: {e!r:.120}")
                        continue
                    if not replied(it):
                        failures.append(f"/{name} voice={in_voice} {list(args.values())!r:.60}: 沒有回覆使用者")
        self.assertEqual(failures, [], "\n".join(failures[:30]))

    def test_every_command_while_playing(self):
        vc = mock.MagicMock()
        vc.is_connected.return_value = True
        vc.is_playing.return_value = True
        vc.is_paused.return_value = False
        vc.disconnect = mock.AsyncMock()
        vc.move_to = mock.AsyncMock()
        state.queues[GID] = [{"url": "https://www.youtube.com/watch?v=AAAAAAAAAAA", "title": "歌" * 300}] * 30
        state.now_playing[GID] = {"url": "https://www.youtube.com/watch?v=BBBBBBBBBBB", "title": "現在" * 200}
        failures = []
        with mock.patch.object(player, "connect_voice", mock.AsyncMock(return_value=vc)), \
             mock.patch.object(player, "play_next", mock.AsyncMock()), \
             mock.patch.object(commands, "play_next", mock.AsyncMock()), \
             mock.patch.object(commands, "_update_panel", mock.AsyncMock()), \
             mock.patch.object(commands, "_get_output_channel", mock.AsyncMock(return_value=None)):
            for name, cmd in all_commands():
                for args in arg_sets(cmd)[:4]:
                    it = make_interaction(in_voice=True)
                    it.guild.voice_client = vc
                    vc.channel = it.user.voice.channel
                    try:
                        asyncio.run(asyncio.wait_for(cmd.callback(it, **args), 10))
                    except Exception as e:
                        failures.append(f"/{name} {list(args.values())!r:.60}: {e!r:.160}")
                        continue
                    if not replied(it):
                        failures.append(f"/{name} {list(args.values())!r:.60}: 沒有回覆使用者")
        self.assertEqual(failures, [], "\n".join(failures[:30]))
