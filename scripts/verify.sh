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

# 风扇转速：这是唯一的触发信号，读不到等于整个工具失效
if echo "$out" | grep -q "风扇转速: n/a"; then
  bad "风扇转速读不到（SMC key F0Ac 不可用，保护完全不会生效）"
elif echo "$out" | grep -Eq "风扇转速: [0-9]+ rpm"; then
  ok "风扇转速读数正常"
else
  bad "风扇转速输出格式异常"
fi

# 最低/最高转速：用来确认读到的是真实风扇而不是占位值
if echo "$out" | grep -Eq "最低 [1-9][0-9]* rpm"; then
  ok "风扇转速区间正常"
else
  info "最低转速读不到（不影响触发，只是少了参考值）"
fi

# 温度：仅用于诊断，读不到不判失败
if echo "$out" | grep -Eq "CPU 温度: [0-9]+\.[0-9]°C"; then
  ok "温度读数正常（仅诊断用，不参与触发）"
else
  info "温度读不到（不影响触发，触发只看风扇转速）"
fi

# CPU 占用：恢复条件用到（风扇安静且 CPU 低才恢复）
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
  # 版本核对：v0.3.0 起日志会写"触发: 风扇 >="
  if tail -50 /var/log/mac_not_jet.log 2>/dev/null | grep -q "触发: 风扇"; then
    ok "运行中的是 v0.3.0（风扇触发）"
  else
    info "日志里没有风扇触发记录——若刚更新过，重启服务：sudo launchctl kickstart -k system/$LABEL"
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
