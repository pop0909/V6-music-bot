"""台北時區（全專案共用一份）。Windows 沒裝 tzdata 時退回固定 +8（台北沒有日光節約，結果一樣）。"""
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    TAIPEI = ZoneInfo("Asia/Taipei")
except Exception:
    TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")


def taipei(ts):
    """epoch 秒 → 台北時間的 datetime"""
    return datetime.fromtimestamp(ts, TAIPEI)
