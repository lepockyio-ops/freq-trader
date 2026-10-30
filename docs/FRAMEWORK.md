# freq-trader 框架说明（Binance USDⓈ-M 永续 · freqtrade · 策略留空）

## 框架与策略的分工

| 由框架完成（`user_data/strategies/framework_base.py` 与运维工具；策略只要不覆盖框架回调就不能绕过） | 由你的策略完成（继承 `FrameworkStrategy`） |
| --- | --- |
| 入场计划随订单保存（`enter_tag`）：定仓、杠杆、入场检查、成交后止损只读这一份计划 | `timeframe`（周期只由策略决定，配置里不要写）、`startup_candle_count` |
| 止损护栏：按下单价检查距离在 `stop_min_pct`–`stop_max_pct`；成交后再按成交价检查，不合格立即离场 | |
| 仓位：单笔风险（含成本与资金费预留）、名义价值 ≤ 1 倍权益 | `populate_indicators()`：指标 |
| 杠杆：强平距离 ≥ 3 倍止损距离，且不超过上限与交易所上限 | `populate_entry_trend()`：`enter_long`、`fw_stop`（绝对止损价）、可选 `fw_entry_cap` |
| 入场前检查：事件窗口、事件日历有效性、新闻守卫、总在险风险、相关性；检查本身出错一律拒绝 | 可选 `take_profit_legs()`：分批止盈（R 倍数、比例、标签） |
| 交易所端止损（按绝对价格）、最长持有天数、Protections（连亏暂停、日/周故障保护、冷却） | 可选 `strategy_exit()`、`populate_exit_trend()`：离场 |
| 监控告警、官方事件日历、复盘指标、研究矩阵、`s03ft` 命令、OpenClaw 运维、一键部署 | 可选 `framework_params`：覆盖框架默认值 |

## 接入策略

1. 按 `docs/STRATEGY_TEMPLATE.md` 写好规格。
2. 修改 `user_data/strategies/MyStrategy.py`（删掉 `is_template = True`），或另建文件并把 `user_data/config.json`（部署后为 `/etc/s03/ft-config.json`）的 `"strategy"` 改成你的类名。
3. `pytest -q`；`s03ft research plan / run / aggregate`（研究矩阵）；`s03ft research g0`（偏差检查）。
4. 按 G1–G9 逐级上线；`s03ft doctor` 会检查是否仍在用空白模板。

默认策略是空白模板 `MyStrategy`：freqtrade、监控、告警、复盘、OpenClaw 都能运行，但不会产生任何入场；
`s03ft daily` 的 problems 会提示“仍在使用空白策略模板”。示例 `examples/strategies/SmaCrossExample.py` 只用于验证框架能跑通。

## 研究前端（v2.2：找想法 → 筛因子 → 回测 → 统计检验）

`fwresearch/`（两个模板共用）提供想法登记簿、内置因子库（55 个）与因子检验（5 个币时默认跨币种比较）、逐笔 R 的统计检验（Bootstrap、符号翻转、滚动分段、
Deflated Sharpe、PBO）。找想法可选接入 Vibe-Trading，经 `mcp-guard` 白名单代理，只给研究 agent 用，交易类工具一律拒绝。
入口：`s03ft lab …`；详见 `docs/RESEARCH_LAB.md`。

## 注意

- **止损绑定（v2.6）**：信号行的 `fw_stop`、`fw_entry_cap` 和信号时间由框架写进 `enter_tag`（策略自己的 `enter_tag` 保留在 `u=` 之后），随订单存进数据库。
  成交（含部分成交、重启后第一次检查）时按成交价校验：止损低于成交价、距离不超过上限、实际风险不超过预算；成交价比下单价更优导致距离不足下限时，止损放宽到下限 + 0.02 个百分点（风险仍在预算内）。
  不满足、或仓位没有框架计划（例如手动开仓）：`custom_exit` 立即以 `fw_invalid_stop:<原因>` 市价离场。
- **分批止盈（v2.6）**：每一档是否完成按实际成交量判断（初始数量 − 当前数量，允许 2% 取整误差），不在下单前标记；有未完成订单时不重复下单；
  同一档下单 3 次仍未成交（拒单、低于最小名义等）就跳过并告警。
- **总风险（v2.7 修正）**：入场已走完的仓位按绑定的止损 × 持仓数量；入场还在进行（未成交或部分成交）的按止损 × 下单数量（`amount_requested`）和下单价计入。
  freqtrade 新建交易时 `amount = 0`、挂单期间不更新，v2.6 用的是 `amount`，挂单风险实际按 0 计算。任何一笔缺止损、缺计划、数量读不出或已判为无效时，不再新增风险。
- **成交价更优时的止损（v2.7，参数 `better_fill_policy`，由你决定）**：按成交价算的止损距离低于下限时，`widen`（默认，v2.6 的行为）放宽到下限 + 0.02 个百分点，
  风险仍在预算内但止损不在原结构位；`exit` 立即离场；`keep` 保留原结构止损（违反距离下限）。研究方案 `fill_exit`、`fill_keep` 可与 `base` 做组合回测对比，
  看交易数、亏损尾部、成本和回撤，不只看收益。决定后写进 `framework_params` 和策略规格。
- **部分成交（v2.7）**：freqtrade 在入场单仍挂着时不更新持仓数量，交易所上不挂任何止损。框架在 `check_entry_timeout` 里发现入场单已部分成交，就撤掉剩余部分；
  已成交部分随即成为正式仓位，下一轮循环（约 5 秒）绑定止损并挂到交易所。剩余部分太小、撤单会留下无法平仓的碎仓时，freqtrade 会拒绝撤单，此时仍要等整单成交或超时。
- **相关性（v2.6）**：框架在 `informative_pairs()` 里固定订阅白名单全部交易对的日线（子类覆盖时要合并 `super().informative_pairs()`）；
  日线缺失、不足 31 根或算不出相关系数时拒绝入场。回测需要先下载 1d 数据。
- **周期**：`timeframe` 只写在策略里。配置里写了会覆盖策略（freqtrade 规定配置优先）；`s03ft doctor` 会检查。
- **事件日历**：`event_file_required` 为真（实盘默认）时，未配置或找不到文件、文件损坏或时间非法、超过 3 天未更新、所有来源失败、
  某个失败来源没有沿用到事件、覆盖区间不含现在，都拒绝入场。部分来源失败但沿用了上一版事件时放行并告警。
- 框架的止损用 freqtrade 的 `stop_loss_abs`；`initial_stop_loss_abs` 是兜底的 −99% 止损，不代表策略止损（复盘指标已按此计算 R）。
- `s03` 是框架的命名空间（包名、服务名、agent 名），与策略无关。原 S-03 策略在 git 历史 v1.x 中。
