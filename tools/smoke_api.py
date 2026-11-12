"""API 煙霧 / 模糊測試：對模擬器（sim.py，5055 埠）的每個 /api 路由丟各種奇怪輸入，
任何 5xx 或非 JSON 回應都算問題。用法：python tools/smoke_api.py"""
import json, os, sys, urllib.request, urllib.error
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("DISCORD_TOKEN", "x")
from musicbot import webapi, extapi, userlistapi  # noqa: F401  註冊路由

BASE = os.getenv("SMOKE_BASE", "http://127.0.0.1:5055")
OWNER = os.getenv("SMOKE_OWNER_TOKEN", "OWNER-TOKEN-TEST")
SKIP = {"/api/progress-stream", "/api/update-ytdlp", "/api/restart", "/api/system/update"}
BODIES = [None, b"not json", b"[]", b"null", b"123", json.dumps({"url": None, "guild_id": "abc", "title": 5, "name": ["x"], "volume": "lol", "index": -1, "seconds": 1e309 if False else 1e12, "songs": "x", "id": {}}).encode(),
          json.dumps({"url": "https://www.youtube.com/watch?v=AAAAAAAAAAA", "guild_id": 111, "title": "t" * 5000, "name": "名" * 300, "volume": 999, "index": 99999}).encode()]
problems = []
COOKIE = [None]


def call(method, path, body, token, cookie=True):
    h = {"Host": "localhost:5000"}
    if body is not None: h["Content-Type"] = "application/json"
    if token: h["X-Ext-Token"] = token
    elif cookie and COOKIE[0]: h["Cookie"] = COOKIE[0]
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def fill(rule):
    p = rule.rule
    for k, v in (("<int:gid>", "111"), ("<gid>", "111"), ("<int:guild_id>", "111"), ("<guild_id>", "111"), ("<mid>", "nosuch"), ("<lid>", "nosuch"), ("<jid>", "nosuch"), ("<int:index>", "0"), ("<index>", "0")):
        p = p.replace(k, v)
    return None if "<" in p else p


def login():
    """用擁有者金鑰登入面板，拿到 session cookie（之後的面板 API 都帶著它）"""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k): return None
    req = urllib.request.Request(BASE + "/login", data=urllib.parse.urlencode({"key": OWNER}).encode(), method="POST",
                                 headers={"Host": "localhost:5000", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=20)
    except urllib.error.HTTPError as e:
        sc = e.headers.get("Set-Cookie") or ""
        if e.code == 302 and sc:
            COOKIE[0] = sc.split(";", 1)[0]
            return
        problems.append(f"登入失敗 {e.code}")


import urllib.parse  # noqa: E402
rules = [r for r in webapi.flask_app.url_map.iter_rules() if r.rule.startswith("/api")]
n = 0
# 沒登入：每個 /api 路由都要 401（擴充功能路由沒金鑰也是 401）
for r in rules:
    path = fill(r)
    if not path or path in SKIP or any(path.startswith(s) for s in SKIP): continue
    for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
        code, ct, data = call(m, path, b"{}" if m != "GET" else None, None, cookie=False); n += 1
        if code != 401:
            problems.append(f"未登入 {m} {path} → {code}（應該 401）")
login()
for r in rules:
    path = fill(r)
    if not path or path in SKIP or any(path.startswith(s) for s in SKIP): continue
    for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
        if m in ("POST", "DELETE") and ("restart" in path or "update" in path): continue
        for tok in ((None, "bad-token", OWNER) if path.startswith("/api/ext") else (None,)):
            for body in (BODIES if m in ("POST", "PUT", "PATCH") else [None]):
                if m == "DELETE" and "members" in path and tok == OWNER: continue
                code, ct, data = call(m, path, body, tok); n += 1
                if code >= 500 or ("json" not in ct and code != 204 and not path.endswith("stream")):
                    problems.append(f"{m} {path} tok={'owner' if tok == OWNER else tok} body={(body or b'')[:40]!r} → {code} {ct} {data[:120]!r}")
print(f"送出 {n} 個請求")
for p in problems[:60]: print(" ✗", p)
print(f"問題 {len(problems)} 項")
sys.exit(1 if problems else 0)
