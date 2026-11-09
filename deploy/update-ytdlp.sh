#!/usr/bin/env bash
# Music Bot V5 - 更新 yt-dlp（給 systemd 用，輸出進 journald）
#
#   update-ytdlp.sh                       只更新（musicbot.service 的 ExecStartPre 用；bot 本來就要啟動）
#   update-ytdlp.sh --restart-if-changed  更新；版本有變才重啟 bot（musicbot-update.service 用）
#                                         有伺服器正在語音中就延後，每 10 分鐘再看，最多等 6 小時
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
DEFER_MAX="${MUSICBOT_DEFER_MAX:-36}"      # 次（600s x 36 = 6 小時）

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

# 有任何伺服器在語音中（可能正在聽歌）→ 回傳 0
bot_busy() {
    "$PY" - "$WEB_PORT" <<'PYEOF' 2>/dev/null
import json, sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/guilds", timeout=5) as r:
        guilds = json.loads(r.read().decode("utf-8")).get("guilds", [])
    sys.exit(0 if any(g.get("active") for g in guilds) else 1)
except Exception:
    sys.exit(1)
PYEOF
}

if [ ! -x "$PY" ]; then
    log "找不到 Python：$PY（請先執行 deploy/install.sh 建立 venv），略過更新"
    exit 0
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

if ! sysctl_cmd is-active --quiet "$SERVICE"; then
    log "$SERVICE 沒有在執行，下次啟動就會使用新版"
    exit 0
fi

for ((i = 0; i < DEFER_MAX; i++)); do
    if ! bot_busy; then
        break
    fi
    log "有伺服器正在語音中，$((DEFER_STEP / 60)) 分鐘後再檢查（第 $((i + 1))/$DEFER_MAX 次）"
    sleep "$DEFER_STEP"
done

log "重啟 $SERVICE 以套用新版 yt-dlp"
sysctl_cmd try-restart "$SERVICE" || log "重啟失敗"
exit 0
