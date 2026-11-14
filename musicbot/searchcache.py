"""搜尋結果暫存：同一個關鍵字短時間內重複搜尋，就不用再等 YouTube。

- 只放記憶體（不寫硬碟、不存音檔），每筆只有歌名 / 網址 / 長度 / 縮圖網址這些文字
- 最多 MAX_ENTRIES 筆，超過就丟最久沒用的（LRU）；每筆放 TTL 秒後過期
- key = (種類, 正規化後的關鍵字)：種類區分「面板搜尋 5 筆」「/play 文字點歌 1 筆」「Spotify 匯入找歌 1 筆」
- 失敗、沒有結果都不存；直接貼網址的解析（會拿到會過期的串流網址）不經過這裡
"""
import copy
import re
import threading
import time
import unicodedata
from collections import OrderedDict

MAX_ENTRIES = 300
TTL = 3600.0

_lock = threading.Lock()
_data = OrderedDict()       # key: (expires_at, results)
stats = {"hits": 0, "misses": 0, "stores": 0}

_URL_RE = re.compile(r"^\s*(https?://|www\.|[\w-]+\.(com|be|tv|net|org)/)", re.I)


def is_url(query):
    return bool(_URL_RE.match(query or ""))


def normalize(query):
    """全形 / 半形、大小寫、多餘空白都視為同一個關鍵字"""
    q = unicodedata.normalize("NFKC", query or "").casefold()
    return " ".join(q.split())


def _key(kind, query):
    return (kind, normalize(query))


def get(kind, query):
    """命中回傳結果的複本（list），沒有 / 過期回傳 None"""
    k = _key(kind, query)
    if not k[1]:
        return None
    now = time.monotonic()
    with _lock:
        item = _data.get(k)
        if item and item[0] > now:
            _data.move_to_end(k)
            stats["hits"] += 1
            return copy.deepcopy(item[1])
        if item:
            del _data[k]
        stats["misses"] += 1
    return None


def put(kind, query, results):
    """存一筆；空結果 / 空關鍵字 / 網址不存"""
    k = _key(kind, query)
    if not k[1] or not results or is_url(query):
        return
    with _lock:
        _data[k] = (time.monotonic() + TTL, copy.deepcopy(list(results)))
        _data.move_to_end(k)
        while len(_data) > MAX_ENTRIES:
            _data.popitem(last=False)
        stats["stores"] += 1


def clear():
    with _lock:
        _data.clear()


def size():
    with _lock:
        return len(_data)
