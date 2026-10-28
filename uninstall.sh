#!/bin/bash
# mac-not-jet uninstaller
# Stops the daemon and removes installed files. Does NOT touch Low Power Mode.
set -uo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "请用 sudo 运行: sudo ./uninstall.sh" >&2
    exit 1
fi

PLIST_DST="/Library/LaunchDaemons/com.local.mac-not-jet.plist"
SCRIPT_DST="/usr/local/bin/mac_not_jet.py"

if launchctl list 2>/dev/null | grep -q "com.local.mac-not-jet"; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    echo "守护进程已停止。"
fi

rm -f "$PLIST_DST" "$SCRIPT_DST"
echo "已移除: $PLIST_DST"
echo "已移除: $SCRIPT_DST"
echo "低电量模式保持当前状态，如需关闭: 系统设置 → 电池 → 低电量模式"
