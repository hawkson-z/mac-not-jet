# mac-not-jet

**不要让你的 Mac 起飞。** 一个极小的 macOS 守护进程：风扇转速升到设定值且持续几秒，就自动切到**低电量模式**（CPU 降频、温度回落、风扇安静）；安静下来之后再自动切回。

买的是 Pro、用得像 Air（日常轻载、烦风扇噪音、不需要峰值性能）的人，就是它的目标用户。

---

## 中文

### 工作原理

每 5 秒采样一次，只做两件事（零依赖，不装任何东西）：

- **风扇转速** — 读 SMC 的 `F0Ac`，经 IOKit 直接访问。微秒级、无子进程、**不需要 root**
- **CPU 占用率** — Mach `host_processor_info` 系统调用（微秒级，无子进程）

温度只用于日志，不参与判断（扫描 SMC 的 `Tp`/`Te`/`Tf` 系列传感器，10 分钟一次心跳时读）。

**为什么用风扇转速而不是温度或热压力：**

噪音的直接来源是风扇，不是温度数字。而且 macOS 在 Apple Silicon 上的热压力判定极其迟钝——实测 10 核全部满载（CPU 99.8%）、温度 90°C 时，`thermalpressurelevel` 仍然报 `Nominal(0)`。用热压力做触发条件，结果是永远不触发；等它升到 `Moderate`，风扇早就全速了。

风扇转速是唯一和你耳朵直接对应的信号。Mac 的风扇通常处于停转（0 rpm），需要散热时才启动，这台机器的实测区间是：

| 项 | 值 |
|---|---|
| 停转 | 0 rpm |
| 最低转速 | ~2317 rpm（2500 rpm 上下，安静环境基本听不到） |
| 最高转速 | ~6550 rpm |
| 触发线（默认） | 4000 rpm |

噪声实测（14 寸 MacBook Pro M5，Notebookcheck）：2500 rpm ≈ 25 dB(A) 基本听不到，3800 rpm ≈ 30 dB(A) 已经能明确听见，5150 rpm ≈ 41 dB(A)，6550 rpm ≈ 48 dB(A)。所以触发线定在 4000——开始觉得吵了才动手，不去赌听不见的区间。

```
        风扇 >= 4000 rpm（持续 5 秒）
正常 ───────────────────────────────► 低电量模式
  ▲                                     │
  └── 风扇 <= 2500 rpm 且 CPU <= 50% ───┘
      （持续 60 秒；且切换后至少 10 分钟内
        不恢复，防止反复横跳）
```

注意恢复条件用的是「回落到低档」而不是「停转」：风扇最低就是 ~2317 rpm，切到低电量模式后它不会回到 0，只会降到最低档。按停转判定会导致永久卡在低电量模式。

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

脚本按 `hw.ncpu` 起同等数量的 `yes` 进程——单个 `yes` 只压一个核，在 10 核机器上才 10%，永远够不到触发线。

守护进程的日志在 `/var/log/mac_not_jet.log`：

```bash
tail -f /var/log/mac_not_jet.log
```

触发时会有这样一行：

```
★ 风扇持续 5s 在 4210 rpm（92.1°C），切换低电量模式
```

### 调参数

改 `mac_not_jet.py` 顶部的常量：

| 常量 | 默认值 | 含义 |
|---|---|---|
| `FAN_TRIGGER` | 4000 rpm | 触发转速。**这是唯一需要按自己机器调整的值** |
| `FAN_RECOVER` | 2500 rpm | 恢复转速，应设为「听起来已经安静」的档位 |
| `CPU_RECOVER` | 50 % | 恢复所需 CPU 占用上限（防止刚恢复又热起来） |
| `TRIGGER_SUSTAIN` | 5 s | 风扇需在该转速以上持续多久才动手 |
| `RECOVER_SUSTAIN` | 60 s | 恢复条件需持续的时长 |
| `MIN_HOLD` | 600 s | 低电量模式最短保持时间 |
| `TICK` | 5 s | 采样间隔 |

**`FAN_TRIGGER` 怎么定**：跑一次 `./scripts/stress.sh 180`，同时用另一终端 `watch -n2 "python3 mac_not_jet.py --once | head -1"` 观察转速，听声音——记住「开始觉得吵」的那个转速，填进去。默认 4000 是按 14 寸 M5 的噪声实测定的：3800 rpm 约 30 dB(A) 已能明确听见，4000 稍高于这条线。

改完重装一次（`sudo ./install.sh` 会拷贝脚本）即可生效。

### 说明与限制

- 只有改电源设置需要 root。读风扇、读温度、读 CPU 全都不需要；`--once` 可以直接以普通用户运行。
- 不依赖 `powermetrics`：它的 `smc` sampler 在 Apple Silicon 上已被移除，本工具直接走 IOKit 读 SMC，不受影响。守护进程自身占用趋近 0%（每 5 秒两次系统调用）。
- 温度 key 列表按 Apple Silicon 各代（M1–M5）列了一遍，读不到的自动跳过。Intel Mac 的风扇也能读，但温度 key 不同，未适配。
- 阈值是按「风扇声音」而不是「温度」定的，每台机器散热曲线不同，务必按上面的方法自己调一次。
- 守护进程退出时**有意不**恢复低电量模式——机器保持在当前状态，不做多余动作。

---

## English

**Don't let your Mac be a jet.** A tiny daemon for Apple Silicon MacBooks: when the fan spins up past a threshold for a few seconds, switch to **Low Power Mode** (the CPU throttles, the temperature drops, the fan goes quiet); switch back once it's quiet again.

If you own a MacBook Pro but work like it's an Air (light tasks, hate fan noise, don't need peak performance), this is for you.

### How it works

Every 5 seconds it samples two things (zero dependencies):

- **Fan speed** — SMC key `F0Ac`, read through IOKit. Microseconds, no subprocess, **no root required**
- **CPU usage** — Mach `host_processor_info` syscall (microseconds, no subprocess)

Temperature is logged only, never used for decisions (SMC `Tp`/`Te`/`Tf` sensors, scanned once per 10-minute heartbeat).

**Why fan speed instead of temperature or thermal pressure:**

Noise comes from the fan, not from a temperature number. And macOS judges thermal
pressure very conservatively on Apple Silicon — measured here: 10 cores fully
loaded (CPU 99.8%) at 90 °C, `thermalpressurelevel` still reported `Nominal(0)`.
Triggering on pressure means never triggering; by the time it reaches `Moderate`,
the fan has been at full speed for a while.

Fan speed is the only signal that maps directly to what you hear. The fan sits at
0 rpm until cooling is needed; measured range on this machine:

| Item | Value |
|---|---|
| Stopped | 0 rpm |
| Minimum | ~2317 rpm (around 2500 rpm, inaudible in a quiet room) |
| Maximum | ~6550 rpm |
| Trigger (default) | 4000 rpm |

Measured noise (14" MacBook Pro M5, Notebookcheck): 2500 rpm ≈ 25 dB(A) is
inaudible, 3800 rpm ≈ 30 dB(A) is already clearly audible, 5150 rpm ≈ 41 dB(A),
6550 rpm ≈ 48 dB(A). Hence 4000 — act once it actually bothers you, instead of
throttling through ranges you can't hear.

```
        fan >= 4000 rpm (sustained 5 s)
NORMAL ────────────────────────────────► LOW POWER
   ▲                                       │
   └── fan <= 2500 rpm AND CPU <= 50% ─────┘
       (sustained 60 s, no earlier than 10 min
        after the switch — prevents flapping)
```

Recovery uses "dropped back to idle speed" rather than "stopped": the fan's floor
is ~2317 rpm, so it never returns to 0 after throttling — only to its minimum.
Waiting for a full stop would leave Low Power Mode stuck on forever.

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

The script spawns `hw.ncpu` workers — a single `yes` only loads one core, about
10% on a 10-core Mac, never enough to cross the trigger line.

Logs go to `/var/log/mac_not_jet.log` when running as a daemon:

```bash
tail -f /var/log/mac_not_jet.log
```

A trigger looks like this:

```
★ 风扇持续 5s 在 4210 rpm（92.1°C），切换低电量模式
```

### Tune it

Edit the constants at the top of `mac_not_jet.py`:

| Constant | Default | Meaning |
|---|---|---|
| `FAN_TRIGGER` | 4000 rpm | Trigger speed. **The only value you need to tune per machine** |
| `FAN_RECOVER` | 2500 rpm | Recovery speed; set it to where it already sounds quiet |
| `CPU_RECOVER` | 50 % | CPU ceiling for recovery (stops it recovering straight back into heat) |
| `TRIGGER_SUSTAIN` | 5 s | How long the fan must stay above the trigger speed |
| `RECOVER_SUSTAIN` | 60 s | How long recovery conditions must hold |
| `MIN_HOLD` | 600 s | Minimum time in Low Power Mode |
| `TICK` | 5 s | Sampling interval |

**How to pick `FAN_TRIGGER`**: run `./scripts/stress.sh 180` and, in another
terminal, `watch -n2 "python3 mac_not_jet.py --once | head -1"` while listening.
Note the speed at which the noise starts to bother you and use that. The default
4000 follows the measured noise curve of a 14" M5: 3800 rpm ≈ 30 dB(A) is clearly
audible, and 4000 sits just above that line.

After editing, reinstall (`sudo ./install.sh` copies the script) to apply.

### Notes & limitations

- Only changing the power setting needs root. Fan, temperature and CPU reads don't; `--once` runs fine as a normal user.
- No `powermetrics` dependency: its `smc` sampler was removed on Apple Silicon, and this reads the SMC through IOKit directly. The daemon's own footprint stays near 0% (two syscalls every 5 seconds).
- The temperature key list covers Apple Silicon generations M1–M5 (unreadable keys are skipped). Intel fans can be read too, but the temperature keys differ and aren't mapped.
- Thresholds are derived from *fan noise*, not temperature; cooling curves differ per machine, so tune it once as described above.
- The daemon intentionally does **not** restore Low Power Mode on exit — it leaves the machine in its current state.

## Changelog

See [CHANGELOG.md](CHANGELOG.md). Short version: v0.1.0, v0.2.0 and v0.2.1 all
failed to trigger on Apple Silicon for different reasons (a sampler that no
longer exists; then a level vocabulary `powermetrics` never emits; then a thermal
pressure signal the OS barely moves). v0.3.0 goes back to a physical quantity —
fan RPM read straight from the SMC — which is also the thing that actually makes
noise. It is still 0.x: no version has yet been verified through a full
loud → throttle → quiet → recover cycle on real hardware.

---

## Prior art

- [macos-thermal-governor](https://github.com/abhishekthulasi/macos-thermal-governor) — Rust daemon, event-driven via kernel thermal-pressure notifications, no custom thresholds. Good if you want the OS to decide everything.

## License

MIT
