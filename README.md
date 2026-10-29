# mac-not-jet

**不要让你的 Mac 起飞。** 一个极小的 macOS 守护进程：CPU 持续过热、风扇起飞时，自动切到**低电量模式**（CPU 降频、温度回落、风扇安静）；凉下来之后再自动切回。

买的是 Pro、用得像 Air（日常轻载、烦风扇噪音、不需要峰值性能）的人，就是它的目标用户。

---

## 中文

### 工作原理

每 2 秒采样两项指标（零依赖，不需要装任何东西）：

- CPU 占用率 — Mach `host_processor_info` 系统调用（微秒级，无子进程）
- 系统热压力 — `notify_get_state(com.apple.system.thermalpressurelevel)`（微秒级，无子进程，不需要 root）

热压力是系统自己对"我热不热"的判断，等级数值来自 `OSThermalPressureLevel`：

| 数值 | 等级 | 含义 |
|---|---|---|
| 0 | Nominal | 正常 |
| 10 | Light | 偏热 |
| 20 | Moderate | 热 |
| 30 | Heavy | 很热，开始明显降频 |
| 40 | Trapping | 正在强制降载 |
| 50 | Sleeping | 正在强制睡眠降温 |

状态机：

```
        CPU >= 95% 且 热压力 >= Moderate(20)
        （两项同时持续 30 秒）
正常 ─────────────────────────────────────────────► 低电量模式
  ▲                                                    │
  └──── 热压力 <= Light(10) 且 CPU <= 50% ─────────────┘
        （持续 30 秒；且切换后至少 10 分钟内不恢复，
          防止反复横跳）
```

触发时执行 `pmset -a lowpowermode 1`；恢复前会先读当前实际开关状态，绝不跟你手动设置打架——你手动关了低电量模式，它就安静地重置内部状态。

低电量模式本身的降温效果是实测的：手动开启后持续满载，温度从 ~95°C / 风扇全速降到 ~60°C / 安静，代价是算力约减半。这是一笔交易——需要持续峰值性能的人不要用。

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
# 单次读数，不执行任何切换（读传感器不需要 root）
python3 mac_not_jet.py --once

# 自检：带断言的检查，退出码 0 = 读数链路真的通
./scripts/verify.sh

# 前台运行，看状态机工作
sudo python3 mac_not_jet.py
```

装好之后想验证真的会触发（**会把风扇吹起来**）：

```bash
./scripts/stress.sh        # 压满全部核心，Ctrl+C 停
./scripts/stress.sh 120    # 或压 120 秒后自动停
```

`yes > /dev/null` 只压一个核，在十几核的机器上才 ~10%，永远够不到 95% 的触发线，所以脚本按 `hw.ncpu` 起同样数量的进程。

守护进程的日志在 `/var/log/mac_not_jet.log`：

```bash
tail -f /var/log/mac_not_jet.log
```

### 调参数

改 `mac_not_jet.py` 顶部的常量：

| 常量 | 默认值 | 含义 |
|---|---|---|
| `CPU_TRIGGER` | 95 % | 视为”持续满载”的 CPU 占用 |
| `TRIGGER_PRESSURE` | `PRESSURE_MODERATE` (20) | 触发用的热压力等级，可选 NOMINAL/LIGHT/MODERATE/HEAVY/TRAPPING/SLEEPING |
| `TRIGGER_SUSTAIN` | 30 s | 两条件需同时持续的时长 |
| `CPU_RECOVER` | 50 % | 恢复 CPU 阈值 |
| `RECOVER_PRESSURE` | `PRESSURE_LIGHT` (10) | 恢复到该压力等级以下才算凉下来 |
| `RECOVER_SUSTAIN` | 30 s | 恢复条件需持续的时长 |
| `MIN_HOLD` | 600 s | 低电量模式最短保持时间 |

想更保守（只在系统已经明显降频时才动手）把触发线改成 `PRESSURE_HEAVY`；想更早介入（风扇刚起来就压）可下调到 `PRESSURE_LIGHT`——但 `LIGHT` 在长时间中等负载下也可能出现，会更容易切。

改完重装一次（`sudo ./install.sh` 会拷贝脚本）即可生效。

### 说明与限制

- 需要 root：改电源设置要求权限。CPU 采样走 Mach 系统调用、热压力走 notify API、切模式走 `pmset`，约 300 行，无任何依赖，可自行审计。守护进程自身占用趋近 0%（单次读热压力约 9 微秒）。
- 只看热压力，没有精确温度：Apple Silicon 上 `powermetrics --samplers smc` 已不存在（温度/风扇读数没了），系统没有公开的免特权温度接口。触发信号因此改成系统自己的热压力等级。
- 热压力是"综合判断"，可能比你体感慢半拍；想更早介入就下调 `TRIGGER_PRESSURE`。
- Intel Mac 理论可用，但阈值是在 Apple Silicon 上调的。
- 守护进程退出时**有意不**恢复低电量模式——机器保持在当前状态，不做多余动作。

---

## English

**Don't let your Mac be a jet.** A tiny daemon for Apple Silicon MacBooks that automatically switches to **Low Power Mode** when the CPU gets hot and the fans get loud — and switches back when things cool down.

If you own a MacBook Pro but work like it's an Air (light tasks, hate fan noise, don't need peak performance), this is for you.

### How it works

Every ~2 seconds it samples two signals (zero dependencies):

- CPU usage — Mach `host_processor_info` syscall (microseconds, no subprocess)
- Thermal pressure — `notify_get_state(com.apple.system.thermalpressurelevel)`
  (microseconds, no subprocess, no root needed)

Thermal pressure is the system's own verdict on how hot it is. Values come from
`OSThermalPressureLevel`: `0 Nominal` / `10 Light` / `20 Moderate` /
`30 Heavy` / `40 Trapping` / `50 Sleeping`.

State machine:

```
        CPU >= 95% AND thermal pressure >= Moderate(20)
        (both sustained for 30 s)
NORMAL ───────────────────────────────────────────────► LOW POWER
   ▲                                                      │
   └──── thermal pressure <= Light(10) AND CPU <= 50% ────┘
        (sustained 30 s, no earlier than 10 min
         after the switch — prevents flapping)
```

On trigger it runs `pmset -a lowpowermode 1`; on recovery it reads the actual
current state first and never fights your manual settings — if you turned Low
Power Mode off by hand, it just resets its internal state quietly.

Low Power Mode's cooling effect is measured: switch it on manually under
sustained full load and a 14" MacBook Pro (M-series) drops from ~95 °C / fans at
max to ~60 °C / quiet, at roughly half the compute throughput. That's the trade —
if you need sustained peak performance, don't use this.

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
# one-shot reading, no switching (sensor reads don't need root)
python3 mac_not_jet.py --once

# self-check with assertions; exit code 0 means the reading path really works
./scripts/verify.sh

# run in foreground, watch the state machine
sudo python3 mac_not_jet.py
```

To verify it actually triggers once installed (**this will spin the fans up**):

```bash
./scripts/stress.sh        # saturate every core, Ctrl+C to stop
./scripts/stress.sh 120    # or run for 120 seconds
```

A single `yes > /dev/null` only loads one core — about 10% on a 10+ core Mac, never
enough to cross the 95% line — so the script spawns `hw.ncpu` workers.

Logs go to `/var/log/mac_not_jet.log` when running as a daemon:

```bash
tail -f /var/log/mac_not_jet.log
```

### Tune it

Edit the constants at the top of `mac_not_jet.py`:

| Constant | Default | Meaning |
|---|---|---|
| `CPU_TRIGGER` | 95 % | CPU usage that counts as "sustained full load" |
| `TRIGGER_PRESSURE` | `PRESSURE_MODERATE` (20) | Pressure level to trigger on (NOMINAL/LIGHT/MODERATE/HEAVY/TRAPPING/SLEEPING) |
| `TRIGGER_SUSTAIN` | 30 s | How long both conditions must hold |
| `CPU_RECOVER` | 50 % | Recovery CPU threshold |
| `RECOVER_PRESSURE` | `PRESSURE_LIGHT` (10) | Pressure level that counts as cooled down |
| `RECOVER_SUSTAIN` | 30 s | How long recovery conditions must hold |
| `MIN_HOLD` | 600 s | Minimum time in Low Power Mode |

Want to be more conservative (only act once the system is clearly throttling)? Use
`PRESSURE_HEAVY`. Want to step in earlier (as soon as the fans start)? Drop to
`PRESSURE_LIGHT` — but expect more frequent switches under moderate sustained load.

After editing, reinstall (`sudo ./install.sh` copies the script) to apply.

### Notes & limitations

- Needs root: changing power settings requires it. CPU usage comes from the Mach `host_processor_info` syscall, thermal pressure from the `notify` API, switching from `pmset` — ~300 lines, no dependencies. The daemon's own footprint stays near 0% (one pressure read is ~9 µs).
- Pressure only, no exact temperature: `powermetrics --samplers smc` no longer exists on Apple Silicon (temperature and fan readings are gone), and macOS exposes no public unprivileged temperature API. The trigger signal is therefore the system's own thermal pressure level.
- Thermal pressure is a judgement call by the OS and may lag behind how the machine actually feels; lower `TRIGGER_PRESSURE` to step in earlier.
- Intel Macs: works in principle, but thresholds were tuned on Apple Silicon.
- The daemon intentionally does **not** restore Low Power Mode on exit — it leaves the machine in its current state.

## Changelog

See [CHANGELOG.md](CHANGELOG.md). Short version: v1.0 and v2.0 both silently
failed to trigger on Apple Silicon (a sampler that no longer exists; then a level
vocabulary that `powermetrics` never emits). v2.1 reads the raw pressure value
instead of parsing names.

---

## Prior art

- [macos-thermal-governor](https://github.com/abhishekthulasi/macos-thermal-governor) — Rust daemon, event-driven via kernel thermal-pressure notifications, no custom thresholds. Good if you want the OS to decide everything.

## License

MIT
