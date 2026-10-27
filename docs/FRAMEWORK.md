# freq-trader 框架说明（Binance USDⓈ-M 永续 · freqtrade · 策略留空）

## 框架与策略的分工

| 由框架完成（`user_data/strategies/framework_base.py` 与运维工具，策略不能绕过） | 由你的策略完成（继承 `FrameworkStrategy`） |
| --- | --- |
| 止损护栏：距离必须在 `stop_min_pct`–`stop_max_pct`，按 `stop_min_mode` 放弃或改为下限 | `timeframe`、`startup_candle_count` |
| 仓位：单笔风险（含成本与资金费预留）、名义价值 ≤ 1 倍权益 | `populate_indicators()`：指标 |
| 杠杆：强平距离 ≥ 3 倍止损距离，且不超过上限与交易所上限 | `populate_entry_trend()`：`enter_long`、`fw_stop`（绝对止损价）、可选 `fw_entry_cap` |
| 入场前检查：事件窗口、事件文件新鲜度、总在险风险、相关性 | 可选 `take_profit_legs()`：分批止盈（R 倍数、比例、标签） |
| 交易所端止损（按绝对价格）、最长持有天数、Protections（连亏暂停、日/周故障保护、冷却） | 可选 `strategy_exit()`、`populate_exit_trend()`：离场 |
| 监控告警、官方事件日历、复盘指标、研究矩阵、`s03ft` 命令、OpenClaw 运维、一键部署 | 可选 `framework_params`：覆盖框架默认值 |

## 接入策略

1. 按 `docs/STRATEGY_TEMPLATE.md` 写好规格。
2. 修改 `user_data/strategies/MyStrategy.py`（删掉 `is_template = True`），或另建文件并把 `user_data/config.json`（部署后为 `/etc/s03/ft-config.json`）的 `"strategy"` 改成你的类名。
3. `pytest -q`；`s03ft research plan / run / aggregate`（研究矩阵）；`s03ft research g0`（偏差检查）。
4. 按 G1–G9 逐级上线；`s03ft doctor` 会检查是否仍在用空白模板。

默认策略是空白模板 `MyStrategy`：freqtrade、监控、告警、复盘、OpenClaw 都能运行，但不会产生任何入场；
`s03ft daily` 的 problems 会提示“仍在使用空白策略模板”。示例 `examples/strategies/SmaCrossExample.py` 只用于验证框架能跑通。

## 注意

- 护栏按“计划入场价”（`fw_entry_cap`，没有则用信号价）检查；实际成交价不高于它，按成交价算的止损距离可能略小于下限（至多差一个限价偏移）。
- 框架的止损用 freqtrade 的 `stop_loss_abs`；`initial_stop_loss_abs` 是兜底的 −99% 止损，不代表策略止损（复盘指标已按此计算 R）。
- `s03` 是框架的命名空间（包名、服务名、agent 名），与策略无关。原 S-03 策略在 git 历史 v1.x 中。
