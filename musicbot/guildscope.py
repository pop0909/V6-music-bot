"""伺服器權限：網頁面板 / 擴充功能的使用者只能看、只能操作「自己有加入」的 Discord 伺服器。

誰算在哪些伺服器：
- 用 Discord 登入 → 機器人成員快取裡有這個 Discord 帳號的伺服器（機器人開了 members intent，快取是完整的）
- 擁有者（Discord 登入 / 擁有者金鑰 / 舊的金鑰登入）→ 擁有者的 Discord（「我的 Discord」連結 > OWNER_ID > 應用程式擁有者）
  所在的伺服器；查不到擁有者的 Discord 或擁有者不在任何一個伺服器 → 全部（後備，避免把擁有者鎖在外面）
- 成員金鑰（擴充功能 / 舊的金鑰登入）→ 成員管理裡連結的 Discord 帳號所在的伺服器；沒連結 → 沒有任何伺服器

「目前的伺服器」（active guild）每個人各一份（Discord 帳號為準，同一個人的面板分頁和擴充功能共用）：
- 自己選過而且還能用 → 用自己的
- 沒選過 → 跟「全域預設」（擁有者最後選的 / 最後從網頁加入語音的伺服器），前提是自己也在那個伺服器
- 都沒有 → （需要一個伺服器的操作才會用到）機器人在語音中的、自己能用的第一個伺服器 → 自己能用的第一個伺服器
所以一般成員切換伺服器只影響自己，不會把別人的面板 / 擴充功能拉走；擁有者的選擇仍是大家的預設。
"""
from flask import g, jsonify, request

from . import state as _state
from .state import guild_names, state_lock, voice_clients

FORBIDDEN = "你不在這個 Discord 伺服器裡，不能查看或操作它"

_active = {}            # 使用者 → 自己選的伺服器 id


def _client():
    from .bot import client
    return client


def current_ident():
    """這個請求是誰（擴充功能 API 用 g.ext_user，其他網頁 API 用 g.panel_user）"""
    return g.get("ext_user") or g.get("panel_user")


def discord_id_of(ident):
    """身分 → Discord 使用者 id（字串）；不知道 → None"""
    if not ident:
        return None
    did = ident.get("discord_id")
    if did and str(did).isdigit():
        return str(did)
    try:
        if ident.get("owner"):
            from . import discordauth
            return discordauth.owner_discord_id()
        from . import userlists
        did = userlists.discord_links().get(ident.get("id"))
        return str(did) if did and str(did).isdigit() else None
    except Exception:
        return None


def member_guilds(did):
    """這個 Discord 帳號在機器人所在的哪些伺服器裡（看機器人的成員快取）"""
    try:
        guilds = list(_client().guilds)
        uid = int(did)
    except Exception:
        return set()
    out = set()
    for gd in guilds:
        try:
            if gd.get_member(uid) is not None:
                out.add(gd.id)
        except Exception:
            pass
    return out


def allowed_guilds(ident):
    """可用的伺服器 id 集合；None = 全部（擁有者後備）"""
    if not ident:
        return set()
    did = discord_id_of(ident)
    gids = member_guilds(did) if did else set()
    if ident.get("owner") and not gids:
        return None
    return gids


def may_use(gid, ident=None, allowed=False):
    ident = ident if ident is not None else current_ident()
    allowed = allowed_guilds(ident) if allowed is False else allowed
    return allowed is None or int(gid) in allowed


def filter_guild_list(items, ident=None):
    """get_guild_list() 的結果只留自己能用的"""
    allowed = allowed_guilds(ident if ident is not None else current_ident())
    if allowed is None:
        return items
    return [x for x in items if int(x["id"]) in allowed]


def _user_key(ident):
    did = discord_id_of(ident)
    return f"d:{did}" if did else f"k:{(ident or {}).get('id')}"


def _ok(gid, allowed):
    return bool(gid) and gid in guild_names and (allowed is None or gid in allowed)


def chosen_guild(ident=None):
    """面板顯示的「目前伺服器」：自己選的 > 全域預設；都不能用 → None"""
    ident = ident if ident is not None else current_ident()
    allowed = allowed_guilds(ident)
    with state_lock:
        for gid in (_active.get(_user_key(ident)), _state.active_guild_id):
            if _ok(gid, allowed):
                return gid
    return None


def default_guild(ident=None):
    """請求沒帶 guild_id 時要操作哪個伺服器：目前伺服器 > 在語音中的 > 第一個能用的；沒有 → 0"""
    ident = ident if ident is not None else current_ident()
    gid = chosen_guild(ident)
    if gid:
        return gid
    allowed = allowed_guilds(ident)
    with state_lock:
        for gid, vc in list(voice_clients.items()):
            if vc and vc.is_connected() and _ok(gid, allowed):
                return gid
        for gid in list(guild_names):
            if _ok(gid, allowed):
                return gid
    return 0


def set_chosen(gid, ident=None):
    """記住這個人選的伺服器（None = 清除）；擁有者的選擇同時是全域預設"""
    ident = ident if ident is not None else current_ident()
    with state_lock:
        if gid is None:
            _active.pop(_user_key(ident), None)
        else:
            _active[_user_key(ident)] = gid
    if ident and ident.get("owner"):
        from .webcore import _set_active_guild
        _set_active_guild(gid)


def reset_for_tests():
    with state_lock:
        _active.clear()


# ---------- 每個 API 請求的檢查 ----------
def requested_guild_ids():
    """請求裡帶的 guild_id（網址參數、JSON、/api/guilds/<gid>/…）；0 / 空的不算（交給各路由的預設）"""
    out = []
    vals = [request.args.get("guild_id")]
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        d = request.get_json(silent=True)
        if isinstance(d, dict):
            vals.append(d.get("guild_id"))
    vals.append((request.view_args or {}).get("gid"))
    for v in vals:
        if v in (None, "", 0, "0") or isinstance(v, bool):
            continue
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue        # 格式錯：交給路由回原本的錯誤
    return out


def check_request():
    """before_request：帶了別人的伺服器 → 403"""
    if not request.path.startswith("/api/") or request.method == "OPTIONS":
        return None
    ident = current_ident()
    if not ident:
        return None
    gids = requested_guild_ids()
    if not gids:
        return None
    allowed = allowed_guilds(ident)
    for gid in gids:
        if not may_use(gid, ident, allowed):
            return jsonify({"status": "error", "error": "forbidden_guild", "message": FORBIDDEN}), 403
    return None
