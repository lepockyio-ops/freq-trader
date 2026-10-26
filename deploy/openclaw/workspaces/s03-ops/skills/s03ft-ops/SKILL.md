---
name: s03ft-ops
description: S-03 freqtrade 运维：每日例行、服务故障处理、查看状态与日志。只用 s03ft 命令。
metadata: { "openclaw": { "requires": { "bins": ["s03ft"] } } }
---

# S-03 运维技能

## 可用命令（只有这些）
| 命令 | 作用 |
| --- | --- |
| `s03ft daily` | 每日例行：更新事件日历 → 导出复盘数据 → 返回 JSON 汇总（服务、代理、事件、指标、24 小时告警、problems） |
| `s03ft status` | 当前状态（JSON） |
| `s03ft doctor` | 环境自检 |
| `s03ft svc start|stop|restart freqtrade|monitor|gate` | 启停服务 |
| `s03ft job events|export` | 单独运行事件日历或导出 |
| `s03ft logs freqtrade|monitor|gate` | 最近 200 行日志 |
| `s03ft gate status` | 代理状态（持仓、运行状态、最近告警） |
| `s03ft metrics` | 程序算好的复盘指标 |

## 每日流程（被“每日例行”唤醒时）
1. 运行 `s03ft daily`。
2. 按输出给 Pocky 写摘要：服务状态；dry_run 与运行状态（来自 gate）；持仓；未来事件（`events.next_events`）；事件改期（`events.changes` 中的 moved / removed，必须单独列出）；24 小时告警数；`problems` 原文。
3. `problems` 不为空时，按下面的故障流程处理对应服务；事件日历来源异常只转告 Pocky（`events.sources` 原文），不要自己修改任何文件。

## 故障流程（被“服务 X 进入 failed 状态”唤醒时）
1. `s03ft logs <服务>`，判断原因。
2. 网络或交易所接口的暂时错误 → 最多 `s03ft svc restart <服务>` 一次；配置、密钥、策略代码错误 → 不重启。
3. 2 分钟后 `s03ft status`。仍失败：`s03ft svc stop <服务>`（freqtrade 停止时交易所端止损单仍在），通知 Pocky 并附日志要点。
4. 同一服务 24 小时内第二次失败：不重启，直接通知 Pocky。
5. 事件文件超过 3 天未更新时，交易程序会拒绝新开仓：这是设计行为，先 `s03ft job events`，失败就通知 Pocky。
