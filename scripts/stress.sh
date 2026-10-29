#!/bin/bash
# mac-not-jet 压测：把全部核心跑满，观察是否触发低电量模式、停止后是否恢复
#
# 用法: ./scripts/stress.sh [持续秒数]
#   不带参数 = 一直压到 Ctrl+C
#
# 注意：这会把风扇吹起来，请确认真想测再用。
# 触发条件要求 CPU 整体 >= 95%，所以必须压满全部核心（单核只有 ~1/10）。

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-/usr/bin/python3}
DURATION=${1:-0}
NCPU=$(sysctl -n hw.ncpu)
LOGFILE=/var/log/mac_not_jet.log
pids=()

cleanup() {
  trap - INT TERM EXIT
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done
  wait 2>/dev/null
  echo ""
  echo "压测已停止（$NCPU 个 yes 进程已清理）"
  echo "日志: $LOGFILE"
}
trap cleanup INT TERM EXIT

printf "mac-not-jet 压测\n"
printf -- "--------------------------------------------------\n"
printf "核心数: %s（将启动同数量的 yes 进程）\n" "$NCPU"
printf "触发线: CPU>=95%% 且 热压力>=Moderate，持续 30s\n"
printf "打印间隔: 10s    预期: 30~60s 内切到低电量模式\n"
printf -- "--------------------------------------------------\n"
[ -r "$LOGFILE" ] || printf "提示: 读不到 $LOGFILE（改用 sudo 跑可看完整日志）\n"

for _ in $(seq 1 "$NCPU"); do
  yes > /dev/null &
  pids+=($!)
done
printf "已启动 %s 个压测进程\n\n" "${#pids[@]}"

start=$(date +%s)
while :; do
  sleep 10
  now=$(date +%s)
  elapsed=$((now - start))
  reading=$("$PY" "$ROOT/mac_not_jet.py" --once 2>&1 | head -1)
  printf "[%4ss] %s\n" "$elapsed" "$reading"
  if [ "$DURATION" -gt 0 ] && [ "$elapsed" -ge "$DURATION" ]; then
    printf "\n达到设定时长（%ss），停止压测并观察恢复\n" "$DURATION"
    break
  fi
done

# 停掉负载，留一段时间观察恢复（最短保持 10 分钟，通常不会立刻恢复）
for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done
pids=()
printf "\n负载已撤，继续观察 90s（最短保持 600s，恢复通常要等）\n"
for i in $(seq 1 9); do
  sleep 10
  reading=$("$PY" "$ROOT/mac_not_jet.py" --once 2>&1 | head -1)
  printf "[+%3ss] %s\n" "$((i * 10))" "$reading"
done
