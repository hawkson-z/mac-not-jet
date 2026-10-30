# Changelog

版本号约定：在真机上跑通完整一轮「吵起来 → 降频 → 安静 → 恢复」并确认有效之前，
只发 `0.x`。中间每次改动递增小版本（修 bug 用第三位）。第一个实测有效的版本
才叫 `v1.0.0`。

## v0.3.1 — 2026-10-30

v0.3.0 的逻辑正确性没问题，但 review 发现四个会导致静默失效或行为怪异的缺陷：

1. **SMC 连接失效后不重连。** `SMC.read()` 失败只会返回 None，从不重建 IOKit
   连接（docstring 声称会）。笔记本睡眠唤醒后连接可能失效 → 守护进程永久
   「失明」，只剩每 10 分钟一条警告。修复：IOKit 层失败自动重连一次再试；
   连续 20 轮（约 100 秒）读不到就退出，靠 launchd 的 KeepAlive 拉起新进程
   自愈。注意只对 IOKit 层失败重连——「key 不存在」是正常情况（单风扇机型
   读 F1Ac、温度表里不适用的 key），重连只会白白空转。
2. **恢复分支把「读不到」当「用户已手动关闭」。** `get_lpm()` 失败返回 None，
   旧代码 `if get_lpm(): ... else: 手动关闭` 会把 None 也当成已关闭，状态机
   被错误重置。修复：三态判断，读不到就稍后重查，不动状态。
3. **和用户手动操作打架。** 旧代码在用户手动关掉低电量模式后，若风扇仍在
   触发线上，5 秒后会强行切回；启动时接管的、用户自己开的低电量模式，也可能
   在恢复条件满足时被代为关掉。修复：手动关闭后 30 分钟内不自动切换
   （`MANUAL_OVERRIDE_S`）；不是自己开的低电量模式只监视、不代为关闭。
4. **恢复路径 pmset 失败无退避**（触发路径修过，这里漏了）：失败后每 5 秒
   重试刷日志。修复：失败后重新走满 60 秒计时时长，兼作退避。

顺带：双风扇机型（16 寸 MBP）读 F0Ac + F1Ac 取最大值，按较吵的那把判断；
`stress.sh` 的提示文案还是 v0.2 时代的「CPU>=95% 且热压力」，已更正。

---

## v0.3.0 — 2026-10-29

### 触发信号换回物理量：风扇转速

v0.2.1 的技术实现没问题，但选错了信号。实测证据（10 核全部满载，`yes` × 16）：

```
09:54:54 心跳: 状态=normal cpu=99.8% 压力=Nominal
```

CPU 99.8%、结温 90°C，系统热压力仍然是 `Nominal(0)`。也就是说这条触发线在
真机上永远够不到——**v0.2.1 从装上到被换掉，一次都没触发过**。macOS 在 Apple
Silicon 上对热压力的判定极其迟钝，等它升到 `Moderate`，风扇早已全速运转。

同时确认了另一个事实：`powermetrics --samplers smc` 虽然被移除，但 **SMC 本身
还在**，可以经 IOKit 直接读，不需要 root：

```python
IOServiceGetMatchingService(0, IOServiceMatching(b"AppleSMC"))
IOConnectCallStructMethod(conn, 2, &in, size, &out, &outSize)   # KERNEL_INDEX_SMC
```

实测读到的这台机器：

| Key | 类型 | 值 |
|---|---|---|
| `FNum` | ui8 | 1（风扇数量） |
| `F0Ac` | flt（小端） | 当前转速，停转时 0 |
| `F0Mn` | flt | 2317 rpm |
| `F0Mx` | flt | 6550 rpm |
| `Tp00`…`Tp0y` | flt | CPU 温度，18 个传感器，空闲约 55–62°C |

### 新的触发与恢复

```
触发: 风扇 >= 4000 rpm 持续 5 秒
恢复: 风扇 <= 2500 rpm 且 CPU <= 50% 持续 60 秒（切换后至少保持 10 分钟）
```

两个设计决定：

1. **不按「风扇停转」判恢复。** 风扇最低档就是 ~2317 rpm，切到低电量模式后它
   不会回到 0，只会降到最低档。按「停转」判定会导致永久卡在低电量模式。
2. **触发线 4000 而不是「只要转就触发」。** 起初定的是 3000，查到这台机器
   （14 寸 M5，Notebookcheck 实测）的噪声曲线后上调：2500 rpm ≈ 25 dB(A) 基本
   听不到，3800 rpm ≈ 30 dB(A) 已经能明确听见，5150 rpm ≈ 41 dB(A)，6550 rpm
   ≈ 48 dB(A)。3000 落在「听不见」的区间里，会在完全无感的情况下白降频；
   4000 略高于「开始能听见」，只在真的吵起来时才动手。

### 其它

- 采样间隔 2s → 5s（读 SMC 本身是微秒级，放慢只为减少唤醒次数）
- 温度恢复为可用信号，但仅用于日志与诊断，不参与判断
- 修 `pmset` 切换失败时的日志刷屏：持续失败会每 5 秒打一条 ★，
  改为 60 秒退避
- `--once` 输出风扇转速、转速区间、最高温、CPU、当前模式

---

## v0.2.1 — 2026-10-29

### 修掉一个让 v0.2.0 同样失效的 bug

v0.2.0 用 `powermetrics --samplers thermal` 读热压力，再按
`Nominal / Elevated / High / Serious / Critical` 这套名字解析。但这套名字
**powermetrics 根本不会输出**——它输出的是
`Nominal / Moderate / Heavy / Sleeping / Trapping / Undefined`
（数值对应 `OSThermalPressureLevel`：0 / 20 / 30 / 50 / 40）。

结果是：空闲时输出 `Nominal` 恰好命中，看起来一切正常；一旦真的热起来，
输出 `Moderate` 或 `Heavy`，解析失败返回 `None`，主循环走 fail-safe
「不触发」——**和 v0.1.0 一样永远不会切**。

### 改法：不再解析名字，直接读数值

热压力改走 `notify_get_state(com.apple.system.thermalpressurelevel)`：

```python
lib.notify_register_check(b"com.apple.system.thermalpressurelevel", byref(token))
lib.notify_get_state(token, byref(state))   # → 0 / 10 / 20 / 30 / 40 / 50
```

带来三个顺带好处：

| | v0.2.0 | v0.2.1 |
|---|---|---|
| 子进程 | 每 10 秒 spawn 一次 `powermetrics` | 无 |
| 需要 root | 是 | 否（守护进程仍是 root，但读取本身不需要） |
| 单次开销 | 约 1 秒 CPU | 约 9 微秒（实测 1000 次 9.4ms） |
| 等级名对不上 | 会静默失效 | 结构上不可能发生 |

### 其它修正

- `--once` 的 CPU 读数不再恒为 `n/a`（内部先建基线、隔 1 秒再采）
- 启动时先做一次自检：热压力读不到就写日志并退出，不再"装作在工作"
- `set_lpm()` 改为执行后重试 3 次、每次间隔 250ms 再判定失败
- 删除 10 秒缓存：既然单次读取是微秒级，缓存只会带来"冻结在旧值"的风险
  （若冻结在 `Heavy`，触发后恢复条件永不满足 → 永久停在低电量模式）
- 默认触发线定为 `Moderate`（20），恢复线 `Light`（10）；想更保守可改
  `PRESSURE_HEAVY`

### 脚本

- 新增 `scripts/verify.sh`：非交互自检，带断言与退出码，能识别"跑的是旧版本"
- 新增 `scripts/stress.sh`：按 `hw.ncpu` 起满所有核心的 `yes` 进程
  （旧脚本只压 1 核，M5 上约 10%，永远到不了 95%，测不出触发）
- `.gitignore` 增加 `__pycache__/`、`*.pyc`

---

## v0.2.0 — 2026-10-28

`powermetrics --samplers smc` 在 Apple Silicon 上已不存在（`unrecognized
sampler: smc`），温度与风扇读数恒为 `None`，v0.1.0 从装上起就是空壳。改用系统
热压力等级作为触发信号。

- 删除 `TEMP_TRIGGER` / `TEMP_RECOVER` / `FAN_TRIGGER` 与温度、风扇读取代码
- 触发条件从三条件（CPU + 温度 + 风扇）简化为两条件（CPU + 热压力）
- `TRIGGER_SUSTAIN` 10s → 30s

**遗留问题**：等级名解析错误（见 v0.2.1），实际仍然无法触发。

---

## ~~v0.2.0 热压力等级对照~~（已废弃）

v0.2.0 曾给出下面这张表，其中的等级名在 powermetrics 输出里并不存在，已作废。
真实等级见 v0.2.1 说明：`Nominal 0 / Light 10 / Moderate 20 / Heavy 30 /
Trapping 40 / Sleeping 50`。

---

## v0.1.0 — 2026-10-28

首个版本。CPU 占用（Mach `host_processor_info`）+ 温度/风扇
（`powermetrics --samplers smc`）三条件触发，切换低电量模式，带回滞与最短
保持时间。

**结果**：Apple Silicon 上温度/风扇永远读不到，功能未生效。
