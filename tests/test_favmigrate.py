"""最愛拿掉：舊的 favorites/<伺服器 id>.txt 啟動時一次搬進擁有者的「我的歌單」（favmigrate.py）。"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-dummy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from musicbot import favmigrate, userlists, state  # noqa: E402

U = "https://www.youtube.com/watch?v=%011d"
G1, G2 = 111111111111111111, 222222222222222222


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fav = os.path.join(self.tmp, "favorites")
        os.makedirs(self.fav)
        self.marker = os.path.join(self.tmp, "data", "fav_migrated.json")
        self.p = [mock.patch.object(userlists, "FILE", os.path.join(self.tmp, "data", "user_playlists.json")),
                  mock.patch.object(favmigrate, "FAVORITES_DIR", self.fav),
                  mock.patch.object(favmigrate, "MARKER", self.marker)]
        for p in self.p: p.start()
        userlists._reset_cache()

    def tearDown(self):
        for p in self.p: p.stop()
        userlists._reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, gid, lines):
        path = os.path.join(self.fav, f"{gid}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(l + "\n" for l in lines))
        return path

    def lists(self, key="owner"):
        return {p["name"]: userlists.get_playlist(key, p["id"]) for p in userlists.list_playlists(key)}

    def run_migrate(self, names=None, key="owner"):
        with self.assertLogs("musicbot", "INFO") as cm:
            res = favmigrate.migrate(key, names if names is not None else {G1: "聽歌群", G2: "第二群"})
        self.log = "\n".join(cm.output)
        return res


class Migrate(Base):
    def test_normal_one_list_per_guild(self):
        p1 = self.write(G1, [f"{U % 1} 第一首", f"{U % 2} 第二首 有空白", "# 註解", ""])
        self.write(G2, [f"{U % 3} 第三首"])
        before = hashlib.md5(open(p1, "rb").read()).hexdigest()
        res = self.run_migrate()
        ls = self.lists()
        self.assertEqual(sorted(ls), ["最愛（第二群）", "最愛（聽歌群）"])
        self.assertEqual([(s["url"], s["title"]) for s in ls["最愛（聽歌群）"]["songs"]],
                         [(U % 1, "第一首"), (U % 2, "第二首 有空白")])
        self.assertEqual(ls["最愛（聽歌群）"]["source"], "最愛搬移")
        self.assertEqual(res[str(G1)]["added"], 2)
        self.assertIn("最愛 2 首", self.log)
        self.assertIn("原檔 favorites/", self.log)
        # 原檔不刪、不改
        self.assertEqual(hashlib.md5(open(p1, "rb").read()).hexdigest(), before)
        self.assertEqual(sorted(os.listdir(self.fav)), [f"{G1}.txt", f"{G2}.txt"])
        # 記號檔 0600
        m = json.load(open(self.marker, encoding="utf-8"))
        self.assertEqual(sorted(m["guilds"]), sorted([str(G1), str(G2)]))
        if os.name == "posix":
            self.assertEqual(os.stat(self.marker).st_mode & 0o777, 0o600)

    def test_empty_favorites_make_no_list(self):
        self.write(G1, ["# 只有註解", ""])
        res = self.run_migrate()
        self.assertEqual(self.lists(), {})
        self.assertEqual(res[str(G1)]["found"], 0)
        self.assertIn("最愛是空的", self.log)
        self.assertIn(str(G1), json.load(open(self.marker, encoding="utf-8"))["guilds"])

    def test_no_favorites_folder(self):
        shutil.rmtree(self.fav)
        self.assertEqual(favmigrate.migrate("owner", {}), {})
        self.assertFalse(os.path.exists(self.marker))

    def test_second_run_does_nothing(self):
        self.write(G1, [f"{U % 1} a", f"{U % 2} b"])
        self.run_migrate()
        pl = self.lists()["最愛（聽歌群）"]
        userlists.delete("owner", pl["id"])                  # 擁有者把搬過來的刪了：重啟也不會再冒出來
        self.assertEqual(favmigrate.migrate("owner", {G1: "聽歌群"}), {})
        self.assertEqual(self.lists(), {})

    def test_marker_lost_rerun_no_duplicates(self):
        self.write(G1, [f"{U % 1} a", f"{U % 2} b"])
        self.run_migrate()
        os.remove(self.marker)
        self.write(G1, [f"{U % 1} a", f"{U % 2} b", f"{U % 3} c"])
        res = self.run_migrate()
        songs = self.lists()["最愛（聽歌群）"]["songs"]
        self.assertEqual([s["url"] for s in songs], [U % 1, U % 2, U % 3])
        self.assertEqual((res[str(G1)]["added"], res[str(G1)]["dupes"]), (1, 2))
        self.assertEqual(len(self.lists()), 1)

    def test_missing_guild_name_uses_id(self):
        self.write(G2, [f"{U % 1} a"])
        self.run_migrate(names={})
        self.assertEqual(list(self.lists()), [f"最愛（{G2}）"])

    def test_long_guild_name_fits_name_limit(self):
        self.write(G1, [f"{U % 1} a"])
        self.run_migrate(names={G1: "超" * 80})
        (name,) = self.lists()
        self.assertLessEqual(len(name), userlists.NAME_MAX)
        self.assertTrue(name.startswith("最愛（超") and name.endswith("…）"))

    def test_duplicates_and_unsupported_urls_skipped(self):
        self.write(G1, [f"{U % 1} a", f"{U % 1} a again", "https://evil.example.com/x.mp3 壞", "nope", f"{U % 2} b"])
        res = self.run_migrate()
        self.assertEqual([s["url"] for s in self.lists()["最愛（聽歌群）"]["songs"]], [U % 1, U % 2])
        self.assertEqual((res[str(G1)]["found"], res[str(G1)]["added"], res[str(G1)]["dupes"], res[str(G1)]["invalid"]), (5, 2, 1, 2))
        self.assertIn("網址不支援略過 2 首", self.log)

    def test_over_song_limit_splits(self):
        with mock.patch.object(userlists, "MAX_SONGS", 3):
            self.write(G1, [f"{U % i} s{i}" for i in range(8)])
            res = self.run_migrate()
            ls = self.lists()
        self.assertEqual(sorted(ls), ["最愛（聽歌群）", "最愛（聽歌群）2", "最愛（聽歌群）3"])
        self.assertEqual([len(ls[n]["songs"]) for n in ("最愛（聽歌群）", "最愛（聽歌群）2", "最愛（聽歌群）3")], [3, 3, 2])
        self.assertEqual(res[str(G1)]["added"], 8)

    def test_list_cap_reported_and_retried_next_start(self):
        for i in range(userlists.MAX_LISTS):
            userlists.create("owner", f"既有 {i}")
        self.write(G1, [f"{U % 1} a"])
        with self.assertLogs("musicbot", "WARNING") as cm:
            res = favmigrate.migrate("owner", {G1: "聽歌群"})
        self.assertIn("error", res[str(G1)])
        self.assertTrue(any("下次啟動再試" in x for x in cm.output))
        self.assertFalse(os.path.exists(self.marker))       # 沒搬成：不記號
        userlists.delete("owner", userlists.list_playlists("owner")[0]["id"])
        res = self.run_migrate(names={G1: "聽歌群"})
        self.assertEqual(res[str(G1)]["added"], 1)

    def test_ignores_non_guild_files(self):
        with open(os.path.join(self.fav, "notes.txt"), "w") as f:
            f.write(f"{U % 1} x\n")
        self.assertEqual(favmigrate.migrate("owner", {}), {})

    def test_goes_to_given_owner_key(self):
        self.write(G1, [f"{U % 1} a"])
        self.run_migrate(key="m1")
        self.assertEqual(self.lists("owner"), {})
        self.assertEqual(list(self.lists("m1")), ["最愛（聽歌群）"])


class OwnerKey(Base):
    def test_owner_key_follows_discord_login(self):
        with mock.patch("musicbot.discordauth.owner_discord_id", return_value="439430754368225280"):
            self.assertEqual(favmigrate.owner_key(), "owner")              # 沒連結 → 擁有者那份
            userlists.link_discord("member1", "439430754368225280")
            self.assertEqual(favmigrate.owner_key(), "member1")           # 連結到誰就跟 Discord 登入看到的一樣
        with mock.patch("musicbot.discordauth.owner_discord_id", return_value=None):
            self.assertEqual(favmigrate.owner_key(), "owner")

    def test_run_never_raises(self):
        with mock.patch.object(favmigrate, "migrate", side_effect=RuntimeError("x")), self.assertLogs("musicbot", "ERROR"):
            self.assertEqual(favmigrate.run({}), {})

    def test_run_uses_guild_names(self):
        self.write(G1, [f"{U % 1} a"])
        with mock.patch("musicbot.discordauth.owner_discord_id", return_value=None), self.assertLogs("musicbot", "INFO") as cm:
            favmigrate.run({G1: "聽歌群"})
        self.assertIn("最愛（聽歌群）", self.lists())
        self.assertTrue(any("完成 1/1 個伺服器" in x for x in cm.output))


class Startup(unittest.TestCase):
    def test_on_ready_runs_migration_once(self):
        src = open(os.path.join(ROOT, "musicbot", "bot.py"), encoding="utf-8").read()
        i, j = src.index("_synced_once = True"), src.index("favmigrate.run(")
        self.assertLess(i, j)                       # 只在第一次 on_ready
        self.assertLess(src.index("guild_names[g.id] = g.name"), j)   # 伺服器名稱先載好


if __name__ == "__main__":
    unittest.main()
