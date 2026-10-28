#!/bin/bash
# mac-not-jet installer
# Copies the script to /usr/local/bin and registers a LaunchDaemon.
set -euo pipefail

cd "$(dirname "$0")"

SCRIPT_SRC="mac_not_jet.py"
SCRIPT_DST="/usr/local/bin/mac_not_jet.py"
PLIST_SRC="com.local.mac-not-jet.plist"
PLIST_DST="/Library/LaunchDaemons/com.local.mac-not-jet.plist"

if [[ $EUID -ne 0 ]]; then
    echo "请用 sudo 运行: sudo ./install.sh" >&2
    exit 1
fi

# 旧版本卸载（如已在跑，先停干净）
./uninstall.sh 2>/dev/null || true

install -m 755 "$SCRIPT_SRC" "$SCRIPT_DST"
install -m 644 "$PLIST_SRC" "$PLIST_DST"
chown root:wheel "$PLIST_DST"

launchctl load "$PLIST_DST"

echo "已安装并启动。"
echo "  查看日志:  tail -f /var/log/mac_not_jet.log"
echo "  单次读数:  sudo python3 $SCRIPT_DST --once"
echo "  卸载:      sudo ./uninstall.sh"
