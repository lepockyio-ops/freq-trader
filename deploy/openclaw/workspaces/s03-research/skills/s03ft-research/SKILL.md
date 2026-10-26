---
name: s03ft-research
description: S-03 研究：下载数据、研究矩阵回测（子 agent 并行）、复核、G0 偏差检查、事件历史、规格审查。
metadata: { "openclaw": { "requires": { "bins": ["s03ft", "freqtrade"] } } }
---

# S-03 研究技能

## 命令
| 步骤 | 命令 |
| --- | --- |
| 自检 | `s03ft doctor --research` |
| 数据 | `freqtrade download-data -c user_data/config.json --timeframes 1d --timerange 20200101-`；`... --timeframes 1m 15m --timerange 20210101-` |
| 事件历史 | `s03ft events build --out research/events_history.json --history`（BLS 只有 2025 年起的日程，报告中注明） |
| 计划 | `s03ft research plan --pairs BTC,ETH,SOL,BNB --years 2022-2025 --variants base,stop_floor,cost2x` |
| 单格 | `s03ft research run --job <任务>` |
| 汇总 | `s03ft research aggregate` |
| 复核 | `s03ft research verify --job <任务>` |
| G0 | `s03ft research g0 --timerange 20250101-` |
| 测试 | `s03ft test` |

## 并行方式
1. `plan` 生成 `research/jobs.json`。
2. 用 sessions_spawn 派生子 agent（context: isolated，最多 4 个同时运行，只嵌套 1 层），每个只执行一条 `s03ft research run --job <任务>`，原样返回最后一行 JSON。
3. 全部完成后 `aggregate`。
4. 派生复核子 agent（任务模板 `tasks/verify.md`），随机抽 3 格 `verify`；任一不一致，整份报告标“未核验”。
5. 策略代码有改动时，派生规格审查子 agent（`tasks/spec-review.md`），并要求 `s03ft test` 全部通过。
