"""本機壓力測試用的假 Discord 環境：真的 Flask + 真的 bot 邏輯，語音 / YouTube / FFmpeg 全部假的。"""
import asyncio, os, sys, threading, time, types, random
from unittest import mock
os.environ.update(DISCORD_TOKEN="x", EXT_TOKEN="OWNER-TOKEN-TEST", WEB_PORT="5055", IDLE_SLEEP_MINUTES="60")
# 資料一律放暫存資料夾（不碰專案的 data/、list.txt、favorites/）；SIM_DATA=資料夾 可指定、SIM_DATA=repo 用專案本身
import tempfile
SIM_DATA = os.getenv("SIM_DATA") or tempfile.mkdtemp(prefix="v6sim-")
if SIM_DATA != "repo":
    os.makedirs(os.path.join(SIM_DATA, "favorites"), exist_ok=True)
    os.environ["HISTORY_DB"] = os.path.join(SIM_DATA, "history.db")
    _lst = os.path.join(SIM_DATA, "list.txt")
    if not os.path.exists(_lst):
        with open(_lst, "w", encoding="utf-8") as _f:
            _f.write("".join(f"https://www.youtube.com/watch?v=SIMSONG{i:04d} 模擬歌曲 {i}\n" for i in range(1, 9)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 專案根目錄
import discord
from musicbot import bot, state, player, webcore, prefetch, autoplay, audio, webapi, extapi, idlesleep  # noqa
from musicbot.bot import client
if SIM_DATA != "repo":
    from musicbot import storage as _st0, extmembers as _em, userlists as _ul, alerts as _al, webauth as _wa
    _st0.LIST_FILE = os.path.join(SIM_DATA, "list.txt")
    _st0.FAVORITES_DIR = os.path.join(SIM_DATA, "favorites")
    _em.FILE = os.path.join(SIM_DATA, "ext_members.json")
    _ul.FILE = os.path.join(SIM_DATA, "user_playlists.json")
    idlesleep.SESSION_FILE = os.path.join(SIM_DATA, "voice_session.json")
    _al.EXIT_FILE = os.path.join(SIM_DATA, "last_exit.json")
    _wa.SECRET_FILE = os.path.join(SIM_DATA, "panel_secret")
    audio.SETTINGS_FILE = os.path.join(SIM_DATA, "audio_settings.json")
    print(f"sim data: {SIM_DATA}", flush=True)

SONG_LEN = float(os.getenv("SIM_SONG_LEN", "4"))
G = 111
loop = asyncio.new_event_loop()
client.loop = loop
EVENTS = []
FFMPEG_OPENS = []   # 每次開 FFmpeg 的參數（before_options / options），測試看濾鏡有沒有套上


class FakeMember:
    def __init__(self, bot_=False): self.bot = bot_; self.display_name = "u"


class FakeVoiceChannel(discord.VoiceChannel):
    def __init__(self, guild, cid, name, n=2):
        self._g, self.id, self.name, self._n = guild, cid, name, n
        self.bitrate = 96000
    @property
    def guild(self): return self._g
    @property
    def members(self): return [FakeMember(), FakeMember(True)][:self._n] + [FakeMember() for _ in range(max(0, self._n - 2))]
    async def connect(self, timeout=15, reconnect=True, **kw):
        vc = FakeVC(self)
        self._g.voice_client = vc
        return vc


class FakeVC:
    def __init__(self, ch):
        self.channel, self.guild = ch, ch.guild
        self._playing = self._paused = False
        self._connected = True
        self._src = None; self._after = None; self._timer = None
        self.latency = self.average_latency = 0.03
    def is_connected(self): return self._connected
    def is_playing(self): return self._playing and not self._paused
    def is_paused(self): return self._paused
    @property
    def source(self): return self._src
    @source.setter
    def source(self, v): self._src = v
    def _arm(self, t):
        if self._timer: self._timer.cancel()
        src = self._src
        def end():
            if self._src is src and self._playing and not self._paused:
                self._finish()
        self._timer = threading.Timer(t, end); self._timer.daemon = True; self._timer.start()
    def _finish(self):
        self._playing = self._paused = False
        after, self._after = self._after, None
        if self._timer: self._timer.cancel()
        if after: threading.Thread(target=after, args=(None,), daemon=True).start()
    def play(self, src, after=None, **kw):
        if self._playing: raise discord.ClientException("Already playing audio.")
        if not self._connected: raise discord.ClientException("Not connected to voice.")
        self._src, self._after, self._playing, self._paused = src, after, True, False
        EVENTS.append(("play", time.time()))
        self._arm(SONG_LEN)
    def stop(self):
        if self._playing or self._paused: self._finish()
    def pause(self):
        if self._playing: self._paused = True
    def resume(self):
        if self._paused: self._paused = False; self._arm(SONG_LEN / 2)
    async def disconnect(self, force=False):
        self.stop(); self._connected = False
        if self.guild.voice_client is self: self.guild.voice_client = None
    async def move_to(self, ch): self.channel = ch
    def cleanup(self): pass


class FakeGuild:
    def __init__(self):
        self.id, self.name = G, "測試伺服器"
        self.voice_client = None
        self.vch = FakeVoiceChannel(self, 999, "語音一")
        self.extra = [FakeVoiceChannel(self, 1000 + i, nm, n) for i, (nm, n) in enumerate([("瓦", 1), ("APEX", 0), ("LOL", 0), ("兄弟幫", 3), ("暈船勒戒所", 0)])]
        self.text_channels = [FAKE_TEXT]
        self.bitrate_limit = 384000
    def get_channel(self, cid): return self.vch if cid == 999 else next((c for c in self.extra if c.id == cid), None)
    @property
    def voice_channels(self): return [self.vch] + self.extra
    @property
    def channels(self): return [self.vch]
    def get_member(self, uid): return None


class FakeMsg:
    def __init__(self, ch): self.channel = ch
    async def edit(self, **kw): return self
    async def delete(self): pass


class FakeText:
    id = 5; name = "文字"
    async def send(self, content=None, **kw):
        if content and len(content) > 2000: raise discord.HTTPException(mock.Mock(status=400, reason="too long"), "Must be 2000 or fewer in length.")
        EVENTS.append(("send", content)); return FakeMsg(self)


FAKE_TEXT = FakeText()
GUILD = FakeGuild()


def fake_extract(url):
    time.sleep(random.uniform(0.02, 0.15))
    if "PRIVATEVID1" in url: raise Exception("ERROR: [youtube] PRIVATEVID1: Private video. Sign in if you've been granted access")
    if "BOTCHECK" in url: raise Exception("Sign in to confirm you're not a bot")
    return {"url": "https://rr.googlevideo.com/videoplayback?expire=%d&id=%s" % (time.time() + 3600, url[-11:]),
            "duration": 0 if "LIVE" in url else 200, "thumbnail": "", "title": "yt-" + url[-11:]}


class FakeYDL:
    def __init__(self, opts): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def extract_info(self, url, download=False):
        if "list=" in url:
            return {"title": "清單", "entries": [{"id": "PL%09d" % i, "title": "清單歌 %d" % i, "duration": 100} for i in range(30)]}
        if url.startswith("ytsearch"):
            return {"entries": [{"id": "SEARCH00001", "title": "搜尋結果", "duration": 100}]}
        if "PRIVATEVID1" in url: raise Exception("Private video")
        return {"id": url[-11:], "title": "yt-" + url[-11:], "duration": 200, "webpage_url": url}


patches = [
    mock.patch.object(type(client), "voice_clients", new_callable=mock.PropertyMock,
                      side_effect=lambda: [GUILD.voice_client] if GUILD.voice_client else []),
    mock.patch.object(type(client), "guilds", new_callable=mock.PropertyMock, return_value=[GUILD]),
    mock.patch.object(type(client), "get_guild", lambda self, gid: GUILD if gid == G else None),
    mock.patch.object(type(client), "is_ready", lambda self: True),
    mock.patch.object(type(client), "latency", new_callable=mock.PropertyMock, return_value=0.05),
    mock.patch.object(prefetch, "extract_stream", fake_extract),
    mock.patch.object(player, "_get_output_channel", mock.AsyncMock(return_value=FAKE_TEXT)),
    mock.patch.object(player.discord, "FFmpegPCMAudio", lambda *a, **k: FFMPEG_OPENS.append(k) or mock.MagicMock()),
    mock.patch.object(player.discord, "PCMVolumeTransformer", lambda src, volume=1.0: types.SimpleNamespace(volume=volume, original=src, cleanup=lambda: None)),
    mock.patch.object(audio, "prime_source", mock.AsyncMock(side_effect=lambda s: s)),
    mock.patch.object(player, "progress_updater", mock.AsyncMock()),
    mock.patch.object(autoplay, "find_next", mock.AsyncMock(return_value=None)),
    mock.patch.object(webapi.yt_dlp, "YoutubeDL", FakeYDL),
    mock.patch.object(extapi, "_lookup_title", lambda url: "查到的標題"),
    mock.patch("musicbot.importer._spotify_tracks", lambda url: ("Spotify 歌單", [("歌%d" % i, "歌手") for i in range(8)])),
]
for p in patches: p.start()
from musicbot import storage as _st; _st.load_local_playlist()
state.guild_names[G] = GUILD.name
state.current_voice_channel[G] = {"id": 999, "name": "語音一"}


def errhandler(lp, ctx):
    EVENTS.append(("loop-exc", repr(ctx.get("exception") or ctx.get("message"))))
    print("LOOP EXC:", ctx.get("message"), repr(ctx.get("exception")), flush=True)
loop.set_exception_handler(errhandler)
threading.Thread(target=loop.run_forever, daemon=True).start()
asyncio.run_coroutine_threadsafe(player.connect_voice(GUILD, GUILD.vch), loop).result(5)
from flask import request as _rq, jsonify as _js
@webapi.flask_app.route("/sim/channel", methods=["POST"])
def _sim_channel():
    """測試用：改頻道人數 / 新增頻道 / 把機器人移到某頻道"""
    d = _rq.get_json(force=True) or {}
    if d.get("add"):
        GUILD.extra.append(FakeVoiceChannel(GUILD, 2000 + len(GUILD.extra), d["add"], int(d.get("n", 0))))
    if d.get("id") is not None and d.get("n") is not None:
        ch = GUILD.get_channel(int(d["id"]))
        if ch: ch._n = int(d["n"])
    if d.get("move") is not None:
        ch = GUILD.get_channel(int(d["move"]))
        if ch:
            state.current_voice_channel[G] = {"id": ch.id, "name": ch.name}
            if GUILD.voice_client: GUILD.voice_client.channel = ch
            state.notify_change()
    return _js(ok=True, channels=[(c.id, c.name, c._n) for c in GUILD.voice_channels])


@webapi.flask_app.route("/sim/ffmpeg", methods=["GET"])
def _sim_ffmpeg():
    """測試用：最近幾次開 FFmpeg 的參數 + 播放中換音效的實測"""
    return _js(opens=FFMPEG_OPENS[-5:], count=len(FFMPEG_OPENS), reapply=player.reapply_stats.get(G))


if __name__ == "__main__":
    print("sim ready", flush=True)
    webapi.flask_app.run(host="127.0.0.1", port=int(os.getenv("SIM_PORT", "5055")), threaded=True, debug=False, use_reloader=False)
