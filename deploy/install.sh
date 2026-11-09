#!/usr/bin/env bash
# Music Bot V5 — Linux 安裝腳本（建立 venv、安裝套件、安裝並啟用 systemd 服務）
#
# 系統層級（預設，開機就啟動，不需登入）：
#   sudo ./deploy/install.sh [--user 執行帳號] [--dir 專案路徑] [--no-start]
# 使用者層級（不需 root；要開機自動跑需 loginctl enable-linger）：
#   ./deploy/install.sh --user-mode [--dir 專案路徑] [--no-start]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
USER_MODE=0
START=1

usage() { sed -n '2,8p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --user) RUN_USER="$2"; shift 2 ;;
        --dir) APP_DIR="$(cd "$2" && pwd)"; shift 2 ;;
        --user-mode) USER_MODE=1; shift ;;
        --no-start) START=0; shift ;;
        -h|--help) usage 0 ;;
        *) echo "未知參數：$1"; usage 1 ;;
    esac
done

info() { echo -e "\033[1;36m[install]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
die() { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

case "$APP_DIR" in
    *" "*) die "專案路徑不能有空白（systemd 的 ExecStart 不好處理）：$APP_DIR" ;;
esac
[ -f "$APP_DIR/main.py" ] || die "找不到 $APP_DIR/main.py"

if [ "$USER_MODE" -eq 1 ]; then
    [ "$(id -u)" -ne 0 ] || die "--user-mode 請用一般使用者執行（不要 sudo）"
    RUN_USER="$(id -un)"
    UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    SYSTEMCTL=(systemctl --user)
else
    [ "$(id -u)" -eq 0 ] || die "系統層級安裝需要 root：sudo $0 $*（或改用 --user-mode）"
    id "$RUN_USER" >/dev/null 2>&1 || die "使用者不存在：$RUN_USER"
    UNIT_DIR="/etc/systemd/system"
    SYSTEMCTL=(systemctl)
fi
RUN_GROUP="$(id -gn "$RUN_USER")"
VENV="$APP_DIR/venv"
PY="$VENV/bin/python"

as_user() {
    if [ "$(id -u)" -eq 0 ] && [ "$RUN_USER" != "root" ]; then
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

info "專案：$APP_DIR"
info "執行帳號：$RUN_USER（$([ "$USER_MODE" -eq 1 ] && echo 使用者層級 || echo 系統層級)）"

# ---------- 前置檢查 ----------
command -v python3 >/dev/null || die "找不到 python3（Debian/Ubuntu：sudo apt install python3 python3-venv）"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "需要 Python 3.10 以上（目前 $(python3 -V)）"
python3 -c 'import venv, ensurepip' 2>/dev/null \
    || die "缺少 venv/ensurepip（Debian/Ubuntu：sudo apt install python3-venv）"
command -v ffmpeg >/dev/null || warn "找不到 ffmpeg，無法播放音樂（sudo apt install ffmpeg，或在 .env 設 FFMPEG_PATH）"
command -v systemctl >/dev/null || die "這台機器沒有 systemd"
as_user test -w "$APP_DIR" || die "$RUN_USER 對 $APP_DIR 沒有寫入權限（list.txt / favorites 需要）：sudo chown -R $RUN_USER: $APP_DIR"

if [ ! -f "$APP_DIR/.env" ]; then
    warn "找不到 $APP_DIR/.env — 請建立並寫入 DISCORD_TOKEN=你的token"
elif ! grep -q '^DISCORD_TOKEN=.\+' "$APP_DIR/.env"; then
    warn ".env 裡沒有 DISCORD_TOKEN"
else
    chmod 600 "$APP_DIR/.env"
    if [ "$(id -u)" -eq 0 ]; then
        chown "$RUN_USER:$RUN_GROUP" "$APP_DIR/.env"
    fi
fi

# ---------- venv 與套件 ----------
if [ ! -x "$PY" ]; then
    info "建立 venv：$VENV"
    as_user python3 -m venv "$VENV"
fi
info "安裝 / 更新套件（requirements.txt）"
as_user "$PY" -m pip install -q --disable-pip-version-check -U pip
as_user "$PY" -m pip install -q --disable-pip-version-check -U -r "$APP_DIR/requirements.txt"
as_user "$PY" -c 'import discord, yt_dlp, flask, dotenv, nacl' || die "套件匯入失敗，請看上面的錯誤"
chmod +x "$APP_DIR/deploy/update-ytdlp.sh"

# ---------- systemd units ----------
mkdir -p "$UNIT_DIR"
render() {
    local src="$1" dst="$2"
    sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@RUN_USER@|$RUN_USER|g" \
        -e "s|@RUN_GROUP@|$RUN_GROUP|g" -e "s|@PYTHON@|$PY|g" "$src" > "$dst.tmp"
    if [ "$USER_MODE" -eq 1 ]; then
        # 使用者層級：沒有 User=/Group=、沒有 network-online.target、沙箱選項需要 root 才能用
        sed -i -e '/^User=/d' -e '/^Group=/d' -e '/network-online.target/d' \
            -e '/^NoNewPrivileges=/d' -e '/^PrivateTmp=/d' -e '/^ProtectSystem=/d' \
            -e 's|MUSICBOT_USER=[^ ]*|MUSICBOT_USER=|' \
            -e 's|^WantedBy=multi-user.target|WantedBy=default.target|' "$dst.tmp"
    fi
    mv "$dst.tmp" "$dst"
    chmod 644 "$dst"
    info "已安裝 $dst"
}
for u in musicbot.service musicbot-failure.service musicbot-update.service musicbot-update.timer; do
    render "$APP_DIR/deploy/systemd/$u.in" "$UNIT_DIR/$u"
done

"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" enable musicbot.service musicbot-update.timer
"${SYSTEMCTL[@]}" start musicbot-update.timer
if [ "$START" -eq 1 ]; then
    info "啟動 musicbot.service"
    "${SYSTEMCTL[@]}" restart musicbot.service
else
    info "已設定開機自動啟動，但沒有立即啟動（--no-start）"
fi

J="journalctl"; [ "$USER_MODE" -eq 1 ] && J="journalctl --user"
S="systemctl"; [ "$USER_MODE" -eq 1 ] && S="systemctl --user"
cat <<MSG

完成！常用指令：
  狀態：     $S status musicbot
  即時日誌： $J -u musicbot -f
  更新日誌： $J -u musicbot-update
  重啟：     $S restart musicbot
  下次更新： $S list-timers musicbot-update.timer
  移除：     $( [ "$USER_MODE" -eq 1 ] && echo "" || echo "sudo " )$APP_DIR/deploy/uninstall.sh$( [ "$USER_MODE" -eq 1 ] && echo " --user-mode" )
MSG
if [ "$USER_MODE" -eq 1 ]; then
    echo
    warn "使用者層級服務預設只在你登入時執行。要開機（未登入）也跑：sudo loginctl enable-linger $RUN_USER"
fi
