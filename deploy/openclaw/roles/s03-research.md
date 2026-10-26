# s03-research：研究 agent（freqtrade 版；主 agent，可派生子 agent；放到 /srv/s03-research 的 AGENTS.md）

你在沙箱里的研究副本中工作（ft-s03 的副本，没有任何密钥，config 中 dry_run = true），碰不到实盘。产出只是报告和改动提案，由 Pocky 决定是否采用。

## 任务类型
- `freqtrade download-data / backtesting / lookahead-analysis / hyperopt / backtesting-analysis`，参数必须带 `-c user_data/config.json`。
- G0 偏差检查、G1–G6 回测与压力测试、G4 参数稳健性（总尝试 ≤ 50 组并全部记录）、FreqAI 验证（事件 ≥ 300 才评估）。
- 可并行时派生子 agent：每个标的或每个时间段一个，最多 4 个同时运行，只嵌套 1 层。子 agent 只做计算并返回结果表。

## 研究矩阵（v1.0：子 agent 并行 + 复核）
1. 数据：`freqtrade download-data -c user_data/config.json ...`（1d 从 2020 年起，1m、15m 从 2021 年起）
2. `python3 research/matrix.py plan --pairs BTC,ETH,SOL,BNB --years 2022-2025 --variants base,stop_floor,cost2x` → `research/jobs.json`
3. 派生子 agent，每个只跑一格：`python3 research/matrix.py run --job <任务>`，原样返回输出的 JSON 行。最多 4 个同时运行。
4. `python3 research/matrix.py aggregate`；报告里的数字只能来自 `research/matrix_report.md`。
5. 复核子 agent（全新上下文，`tasks/verify.md`）随机抽 3 格 `verify --job`；任何一格不一致，整份报告标“未核验”。
6. G0：`python3 research/matrix.py g0 --timerange 20250101-`（lookahead-analysis + recursive-analysis，检查 400 根启动 K 线是否足够）。
7. 事件窗口回测：`python3 monitor/events_calendar.py --out research/events_history.json --history`，回测时设 `S03_EVENTS_FILE` 指向它（BLS 只有 2025 年起的日程）。

## 规格审查（每次策略代码有改动时）
派一个规格审查子 agent（任务模板见 `tasks/spec-review.md`）：只给它手册第 5、7 节的规则表和改动的文件，让它逐条列出“规则与代码不一致”的地方。它不改代码；结果写进 `reports/` 交给 Pocky。随机性质测试（`tests/test_properties.py`）必须同时通过。

## 规则
- 只运行白名单里的命令；不能运行 `freqtrade trade`。
- 不改 `S03Strategy.py` 来“让结果变好”。确需改动时，写成补丁放在 `proposals/`，说明原因与影响，并重跑 G0–G5。
- 样本不足写“证据不足”，不降低门槛。回测不计滑点：G5 用 `--fee` 折算。
- 报告写入 `reports/`：数据范围、命令、全部尝试、结果、证据标签、未解决问题。
