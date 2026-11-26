"""歌單匯入：YouTube 播放清單（yt-dlp extract_flat）與 Spotify 歌單／專輯（公開 embed 頁面，不需 API key）

Spotify 每首歌用「歌名 歌手」到 YouTube 搜尋第一筆，多執行緒並行。
"""
import json
import re
import threading
import time
import uuid
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import yt_dlp

from .config import log
from .utils import clean_str
from . import ytdl

IMPORT_CAP = 200
_SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?(playlist|album|track)/([A-Za-z0-9]+)")
_jobs = {}
_jobs_lock = threading.Lock()


def is_spotify(url):
    return bool(_SPOTIFY_RE.search(url or ""))


def looks_like_playlist(url):
    u = (url or "").lower()
    return is_spotify(u) or "list=" in u or "/playlist" in u


def _spotify_tracks(url):
    m = _SPOTIFY_RE.search(url)
    kind, sid = m.group(1), m.group(2)
    req = urllib.request.Request(f"https://open.spotify.com/embed/{kind}/{sid}",
                                 headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", "replace")
    m2 = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m2:
        raise RuntimeError("讀不到 Spotify 頁面（歌單可能是私人的）")
    try:
        ent = json.loads(m2.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("Spotify 頁面格式改變，暫時無法讀取這個歌單")
    name = clean_str(ent.get("name") or ent.get("title") or "Spotify")
    tracks = ent.get("trackList") or []
    if kind == "track" and not tracks:
        tracks = [{"title": ent.get("title") or ent.get("name"), "subtitle": ent.get("subtitle", "")}]
    out = []
    for t in tracks:
        title = (t.get("title") or "").strip()
        artist = (t.get("subtitle") or "").replace("\xa0", " ").split(",")[0].strip()
        if title:
            out.append((title, artist))
    return name, out


def _yt_search_one(title, artist):
    q = f"{title} {artist}".strip()
    from . import searchcache
    hit = searchcache.get("spotify1v", q)       # 同一首歌重複匯入不用再搜
    if hit:
        return hit[0]
    opts = ytdl.build_opts({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch3:{q}", download=False)    # 第一筆可能是頻道：多抓兩筆，取第一首影片
    for e in ytdl.video_entries(info, 1):
        r = {"url": f"https://www.youtube.com/watch?v={e['id']}",
             "title": clean_str(e.get("title") or q), "duration": e.get("duration") or 0}
        searchcache.put("spotify1v", q, [r])
        return r
    return None


def _youtube_list(url, cap):
    opts = ytdl.build_opts({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
                            "noplaylist": False, "playlistend": cap, "skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("無法解析網址")
    entries = info.get("entries")
    if entries is None:
        entries = [info]
    songs = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        u = e.get("webpage_url") or e.get("url") or ""
        if not u.startswith("http"):
            u = f"https://www.youtube.com/watch?v={e['id']}"
        t = e.get("title") or ""
        if t in ("[Private video]", "[Deleted video]"):
            continue
        songs.append({"url": u, "title": clean_str(t or "未知"), "duration": e.get("duration") or 0})
        if len(songs) >= cap:
            break
    return clean_str(info.get("title") or "播放清單"), songs


def _bili_collection(url, cap, progress=None):
    """B 站合集／公開收藏夾：走官方 API。不是這類網址 → None（交給後面的 YouTube／Spotify）。"""
    from . import bilibili
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        if "://" not in raw:
            raw = "https://" + raw
        host = (urllib.parse.urlparse(raw).hostname or "").lower().rstrip(".")
    except Exception:
        return None
    if any(host == x or host.endswith("." + x) for x in ("b23.tv", "b23.wtf", "bili2233.cn")):
        raw = bilibili.resolve_short(url) or raw
    info = bilibili.parse_resource(raw) or bilibili.parse_resource(url)
    if not info or info.get("kind") not in ("season", "favlist"):
        return None
    result = bilibili.resolve_list(raw, expand_season=True, cap=cap)
    if not result or result.get("kind") in ("bangumi", "series"):
        raise RuntimeError((result or {}).get("message") or "暫不支援這個合集或收藏夾")
    songs = []
    for s in result.get("songs") or []:
        u = (s.get("url") or "").strip()
        if not u:
            continue
        songs.append({"url": u, "title": clean_str(s.get("title") or u), "duration": s.get("duration") or 0})
        if len(songs) >= cap:
            break
    if progress:
        progress(len(songs), len(songs))
    return (clean_str(result.get("title") or "合集"), songs, 0)


def resolve(url, cap=IMPORT_CAP, progress=None):
    """回傳 (清單名稱, [ {url,title,duration} ], 略過數)。progress(done, total) 會被回呼。"""
    bili = _bili_collection(url, cap, progress)
    if bili is not None:
        return bili
    if not is_spotify(url):
        name, songs = _youtube_list(url, cap)
        if progress:
            progress(len(songs), len(songs))
        return name, songs, 0
    name, tracks = _spotify_tracks(url)
    tracks = tracks[:cap]
    total = len(tracks)
    results = [None] * total
    done = [0]
    lock = threading.Lock()
    abort = threading.Event()   # 一首遇到機器人驗證 → 其他還沒跑的直接放棄（不要再打 199 次 YouTube）

    def work(i):
        if abort.is_set():
            return
        try:
            results[i] = _yt_search_one(*tracks[i])
        except Exception as e:
            if ytdl.is_bot_check(e):
                abort.set()
                raise
            log.warning(f"[匯入] 找不到 {tracks[i][0]}：{str(e)[:120]}")
        finally:
            with lock:
                done[0] += 1
                if progress:
                    progress(done[0], total)

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(work, range(total)))
    songs = [s for s in results if s]
    return name, songs, total - len(songs)


# ---------- 背景工作（網頁面板輪詢進度） ----------
def start_job(url, on_done, cap=IMPORT_CAP, meta=None):
    """在背景執行緒解析；on_done(name, songs) 回傳加入數量。回傳 job id。
    meta：額外欄位（例如 owner_key，查詢進度時用來確認是本人）"""
    jid = uuid.uuid4().hex[:12]
    job = {**(meta or {}), "id": jid, "status": "running", "done": 0, "total": 0, "name": "", "added": 0,
           "skipped": 0, "full_skipped": 0, "message": "", "ts": time.time()}
    with _jobs_lock:
        for k in [k for k, v in _jobs.items() if time.time() - v["ts"] > 3600]:
            _jobs.pop(k, None)
        _jobs[jid] = job

    def prog(d, t):
        job["done"], job["total"] = d, t

    def run():
        try:
            name, songs, skipped = resolve(url, cap=cap, progress=prog)
            job["name"], job["skipped"] = name, skipped
            if not songs:
                job.update(status="error", message="清單裡沒有可加入的歌曲")
                return
            res = on_done(name, songs)
            if isinstance(res, tuple):
                job["added"], job["full_skipped"] = (res[0] or 0), (res[1] or 0)
            else:
                job["added"], job["full_skipped"] = (res or 0), 0
            job["status"] = "done"
        except Exception as e:
            log.error(f"[匯入] {url} 失敗：{str(e)[:200]}")
            job.update(status="error", message=ytdl.friendly_error(e, "匯入歌單時"))
            try:
                from . import alerts
                alerts.note_error(e, "匯入歌單")
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()
    return jid


def get_job(jid):
    with _jobs_lock:
        j = _jobs.get(jid)
        return dict(j) if j else None
