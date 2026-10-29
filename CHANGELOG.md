# Changelog

## v2.1 — 2026-10-29

### 修掉一个让 v2.0 同样失效的 bug

v2.0 用 `powermetrics --samplers thermal` 读热压力，再按
`Nominal / Elevated / High / Serious / Critical` 这套名字解析。但这套名字
**powermetrics 根本不会输出**——它输出的是
`Nominal / Moderate / Heavy / Sleeping / Trapping / Undefined`
（数值对应 `OSThermalPressureLevel`：0 / 20 / 30 / 50 / 40）。

结果是：空闲时输出 `Nominal` 恰好命中，看起来一切正常；一旦真的热起来，
输出 `Moderate` 或 `Heavy`，解析失败返回 `None`，主循环走 fail-safe
「不触发」——**和 v1.0 一样永远不会切**。

### 改法：不再解析名字，直接读数值

热压力改走 `notify_get_state(com.apple.system.thermalpressurelevel)`：

```python
lib.notify_register_check(b"com.apple.system.thermalpressurelevel", byref(token))
lib.notify_get_state(token, byref(state))   # → 0 / 10 / 20 / 30 / 40 / 50
```

带来三个顺带好处：

| | v2.0 | v2.1 |
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

## v2.0 — 2026-10-28

`powermetrics --samplers smc` 在 Apple Silicon 上已不存在（`unrecognized
sampler: smc`），温度与风扇读数恒为 `None`，v1.0 从装上起就是空壳。改用系统
热压力等级作为触发信号。

- 删除 `TEMP_TRIGGER` / `TEMP_RECOVER` / `FAN_TRIGGER` 与温度、风扇读取代码
- 触发条件从三条件（CPU + 温度 + 风扇）简化为两条件（CPU + 热压力）
- `TRIGGER_SUSTAIN` 10s → 30s

**遗留问题**：等级名解析错误（见 v2.1），实际仍然无法触发。

---

## ~~v2.0 热压力等级对照~~（已废弃）

v2.0 曾给出下面这张表，其中的等级名在 powermetrics 输出里并不存在，已作废。
真实等级见 v2.1 说明：`Nominal 0 / Light 10 / Moderate 20 / Heavy 30 /
Trapping 40 / Sleeping 50`。

---

## v1.0 — 2026-10-28

首个版本。CPU 占用（Mach `host_processor_info`）+ 温度/风扇
（`powermetrics --samplers smc`）三条件触发，切换低电量模式，带回滞与最短
保持时间。

**结果**：Apple Silicon 上温度/风扇永远读不到，功能未生效。
