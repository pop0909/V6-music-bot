"""擁有者是誰（全專案共用一份判斷）。

順序：.env 的 OWNER_ID（Discord 使用者 ID，純數字）→ 沒設就用 Discord 應用程式的擁有者（團隊應用程式 = 團隊擁有者）。
OWNER_ID 有填但不是純數字 → 寫一次警告並當作沒設（退回應用程式擁有者），不會整個壞掉。
另外「成員管理」連結為「我的 Discord」的帳號也算擁有者（見 discordauth.owner_ids）。
"""
import os

from .config import log

_warned = set()


def configured_id():
    """.env 的 OWNER_ID（字串）；沒設 / 格式錯 → None"""
    raw = (os.getenv("OWNER_ID") or "").strip().strip('"').strip("'")
    if not raw:
        return None
    if raw.isdigit():
        return raw
    if raw not in _warned:
        _warned.add(raw)
        log.warning("[擁有者] .env 的 OWNER_ID 格式不對（應該是 17～20 位數字的 Discord 使用者 ID），先改用應用程式擁有者")
    return None


def _from_app(app):
    if not app:
        return None
    owner = app.team.owner if getattr(app, "team", None) and app.team.owner else getattr(app, "owner", None)
    return str(owner.id) if owner else None


def app_owner_id(client=None):
    """機器人應用程式的擁有者（只看已載入的 client.application，不連網）；不知道 → None"""
    try:
        if client is None:
            from .bot import client
        return _from_app(client.application)
    except Exception:
        return None


def owner_id(client=None):
    """擁有者的 Discord ID（字串）：OWNER_ID > 應用程式擁有者；不知道 → None"""
    return configured_id() or app_owner_id(client)


async def resolve(client):
    """跟 owner_id 一樣，但 client.application 還沒載入時會向 Discord 查一次（私訊通知用）"""
    oid = owner_id(client)
    if oid:
        return oid
    try:
        return _from_app(await client.application_info())
    except Exception as e:
        log.warning(f"[擁有者] 取得應用程式擁有者失敗：{e}")
        return None
