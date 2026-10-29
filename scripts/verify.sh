#!/bin/bash
# mac-not-jet 自检：验证读数链路是否真的工作（而不是"进程在跑")
#
# 用法: ./scripts/verify.sh
# 退出码: 0 = 全部通过，1 = 有检查失败

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-/usr/bin/python3}
LABEL=com.local.mac-not-jet
fail=0

ok()  { printf "  [ok]   %s\n" "$1"; }
bad() { printf "  [FAIL] %s\n" "$1"; fail=1; }
info() { printf "  [--]   %s\n" "$1"; }

echo "mac-not-jet 自检"
echo "--------------------------------------------------"

if [ ! -x "$PY" ]; then
  bad "找不到 python3: $PY"
fi

out=$("$PY" "$ROOT/mac_not_jet.py" --once 2>&1)
echo "$out"
echo "--------------------------------------------------"

# 热压力：必须落在已知等级里，n/a 说明读取链路又断了
if echo "$out" | grep -q "热压力: n/a"; then
  bad "热压力读不到（notify_get_state 失败，保护不会生效）"
elif echo "$out" | grep -Eq "热压力: (Nominal|Light|Moderate|Heavy|Trapping|Sleeping)\("; then
  ok "热压力读数正常"
else
  bad "热压力输出格式异常（等级词不在已知集合内）"
fi

# CPU 占用：首次采样是基线，--once 内部已处理，这里不该再是 n/a
if echo "$out" | grep -q "CPU 占用: n/a"; then
  bad "CPU 占用读不到（Mach host_processor_info 失败）"
elif echo "$out" | grep -q "CPU 占用:"; then
  ok "CPU 占用读数正常"
else
  bad "CPU 占用输出缺失"
fi

# 守护进程（未安装不算失败，只是提醒）
if launchctl print "system/$LABEL" >/dev/null 2>&1; then
  ok "守护进程已安装并在运行"
  if tail -20 /var/log/mac_not_jet.log 2>/dev/null | grep -q "启动自检通过"; then
    ok "日志中有启动自检通过记录"
  else
    info "日志里没有「启动自检通过」——可能还在跑旧版本，执行 sudo ./install.sh 更新"
  fi
else
  info "守护进程未安装，本次只验证了读数链路"
fi

echo "--------------------------------------------------"
if [ $fail -eq 0 ]; then
  echo "结果: 通过"
else
  echo "结果: 失败（保护功能不可靠，先别装）"
fi
exit $fail
