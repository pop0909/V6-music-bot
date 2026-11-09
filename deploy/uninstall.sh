#!/usr/bin/env bash
# Music Bot V5 — 移除 systemd 服務
#   sudo ./deploy/uninstall.sh [--purge-venv]
#   ./deploy/uninstall.sh --user-mode [--purge-venv]
# 不會刪除 .env、list.txt、favorites/
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_MODE=0
PURGE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --user-mode) USER_MODE=1; shift ;;
        --purge-venv) PURGE=1; shift ;;
        -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
        *) echo "未知參數：$1"; exit 1 ;;
    esac
done

if [ "$USER_MODE" -eq 1 ]; then
    UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    SYSTEMCTL=(systemctl --user)
else
    [ "$(id -u)" -eq 0 ] || { echo "需要 root：sudo $0 $*（使用者層級請加 --user-mode）"; exit 1; }
    UNIT_DIR="/etc/systemd/system"
    SYSTEMCTL=(systemctl)
fi

"${SYSTEMCTL[@]}" disable --now musicbot-update.timer musicbot.service 2>/dev/null || true
"${SYSTEMCTL[@]}" stop musicbot-update.service 2>/dev/null || true
for u in musicbot.service musicbot-update.service musicbot-update.timer; do
    if [ -f "$UNIT_DIR/$u" ]; then
        rm -f "$UNIT_DIR/$u"
        echo "[uninstall] 已移除 $UNIT_DIR/$u"
    fi
done
"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" reset-failed musicbot.service musicbot-update.service 2>/dev/null || true

if [ "$PURGE" -eq 1 ] && [ -d "$APP_DIR/venv" ]; then
    rm -rf "$APP_DIR/venv"
    echo "[uninstall] 已刪除 $APP_DIR/venv"
fi
echo "[uninstall] 完成（.env、list.txt、favorites/ 保留）"
