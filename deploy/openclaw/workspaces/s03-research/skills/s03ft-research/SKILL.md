---
name: s03ft-research
description: S03 研究：找想法（Vibe-Trading，经白名单代理）、筛因子、研究矩阵回测（子 agent 并行）、复核、统计检验（DSR、PBO、Bootstrap）、G0 偏差检查、规格审查。
metadata: { "openclaw": { "requires": { "bins": ["s03ft", "freqtrade"] } } }
---

# S03 研究技能

## 命令
| 步骤 | 命令 |
| --- | --- |
| 自检 | `s03ft doctor --research` |
| 数据 | `freqtrade download-data -c user_data/config.json --timeframes 1d --timerange 20200101-`；`... --timeframes 1m 15m --timerange 20210101-` |
| 事件历史 | `s03ft events build --out research/events_history.json --history`（BLS 只有 2025 年起的日程，报告中注明） |
| 计划 | `s03ft research plan --pairs BTC,ETH,SOL,BNB,HYPE --years 2022-2025 --variants base,stop_floor,cost2x` |
| 单格 | `s03ft research run --job <任务>` |
| 汇总 | `s03ft research aggregate` |
| 复核 | `s03ft research verify --job <任务>` |
| G0 | `s03ft research g0 --timerange 20250101-` |
| 筛因子 | `s03ft lab factors --coins BTC,ETH,SOL,BNB,HYPE --tf 1d`（研究机装了 Vibe-Trading 时可加 `--zoo all`） |
| 统计检验（矩阵） | `s03ft lab matrix`（读 `research/results/trades/`，输出 `research/lab/matrix-*.md`） |
| 统计检验（单份） | `s03ft lab stats --trades research/results/trades/<任务>.csv` |
| 想法登记簿 | `s03ft lab idea add|list|show|move|attach|trials …` |
| 测试 | `s03ft test` |

## 并行方式
1. `plan` 生成 `research/jobs.json`。
2. 用 sessions_spawn 派生子 agent（context: isolated，最多 4 个同时运行，只嵌套 1 层），每个只执行一条 `s03ft research run --job <任务>`，原样返回最后一行 JSON。
3. 全部完成后 `aggregate`。
4. 派生复核子 agent（任务模板 `tasks/verify.md`），随机抽 3 格 `verify`；任一不一致，整份报告标“未核验”。
5. 策略代码有改动时，派生规格审查子 agent（`tasks/spec-review.md`），并要求 `s03ft test` 全部通过。

## 研究前端：找想法 → 筛因子 → 回测 → 统计检验
所有数字只能来自程序输出（`research/lab/*.json|md`）。
1. **找想法**：用 `vibe-trading` MCP 服务器（研究目标、读网页 / 文档、行情与宏观数据、多智能体讨论、因子库）；交易类工具已被代理拒绝，不要尝试。
   并行时派生找想法子 agent（`tasks/idea-scout.md`），每个只负责一个主题。合格的想法用
   `s03ft lab idea add --title … --hypothesis … --mechanism … --falsification … --source vibe-trading --ref <研究目标 id>` 登记。
2. **筛因子**：`s03ft lab factors …`。只有 alive / reversed 且 `fdr_significant = True` 才算候选；把报告挂到想法上
   （`lab idea attach --id … --kind factors --path …`），状态改为 `screening`。
3. **回测**：把想法写成策略提案放进 `proposals/`（继承 FrameworkStrategy），由 Pocky 放进研究副本后跑研究矩阵；状态改为 `backtesting`。
4. **统计检验**：`s03ft lab matrix`。结论“通过”才能 `attach --kind stats` 后改为 `validated`；否则改为 `rejected` 并写原因。
   不得为了通过而反复换参数：每换一次都是一次新的尝试，必须登记。
5. **批准**：只能由 Pocky 本人执行（`python -m fwresearch idea move … --to approved --by human --approver …`）。
