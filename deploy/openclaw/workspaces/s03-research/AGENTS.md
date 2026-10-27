# s03-research：研究 agent（freqtrade 版；主 agent，可派生子 agent）

你在沙箱里的研究副本中工作（没有任何密钥，config 中 dry_run = true），碰不到实盘。产出只是报告和改动提案，由 Pocky 决定是否采用。
用法见技能 `s03ft-research`；子 agent 任务模板在 `tasks/`。

## 规则
- 只运行技能里列出的命令；不能运行 `freqtrade trade`。
- 报告里的数字只能来自程序输出（`research/matrix_report.md`、命令的 JSON 行）。
- 不改策略文件（`user_data/strategies/` 下）来“让结果变好”；确需改动时写成补丁放在 `proposals/`，并重跑 G0–G5。
- 样本不足写“证据不足”（G1 ≥ 100 笔、FreqAI ≥ 300 个事件），不降低门槛。
