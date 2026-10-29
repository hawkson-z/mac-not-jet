#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mac-not-jet — 风扇一转，就把 CPU 按下去

触发（持续 TRIGGER_SUSTAIN 秒）:
  风扇转速 >= FAN_TRIGGER (rpm)

恢复（已保持 MIN_HOLD 秒后，持续 RECOVER_SUSTAIN 秒）:
  风扇转速 <= FAN_RECOVER  且  CPU 占用 <= CPU_RECOVER

  注意用「回落到低档」而非「停转」判恢复：这台机器的风扇最低转速约
  2317 rpm，触发降频后它不会回到 0，按停转判定会导致永不恢复。

采样策略（每 TICK 秒一次，守护进程自身开销接近零）:
  风扇转速:     SMC key F0Ac，经 IOKit 直接读取。免 root、无子进程、微秒级
  CPU 占用率:   Mach host_processor_info 系统调用，微秒级，无子进程
  CPU 温度:     仅在心跳/触发时扫描（SMC 的 Tp/Te/Tf 系列 key），平时不读

为什么用风扇转速而不是温度:
  用户的痛点是风扇噪音，不是温度数字。风扇只要转起来就有声音，
  以它为准最直接。而且 macOS 的热压力等级（thermalpressurelevel）
  在 Apple Silicon 上判定极迟钝 —— 实测 10 核满载 99.8%、90°C 时
  仍是 Nominal(0)，用它会永远不触发。

用法:
  sudo python3 mac_not_jet.py          # 前台运行（调试用）
  python3 mac_not_jet.py --once        # 采样一次并打印读数（读取不需要 root）

版本历史:
  v0.1.0  用 powermetrics --samplers smc 读温度/风扇 —— Apple Silicon 上该 sampler 已不存在
  v0.2.0  改用系统热压力等级（powermetrics --samplers thermal）
  v0.2.1  热压力改走 notify API 拿数值，避开 powermetrics 的词表
  v0.3.0  回到物理量：直接经 IOKit 读 SMC 的风扇转速，以「风扇转动」为唯一触发信号。
          热压力路线整体废弃（实测满载时仍为 Nominal，永远不触发）。

版本号约定:
  在真机上跑通完整一轮「吵起来 → 降频 → 安静 → 恢复」并确认有效之前，只发 0.x。
  第一个通过实测验证的版本才叫 v1.0.0，不要提前跳号。
"""
import ctypes
import re
import struct
import subprocess
import sys
import time
from datetime import datetime

VERSION = "0.3.0"

# ===== 可调参数 =====
FAN_TRIGGER = 4000.0          # 触发转速 (rpm)。实测噪声（14 寸 M5）：2500 rpm ≈ 25 dB(A)
                              # 基本听不到，3800 rpm ≈ 30 dB(A) 已能明确听见。定 4000 是
                              # 宁可晚动手也不白降频：开始觉得吵了才切
FAN_RECOVER = 2500.0          # 恢复转速 (rpm)。这台机器的风扇最低转速约 2317，
                              # 触发降频后不会停转、只会回落到最低档，故用「回落」而非「停转」判恢复

CPU_RECOVER = 50.0            # 恢复所需 CPU 占用上限 (%)

TRIGGER_SUSTAIN = 5           # 触发需持续秒数（约 1 个采样周期，风扇吵起来就尽快降频）
RECOVER_SUSTAIN = 60          # 恢复需持续秒数
MIN_HOLD = 600                # 切换后最短保持秒数（防反复横跳）
LPM_FAIL_BACKOFF = 60         # pmset 切换失败后的重试退避（防日志刷屏）

TICK = 5                      # 主循环间隔秒数
HEARTBEAT = 600               # 心跳日志间隔秒数（心跳时才扫描温度）
WARN_INTERVAL = 600           # 读数失败告警的最小间隔（防日志膨胀）
LPM_CHECK_INTERVAL = 60       # lpm 状态下核对实际低电量模式开关的间隔秒数

# ===== 温度传感器 key（按芯片代际，读不到的直接跳过）=====
# 仅用于日志与诊断，不参与触发判断。
TEMP_KEYS = [
    # M5
    "Tp00", "Tp04", "Tp08", "Tp0C", "Tp0G", "Tp0K", "Tp0O", "Tp0R", "Tp0U",
    "Tp0X", "Tp0a", "Tp0d", "Tp0g", "Tp0j", "Tp0m", "Tp0p", "Tp0u", "Tp0y",
    # M1 / M2
    "Tp01", "Tp05", "Tp09", "Tp0D", "Tp0H", "Tp0L", "Tp0P", "Tp0T", "Tp0b",
    "Tp0f", "Tp1h", "Tp1t", "Tp1p", "Tp1l",
    # M3 / M4
    "Te05", "Te09", "Te0H", "Te0L", "Te0P", "Te0S", "Tf04", "Tf09", "Tf0A",
    "Tf0B", "Tf0D", "Tf0E",
]


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


# ---------- SMC：经 IOKit 直接读风扇与温度，免 root、无子进程 ----------

_iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
_libsys = ctypes.CDLL("/usr/lib/libSystem.B.dylib")

_iokit.IOServiceMatching.restype = ctypes.c_void_p
_iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
_iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
_iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_iokit.IOServiceOpen.restype = ctypes.c_int32
_iokit.IOServiceOpen.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.POINTER(ctypes.c_uint32)]
_iokit.IOServiceClose.restype = ctypes.c_int32
_iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
_iokit.IOObjectRelease.restype = ctypes.c_int32
_iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
_iokit.IOConnectCallStructMethod.restype = ctypes.c_int32
_iokit.IOConnectCallStructMethod.argtypes = [
    ctypes.c_uint32, ctypes.c_uint32,
    ctypes.c_void_p, ctypes.c_size_t,
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
]

_TASK_SELF = ctypes.c_uint32.in_dll(_libsys, "mach_task_self_").value
_KERNEL_INDEX_SMC = 2
_CMD_READ_BYTES = 5
_CMD_READ_KEYINFO = 9

_U8, _U16, _U32 = ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32


class _Vers(ctypes.Structure):
    _fields_ = [("major", _U8), ("minor", _U8), ("build", _U8),
                ("reserved", _U8), ("release", _U16)]


class _LimitData(ctypes.Structure):
    _fields_ = [("version", _U16), ("length", _U16), ("cpuPLimit", _U32),
                ("gpuPLimit", _U32), ("memPLimit", _U32)]


class _KeyInfo(ctypes.Structure):
    _fields_ = [("dataSize", _U32), ("dataType", _U32), ("dataAttributes", _U8)]


class _SMCKeyData(ctypes.Structure):
    """对应 SMCKeyData_t，实测大小 80 字节"""
    _fields_ = [("key", _U32), ("vers", _Vers), ("pLimitData", _LimitData),
                ("keyInfo", _KeyInfo), ("result", _U8), ("status", _U8),
                ("data8", _U8), ("data32", _U32), ("bytes", _U8 * 32)]


def _fourcc(s):
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


class SMC:
    """常驻一个 IOKit 连接；读失败时自动重连一次。"""

    def __init__(self):
        self.conn = 0
        self._open()

    def _open(self):
        svc = _iokit.IOServiceGetMatchingService(
            0, _iokit.IOServiceMatching(b"AppleSMC"))
        if not svc:
            return False
        conn = _U32(0)
        kr = _iokit.IOServiceOpen(svc, _TASK_SELF, 0, ctypes.byref(conn))
        _iokit.IOObjectRelease(svc)
        if kr != 0:
            return False
        self.conn = conn.value
        return True

    def _reopen(self):
        if self.conn:
            _iokit.IOServiceClose(self.conn)
            self.conn = 0
        return self._open()

    def read(self, key):
        """返回 (dataType, bytes) ；失败返回 None"""
        if not self.conn and not self._open():
            return None
        # 第一步：取 key 的信息（长度、类型）
        inp = _SMCKeyData()
        out = _SMCKeyData()
        inp.key = _fourcc(key)
        inp.data8 = _CMD_READ_KEYINFO
        out_sz = ctypes.c_size_t(ctypes.sizeof(out))
        kr = _iokit.IOConnectCallStructMethod(
            self.conn, _KERNEL_INDEX_SMC,
            ctypes.byref(inp), ctypes.sizeof(inp),
            ctypes.byref(out), ctypes.byref(out_sz))
        if kr != 0 or out.result != 0:
            return None
        size = out.keyInfo.dataSize
        if not 1 <= size <= 32:
            return None
        dtype = out.keyInfo.dataType.to_bytes(4, "big").decode("ascii", "replace")

        # 第二步：读数据
        inp2 = _SMCKeyData()
        out2 = _SMCKeyData()
        inp2.key = _fourcc(key)
        inp2.keyInfo.dataSize = size
        inp2.data8 = _CMD_READ_BYTES
        out_sz2 = ctypes.c_size_t(ctypes.sizeof(out2))
        kr = _iokit.IOConnectCallStructMethod(
            self.conn, _KERNEL_INDEX_SMC,
            ctypes.byref(inp2), ctypes.sizeof(inp2),
            ctypes.byref(out2), ctypes.byref(out_sz2))
        if kr != 0 or out2.result != 0:
            return None
        return dtype, bytes(out2.bytes[:size])


_smc = None


def _get_smc():
    global _smc
    if _smc is None:
        _smc = SMC()
    return _smc


def _to_float(dtype, data, lo, hi):
    """把 SMC 原始字节转成浮点数，结果必须落在 [lo, hi] 内，否则返回 None。

    flt 的字节序在不同芯片上不一致（Apple Silicon 实测小端），
    所以两种都试，取落在合理范围内的那个 —— 比按型号猜更可靠。
    """
    if dtype == "flt ":
        if len(data) < 4:
            return None
        for order in ("<", ">"):
            v = struct.unpack(order + "f", data[:4])[0]
            if lo <= v <= hi:
                return v
        return None
    if dtype == "sp78":
        if len(data) < 2:
            return None
        v = ((data[0] << 8) | data[1]) / 256.0
        return v if lo <= v <= hi else None
    if dtype == "fpe2":
        if len(data) < 2:
            return None
        v = ((data[0] << 8) | data[1]) / 4.0
        return v if lo <= v <= hi else None
    if dtype in ("ui8 ", "ui16", "ui32"):
        v = int.from_bytes(data, "big")
        return float(v) if lo <= v <= hi else None
    return None


def read_fan_rpm():
    """当前风扇转速 (rpm)。停转返回 0.0；读不到返回 None。"""
    r = _get_smc().read("F0Ac")
    if r is None:
        return None
    return _to_float(r[0], r[1], 0.0, 20000.0)


def read_fan_limits():
    """返回 (最低转速, 最高转速)，读不到的位置为 None。"""
    out = []
    for key in ("F0Mn", "F0Mx"):
        r = _get_smc().read(key)
        out.append(_to_float(r[0], r[1], 0.0, 20000.0) if r else None)
    return out[0], out[1]


def read_temps():
    """扫描温度传感器，返回 (最高温度, 有效读数个数)。都读不到返回 (None, 0)。"""
    vals = []
    smc = _get_smc()
    for key in TEMP_KEYS:
        r = smc.read(key)
        if r is None:
            continue
        v = _to_float(r[0], r[1], 10.0, 120.0)
        if v is not None:
            vals.append(v)
    if not vals:
        return None, 0
    return max(vals), len(vals)


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


def fmt(v, unit, digits=1):
    return "n/a" if v is None else ("%.*f%s" % (digits, v, unit))


def sample_once():
    """--once 模式：打印一次完整读数（只需读权限，不需要 root）。"""
    read_cpu_usage()          # 第一次只建立基线，必然返回 None
    time.sleep(1.0)
    cpu = read_cpu_usage()
    fan = read_fan_rpm()
    fan_min, fan_max = read_fan_limits()
    temp, count = read_temps()
    lpm = get_lpm()

    print("风扇转速: %s（最低 %s / 最高 %s）" % (
        fmt(fan, " rpm", 0), fmt(fan_min, " rpm", 0), fmt(fan_max, " rpm", 0)))
    print("CPU 温度: %s（%d 个传感器）" % (fmt(temp, "°C"), count))
    print("CPU 占用: %s" % fmt(cpu, "%"))
    print("当前低电量模式: %s" % lpm)
    print("触发条件: 风扇 >= %.0f rpm 持续 %ds；恢复: 风扇 <= %.0f rpm 且 CPU <= %.0f%% 持续 %ds"
          % (FAN_TRIGGER, TRIGGER_SUSTAIN, FAN_RECOVER, CPU_RECOVER, RECOVER_SUSTAIN))
    # 版本放在最后一行：前面几行是数据，按行取值的东西（如 README 里的 head -1）不该被它挤掉
    print("版本: mac-not-jet v%s" % VERSION)
    if fan is None:
        print("⚠ 风扇转速读不到：本工具无法工作（SMC key F0Ac 不可用）")
        return 1
    return 0


def main():
    if "--once" in sys.argv:
        return sample_once()

    state = "normal"       # normal / lpm
    trig_start = None      # 触发条件连续满足的起始时刻
    rec_start = None       # 恢复条件连续满足的起始时刻
    lpm_since = None
    last_beat = 0.0
    last_warn = 0.0        # 上次读数失败告警时刻
    last_lpm_check = 0.0   # 上次核对实际 LPM 开关时刻
    last_lpm_fail = 0.0    # 上次 pmset 切换失败时刻（用于退避）

    # 启动自检：读不到风扇转速就别装样子，明确报错退出
    fan = read_fan_rpm()
    if fan is None:
        log("启动自检失败：读不到风扇转速（SMC key F0Ac）。保护功能未生效。")
        return 1
    fan_min, fan_max = read_fan_limits()
    temp, count = read_temps()
    log("启动 mac-not-jet v%s。触发: 风扇 >= %.0f rpm 持续 %ds；恢复: 风扇 <= %.0f rpm 且 CPU <= %.0f%% "
        "持续 %ds（最短保持 %ds）；采样间隔 %ds" % (
            VERSION, FAN_TRIGGER, TRIGGER_SUSTAIN, FAN_RECOVER, CPU_RECOVER,
            RECOVER_SUSTAIN, MIN_HOLD, TICK))
    log("启动自检通过：风扇 %s（最低 %s / 最高 %s），温度 %s（%d 个传感器）" % (
        fmt(fan, " rpm", 0), fmt(fan_min, " rpm", 0), fmt(fan_max, " rpm", 0),
        fmt(temp, "°C"), count))

    # 启动时同步实际状态（低电量模式重启后是粘性的）
    if get_lpm():
        state = "lpm"
        lpm_since = time.time()
        log("启动时检测到低电量模式已开启，按 lpm 状态接管")

    read_cpu_usage()          # 建立 CPU tick 基线，避免首轮读数缺失误告警
    time.sleep(0.5)

    while True:
        t0 = time.time()
        try:
            cpu = read_cpu_usage()
            fan = read_fan_rpm()
        except Exception as e:
            log("采样异常: %s" % e)
            time.sleep(TICK)
            continue

        now = time.time()
        if fan is None:
            # 风扇读数失败：既不触发也不恢复（fail-safe），限频告警
            if now - last_warn >= WARN_INTERVAL:
                last_warn = now
                log("警告: 风扇转速读不到（SMC F0Ac），本轮跳过")
            time.sleep(TICK)
            continue
        last_warn = 0.0
        loud = fan >= FAN_TRIGGER     # 吵：到了触发线
        quiet = fan <= FAN_RECOVER    # 安静：回落到最低档附近

        if state == "normal":
            if loud:
                if trig_start is None:
                    trig_start = now
                    log("风扇升到 %.0f rpm（触发线 %.0f），开始计时" % (
                        fan, FAN_TRIGGER))
                elif (now - trig_start >= TRIGGER_SUSTAIN
                      and now - last_lpm_fail >= LPM_FAIL_BACKOFF):
                    temp, count = read_temps()
                    log("★ 风扇持续 %.0fs 在 %.0f rpm（%s），切换低电量模式" % (
                        now - trig_start, fan, fmt(temp, "°C")))
                    if set_lpm(True):
                        state = "lpm"
                        lpm_since = now
                        trig_start = None
                    else:
                        last_lpm_fail = now
                        log("错误: pmset 切换失败（需要 root 权限），"
                            "%d 秒后重试" % LPM_FAIL_BACKOFF)
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
            elif quiet and cpu is not None and cpu <= CPU_RECOVER:
                if rec_start is None:
                    rec_start = now
                    log("风扇回落到 %.0f rpm 且 CPU %s，开始计时恢复" % (
                        fan, fmt(cpu, "%")))
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
            temp, count = read_temps()
            log("心跳: 状态=%s 风扇=%s 温度=%s cpu=%s" % (
                state, fmt(fan, " rpm", 0), fmt(temp, "°C"), fmt(cpu, "%")))

        elapsed = time.time() - t0
        if elapsed < TICK:
            time.sleep(TICK - elapsed)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        log("退出")
