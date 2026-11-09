"""共用工具：字串清理與時間格式化"""


def clean_str(text):
    """清除字串中的無效 surrogate 字元（U+D800-U+DFFF）

    YouTube 歌名有時會含有這些字元，會讓 Discord 的 orjson crash。
    逐字元比對是最可靠的方式，因為 Python re 模組對 surrogate 處理不一致。
    """
    if not text:
        return text
    result = []
    for ch in text:
        if "\ud800" <= ch <= "\udfff":
            result.append("\ufffd")
        else:
            result.append(ch)
    return "".join(result)


def format_time(seconds):
    if seconds <= 0:
        return "0:00"
    seconds = int(seconds)
    h, m = divmod(seconds, 3600)
    m, s = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"


def parse_time_str(text):
    """把「1:23」「01:02:03」「83」「83.5」轉成秒數（float）；格式不對回傳 None"""
    if text is None:
        return None
    t = str(text).strip()
    if not t or len(t) > 16:
        return None
    parts = t.split(":")
    if len(parts) > 3:
        return None
    try:
        if len(parts) == 1:
            v = float(parts[0])
        else:
            nums = [int(x) for x in parts[:-1]] + [float(parts[-1])]
            if any(n < 0 for n in nums) or nums[-1] >= 60 or (len(nums) == 3 and nums[1] >= 60):
                return None
            v = 0.0
            for n in nums:
                v = v * 60 + n
    except ValueError:
        return None
    if v != v or v in (float("inf"), float("-inf")) or v < 0:
        return None
    return v


# ==================== 使用者輸入的歌曲網址 / 標題 ====================
MAX_URL_LEN = 2048
MAX_TITLE_LEN = 200


def valid_song_url(url):
    """只接受 http(s) 網址（擋掉 javascript:、亂打的字、超長字串）"""
    if not isinstance(url, str):
        return False
    url = url.strip()
    if not url or len(url) > MAX_URL_LEN or any(c.isspace() for c in url):
        return False
    low = url.lower()
    return low.startswith("https://") or low.startswith("http://")


def safe_title(title, url=""):
    """標題：非字串 / 空白 → 用網址；清掉無效字元並截到 200 字（避免 Discord 訊息超過 2000 字）"""
    t = clean_str(title) if isinstance(title, str) else ""
    t = " ".join((t or "").split())
    return (t[:MAX_TITLE_LEN] if t else (url or "")[:MAX_TITLE_LEN])
