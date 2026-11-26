"""Bilibili：網址正規化、公開 API 搜尋與解析音訊（DASH m4a）。

B 站影片頁對機房 IP 常回 HTTP 412，yt-dlp 走網頁解析會失敗。改走公開 API：
- 搜尋：/x/web-interface/wbi/search/type（WBI 簽名）；失敗或回 HTML 再試 search/all
- 資訊：/x/web-interface/view（多 P 在 data.pages）
- 合集：/x/polymer/web-space/seasons_archives_list（分頁）
- 公開收藏夾：/x/v3/fav/resource/list（分頁）
- 音訊：/x/player/playurl（fnval=16 DASH）；失敗再試 WBI 簽名
不支援番劇。合集／收藏夾最多加入 COLLECTION_CAP 首。

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


def _host_matches(h, names):
    return any(h == x or h.endswith("." + x) for x in names)


def is_host(url):
    """是否為 B 站頁面主機（含子網域、短網址）。不是音訊 CDN。"""
    return _host_matches(_host(url), _HOSTS)


def is_cdn_host(url):
    return _host_matches(_host(url), _CDN_HOST_SUFFIXES)


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


def _page_url(url):
    """解析 B 站頁面網址（不含短網址）。回傳 urlparse 結果，不合法則 None。"""
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
    if not _host_matches(host, _HOSTS) or _host_matches(host, _SHORT_HOSTS):
        return None
    return u


def parse(url):
    """從網址取出 bvid / aid / 分 P。短網址需先 resolve_short。"""
    u = _page_url(url)
    if not u:
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


# 合集／公開收藏夾（番劇不在範圍內）
COLLECTION_CAP = 200
SEASON_PAGE_SIZE = 30
FAV_PAGE_SIZE = 20
_BANGUMI_RE = re.compile(r"/(?:bangumi|cheese)(?:/|$)", re.I)
_ML_RE = re.compile(r"(?:^|/)ml(\d+)", re.I)


def parse_resource(url):
    """辨識影片、合集、公開收藏夾或番劇。短網址需先 resolve_short。"""
    u = _page_url(url)
    if not u:
        return None
    host = (u.hostname or "").lower().rstrip(".")
    path = u.path or ""
    q = urllib.parse.parse_qs(u.query or "", keep_blank_values=True)
    def _q(name):
        v = (q.get(name) or [None])[0]
        return v
    def _num(name):
        v = _q(name)
        if v and str(v).isdigit():
            return int(v)
        return None

    if _BANGUMI_RE.search(path):
        return {"kind": "bangumi"}
    if "channel/seriesdetail" in path or (_q("type") or "").lower() == "series":
        return {"kind": "series"}

    mid = None
    m = re.search(r"space\.bilibili\.com", host) or host.endswith(".bilibili.com") or host == "bilibili.com"
    sm = re.search(r"/(\d{1,16})(?:/|$)", path)
    if sm and ("space.bilibili.com" in host or path.startswith("/list/") or "/medialist/" in path):
        mid = int(sm.group(1))

    sid = _num("sid") or _num("business_id")
    fid = _num("fid")
    ml = _ML_RE.search(path)
    if ml:
        fid = int(ml.group(1))

    # space 合集頁
    cm = re.search(r"/(\d{1,16})/channel/collectiondetail", path)
    if cm:
        mid = int(cm.group(1))
        sid = sid or _num("sid")
        if sid:
            return {"kind": "season", "mid": mid, "season_id": sid}
    lm = re.search(r"/(\d{1,16})/lists/(\d+)", path)
    if lm:
        mid, sid = int(lm.group(1)), int(lm.group(2))
        return {"kind": "season", "mid": mid, "season_id": sid}

    # www 播放列表：/list/{mid}?sid= 或 /list/ml{fid}
    if re.search(r"/list/", path):
        if fid:
            return {"kind": "favlist", "fid": fid}
        if mid and sid:
            bvid = None
            bm = _BV_RE.search(_q("bvid") or path + "&" + (u.query or ""))
            if bm:
                bvid = bm.group(1)
            if bvid:
                video = parse(canonical(bvid=bvid, p=_num("p") or 1)) or {"bvid": bvid, "aid": None, "p": _num("p") or 1}
                video.update(kind="video", mid=mid, season_id=sid)
                return video
            return {"kind": "season", "mid": mid, "season_id": sid}

    if "/medialist/" in path:
        if fid:
            return {"kind": "favlist", "fid": fid}
        if mid and sid:
            return {"kind": "season", "mid": mid, "season_id": sid}
        biz = (_q("business") or "")
        if mid and (sid or "collection" in biz):
            if sid:
                return {"kind": "season", "mid": mid, "season_id": sid}

    if "/favlist" in path:
        mid = mid or (int(sm.group(1)) if sm else None)
        ftype = (_q("ftype") or "").lower()
        if fid and ftype == "collect":
            if mid:
                return {"kind": "season", "mid": mid, "season_id": fid}
        if fid:
            return {"kind": "favlist", "fid": fid}

    video = parse(url)
    if video:
        video = dict(video)
        video["kind"] = "video"
        return video
    return None


def canonical_resource(info):
    if not info:
        return None
    kind = info.get("kind")
    if kind == "season" and info.get("mid") and info.get("season_id"):
        return f"https://space.bilibili.com/{int(info['mid'])}/channel/collectiondetail?sid={int(info['season_id'])}"
    if kind == "favlist" and info.get("fid"):
        return f"https://www.bilibili.com/medialist/detail/ml{int(info['fid'])}"
    if kind == "video":
        return canonical(bvid=info.get("bvid"), aid=info.get("aid"), p=info.get("p") or 1)
    return None


def _canonical_any(url):
    return normalize(url) or canonical_resource(parse_resource(url))


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
    if not _host_matches(host, _SHORT_HOSTS):
        return _canonical_any(raw)
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
                n = _canonical_any(final)
                return n or final
        except urllib.error.HTTPError as e:
            loc = ""
            if e.headers:
                loc = e.headers.get("Location") or ""
            nxt = loc or e.geturl() or ""
            if not nxt or nxt == current:
                n = _canonical_any(e.geturl() or current)
                return n
            if nxt.startswith("/"):
                p = urllib.parse.urlparse(current)
                nxt = f"{p.scheme}://{p.netloc}{nxt}"
            n = _canonical_any(nxt)
            if n:
                return n
            current = nxt
            continue
        except Exception as ex:
            log.info(f"[Bilibili] 短網址解析失敗：{ex!r}"[:200])
            return None
    return _canonical_any(current)


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
class BiliApiFormatError(RuntimeError):
    """API 回了非 JSON（例如舊搜尋端點的 HTML 錯誤頁）。"""


def _looks_like_html(body, headers=None):
    ctype = ""
    if headers is not None:
        ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    if "text/html" in ctype and "json" not in ctype:
        return True
    head = (body or b"").lstrip()[:64].lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html") or head.startswith(b"<head")


def _api_json(path_or_url, params=None, signed=False, referer=None):
    url = path_or_url if path_or_url.startswith("http") else API + path_or_url
    params = dict(params or {})
    if signed:
        params = _wbi_sign(params)
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = None
    if referer:
        headers = default_headers()
        headers["Referer"] = referer
    status, hdrs, body, _final = _http(url, headers=headers)
    if status != 200:
        raise RuntimeError(f"B 站 API 回應 {status}")
    if _looks_like_html(body, hdrs):
        raise BiliApiFormatError("B 站 API 回傳的不是 JSON")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError as e:
        raise BiliApiFormatError("B 站 API 回傳無法解析") from e
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


def _search_items(data):
    """從 search/type 或 search/all（分組）的回應取出影片列。"""
    block = (data or {}).get("data") or {}
    result = block.get("result")
    if result is None:
        result = block.get("video") or []
    items = []
    for r in result or []:
        if not isinstance(r, dict):
            continue
        if "result_type" in r and isinstance(r.get("data"), list):
            if r.get("result_type") == "video":
                items.extend(x for x in r["data"] if isinstance(x, dict))
            continue
        items.append(r)
    return items


def _search_rows(items, limit):
    out = []
    for r in items:
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


# 搜尋端點：dcbot 上舊的 search/type 會回 HTML 錯誤頁；WBI 與 search/all 正常。
_SEARCH_ENDPOINTS = (
    ("/x/web-interface/wbi/search/type", {"search_type": "video", "page": 1}, True),
    ("/x/web-interface/search/all/v2", {"page": 1}, False),
    ("/x/web-interface/search/all", {"page": 1}, False),
)


def search(keyword, limit=5):
    q = (keyword or "").strip()
    if not q:
        return []
    last_err = None
    for path, extra, signed in _SEARCH_ENDPOINTS:
        params = dict(extra)
        params["keyword"] = q
        try:
            data = _api_json(path, params, signed=signed)
        except (BiliApiFormatError, RuntimeError) as e:
            last_err = e
            log.info(f"[Bilibili] 搜尋改試下一個端點（{path}：{e}）")
            continue
        if data.get("code") != 0:
            last_err = RuntimeError(data.get("message") or "B 站搜尋失敗")
            log.info(f"[Bilibili] 搜尋改試下一個端點（{path}：{last_err}）")
            continue
        return _search_rows(_search_items(data), limit)
    raise last_err or RuntimeError("B 站搜尋失敗")


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


def _song(url, title, duration=0, thumbnail=""):
    return {
        "url": url,
        "title": clean_str(title or "未知")[:200],
        "duration": int(duration or 0),
        "thumbnail": _thumb(thumbnail or ""),
    }


def _collection_meta(season, mid=None):
    if not isinstance(season, dict):
        return None
    sid = season.get("id") or season.get("season_id")
    if not sid:
        return None
    count = int(season.get("ep_count") or season.get("total") or 0)
    mid = mid or season.get("mid")
    title = clean_str(season.get("title") or season.get("name") or "合集")
    return {"id": int(sid), "title": title, "count": count, "mid": int(mid) if mid else None}


def _songs_from_archives(archives, cap):
    out = []
    for a in archives or []:
        if not isinstance(a, dict):
            continue
        bvid = a.get("bvid") or ""
        if not bvid or not _BV_RE.fullmatch(bvid):
            continue
        out.append(_song(canonical(bvid=bvid), a.get("title"), a.get("duration"), a.get("pic")))
        if len(out) >= cap:
            break
    return out


def _songs_from_episodes(season, cap):
    out, seen = [], set()
    for sec in (season or {}).get("sections") or []:
        for ep in (sec.get("episodes") or []) if isinstance(sec, dict) else []:
            if not isinstance(ep, dict):
                continue
            bvid = ep.get("bvid") or ""
            if not bvid or not _BV_RE.fullmatch(bvid) or bvid in seen:
                continue
            seen.add(bvid)
            arc = ep.get("arc") if isinstance(ep.get("arc"), dict) else {}
            page = ep.get("page") if isinstance(ep.get("page"), dict) else {}
            title = ep.get("title") or arc.get("title") or ""
            dur = page.get("duration") or arc.get("duration") or ep.get("duration") or 0
            thumb = arc.get("pic") or ep.get("cover") or ""
            out.append(_song(canonical(bvid=bvid), title, dur, thumb))
            if len(out) >= cap:
                return out
    return out


def _songs_from_medias(medias, cap, already=0):
    out = []
    for m in medias or []:
        if not isinstance(m, dict):
            continue
        # type 2 = 普通投稿；有 ogv 的番劇略過
        typ = m.get("type")
        if typ not in (None, 2, "2"):
            continue
        if m.get("ogv"):
            continue
        bvid = m.get("bvid") or m.get("bv_id") or ""
        if not bvid or not _BV_RE.fullmatch(bvid):
            continue
        out.append(_song(canonical(bvid=bvid), m.get("title"), m.get("duration"), m.get("cover") or m.get("pic")))
        if already + len(out) >= cap:
            break
    return out


def _resolve_season(mid, season_id, cap=COLLECTION_CAP):
    mid, season_id = int(mid), int(season_id)
    songs, title, total = [], "合集", 0
    page_num = 1
    meta = None
    while len(songs) < cap:
        referer = f"https://space.bilibili.com/{mid}/channel/collectiondetail?sid={season_id}"
        data = _api_json("/x/polymer/web-space/seasons_archives_list", {
            "mid": mid, "season_id": season_id, "sort_reverse": "false",
            "page_num": page_num, "page_size": SEASON_PAGE_SIZE,
        }, referer=referer)
        if data.get("code") != 0 or not data.get("data"):
            if not songs:
                msg = data.get("message") or "找不到這個合集"
                if data.get("code") in (-404, 404, -403, 403):
                    msg = "找不到這個合集，或不是公開的"
                raise RuntimeError(msg)
            break
        block = data["data"]
        meta = block.get("meta") or meta or {}
        page = block.get("page") or {}
        total = int(meta.get("total") or page.get("total") or total or 0)
        title = clean_str(meta.get("name") or title)
        batch = _songs_from_archives(block.get("archives"), cap - len(songs))
        if not batch:
            break
        songs.extend(batch)
        if total and len(songs) >= total:
            break
        if len(block.get("archives") or []) < SEASON_PAGE_SIZE:
            break
        page_num += 1
        if page_num > 50:
            break
    if not songs:
        raise RuntimeError("這個合集裡沒有可加入的影片")
    if not total:
        total = len(songs)
    return {
        "kind": "season",
        "title": title,
        "songs": songs[:cap],
        "truncated": len(songs) >= cap and total > cap,
        "total": max(total, len(songs)),
        "collection": {"id": season_id, "title": title, "count": max(total, len(songs)), "mid": mid},
        "expanded": True,
    }


def _resolve_fav(fid, cap=COLLECTION_CAP):
    fid = int(fid)
    songs, title, total = [], "收藏夾", 0
    pn = 1
    while len(songs) < cap:
        data = _api_json("/x/v3/fav/resource/list", {
            "media_id": fid, "pn": pn, "ps": FAV_PAGE_SIZE, "platform": "web",
        }, referer=f"https://www.bilibili.com/medialist/detail/ml{fid}")
        if data.get("code") != 0:
            msg = data.get("message") or "找不到這個收藏夾"
            if data.get("code") in (-403, 403, -404, 404, 710000, 83001, 83002):
                msg = "這個收藏夾不是公開的，或需要登入才能看"
            raise RuntimeError(msg)
        if not data.get("data"):
            if not songs:
                raise RuntimeError("找不到這個收藏夾，或不是公開的")
            break
        block = data["data"]
        info = block.get("info") or {}
        title = clean_str(info.get("title") or title)
        total = int(info.get("media_count") or total or 0)
        batch = _songs_from_medias(block.get("medias"), cap, already=len(songs))
        songs.extend(batch)
        if not block.get("has_more"):
            break
        if not block.get("medias"):
            break
        pn += 1
        if pn > 50:
            break
    if not songs:
        raise RuntimeError("這個收藏夾裡沒有可加入的影片")
    if not total:
        total = len(songs)
    return {
        "kind": "favlist",
        "title": title,
        "songs": songs[:cap],
        "truncated": len(songs) >= cap and total > cap,
        "total": max(total, len(songs)),
        "collection": None,
        "expanded": True,
    }


def _resolve_input(url):
    raw = (url or "").strip()
    host = _host(raw if "://" in raw else "https://" + raw)
    if _host_matches(host, _SHORT_HOSTS):
        return resolve_short(raw) or raw
    return raw


def resolve_list(url, *, expand_season=False, cap=COLLECTION_CAP):
    """解析影片、合集或公開收藏夾。回傳 {kind, title, songs, truncated, total, collection, expanded}。"""
    cap = max(1, int(cap or COLLECTION_CAP))
    raw = _resolve_input(url)
    info = parse_resource(raw) or parse_resource(url)
    if info is None:
        # 也許是短網址已轉成影片
        pi = page_info(raw)
        if pi:
            info = dict(pi)
            info["kind"] = "video"
    if not info:
        raise RuntimeError("不是有效的 B 站影片、合集或公開收藏夾網址")
    kind = info.get("kind")
    if kind == "bangumi":
        raise RuntimeError("暫不支援番劇網址")
    if kind == "series":
        raise RuntimeError("暫不支援頻道「系列」網址，請改用合集或公開收藏夾")
    if kind == "season":
        return _resolve_season(info["mid"], info["season_id"], cap=cap)
    if kind == "favlist":
        return _resolve_fav(info["fid"], cap=cap)

    # 單支影片（可能屬於合集）
    if not info.get("bvid") and not info.get("aid"):
        raise RuntimeError("不是有效的 B 站影片網址")
    data = view(bvid=info.get("bvid"), aid=info.get("aid"))
    songs = parts_from_view(data, bvid=info.get("bvid") or data.get("bvid"), aid=info.get("aid") or data.get("aid"))
    raw_q = (url or "").strip()
    specified = bool(re.search(r"(?:^|[?&])p=\d+", raw_q.split("#")[0]))
    if specified and info.get("p") and len(songs) >= info["p"]:
        songs = [songs[info["p"] - 1]]
    season = data.get("ugc_season") if isinstance(data.get("ugc_season"), dict) else None
    # 網址已帶合集編號時也可展開
    if not season and info.get("season_id") and info.get("mid"):
        season = {"id": info["season_id"], "mid": info["mid"]}
    col = _collection_meta(season, mid=info.get("mid"))
    if col and not col.get("count"):
        col["count"] = int((season or {}).get("ep_count") or 0)
    if expand_season and (col or (season and (season.get("id") or season.get("season_id")))):
        mid = (col or {}).get("mid") or (season or {}).get("mid") or info.get("mid")
        sid = (col or {}).get("id") or (season or {}).get("id") or (season or {}).get("season_id") or info.get("season_id")
        if mid and sid:
            try:
                return _resolve_season(mid, sid, cap=cap)
            except RuntimeError:
                fallback = _songs_from_episodes(season, cap)
                if fallback:
                    title = (col or {}).get("title") or clean_str((season or {}).get("title") or "合集")
                    return {
                        "kind": "season", "title": title, "songs": fallback,
                        "truncated": False, "total": len(fallback),
                        "collection": col, "expanded": True,
                    }
                raise
    title = songs[0]["title"].split("（P")[0] if songs else clean_str(data.get("title") or "未知")
    offer = col if col and col.get("count", 0) > 1 and not expand_season else None
    return {
        "kind": "video",
        "title": title,
        "songs": songs,
        "truncated": False,
        "total": len(songs),
        "collection": offer,
        "expanded": False,
    }


def resolve_for_queue(url, *, expand_season=False, cap=COLLECTION_CAP):
    """貼網址要加入佇列：單 P 回 1 首；未指定 P 的多 P 影片回全部；合集／公開收藏夾回清單（有上限）。"""
    return resolve_list(url, expand_season=expand_season, cap=cap)["songs"]


def lookup_ytdl_shape(url):
    """給 Discord /play 用，形狀接近 yt-dlp extract_info。"""
    result = resolve_list(url)
    songs = result.get("songs") or []
    if not songs:
        return None
    title = result.get("title") or songs[0]["title"].split("（P")[0]
    if len(songs) == 1:
        s = songs[0]
        return {"id": s["url"], "webpage_url": s["url"], "title": s["title"], "duration": s["duration"],
                "thumbnail": s.get("thumbnail") or ""}
    return {"title": title, "entries": [
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
