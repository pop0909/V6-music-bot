import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "x")
import discord  # noqa: E402
from musicbot import bot  # noqa: E402


def _forbidden():
    resp = mock.Mock(status=403, reason="Forbidden")
    return discord.Forbidden(resp, {"code": 50001, "message": "Missing Access"})


class SyncFallbackTest(unittest.TestCase):
    def run_sync(self, in_guild, guild_exc=None, existing=()):
        tree = bot.tree
        with mock.patch.object(bot.client, "get_guild", return_value=(object() if in_guild else None)), \
             mock.patch.object(tree, "sync", new=mock.AsyncMock(side_effect=lambda guild=None: (_ for _ in ()).throw(guild_exc) if (guild is not None and guild_exc) else [1, 2])) as sync, \
             mock.patch.object(tree, "fetch_commands", new=mock.AsyncMock(return_value=list(existing))), \
             mock.patch.object(bot.asyncio, "sleep", new=mock.AsyncMock()), \
             self.assertLogs("musicbot", level="INFO") as logs:
            asyncio.run(bot._sync_commands())
        return sync, logs.output

    def test_not_in_guild_keeps_existing_global(self):
        sync, out = self.run_sync(False, existing=[1, 2, 3])
        sync.assert_not_called()
        self.assertFalse(any("ERROR" in o for o in out))
        self.assertTrue(any("不在 SYNC_GUILD_ID" in o for o in out))

    def test_forbidden_falls_back_to_global_when_none(self):
        sync, out = self.run_sync(True, guild_exc=_forbidden(), existing=[])
        self.assertEqual(sync.await_args_list[-1], mock.call())   # 最後一次是全域同步
        self.assertTrue(any("applications.commands" in o and "全域" in o for o in out))
        self.assertFalse(any("ERROR" in o for o in out))

    def test_in_guild_syncs_guild(self):
        sync, out = self.run_sync(True)
        self.assertEqual(sync.await_count, 1)
        self.assertTrue(any("測試伺服器" in o for o in out))


if __name__ == "__main__":
    unittest.main()
