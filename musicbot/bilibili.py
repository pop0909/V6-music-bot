"""Bilibili：網址正規化、公開 API 搜尋與解析音訊（DASH m4a）。

B 站影片頁對機房 IP 常回 HTTP 412，yt-dlp 走網頁解析會失敗。改走公開 API：
- 搜尋：/x/web-interface/search/type
- 資訊：/x/web-interface/view（多 P 在 data.pages）
- 音訊：/x/player/playurl（fnval=16 DASH）；失敗再試 WBI 簽名

音訊 CDN（*.bilivideo.com 等）必須帶 Referer: https://www.bilibili.com，否則 403。
只在記憶體放匿名 buvid3，不讀、不寫 cookies.txt。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

from .config import log
from .utils import clean_str

REFERER = "https://www.bilibili.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
API = "https://api.bilibili.com"
TIMEOUT = 15

# BV 號：BV + 10 個未含 0/I/O/l 的字元（B 站公開規則）
_BV_RE = re.compile(r"(BV[1-9A-HJ-NP-Za-km-z]{10})")
_AV_RE = re.compile(r"(?i)(?:^|/)av(\d+)")
_P_RE = re.compile(r"(?:^|[?&])p=(\d+)")
_HOSTS = ("bilibili.com", "b23.tv", "b23.wtf", "bili2233.cn")
_SHORT_HOSTS = ("b23.tv", "b23.wtf", "bili2233.cn")
_CDN_HOST_SUFFIXES = ("bilivideo.com", "bilivideo.cn")

# WBI mixin（公開演算法，見 B 站前端）
_WBI_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
              33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
              26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]

_lock = threading.Lock()
_state = {"buvid3": None, "wbi_key": None, "wbi_ts": 0}


def _buvid3():
    with _lock:
        if not _state["buvid3"]:
            _state["buvid3"] = str(uuid.uuid4()).upper()
        return _state["buvid3"]


def default_headers():
    return {
        "User-Agent": UA,
        "Referer": REFERER,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cookie": f"buvid3={_buvid3()}",
    }


def stream_headers():
    """給 FFmpeg / 分段代理向音訊 CDN 發請求用（CDN 不需要 buvid）。"""
    return {"User-Agent": UA, "Referer": REFERER}


def reset_runtime():
    """測試用：清掉記憶體裡的匿名 buvid / WBI 金鑰。"""
    with _lock:
        _state.update(buvid3=None, wbi_key=None, wbi_ts=0)


def _host(url):
    try:
        u = url if "://" in (url or "") else "https://" + (url or "").strip()
        return (urllib.parse.urlparse(u).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def is_host(url):
    """是否為 B 站頁面主機（含子網域、短網址）。不是音訊 CDN。"""
    h = _host(url)
    return any(h == x or h.endswith("." + x) for x in _HOSTS)


def is_cdn_host(url):
    h = _host(url)
    return any(h == x or h.endswith("." + x) for x in _CDN_HOST_SUFFIXES)


is_page = is_host


def _page_from_path(path, query=""):
    if not path:
        return None
    m = _BV_RE.search(path)
    bvid = m.group(1) if m else None
    aid = None
    if not bvid:
        m = _AV_RE.search(path)
        aid = int(m.group(1)) if m else None
    if not bvid and not aid:
        return None
    part = 1
    pm = re.search(r"(?:^|[?&])p=(\d+)", query or "")
    if pm:
        try:
            part = max(1, int(pm.group(1)))
        except ValueError:
            part = 1
    return {"bvid": bvid, "aid": aid, "p": part}


def parse(url):
    """從網址取出 bvid / aid / 分 P。短網址需先 resolve_short。"""
    if not url or not isinstance(url, str):
        return None
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        u = urllib.parse.urlparse(raw)
    except Exception:
        return None
    host = (u.hostname or "").lower().rstrip(".")
    if not any(host == x or host.endswith("." + x) for x in _HOSTS):
        return None
    # 短網址路徑不是 BV
    if any(host == x or host.endswith("." + x) for x in _SHORT_HOSTS):
        return None
    return _page_from_path(u.path or "", u.query or "")


def canonical(bvid=None, aid=None, p=1):
    if bvid:
        base = f"https://www.bilibili.com/video/{bvid}"
    elif aid:
        base = f"https://www.bilibili.com/video/av{int(aid)}"
    else:
        return None
    if p and int(p) > 1:
        return f"{base}?p={int(p)}"
    return base


def normalize(url):
    """各種 B 站影片網址 → https://www.bilibili.com/video/BVxxx（多 P 保留 ?p=N）。
    短網址要先解析轉址，這裡只處理已是完整影片路徑的網址。"""
    info = parse(url)
    if not info:
        return None
    return canonical(bvid=info["bvid"], aid=info["aid"], p=info["p"])


# ---------- HTTP（測試可把 _http 換成 stub）----------
def _http(url, headers=None, method="GET", max_bytes=2_000_000):
    h = default_headers()
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read(max_bytes)
            return resp.status, resp.headers, data, resp.geturl()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(max_bytes)
        except Exception:
            pass
        return e.code, e.headers, body, e.geturl() or url


def resolve_short(url):
    """跟隨 b23.tv 等短網址，回傳正規化後的影片網址；失敗回 None。"""
    if not url:
        return None
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    host = _host(raw)
    if not any(host == x or host.endswith("." + x) for x in _SHORT_HOSTS):
        return normalize(raw)
    # 手動跟轉址，避免最後落到影片頁（常 412）時丢掉 Location
    current = raw
    for _ in range(6):
        try:
            req = urllib.request.Request(current, headers=default_headers(), method="GET")
            opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
            # 不用預設自動跟隨：改讀 3xx Location
        except Exception:
            return None
        class _NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise urllib.error.HTTPError(newurl, code, msg, headers, fp)
        try:
            with urllib.request.build_opener(_NoRedir).open(req, timeout=TIMEOUT) as resp:
                final = resp.geturl()
                n = normalize(final)
                return n or final
        except urllib.error.HTTPError as e:
            loc = ""
            if e.headers:
                loc = e.headers.get("Location") or ""
            nxt = loc or e.geturl() or ""
            if not nxt or nxt == current:
                n = normalize(e.geturl() or current)
                return n
            if nxt.startswith("/"):
                p = urllib.parse.urlparse(current)
                nxt = f"{p.scheme}://{p.netloc}{nxt}"
            n = normalize(nxt)
            if n:
                return n
            current = nxt
            continue
        except Exception as ex:
            log.info(f"[Bilibili] 短網址解析失敗：{ex!r}"[:200])
            return None
    return normalize(current)


def page_info(url):
    """把使用者貼的網址（含短網址）變成 {bvid, aid, p, page_url}。"""
    n = normalize(url)
    if not n:
        n = resolve_short(url)
    info = parse(n or url)
    if not info:
        return None
    info["page_url"] = canonical(bvid=info["bvid"], aid=info["aid"], p=info["p"])
    return info


# ---------- 公開 API ----------
def _api_json(path_or_url, params=None, signed=False):
    url = path_or_url if path_or_url.startswith("http") else API + path_or_url
    params = dict(params or {})
    if signed:
        params = _wbi_sign(params)
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    status, _hdrs, body, _final = _http(url)
    if status != 200:
        raise RuntimeError(f"B 站 API 回應 {status}")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError as e:
        raise RuntimeError("B 站 API 回傳無法解析") from e
    return data


def _nav_wbi_key():
    now = time.time()
    with _lock:
        if _state["wbi_key"] and now - _state["wbi_ts"] < 3600:
            return _state["wbi_key"]
    data = _api_json("/x/web-interface/nav")
    img = ((data.get("data") or {}).get("wbi_img") or {})
    img_url = img.get("img_url") or ""
    sub_url = img.get("sub_url") or ""

    def _id(u):
        name = u.rsplit("/", 1)[-1]
        return name.split(".")[0]

    raw = _id(img_url) + _id(sub_url)
    key = "".join(raw[i] for i in _WBI_MIXIN if i < len(raw))[:32]
    if len(key) < 32:
        raise RuntimeError("讀不到 B 站簽名參數")
    with _lock:
        _state["wbi_key"] = key
        _state["wbi_ts"] = now
    return key


def _wbi_sign(params):
    params = {k: v for k, v in params.items() if v is not None}
    params["wts"] = int(time.time())
    # 濾掉 !'()*
    def _clean(v):
        return re.sub(r"[!'()*]", "", str(v))
    items = sorted((k, _clean(v)) for k, v in params.items())
    query = urllib.parse.urlencode(items)
    rid = hashlib.md5((query + _nav_wbi_key()).encode("utf-8")).hexdigest()
    params["w_rid"] = rid
    return params


def _thumb(url):
    u = (url or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    return u


def _parse_duration(v):
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(v) if v > 0 else 0
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if not nums or len(nums) > 3:
        return 0
    t = 0
    for n in nums:
        t = t * 60 + n
    return t


def view(bvid=None, aid=None):
    params = {}
    if bvid:
        params["bvid"] = bvid
    elif aid:
        params["aid"] = int(aid)
    else:
        raise RuntimeError("缺少影片編號")
    data = _api_json("/x/web-interface/view", params)
    if data.get("code") != 0 or not data.get("data"):
        msg = data.get("message") or "找不到這部影片"
        if data.get("code") == -404:
            msg = "找不到這部影片"
        raise RuntimeError(msg)
    return data["data"]


def search(keyword, limit=5):
    q = (keyword or "").strip()
    if not q:
        return []
    data = _api_json("/x/web-interface/search/type", {
        "search_type": "video",
        "keyword": q,
        "page": 1,
    })
    if data.get("code") != 0:
        raise RuntimeError(data.get("message") or "B 站搜尋失敗")
    out = []
    for r in (data.get("data") or {}).get("result") or []:
        bvid = r.get("bvid") or ""
        if not bvid or not _BV_RE.fullmatch(bvid):
            # 有時結果會夾廣告 / 直播間，沒有 bvid
            continue
        title = clean_str(re.sub(r"</?em[^>]*>", "", r.get("title") or "") or "未知")
        out.append({
            "url": canonical(bvid=bvid),
            "title": title,
            "duration": _parse_duration(r.get("duration")),
            "thumbnail": _thumb(r.get("pic") or ""),
            "author": clean_str(r.get("author") or ""),
        })
        if len(out) >= limit:
            break
    return out


def _playurl(bvid, cid, aid=None):
    params = {"cid": int(cid), "qn": 0, "fnval": 16, "fourk": 1, "fnver": 0}
    if bvid:
        params["bvid"] = bvid
    if aid:
        params["avid"] = int(aid)
    data = _api_json("/x/player/playurl", params)
    if data.get("code") == 0 and data.get("data"):
        return data["data"]
    # 未簽名被拒 → WBI
    data = _api_json("/x/player/wbi/playurl", params, signed=True)
    if data.get("code") != 0 or not data.get("data"):
        raise RuntimeError(data.get("message") or "讀不到這部影片的音訊")
    return data["data"]


def _pick_audio(dash_or_data):
    dash = (dash_or_data or {}).get("dash") or {}
    audios = list(dash.get("audio") or [])
    # 有的回應把 FLAC / Dolby 放在 dolby / flac
    for key in ("dolby", "flac"):
        extra = dash.get(key)
        if isinstance(extra, dict) and extra.get("audio"):
            audios.extend(extra["audio"] if isinstance(extra["audio"], list) else [extra["audio"]])
    audios = [a for a in audios if isinstance(a, dict) and (a.get("baseUrl") or a.get("base_url"))]
    if not audios:
        return None
    audios.sort(key=lambda a: a.get("bandwidth") or 0, reverse=True)
    a = audios[0]
    return {
        "url": a.get("baseUrl") or a.get("base_url"),
        "mime": a.get("mime_type") or "audio/mp4",
        "bandwidth": a.get("bandwidth") or 0,
        "id": a.get("id"),
    }


def _probe_size(url, headers=None):
    """用 Range: bytes=0-0 問檔案大小；失敗回 0。"""
    h = stream_headers()
    if headers:
        h.update(headers)
    h["Range"] = "bytes=0-0"
    try:
        status, hdrs, _body, _f = _http(url, headers=h)
    except Exception:
        return 0
    if status not in (200, 206):
        return 0
    cr = (hdrs or {}).get("Content-Range") or (hdrs or {}).get("content-range") or ""
    m = re.search(r"/(\d+)$", cr)
    if m:
        return int(m.group(1))
    try:
        return int((hdrs or {}).get("Content-Length") or 0)
    except ValueError:
        return 0


def _cid_for(data, p=1):
    pages = data.get("pages") or []
    if pages:
        for pg in pages:
            if int(pg.get("page") or 1) == int(p or 1):
                return pg.get("cid"), pages
        return pages[0].get("cid"), pages
    return data.get("cid"), pages


def parts_from_view(data, bvid=None, aid=None):
    """view 資料 → 佇列用的多 P 清單。"""
    bvid = bvid or data.get("bvid")
    aid = aid or data.get("aid")
    title = clean_str(data.get("title") or "未知")
    thumb = _thumb(data.get("pic") or "")
    pages = data.get("pages") or []
    if not pages:
        url = canonical(bvid=bvid, aid=aid, p=1)
        return [{"url": url, "title": title, "duration": int(data.get("duration") or 0), "thumbnail": thumb}]
    out = []
    multi = len(pages) > 1
    for pg in pages:
        p = int(pg.get("page") or 1)
        part = clean_str(pg.get("part") or "")
        t = f"{title}（P{p} {part}）" if multi and part and part != title else (f"{title}（P{p}）" if multi else title)
        out.append({
            "url": canonical(bvid=bvid, aid=aid, p=p),
            "title": t[:200],
            "duration": int(pg.get("duration") or 0),
            "thumbnail": thumb,
        })
    return out


def resolve_for_queue(url):
    """貼網址要加入佇列：單 P 回 1 首；未指定 P 的多 P 影片回全部。"""
    info = page_info(url)
    if not info:
        raise RuntimeError("不是有效的 B 站影片網址")
    data = view(bvid=info["bvid"], aid=info["aid"])
    songs = parts_from_view(data, bvid=info["bvid"] or data.get("bvid"), aid=info["aid"] or data.get("aid"))
    # 使用者指定了某一 P → 只加那一 P
    raw = (url or "").strip()
    specified = bool(re.search(r"(?:^|[?&])p=\d+", raw.split("#")[0]))
    if specified and info["p"] and len(songs) >= info["p"]:
        return [songs[info["p"] - 1]]
    return songs


def lookup_ytdl_shape(url):
    """給 Discord /play 用，形狀接近 yt-dlp extract_info。"""
    songs = resolve_for_queue(url)
    if not songs:
        return None
    if len(songs) == 1:
        s = songs[0]
        return {"id": s["url"], "webpage_url": s["url"], "title": s["title"], "duration": s["duration"],
                "thumbnail": s.get("thumbnail") or ""}
    return {"title": songs[0]["title"].split("（P")[0], "entries": [
        {"id": s["url"], "webpage_url": s["url"], "title": s["title"], "duration": s["duration"],
         "thumbnail": s.get("thumbnail") or ""}
        for s in songs
    ]}


def extract_stream(page_url):
    """同步：解析單曲的串流網址、長度、縮圖、給播放器用的 HTTP 標頭與檔案大小。"""
    info = page_info(page_url)
    if not info:
        raise RuntimeError("不是有效的 B 站影片網址")
    data = view(bvid=info["bvid"], aid=info["aid"])
    cid, pages = _cid_for(data, info["p"])
    if not cid:
        raise RuntimeError("讀不到這部影片的分 P 資訊")
    bvid = info["bvid"] or data.get("bvid")
    title = clean_str(data.get("title") or "未知")
    if pages and len(pages) > 1:
        for pg in pages:
            if int(pg.get("page") or 1) == int(info["p"] or 1):
                part = clean_str(pg.get("part") or "")
                if part:
                    title = f"{title}（P{info['p']} {part}）"
                break
    dur = 0
    if pages:
        for pg in pages:
            if int(pg.get("page") or 1) == int(info["p"] or 1):
                dur = int(pg.get("duration") or 0)
                break
    if not dur:
        dur = int(data.get("duration") or 0)
    play = _playurl(bvid, cid, aid=info["aid"] or data.get("aid"))
    audio = _pick_audio(play)
    if not audio:
        raise RuntimeError("這部影片沒有可播放的音訊")
    headers = stream_headers()
    size = _probe_size(audio["url"], headers)
    return {
        "url": audio["url"],
        "duration": dur,
        "thumbnail": _thumb(data.get("pic") or ""),
        "title": title,
        "headers": headers,
        "filesize": size or 0,
        "mime": audio.get("mime") or "audio/mp4",
        "page_url": info["page_url"],
    }
