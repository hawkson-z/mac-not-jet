# mac-not-jet

**不要让你的 Mac 起飞。** 一个极小的 macOS 守护进程：CPU 持续过热、风扇起飞时，自动切到**低电量模式**（CPU 降频、温度回落、风扇安静）；凉下来之后再自动切回。

买的是 Pro、用得像 Air（日常轻载、烦风扇噪音、不需要峰值性能）的人，就是它的目标用户。

---

## 中文

### 工作原理

每 2 秒左右用系统自带工具采样三项指标（零依赖，不需要装任何东西）：

- CPU 占用率 — Mach `host_processor_info` 系统调用（微秒级，无子进程）
- CPU die 温度 + 风扇转速 — `powermetrics --samplers smc`，每 10 秒采一次并缓存

状态机：

```
        CPU >= 95% 且 温度 >= 95°C 且 风扇 >= 4000 rpm
        （三项同时持续 10 秒）
正常 ─────────────────────────────────────────────► 低电量模式
  ▲                                                    │
  └──── 温度 <= 75°C 且 CPU <= 50% ────────────────────┘
        （持续 30 秒；且切换后至少 10 分钟内不恢复，
          防止反复横跳）
```

触发时执行 `pmset -a lowpowermode 1`；恢复前会先读当前实际开关状态，绝不跟你手动设置打架——你手动关了低电量模式，它就安静地重置内部状态。

14 寸 MacBook Pro（Apple Silicon）实测：持续满载从 ~95°C / 风扇全速降到 ~60°C / 安静，代价是算力约减半。这是一笔交易——需要持续峰值性能的人不要用。

### 安装

```bash
git clone https://github.com/hawkson-z/mac-not-jet.git
cd mac-not-jet
sudo ./install.sh
```

脚本装到 `/usr/local/bin/mac_not_jet.py`，注册为 LaunchDaemon（root 运行、开机自启）。要求 macOS 12+，Apple Silicon。

### 卸载

```bash
sudo ./uninstall.sh
```

移除守护进程和脚本。低电量模式本身保持当前状态不动——需要的话去 系统设置 → 电池 手动切换。

### 先试试再装

```bash
# 单次读数，不执行任何切换
sudo python3 mac_not_jet.py --once

# 前台运行，看状态机工作
sudo python3 mac_not_jet.py
```

守护进程的日志在 `/var/log/mac_not_jet.log`：

```bash
tail -f /var/log/mac_not_jet.log
```

### 调参数

改 `mac_not_jet.py` 顶部的常量：

| 常量 | 默认值 | 含义 |
|---|---|---|
| `CPU_TRIGGER` | 95 % | 视为“持续满载”的 CPU 占用 |
| `TEMP_TRIGGER` | 95 °C | 触发温度 |
| `FAN_TRIGGER` | 4000 rpm | 触发风扇转速 |
| `TRIGGER_SUSTAIN` | 10 s | 三条件需同时持续的时长 |
| `TEMP_RECOVER` / `CPU_RECOVER` | 75 °C / 50 % | 恢复阈值 |
| `RECOVER_SUSTAIN` | 30 s | 恢复条件需持续的时长 |
| `MIN_HOLD` | 600 s | 低电量模式最短保持时间 |

改完重装一次（`sudo ./install.sh` 会拷贝脚本）即可生效。

### 说明与限制

- 需要 root：读 SMC 传感器、改电源设置都要求权限。CPU 采样走 Mach 系统调用、温度风扇走 `powermetrics`、切模式走 `pmset`，约 200 行，无任何依赖，可自行审计。守护进程自身占用趋近 0%。
- 风扇读数：部分机型 `powermetrics` 不报风扇，此时风扇条件自动跳过（CPU + 温度双条件兜底触发）。
- Intel Mac 理论可用，但阈值是在 Apple Silicon 上调的。
- 守护进程退出时**有意不**恢复低电量模式——机器保持在当前状态，不做多余动作。

---

## English

**Don't let your Mac be a jet.** A tiny daemon for Apple Silicon MacBooks that automatically switches to **Low Power Mode** when the CPU gets hot and the fans get loud — and switches back when things cool down.

If you own a MacBook Pro but work like it's an Air (light tasks, hate fan noise, don't need peak performance), this is for you.

### How it works

Every ~2 seconds it samples three signals via built-in macOS tools (zero dependencies):

- CPU usage — Mach `host_processor_info` syscall (microseconds, no subprocess)
- CPU die temperature + fan RPM — `powermetrics --samplers smc`, sampled every
  10 s and cached

State machine:

```
        CPU >= 95% AND temp >= 95°C AND fan >= 4000 rpm
        (all sustained for 10 s)
NORMAL ───────────────────────────────────────────────► LOW POWER
   ▲                                                      │
   └──── temp <= 75°C AND CPU <= 50% ─────────────────────┘
        (sustained 30 s, no earlier than 10 min
         after the switch — prevents flapping)
```

On trigger it runs `pmset -a lowpowermode 1`; on recovery it reads the actual
current state first and never fights your manual settings — if you turned Low
Power Mode off by hand, it just resets its internal state quietly.

Typical result on a 14" MacBook Pro (M-series): sustained full load drops from
~95 °C / fans at max to ~60 °C / quiet, at roughly half the compute throughput.
That's the trade — if you need sustained peak performance, don't use this.

### Install

```bash
git clone https://github.com/hawkson-z/mac-not-jet.git
cd mac-not-jet
sudo ./install.sh
```

This installs the script to `/usr/local/bin/mac_not_jet.py` and registers a
LaunchDaemon (runs as root, starts at boot). Requires macOS 12+, Apple Silicon.

### Uninstall

```bash
sudo ./uninstall.sh
```

Removes the daemon and script. Low Power Mode itself is left in whatever state
it's currently in — toggle it in System Settings → Battery if needed.

### Try it first

```bash
# one-shot reading, no switching
sudo python3 mac_not_jet.py --once

# run in foreground, watch the state machine
sudo python3 mac_not_jet.py
```

Logs go to `/var/log/mac_not_jet.log` when running as a daemon:

```bash
tail -f /var/log/mac_not_jet.log
```

### Tune it

Edit the constants at the top of `mac_not_jet.py`:

| Constant | Default | Meaning |
|---|---|---|
| `CPU_TRIGGER` | 95 % | CPU usage that counts as "sustained full load" |
| `TEMP_TRIGGER` | 95 °C | Die temperature to trigger |
| `FAN_TRIGGER` | 4000 rpm | Fan speed to trigger |
| `TRIGGER_SUSTAIN` | 10 s | How long all three must hold |
| `TEMP_RECOVER` / `CPU_RECOVER` | 75 °C / 50 % | Recovery thresholds |
| `RECOVER_SUSTAIN` | 30 s | How long recovery conditions must hold |
| `MIN_HOLD` | 600 s | Minimum time in Low Power Mode |

After editing, reinstall (`sudo ./install.sh` copies the script) to apply.

### Notes & limitations

- Needs root: reading SMC sensors and changing power settings both require it.
  CPU usage comes from the Mach `host_processor_info` syscall, temperature/fan
  from `powermetrics`, switching from `pmset` — ~200 lines, no dependencies.
  The daemon's own footprint stays near 0%.
- Fan readings: if `powermetrics` doesn't report fans on your model, the fan
  condition is skipped (CPU + temperature alone trigger the switch).
- Intel Macs: works in principle, but thresholds were tuned on Apple Silicon.
- The daemon intentionally does **not** restore Low Power Mode on exit — it
  leaves the machine in its current state.

---

## Prior art

- [macos-thermal-governor](https://github.com/abhishekthulasi/macos-thermal-governor) — Rust daemon, event-driven via kernel thermal-pressure notifications, no custom thresholds. Good if you want the OS to decide everything.

## License

MIT
