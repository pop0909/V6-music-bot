#!/usr/bin/env bash
# Music Bot V6 - 更新 yt-dlp（給 systemd 用，輸出進 journald）
#
#   update-ytdlp.sh                       只更新（musicbot.service 的 ExecStartPre 用；bot 本來就要啟動）
#   update-ytdlp.sh --restart-if-changed  每日排程（musicbot-update.service 用）：
#       有人在聽（機器人在任何伺服器的語音頻道裡）就整個跳過，今天不更新也不重啟，明天再試；
#       沒人在聽才更新，版本真的有變、而且還是沒人在聽才重啟 bot。
#       「有沒有人在聽」問本機面板 /api/ext/status（X-Ext-Token = .env 的 EXT_TOKEN，unit 用 EnvironmentFile 載入），
#       沒設 EXT_TOKEN / 連不上 / 回應看不懂 → 一律當作有人在聽（寧可跳過，也不在聽歌中途重啟）。
#       MUSICBOT_DEFER_MAX=N：有人在聽時改成每 MUSICBOT_DEFER_STEP 秒再看一次、最多 N 次（預設 0 = 直接跳過）
#
# 離線 / pip 失敗一律 exit 0，不會擋住 bot 啟動。
# 以 root 執行（系統層級 timer）時，pip 會用 MUSICBOT_USER 身分跑，避免 venv 檔案變成 root 擁有。
set -u

APP_DIR="${MUSICBOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="${MUSICBOT_PYTHON:-$APP_DIR/venv/bin/python}"
SERVICE="${MUSICBOT_SERVICE:-musicbot.service}"
RUN_USER="${MUSICBOT_USER:-}"
WEB_PORT="${WEB_PORT:-5000}"
DEFER_STEP="${MUSICBOT_DEFER_STEP:-600}"   # 秒
DEFER_MAX="${MUSICBOT_DEFER_MAX:-0}"       # 次（0 = 有人在聽就直接跳過）

restart=0
if [ "${1:-}" = "--restart-if-changed" ]; then
    restart=1
fi

log() { echo "[update-ytdlp] $*"; }

as_user() {
    if [ "$(id -u)" -eq 0 ] && [ -n "$RUN_USER" ] && [ "$RUN_USER" != "root" ]; then
        if command -v runuser >/dev/null 2>&1; then
            runuser -u "$RUN_USER" -- "$@"
        elif [ -x /usr/sbin/runuser ]; then
            /usr/sbin/runuser -u "$RUN_USER" -- "$@"
        else
            sudo -u "$RUN_USER" -H -- "$@"
        fi
    else
        "$@"
    fi
}

sysctl_cmd() {
    if [ "$(id -u)" -eq 0 ]; then
        systemctl "$@"
    else
        systemctl --user "$@"
    fi
}

ytdlp_version() {
    as_user "$PY" -c 'import yt_dlp.version as v; print(v.__version__)' 2>/dev/null || echo "none"
}

# 有人可能在聽（機器人在任何伺服器的語音頻道裡）→ 回傳 0；只有「確定沒人在聽」才回傳 1
# （Python 只有確定沒人在聽時用 exit 10；其他結束碼，包括 Python 本身出錯的 1，都算有人在聽）
# 任何錯誤（沒設 EXT_TOKEN、面板沒回應、HTTP 錯誤、JSON 不對、不是擁有者金鑰）都當作有人在聽
bot_busy() {
    EXT_TOKEN="${EXT_TOKEN:-}" "$PY" - "$WEB_PORT" <<'PYEOF' 2>/dev/null
import json, os, sys, urllib.request
tok = os.environ.get("EXT_TOKEN", "").strip().strip('"').strip("'")
if not tok:
    sys.exit(0)
try:
    req = urllib.request.Request(f"http://127.0.0.1:{sys.argv[1]}/api/ext/status", headers={"X-Ext-Token": tok})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read().decode("utf-8"))
    n = d.get("voice_guilds")
    idle = d.get("status") == "ok" and d.get("owner") is True and isinstance(n, int) and not isinstance(n, bool) and n == 0
    sys.exit(10 if idle else 0)
except Exception:
    sys.exit(0)
PYEOF
    [ $? -ne 10 ]
}

# 等到沒人在聽：沒人 → 回傳 0；等了 DEFER_MAX 次還是有人（或判斷不出來）→ 回傳 1
wait_idle() {
    local i
    for ((i = 0; ; i++)); do
        if ! bot_busy; then
            return 0
        fi
        if [ "$i" -ge "$DEFER_MAX" ]; then
            return 1
        fi
        log "有人可能在聽，$((DEFER_STEP / 60)) 分鐘後再檢查（第 $((i + 1))/$DEFER_MAX 次）"
        sleep "$DEFER_STEP"
    done
}

if [ ! -x "$PY" ]; then
    log "找不到 Python：$PY（請先執行 deploy/install.sh 建立 venv），略過更新"
    exit 0
fi

running=0
if [ "$restart" -eq 1 ] && sysctl_cmd is-active --quiet "$SERVICE"; then
    running=1
    # 先確認沒人在聽才動 venv：bot 執行中換掉 yt-dlp 的檔案又不能重啟，可能讓之後才載入的模組新舊混用
    if ! wait_idle; then
        log "有人可能在聽（或判斷不出來），今天跳過更新，明天再試"
        exit 0
    fi
fi

before="$(ytdlp_version)"
log "目前 yt-dlp 版本：$before"
if ! as_user timeout 180 "$PY" -m pip install -U -q --disable-pip-version-check \
        --no-input --retries 1 --timeout 15 yt-dlp; then
    log "pip 更新失敗（可能離線），沿用目前版本"
    exit 0
fi
after="$(ytdlp_version)"

if [ "$before" = "$after" ]; then
    log "已是最新版（$after）"
    exit 0
fi
log "yt-dlp 已更新：$before -> $after"

if [ "$restart" -ne 1 ]; then
    exit 0   # ExecStartPre：bot 接著就會啟動並載入新版
fi

if [ "$running" -ne 1 ]; then
    log "$SERVICE 沒有在執行，下次啟動就會使用新版"
    exit 0
fi

# 更新花了一點時間：重啟前再確認一次沒人在聽
if bot_busy; then
    log "剛剛有人開始聽，不重啟；下次重啟 bot 時就會使用新版"
    exit 0
fi

log "沒人在聽，重啟 $SERVICE 以套用新版 yt-dlp"
sysctl_cmd try-restart "$SERVICE" || log "重啟失敗"
exit 0
