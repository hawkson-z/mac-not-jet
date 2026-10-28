#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mac-not-jet — MacBook Pro 过热自动切低电量模式

触发条件（三条件同时满足、持续 TRIGGER_SUSTAIN 秒）:
  CPU 使用率 >= CPU_TRIGGER
  CPU die 温度 >= TEMP_TRIGGER
  风扇转速 >= FAN_TRIGGER（读不到风扇数据时不阻塞）

恢复条件（切到低电量模式至少 MIN_HOLD 秒后，两条件同时满足、持续 RECOVER_SUSTAIN 秒）:
  温度 <= TEMP_RECOVER 且 CPU 占用 <= CPU_RECOVER
  恢复前会读当前实际状态，若用户已手动退出低电量模式则不动 pmset。

用法:
  sudo python3 mac_not_jet.py          # 前台运行（调试用）
  sudo python3 mac_not_jet.py --once   # 采样一次并打印读数，不执行任何切换
"""
import re
import subprocess
import sys
import time
from datetime import datetime

# ===== 可调参数 =====
TEMP_TRIGGER = 95.0      # 触发温度阈值 (°C)
CPU_TRIGGER = 95.0       # 触发 CPU 使用率阈值 (%)
FAN_TRIGGER = 4000.0     # 触发风扇转速阈值 (rpm)
TRIGGER_SUSTAIN = 10     # 触发条件需持续秒数

TEMP_RECOVER = 75.0      # 恢复温度阈值 (°C)
CPU_RECOVER = 50.0       # 恢复 CPU 占用阈值 (%)
RECOVER_SUSTAIN = 30     # 恢复条件需持续秒数
MIN_HOLD = 600           # 切换后最短保持秒数（防反复横跳）

TICK = 2                 # 采样循环睡眠秒数（top -l 2 + powermetrics 本身约耗 4~7s，
                         # 实际循环节拍 ≈ TICK + 采样耗时；计时均按墙钟，不受影响）
HEARTBEAT = 600          # 心跳日志间隔秒数
WARN_INTERVAL = 600      # 读数失败告警的最小间隔（防日志膨胀）
LPM_CHECK_INTERVAL = 60  # lpm 状态下核对实际低电量模式开关的间隔秒数


def log(msg):
    print("%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def read_cpu_usage():
    """返回近几秒 CPU 总占用率（100 - idle）。top -l 2 的第二段才是即时采样。"""
    try:
        out = subprocess.run(
            ["top", "-l", "2", "-n", "0"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception as e:
        log("top 采样失败: %s" % e)
        return None
    lines = [l for l in out.splitlines() if l.startswith("CPU usage:")]
    if len(lines) < 2:
        return None
    m = re.search(r"([\d.]+)% idle", lines[-1])
    return 100.0 - float(m.group(1)) if m else None


def read_smc():
    """读 CPU die 温度和风扇转速（取最大风扇）。powermetrics 需要 root。"""
    try:
        out = subprocess.run(
            ["powermetrics", "--samplers", "smc", "-i", "1000", "-n", "1"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception as e:
        log("powermetrics 采样失败: %s" % e)
        return None, None
    temp = None
    m = re.search(r"CPU die temperature:\s*([\d.]+)\s*C", out)
    if m:
        temp = float(m.group(1))
    else:
        # 某些机型字段名不同，兜底取所有温度传感器最大值
        temps = [float(x) for x in re.findall(r"temperature:\s*([\d.]+)\s*C", out)]
        if temps:
            temp = max(temps)
    fans = [float(x) for x in re.findall(r"Fan:\s*([\d.]+)\s*rpm", out)]
    fan = max(fans) if fans else None
    return temp, fan


def set_lpm(on):
    subprocess.run(["pmset", "-a", "lowpowermode", "1" if on else "0"], check=False)
    return get_lpm() == on  # 核实是否真的生效


def get_lpm():
    try:
        out = subprocess.run(["pmset", "-g"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:
        return None
    m = re.search(r"lowpowermode\s+(\d)", out)
    return bool(int(m.group(1))) if m else None


def fmt(v, unit):
    return "n/a" if v is None else "%.1f%s" % (v, unit)


def sample_once():
    """--once 模式：打印一次完整读数"""
    cpu = read_cpu_usage()
    temp, fan = read_smc()
    print("CPU 占用: %s | CPU 温度: %s | 风扇: %s | 当前低电量模式: %s" % (
        fmt(cpu, "%"), fmt(temp, "°C"), fmt(fan, "rpm"), get_lpm()))


def main():
    if "--once" in sys.argv:
        sample_once()
        return

    state = "normal"       # normal / lpm
    trig_start = None      # 触发条件连续满足的起始时刻
    rec_start = None       # 恢复条件连续满足的起始时刻
    lpm_since = None
    last_beat = 0.0
    last_warn = 0.0        # 上次读数失败告警时刻
    last_lpm_check = 0.0   # 上次核对实际 LPM 开关时刻

    # 启动时同步实际状态（低电量模式重启后是粘性的）
    if get_lpm():
        state = "lpm"
        lpm_since = time.time()
        log("启动时检测到低电量模式已开启，按 lpm 状态接管")

    log("启动。阈值: 触发 CPU>=%.0f%%/温度>=%.0f°C/风扇>=%.0frpm 持续%ds；"
        "恢复 <=%.0f%%/<=%.0f°C 持续%ds（最短保持%ds）" % (
            CPU_TRIGGER, TEMP_TRIGGER, FAN_TRIGGER, TRIGGER_SUSTAIN,
            CPU_RECOVER, TEMP_RECOVER, RECOVER_SUSTAIN, MIN_HOLD))

    while True:
        t0 = time.time()
        try:
            cpu = read_cpu_usage()
            temp, fan = read_smc()
        except Exception as e:
            log("采样异常: %s" % e)
            time.sleep(TICK)
            continue

        now = time.time()
        if cpu is None or temp is None:
            # 读不到数据（比如没权限）：不触发也不恢复，限频告警防日志膨胀
            if now - last_warn >= WARN_INTERVAL:
                last_warn = now
                log("警告: 读数不完整（持续中）cpu=%s temp=%s fan=%s" % (
                    cpu, temp, fan))
            time.sleep(TICK)
            continue
        last_warn = 0.0

        if state == "normal":
            fan_ok = (fan is None) or (fan >= FAN_TRIGGER)
            if cpu >= CPU_TRIGGER and temp >= TEMP_TRIGGER and fan_ok:
                if trig_start is None:
                    trig_start = now
                    log("触发计时开始: %s %s %s" % (
                        fmt(cpu, "%"), fmt(temp, "°C"), fmt(fan, "rpm")))
                elif now - trig_start >= TRIGGER_SUSTAIN:
                    log("★ 持续过载 %.0fs，切换低电量模式（%s %s %s）" % (
                        now - trig_start, fmt(cpu, "%"), fmt(temp, "°C"),
                        fmt(fan, "rpm")))
                    if set_lpm(True):
                        state = "lpm"
                        lpm_since = now
                        trig_start = None
                    else:
                        log("错误: pmset 切换失败，保留 normal 状态，下轮重试")
                        trig_start = now - TRIGGER_SUSTAIN + TICK  # 尽快重试
            else:
                trig_start = None
        else:  # lpm
            # 防卡死：若用户已手动关闭低电量模式，立即回到 normal 状态，
            # 否则机器仍热时恢复条件永不满足，守护进程会永久卡在 lpm 分支
            if now - last_lpm_check >= LPM_CHECK_INTERVAL:
                last_lpm_check = now
                if get_lpm() is False:
                    log("检测到低电量模式被手动关闭，回到 normal 状态")
                    state = "normal"
                    trig_start = None
                    rec_start = None
                    time.sleep(TICK)
                    continue
            if now - lpm_since < MIN_HOLD:
                rec_start = None  # 保持期内不计时恢复
            elif cpu <= CPU_RECOVER and temp <= TEMP_RECOVER:
                if rec_start is None:
                    rec_start = now
                    log("回落条件满足，开始计时（%s %s）" % (
                        fmt(cpu, "%"), fmt(temp, "°C")))
                elif now - rec_start >= RECOVER_SUSTAIN:
                    if get_lpm():
                        if set_lpm(False):
                            log("☆ 已持续回落，恢复正常模式")
                        else:
                            log("错误: pmset 恢复失败，下轮重试（保持 lpm 状态）")
                            rec_start = now - RECOVER_SUSTAIN + TICK
                            continue
                    else:
                        log("检测到低电量模式已被手动关闭，仅重置内部状态")
                    state = "normal"
                    trig_start = None
                    rec_start = None
            else:
                if rec_start is not None:
                    log("回落条件中断，重新计时")
                rec_start = None

        if now - last_beat >= HEARTBEAT:
            last_beat = now
            log("心跳: 状态=%s cpu=%s temp=%s fan=%s" % (
                state, fmt(cpu, "%"), fmt(temp, "°C"), fmt(fan, "rpm")))

        # 采样本身耗时 2~4s，按剩余时间睡
        elapsed = time.time() - t0
        if elapsed < TICK:
            time.sleep(TICK - elapsed)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("退出")
