---
name: s03ft-review
description: S03 每周复盘：读取程序算好的指标与交易记录，派生子 agent 分标的解读，写周报草稿。
---

# S03 复盘技能

## 输入（全部在 inbox/，只读）
`metrics.md` / `metrics.json`（程序计算：笔数、胜率、期望值 R、盈亏比、最长连亏、离场原因）、`ft_trades.json`、`ft_profit.json`、`ft_daily.json`、`ft_status.json`、`alerts.jsonl`、`events.json`。

## 流程
1. 先读 `metrics.md`。所有统计数字以它为准，不要自己重新数或重算；指标里没有的数字写“未知”。
2. 派生子 agent（context: isolated，最多 4 个，只嵌套 1 层）：BTC、ETH、SOL、BNB 各一个，回答“本周偏差来自执行、策略、市场还是数据”。
3. 再派生两个角色子 agent：风控审查（是否违反第 7 节规则、暂停或熔断原因）、成本归因（手续费、资金费、滑点）。
4. 主 agent 汇总成 `drafts/weekly_<日期>.md`：先归因，再列改动提案（只写提案，不执行），最后发摘要给 Pocky。

## 证据标签
已核验事实（来自 metrics / 交易记录）、来源方观点（含 s03-analyst 的分析）、模型推断、未知。n < 30 时任何结论都属证据不足。
