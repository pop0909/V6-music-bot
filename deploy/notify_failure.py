"""systemd OnFailure 用：musicbot 服務放棄重啟（連續當機）時，用 Discord REST API 私訊擁有者。
不依賴 discord.py，只用標準函式庫。用法：python deploy/notify_failure.py [unit 名稱]"""
import json
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def load_env():
    p = os.path.join(ROOT, ".env")
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def call(method, path, token, body=None):
    req = urllib.request.Request("https://discord.com/api/v10" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                                          "User-Agent": "DiscordBot (musicbot, 1.0)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def main():
    load_env()
    unit = sys.argv[1] if len(sys.argv) > 1 else "musicbot"
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        return
    owner = (os.environ.get("OWNER_ID") or "").strip()
    if not owner.isdigit():
        app = call("GET", "/oauth2/applications/@me", token)
        team = app.get("team")
        owner = (team or {}).get("owner_user_id") or app["owner"]["id"]
    try:
        tail = subprocess.run(["journalctl", "-u", unit, "-n", "15", "--no-pager", "-o", "cat"],
                              capture_output=True, text=True, timeout=10).stdout[-1200:]
    except Exception:
        tail = ""
    dm = call("POST", "/users/@me/channels", token, {"recipient_id": str(owner)})
    msg = (f"🚨 **音樂機器人通知**\n`{unit}` 連續當機，systemd 已停止自動重啟。"
           f"請上伺服器檢查：`journalctl -u {unit} -n 100`\n```{tail or '（沒有日誌）'}```")
    call("POST", f"/channels/{dm['id']}/messages", token, {"content": msg[:1990]})


if __name__ == "__main__":
    main()
