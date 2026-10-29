#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mac-not-jet — MacBook Pro 过热自动切低电量模式

触发条件（两条件同时满足、持续 TRIGGER_SUSTAIN 秒）:
  CPU 使用率 >= CPU_TRIGGER
  系统热压力 >= TRIGGER_PRESSURE

恢复条件（切到低电量模式至少 MIN_HOLD 秒后，两条件同时满足、持续 RECOVER_SUSTAIN 秒）:
  CPU 占用 <= CPU_RECOVER
  系统热压力 <= RECOVER_PRESSURE

采样策略（守护进程自身开销接近零）:
  CPU 占用率: Mach host_processor_info 系统调用，微秒级，无子进程
  热压力:      notify_get_state 读 com.apple.system.thermalpressurelevel
              微秒级、免 root、无子进程；数值 = OSThermalPressureLevel

用法:
  sudo python3 mac_not_jet.py          # 前台运行（调试用）
  python3 mac_not_jet.py --once        # 采样一次并打印读数（读取不需要 root）

版本历史:
  v1.0  用 powermetrics --samplers smc 读温度/风扇 —— Apple Silicon 上该 sampler 已不存在
  v2.0  改用系统热压力等级（powermetrics --samplers thermal）
  v2.1  热压力改走 notify API：输出是数值而非名字，避开 powermetrics 的
        Nominal/Moderate/Heavy/Sleeping/Trapping 词表（此前代码按
        Nominal/Elevated/High/Serious/Critical 解析，真实高温时全部解析失败）
"""
import ctypes
import re
import subprocess
import sys
import time
from datetime import datetime

# ===== 可调参数 =====
CPU_TRIGGER = 95.0            # 触发 CPU 使用率阈值 (%)

CPU_RECOVER = 50.0            # 恢复 CPU 占用阈值 (%)

TRIGGER_SUSTAIN = 30          # 触发条件需持续秒数
RECOVER_SUSTAIN = 30          # 恢复条件需持续秒数
MIN_HOLD = 600                # 切换后最短保持秒数（防反复横跳）

TICK = 2                      # 主循环间隔秒数
HEARTBEAT = 600               # 心跳日志间隔秒数
WARN_INTERVAL = 600           # 读数失败告警的最小间隔（防日志膨胀）
LPM_CHECK_INTERVAL = 60       # lpm 状态下核对实际低电量模式开关的间隔秒数

# ===== 热压力等级（OSThermalPressureLevel, sys/thermal.h） =====
PRESSURE_NOMINAL = 0
PRESSURE_LIGHT = 10
PRESSURE_MODERATE = 20
PRESSURE_HEAVY = 30
PRESSURE_TRAPPING = 40        # 系统正在强制降载
PRESSURE_SLEEPING = 50        # 系统正在强制睡眠降温

PRESSURE_NAMES = {
    PRESSURE_NOMINAL: "Nominal",
    PRESSURE_LIGHT: "Light",
    PRESSURE_MODERATE: "Moderate",
    PRESSURE_HEAVY: "Heavy",
    PRESSURE_TRAPPING: "Trapping",
    PRESSURE_SLEEPING: "Sleeping",
}

# 默认：Moderate 起触发、回到 Light 以下才恢复。
# 想更保守（只在明显降频时才动手）改成 PRESSURE_HEAVY；
# 想更早介入（风扇刚起来就压）可下调到 PRESSURE_LIGHT。
TRIGGER_PRESSURE = PRESSURE_MODERATE
RECOVER_PRESSURE = PRESSURE_LIGHT


def log(msg):
    print("%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


# ---------- CPU 占用率：Mach host_processor_info，零子进程 ----------

_libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
_libc.host_processor_info.argtypes = [
    ctypes.c_uint,                                # host_t
    ctypes.c_int,                                 # flavor (PROCESSOR_CPU_LOAD_INFO=2)
    ctypes.POINTER(ctypes.c_uint),                # out: cpu count
    ctypes.POINTER(ctypes.POINTER(ctypes.c_int)), # out: ticks
    ctypes.POINTER(ctypes.c_uint),                # out: tick count
]
_libc.host_processor_info.restype = ctypes.c_int
_libc.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]

_last_ticks = None


def _read_ticks():
    """全核累计 (user, system, idle, nice)，读不到返回 None"""
    cpu_count = ctypes.c_uint()
    info = ctypes.POINTER(ctypes.c_int)()
    info_count = ctypes.c_uint()
    kr = _libc.host_processor_info(
        _libc.mach_host_self(), 2,
        ctypes.byref(cpu_count), ctypes.byref(info), ctypes.byref(info_count))
    if kr != 0:
        return None
    n = cpu_count.value
    u = s = idle = nice = 0
    for i in range(n):
        base = i * 4  # 每核顺序: user, system, idle, nice
        u += info[base]
        s += info[base + 1]
        idle += info[base + 2]
        nice += info[base + 3]
    _libc.vm_deallocate(_libc.mach_host_self(),
                        ctypes.addressof(info.contents),
                        info_count.value * ctypes.sizeof(ctypes.c_int))
    return (u, s, idle, nice)


def read_cpu_usage():
    """返回上次调用至今的 CPU 总占用率（忙/总）。首次调用返回 None。"""
    global _last_ticks
    ticks = _read_ticks()
    if ticks is None:
        return None
    if _last_ticks is None:
        _last_ticks = ticks
        return None
    delta = [t - l for t, l in zip(ticks, _last_ticks)]
    _last_ticks = ticks
    busy = delta[0] + delta[1] + delta[3]   # user + system + nice
    total = busy + delta[2]                 # + idle
    if total <= 0:
        return None
    return 100.0 * busy / total


# ---------- 热压力：notify API，免 root、微秒级 ----------

_libc.notify_register_check.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
_libc.notify_register_check.restype = ctypes.c_uint32
_libc.notify_get_state.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint64)]
_libc.notify_get_state.restype = ctypes.c_uint32

_NOTIFY_KEY = b"com.apple.system.thermalpressurelevel"
_notify_token = ctypes.c_int(0)
_notify_ok = False


def _pressure_init():
    """注册 thermalpressurelevel 通知 token（只需一次）"""
    global _notify_ok
    kr = _libc.notify_register_check(_NOTIFY_KEY, ctypes.byref(_notify_token))
    _notify_ok = (kr == 0)  # NOTIFY_STATUS_OK
    if not _notify_ok:
        log("错误: 无法注册热压力通知（status=%d）" % kr)
    return _notify_ok


def read_pressure():
    """返回当前热压力等级数值（0/10/20/30/40/50），失败返回 None。

    走 notify API 而不是 powermetrics：无子进程、不需要 root，
    且拿到的是 OSThermalPressureLevel 原始数值，不存在等级名字对不上的问题。
    """
    global _notify_ok
    if not _notify_ok and not _pressure_init():
        return None
    state = ctypes.c_uint64(0)
    kr = _libc.notify_get_state(_notify_token, ctypes.byref(state))
    if kr != 0:
        # token 可能失效，标记后下一轮重新注册
        _notify_ok = False
        return None
    return int(state.value)


# ---------- pmset ----------

PMSET = "/usr/bin/pmset"


def get_lpm():
    try:
        out = subprocess.run([PMSET, "-g"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:
        return None
    m = re.search(r"lowpowermode\s+(\d)", out)
    return bool(int(m.group(1))) if m else None


def set_lpm(on):
    """切换低电量模式并核实结果。pmset 写偏好不保证立刻可读，故重试几次。"""
    for _ in range(3):
        subprocess.run([PMSET, "-a", "lowpowermode", "1" if on else "0"],
                       check=False)
        time.sleep(0.25)
        if get_lpm() == on:
            return True
    return False


def fmt(v, unit):
    return "n/a" if v is None else "%.1f%s" % (v, unit)


def fmt_pressure(level):
    if level is None:
        return "n/a"
    return "%s(%d)" % (PRESSURE_NAMES.get(level, "Unknown"), level)


def sample_once():
    """--once 模式：打印一次完整读数。CPU 占用率需要两次采样才能算出。"""
    read_cpu_usage()          # 第一次只建立基线，必然返回 None
    time.sleep(1.0)
    cpu = read_cpu_usage()
    pressure = read_pressure()
    print("CPU 占用: %s | 热压力: %s | 触发线: %s | 当前低电量模式: %s" % (
        fmt(cpu, "%"), fmt_pressure(pressure), fmt_pressure(TRIGGER_PRESSURE),
        get_lpm()))
    print("（等级：0 Nominal / 10 Light / 20 Moderate / 30 Heavy / "
          "40 Trapping / 50 Sleeping）")


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

    # 启动自检：读不到热压力就直接报错退出，不装作在工作
    probed = read_pressure()
    if probed is None:
        log("启动自检失败：热压力读不到（notify_get_state 失败）。"
            "保护功能未生效，请检查系统版本。")
        return

    log("启动。阈值: 触发 CPU>=%.0f%%/热压力>=%s 持续%ds；"
        "恢复 <=%.0f%%/热压力<=%s 持续%ds（最短保持%ds）" % (
            CPU_TRIGGER, fmt_pressure(TRIGGER_PRESSURE), TRIGGER_SUSTAIN,
            CPU_RECOVER, fmt_pressure(RECOVER_PRESSURE), RECOVER_SUSTAIN,
            MIN_HOLD))
    log("启动自检通过：当前热压力 %s" % fmt_pressure(probed))

    read_cpu_usage()          # 建立 CPU tick 基线，避免首轮读数缺失误告警
    time.sleep(0.5)

    while True:
        t0 = time.time()
        try:
            cpu = read_cpu_usage()
            pressure = read_pressure()
        except Exception as e:
            log("采样异常: %s" % e)
            time.sleep(TICK)
            continue

        now = time.time()
        if cpu is None or pressure is None:
            # 读不到数据：不触发也不恢复，限频告警防日志膨胀
            if now - last_warn >= WARN_INTERVAL:
                last_warn = now
                log("警告: 读数不完整（持续中）cpu=%s pressure=%s" % (
                    fmt(cpu, "%"), pressure))
            time.sleep(TICK)
            continue
        last_warn = 0.0

        if state == "normal":
            if cpu >= CPU_TRIGGER and pressure >= TRIGGER_PRESSURE:
                if trig_start is None:
                    trig_start = now
                    log("触发计时开始: %s 压力=%s" % (
                        fmt(cpu, "%"), fmt_pressure(pressure)))
                elif now - trig_start >= TRIGGER_SUSTAIN:
                    log("★ 持续过载 %.0fs，切换低电量模式（%s 压力=%s）" % (
                        now - trig_start, fmt(cpu, "%"), fmt_pressure(pressure)))
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
            elif cpu <= CPU_RECOVER and pressure <= RECOVER_PRESSURE:
                if rec_start is None:
                    rec_start = now
                    log("回落条件满足，开始计时（%s 压力=%s）" % (
                        fmt(cpu, "%"), fmt_pressure(pressure)))
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
            log("心跳: 状态=%s cpu=%s 压力=%s" % (
                state, fmt(cpu, "%"), fmt_pressure(pressure)))

        elapsed = time.time() - t0
        if elapsed < TICK:
            time.sleep(TICK - elapsed)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("退出")
