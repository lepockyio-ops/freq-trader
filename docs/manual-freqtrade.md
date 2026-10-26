# freqtrade 交易流程手册（S-03 · v1.1）

Oct 26, 2026 · @Pocky

## 结论摘要

**v1.1 更新（2026-10-26）：** 整套流程封装成可直接交给 OpenClaw 执行的库（第 15 节）：统一命令 `s03ft`、4 个 agent 的工作区与技能、一键部署脚本。

**v1.0 更新（2026-10-26）：** 执行了 5 项改进（第 14 节），并修复了策略里的两个问题：最小止损 0.8% 未生效、资金费预留过度压缩仓位。

**v0.4 更新（2026-10-26）：** 整套流程改为由 OpenClaw agent 启动和运行，新增第 13 节。分工与主手册第 17 节相同：s03-ops 运维、s03-analyst 告警分析用单个 agent，s03-research 研究、s03-review 复盘用主 agent 派生子 agent。freqtrade 与监控程序做交易决策和监控；dry\_run 开关、API Key、恢复交易仍只由你控制。

**v0.3 更新（2026-10-26）：** 新增第 12 节“行情监控与告警”。独立的监控进程用 Binance WebSocket 盯盘、按阈值告警，不消耗 LLM token；只有告警才交给 OpenClaw 分析。OpenClaw 唯一能做的动作是经白名单代理调用 freqtrade 的 `/stopentry`（只停新开仓）。告警核心代码与 Hyperliquid 版是同一份。

**v0.2 更新（2026-10-26）：** 按你的决定，交易所定为 **Binance USDⓈ-M 永续合约**（你确认可用、无地区限制），入场保持**实现 A**。freqtrade 本身不支持突破买入单，所以策略改用 1 分钟主时间框架来近似实现 A：1 分钟最高价一触及触发价，就在下一分钟开盘以限价买入，比交易所端的突破单慢约 1 分钟。策略文件已写好，并在 freqtrade 2026.9 上加载、回测、做过前视偏差检查（结果见第 6 节）。下表为 v0.1 时的对比，入场与交易所两行已更新。

这份手册把《加密货币交易流程手册》里的 S-03 策略和风控设定搬到 [freqtrade](https://github.com/freqtrade/freqtrade) 上运行。不再自己调用 Hyperliquid 原生 API，而是由 freqtrade 负责连接交易所、回测、模拟运行和实盘下单。好处是少写大量基础设施，并且自带前视偏差检查、保护机制和 FreqAI；代价是入场方式、止损口径和止损更新速度都要跟着 freqtrade 的规则来。

**与 Hyperliquid 原生版的关键差异：**

| 项目 | Hyperliquid 原生版 | freqtrade 版 | 影响 |
| --- | --- | --- | --- |
| 入场方式 | 实现 A：前高上方挂突破触发单 | 用 1 分钟主框架近似实现 A：触及触发价后，下一分钟开盘限价买入 | 比交易所端突破单慢约 1 分钟；回测、dry-run、实盘口径一致 |
| 止损口径 | 按价格设定 | freqtrade 的 stoploss 是“占自有资金的亏损比例”，有杠杆时会被放大 | 必须用 custom\_stoploss 把价格止损换算成比例 |
| 交易所端止损 | 入场单与止损成组提交 | 入场成交后由 freqtrade 挂出；默认每 60 秒检查更新一次 | 成交到挂出之间有短暂空窗 |
| 交易所 | Hyperliquid | Binance USDⓈ-M 永续合约，交易所端止损用市价单 | 跳空时止损仍会成交，但可能有滑点 |
| 前视偏差检查 | 自写单元测试 | 自带 lookahead-analysis | 更容易发现“偷看未来”的指标 |
| 停手规则 | 自写风控层 | 自带 Protections（连续止损、回撤、冷却期） | 配置即可，回测中也能启用 |
| 机器学习 | 只借鉴 FreqAI 的方法 | 可以直接使用 FreqAI | 但样本不足的问题完全相同 |

**不变的部分：** S-03 的筛选与出场规则、单笔 0.5% 风险、杠杆上限 30 倍（单仓杠杆仍按公式控制在约 7–9 倍）、不设最大回撤、上线门槛 G1–G9。

本文是流程设计，不含策略有效的证据，也不是交易指令。freqtrade 的功能描述来自其 2026-10-26 的官方文档。

## 1. 适用对象、已知条件与假设

适用于 1 个人、账户 10,000 USD、由 freqtrade 全自动下单的场景。“已知”来自你在主手册中的回答，其余为假设。

| 维度 | 内容 | 状态 |
| --- | --- | --- |
| 标的 | BTC、ETH、SOL、BNB 永续合约，用 StaticPairList 固定 | 已知 |
| 交易模式 | trading\_mode = futures，margin\_mode = isolated，只做多（can\_short = False） | 已知 + 假设 |
| 周期 | 主时间框架 1 分钟（用于实现 A）；15 分钟与日线作为信息周期（@informative("1d")） | 已知 |
| 资金 | 10,000 USD（或等值 USDT / USDC） | 已知 |
| 单笔风险 | 0.5% 权益 | 已知 |
| 杠杆 | 上限 30 倍；单仓杠杆按主手册第 7 节公式，约 7–9 倍 | 已知 + 计算规则 |
| 最大回撤 | 不限制；只保留按日、按周的系统故障保护 | 已知 |
| 交易所 | Binance USDⓈ-M 永续合约（USDT 保证金） | 已知 |
| 地区合规 | 你确认可以使用 Binance，没有地区限制 | 已知 |
| 自动化 | freqtrade 全自动；你保留资金、密钥、上线与恢复的决定权 | 已知 + 本方案要求 |
| 运行环境 | Docker 或 Python 3.12+ 环境；可由 OpenClaw 协助值班（见第 11 节） | 假设 |

**硬性边界：** 我不连接你的交易所账户，不索取 API 密钥或钱包私钥，不替你启动实盘。freqtrade 配置里的 dry\_run 默认保持 true。

## 2. 流程总览

所有规则都写在一个 S-03 策略文件里。回测、dry-run 和实盘运行的是同一个文件，只是配置不同。主手册的 8 阶段闭环保持不变，只是每个阶段改由 freqtrade 的组件完成。

&#91;embedded content: freqtrade 版架构 · 4 层\]

蓝色的策略文件是唯一需要自己写的部分。数据、下单、保护机制、监控都由 freqtrade 提供。上线和恢复只由你决定。

## 3. 交易所与数据

**已选定：Binance USDⓈ-M 永续合约。** 在 freqtrade 里，Binance 期货支持逐仓和全仓，交易所端止损可以用市价单。交易对写法为 `BTC/USDT:USDT`、`ETH/USDT:USDT`、`SOL/USDT:USDT`、`BNB/USDT:USDT`。下表保留作对照。

freqtrade 通过 CCXT 连接交易所。下表是官方文档列出支持期货的 7 家交易所（[交易所文档](https://www.freqtrade.io/en/stable/exchanges/)）。对 S-03 来说，最关键的列是“交易所端止损”：只支持限价止损的交易所，在快速下跌时可能不成交，官方文档也建议优先使用市价止损。

| 交易所 | 保证金模式 | 交易所端止损 | 备注 |
| --- | --- | --- | --- |
| Binance | 逐仓、全仓 | 市价、限价 | 本环境访问其公开接口曾被地区限制 |
| OKX | 逐仓 | 市价、限价 | 主手册已用其公开行情交叉核对 |
| Bybit | 逐仓 | 市价、限价 | — |
| Bitget | 逐仓 | 市价、限价 | — |
| Kraken | 逐仓 | 市价、限价 | — |
| Gate.io | 逐仓 | 仅限价 | — |
| Hyperliquid | 逐仓、全仓 | 仅限价 | 历史 K 线只有最近 5,000 根；不支持原生市价单，由限价单加 5% 滑点上限模拟 |

**结论：** 按你的决定用 Binance USDⓈ-M 永续合约。它支持交易所端市价止损，跳空时比 Hyperliquid 的限价止损更可靠。上表保留作为对比。

**交易对写法（freqtrade 期货格式）：** Hyperliquid 用 `BTC/USDC:USDC`，OKX / Bybit / Binance 用 `BTC/USDT:USDT`。上线前用 `freqtrade test-pairlist` 确认 4 个交易对都在列表里。

**数据下载：**

```bash
# 日线要比回测起点再早约 230 天（180 日涨幅 + 均线预热）
freqtrade download-data -c user_data/config.json --timeframes 1d --timerange 20200101-
freqtrade download-data -c user_data/config.json --timeframes 1m 15m --timerange 20210101-
```

- 1 分钟线是主时间框架，也就是实现 A 的触发周期，因此回测时不再需要 `--timeframe-detail`。
- 1 分钟数据量大（2021 年至今每个交易对约 300 万根），下载和回测都较慢，建议按年分段跑。
- 资金费率：freqtrade 会同时下载交易所的历史资金费率；有缺口时用 `futures_funding_rate` 填充，官方建议填 0。本流程在 G5 中按基准费率的 2 倍做压力测试。
- 数据质量：沿用主手册第 3 节的检查清单。freqtrade 不会自动做跨交易所核对，可用 OKX 公开行情做参考价。
- 本次测试环境访问 Binance 公开接口被地区限制，第 6 节的验证改用 OKX 同名永续合约的数据。

## 4. 市场状态与机会筛选

S-03 的第 1–2 步（R1–R6 筛选和支撑区）在 freqtrade 里写成一个日线信息周期的指标函数，结果合并进 15 分钟主数据。规则和阈值与主手册第 4 节完全相同（均为示例值），这里只写在 freqtrade 中怎么放。

| 规则 | freqtrade 中的位置 | 注意 |
| --- | --- | --- |
| R1 强势（30/90/180 日涨幅任一 ≥ 30%） | `@informative("1d")` 修饰的指标函数 | startup\_candle\_count 至少覆盖 181 根日线；实盘可加载的 K 线数受交易所限制 |
| R2 流动性（日均成交额 ≥ 5,000 万 USD） | 同上，用日线成交量 × 收盘价 | 不用 VolumePairList：它不支持回测，并会改变交易对列表 |
| R3 市场背景（BTC 在上行 21 日 EMA 之上，或 ≥ 2 个标的同时强势） | `@informative("1d", "BTC/{stake}:{stake}")` 引入 BTC 日线 | “≥ 2 个标的”要读取其他交易对，可能在 lookahead-analysis 中误报，需人工确认 |
| R4–R6 低点抬高、波动收缩、站上均线 | 同一个日线指标函数 | 摆动点需要右侧 3 根确认，只能用已确认的摆动点 |
| 支撑区（EMA、前高前低、AVWAP 聚合） | 同上，输出 zone\_lo / zone\_hi / retests 三列 | 可直接复用原生版代码中的纯函数（signals/s03.py） |

**防止前视偏差的两个要点：**

- 官方文档提醒“普通合并会隐含前视偏差”。日线数据的时间戳是开盘时间，必须延后一根再合并，当天的日线在收盘前不可见。@informative 修饰器已处理这一点，自己写合并时要照做（[策略自定义文档](https://www.freqtrade.io/en/stable/strategy-customization/)）。
- 摆动低点和 AVWAP 锚点都不能用整段数据的未来最高点，要用“截至当天”的滚动窗口计算。写完后必须跑 lookahead-analysis（第 6 节）。

**状态与禁入：** 数据异常、资金费率过高、宏观事件窗口写在 `confirm_trade_entry` 里，任一条件触发就放弃这次入场。这个回调在下单前的最后一刻运行。

## 5. 策略规格：S-03 在 freqtrade 中的写法

freqtrade 只支持限价单和市价单入场，没有“突破买入”的挂单类型（[回调文档](https://www.freqtrade.io/en/stable/strategy-callbacks/)仅支持修改限价单价格；回测代码也把限价限制在 K 线高点以内）。为了保持**实现 A**，策略把主时间框架设为 **1 分钟**：15 分钟状态机算出触发价，1 分钟最高价首次触及触发价时发出入场信号，下一分钟开盘以限价买入。下表把 S-03 的每条规则对应到策略文件 `S03Strategy.py` 里的方法和配置，参数均为主手册中的示例值。

| S-03 规则 | freqtrade 方法 / 配置 | 写法要点 |
| --- | --- | --- |
| 日线背景 R1–R6、支撑区 | `@informative("1d")` → `daily_s03_columns` | 见第 4 节；与原生版逐日比对 0 处差异 |
| R3 大盘条件 | `@informative("1d", "BTC/{stake}:{stake}")` | 简化为“BTC 收盘在上行的 21 日 EMA 之上”（示例值），未实现“或 ≥ 2 个标的强势” |
| 布防与回升确认 | `populate_indicators` → `run_15m_state_machine` | 与原生版相同的状态机，逐根已收盘 15 分钟 K 线运行：等待 → 布防（低点进支撑区）→ 确认（收阳），跌破支撑下沿回到等待 |
| 触发价与止损价 | 同上 | 触发价 = 最近一根已收盘 15 分钟 K 线高点；止损 = 前一根低点下方 0.1%；止损 < 0.8% 改用当日低点，> 3% 不入场 |
| 触发（实现 A） | `populate_entry_trend` 设 `enter_long = 1`，`enter_tag = "S03"` | 1 分钟最高价首次触及触发价（上一分钟还没触及），且已确认 |
| 入场价上限 | `custom_entry_price` + `order_types.entry = "limit"` | 限价 = min(下一分钟开盘价, 触发价 × 1.001)；`unfilledtimeout` 3 分钟，超时撤单 |
| 禁入检查 | `confirm_trade_entry` | 事件窗口、总在险风险 ≤ 1.5%、与已持仓 30 日相关系数 > 0.8 则放弃 |
| 仓位（0.5% 风险） | `custom_stake_amount` | 见第 7 节公式；freqtrade 的 stake 是保证金，名义价值 = stake × 杠杆 |
| 单仓杠杆 | `leverage()` | 主手册公式，≤ 30 且 ≤ 交易所上限；维持保证金率用代码里的估计值 |
| 价格止损 | `custom_stoploss` + `order_filled` | 成交时把止损价写进 trade 自定义数据，用 `stoploss_from_absolute` 换算成比例；交易所端市价止损 |
| 3R / 5R 分批 | `position_adjustment_enable = True` + `adjust_trade_position` | 返回负 stake 部分平仓：3R 卖初始仓位 1/3，5R 再卖 1/3；档位记在自定义数据里 |
| 强市保留整仓 | 同上 | 其他持仓浮盈合计 ≥ 2R 且 BTC 强势时，3R 不减仓 |
| 乖离减仓 | 同上 | 日线收盘高于 9 日 EMA 超过 2 倍 ATR，卖出剩余仓位的一半 |
| 日线跌破 9 日 EMA 离场 | `custom_exit` | 用已收盘日线判断；离场原因 "ema9\_break" |
| 最长持有 30 天 | `custom_exit` | 离场原因 "max\_hold" |
| 关掉默认离场 | `minimal_roi = {"0": 100}`；`stoploss = -0.99`（兜底）；`use_exit_signal = False` | 避免默认规则干扰 S-03 |

与原生版交易所端突破单相比，1 分钟近似的入场约晚 1 分钟，快速拉升时入场价会略差；限价上限（触发价 × 1.001）防止追高，代价是部分信号不成交。回测、dry-run、实盘用的是同一套规则，所以三者之间可以直接对比。

## 6. 回测、前视偏差检查、hyperopt、dry-run 与上线门槛

上线门槛沿用主手册的 G1–G9，阈值不变，只是每一道门改用 freqtrade 的工具来跑。顺序不能颠倒：先证明没有前视偏差，再看收益；先定参数，再用没参与调参的数据验证。

| 门槛 | freqtrade 工具 | 做法 |
| --- | --- | --- |
| G0 前视偏差 | `freqtrade lookahead-analysis` | 每次改策略都跑；has\_bias 必须为 No。它只检查实际触发过的信号，信号少时可能漏检（[官方说明](https://www.freqtrade.io/en/stable/lookahead-analysis/)） |
| G1 样本量 | `freqtrade backtesting` | 2021 年至今、4 个标的合计 ≥ 100 笔；< 30 笔直接标“证据不足” |
| G2 样本外 | `--timerange` 切分 | 最后 30% 的时间不参与调参；freqtrade 没有内置滚动验证，用多个 timerange 手动切分 |
| G3 滚动验证 | 多段 `--timerange` | 每段 90 天，至少 60% 的段期望值 > 0 |
| G4 参数稳健 | `freqtrade hyperopt` 或手工网格 | 只开放 3–4 个关键参数；总尝试组合 ≤ 50 并全部记录；最优参数附近 ±20% 的结果不能由正转负 |
| G5 成本压力 | `--fee`、futures\_funding\_rate | 手续费翻倍；资金费率取基准的 2 倍；入场延后 1 根 K 线 |
| G6 极端情景 | 回测 + 人工情景 | 取历史最大单日跌幅；交易所端止损只支持限价时，假设止损未成交、改由 liquidation\_buffer 兜底 |
| G7 代码一致性 | dry-run 与回测对照 | 同一段时间，dry-run 产生的信号与回测信号逐条一致 |
| G8 模拟运行 | `dry_run = true` 运行 ≥ 4 周 | 且 ≥ 20 笔；笔数不够就延长，不降低要求 |
| G9 小额实盘 | `dry_run = false` | 须你书面授权；资金 ≤ 1,000 USD，运行 ≥ 8 周 |

**回测参数：**

```bash
freqtrade backtesting -c user_data/config.json --strategy S03Strategy \
  --timerange 20210101-20241231 --enable-protections --export trades
freqtrade lookahead-analysis -c user_data/config.json --strategy S03Strategy --timerange 20250101-
```

- 主时间框架已经是 1 分钟，不再加 `--timeframe-detail`（freqtrade 要求它小于主时间框架）。同一分钟内止损与分批的先后顺序仍要靠假设，影响很小。
- freqtrade 回测按请求价成交、不加滑点（[回测文档](https://www.freqtrade.io/en/stable/backtesting/)）。G5 里用 `--fee` 把滑点折算进手续费。
- `--enable-protections`：只有加上这个参数，第 7 节的保护机制才会在回测中生效。
- 1 分钟数据量大：每个标的 2021 年至今约 300 万根、4 个标的合计约 1,200 万根 K 线，回测较慢，可以按年份分段跑。
- 对照：用原生版的 15 分钟回放（实现 A）跑同一段数据，比较两版的成交笔数和期望值，差异应主要来自约 1 分钟的入场延迟。

**测试环境里已做的验证（2026-10-26，freqtrade 2026.9）：** Binance 公开接口在测试环境所在地区受限，以下用 OKX 永续数据代替（交易对写法相同，策略与交易所无关）。在你本机必须用 Binance 数据重跑一遍。

| 项目 | 结果 | 证据类型 |
| --- | --- | --- |
| 策略加载与回测（2026-10-08 至 10-26） | 正常运行，0 笔交易；这段时间 4 个标的都不满足日线条件，符合预期 | 已核验事实 |
| 日线筛选与原生版逐日比对 | 4 个标的、1,852 个交易日 0 处差异；其中 48 天符合条件（约 2.6%） | 已核验事实 |
| 放宽版回测（仅测试用，放宽日线条件） | 24 笔；3R / 5R 分批、剩余仓位止损都按设计价格成交 | 已核验事实 |
| lookahead-analysis（放宽版） | 入场 0 个、指标 0 个有偏差；离场 1 个被标记，原因未确认（见第 11 节） | 已核验事实 + 未知 |
| 单元测试 | 6 个通过：截断历史与完整历史结果一致、状态机顺序、止损 > 3% 不触发、杠杆公式 | 已核验事实 |

48 天 / 1,852 天说明信号很少：按这个频率，G1 的 100 笔很可能凑不够，届时如实标“证据不足”，不要为了凑笔数放宽条件。

**hyperopt 的约束：** 信号本来就少，hyperopt 很容易在少数几笔交易上“找到”好参数。只在 G1 样本量达标后才使用，并且只在训练段上跑。最终参数必须在没参与的时间段重新过 G2–G5。

## 7. 仓位、杠杆、保护机制与停手规则

风险数字与主手册第 7 节一致。差别在口径：freqtrade 的 stake 是保证金，stoploss 是“占保证金的亏损比例”。官方示例：10 倍杠杆下 10% 的 stoploss 在价格变动 1% 时就触发（[止损文档](https://www.freqtrade.io/en/stable/stoploss/)）。所以仓位和止损都要先按价格算好，再换算成 freqtrade 的口径。

**仓位公式（custom\_stake\_amount）：**

```latex
\text{名义价值} = \frac{\text{权益} \times 0.5\%}{\text{止损距离} + \text{往返成本率}} \qquad \text{stake（保证金）} = \frac{\text{名义价值}}{\text{杠杆}}
```

例（沿用主手册的 SOL 演示数字，换成 Binance 口径）：入场 100，止损 97.2（距离 2.8%）。往返成本 0.16%（taker 约 0.05% × 2 加滑点，示例值）加 10 天资金费预留 0.3%（示例值），合计 0.46%。名义价值 = 50 / 3.26% ≈ 1,534 USDT；SOL 维持保证金率按估计值 1%，杠杆按公式为 10 倍，stake ≈ 153 USDT。换算成 freqtrade 的 stoploss 约为 −28%（2.8% × 10），由 `stoploss_from_absolute` 自动算出，不要手填。

**杠杆与强平：** `leverage()` 按主手册公式计算，让强平距离 ≥ 3 倍止损距离，并且不超过 30 倍和 Binance 该交易对的上限。止损 3% 时约 10 倍；止损越窄杠杆越高，止损 1% 以下会到 30 倍上限。维持保证金率用的是代码里的估计值（BTC 0.4%、ETH 0.5%、SOL 和 BNB 1%），上线前必须对照 Binance 的杠杆档位表核对，名义价值越大档位越高。freqtrade 的 `liquidation_buffer` 默认 0.05，会在强平价与止损之间再加一道保护止损；回测不计强平费用（[杠杆文档](https://www.freqtrade.io/en/stable/leverage/)）。保持默认值或调高。

**已知问题（已在放宽版回测中观察到）：** 止损很窄（< 0.4%）时，0.3% 的资金费预留会占掉仓位公式分母的大部分，实际止损亏损只有约 13–24 USDT，而不是 50 USDT。为了与原生版一致暂未修改；建议改成“预计持有天数 × 当前资金费率”或单独设上限，作为单变量改动回测后再决定（模型推断）。

**保护机制（Protections，[插件文档](https://www.freqtrade.io/en/stable/plugins/)）与主手册规则的对应：**

| 主手册规则 | freqtrade 实现 | 参数（示例值，按分钟写，与主时间框架无关） |
| --- | --- | --- |
| 连亏 6 笔暂停 24 小时 | StoplossGuard | lookback\_period 10,080（7 天），trade\_limit 6，stop\_duration 1,440 |
| 日亏 2% 停新仓（系统故障保护） | MaxDrawdown，calculation\_mode = equity | lookback\_period 1,440，max\_allowed\_drawdown 0.02，stop\_duration 1,440 |
| 周亏 4% 停新仓（系统故障保护） | 第二条 MaxDrawdown | lookback\_period 10,080，max\_allowed\_drawdown 0.04，stop\_duration 10,080 |
| 不设最大回撤 | 不配置更长周期的 MaxDrawdown | — |
| 平仓后不立即重新入场 | CooldownPeriod | stop\_duration 60 |
| 最多 3 笔持仓 | `max_open_trades = 3` | — |
| 总在险风险 ≤ 1.5%、相关性合并 | `confirm_trade_entry` 自写 | 读取未平仓交易的止损价计算；与已持仓 30 日相关系数 > 0.8 时放弃 |
| 事件窗口 | `confirm_trade_entry` 自写 | 在策略参数里填事件时间；资金费率与数据异常检查需另写监控 |

**密钥与账户：**

- 中心化交易所：API Key 只开“交易”权限，关闭提币，绑定服务器 IP；交易用独立子账户，只放当前阶段允许的资金。
- Binance：API Key 只勾选读取和合约交易，不开提现，限制为服务器 IP；交易用单独的子账户，账户里只放当前阶段允许使用的资金。
- 密钥放在单独的 config-private.json 或环境变量中，不进代码仓库、不进任何对话。

## 8. 执行与异常处理

freqtrade 把交易记录存在本地 SQLite 数据库里，重启后会接着管理未平仓的交易。止损要依赖交易所端的止损单，不能依赖机器人在线。

**下单设置（示例值）：**

| 配置项 | 取值 | 说明 |
| --- | --- | --- |
| trading\_mode / margin\_mode | futures / isolated | Binance USDⓈ-M 永续，逐仓 |
| order\_types.entry | limit | 价格由 custom\_entry\_price 给出：min(下一分钟开盘价, 触发价 × 1.001) |
| order\_types.exit | market | 日线跌破 9 日 EMA、分批或到期时保证离场 |
| order\_types.stoploss | market | Binance 期货支持市价止损，跳空时比限价止损更可靠 |
| order\_types.stoploss\_on\_exchange | true | 入场成交后在交易所挂止损单；被手动撤掉会自动重挂 |
| stoploss\_on\_exchange\_interval | 60 秒 | 止损单的检查与更新间隔；调低可能触发交易所限流 |
| unfilledtimeout.entry | 3 分钟 | 未成交就撤单，不追价 |
| dry\_run | true（过 G8 前不改） | 改为 false 属于 G9，由你亲自操作 |

**异常处理：**

| 异常 | freqtrade 的行为 | 你要做的补充 |
| --- | --- | --- |
| 入场成交但止损单尚未挂出 | 下一次处理循环挂出止损 | 在 dry-run 之后、小额实盘的第一周，逐笔检查从成交到止损出现的时间差 |
| 止损单被撤或丢失 | 按 stoploss\_on\_exchange\_interval 重新挂出 | 日报统计重挂次数 |
| 机器人崩溃或断网 | 重启后从数据库恢复；交易所端止损一直有效 | Docker 重启策略设为 always；另设外部健康检查，调用 REST API 的 /ping |
| 交易所维护、接口报错 | 重试并记录日志 | 连续报错时用 Telegram 或 REST API 的 /stopentry 暂停新开仓 |
| 需要紧急全部平仓 | — | /stopentry 后 /forceexit all，再 /stop；这就是本版的“熔断”操作 |
| 价格跳空穿过止损价 | 市价止损会成交，但成交价可能远差于止损价 | 在 G6 中按历史最大跳空计算单笔损失；liquidation\_buffer 兜底 |
| 非机器人发起的仓位变化 | 可能与数据库不一致 | 交易账户只给机器人用，不要手动在同一账户下单 |

**监控入口：** FreqUI 网页、Telegram 机器人、REST API。REST API 必须设密码、只监听本机或内网，不要暴露到公网。

## 9. 复盘与 FreqAI 机器学习优化

复盘结构与主手册第 9 节相同（日报、周报、月报，先归因再改动，每次只改一个变量），数据改从 freqtrade 取。

| 复盘内容 | freqtrade 来源 |
| --- | --- |
| 每日盈亏、未平仓交易 | Telegram /daily、/status；FreqUI |
| 按入场标记、离场原因分组的结果 | 导出回测或 dry-run 交易后，用 backtesting-analysis 分组统计 |
| 保护机制触发记录 | 机器人日志与 Telegram 通知 |
| 实际成交价 vs 信号收盘价（滑点） | 数据库中的订单记录，对照策略日志 |
| 与回测分布对比 | 用导出的交易做 Monte Carlo 重抽样，看实际结果是否落在 5%–95% 区间 |

**FreqAI 在本版可以直接用。** 它是 freqtrade 的内置模块，在策略里训练、预测，并按固定频率重新训练（[FreqAI 文档](https://www.freqtrade.io/en/stable/freqai/)）。但主手册的边界不变：它只能跳过信号或把风险降一半，不能放大仓位，不能改入场、止损和分批规则。

| 设计点 | FreqAI 做法（参数名见 [参数表](https://www.freqtrade.io/en/stable/freqai-parameter-table/)） | 取值（示例值） |
| --- | --- | --- |
| 标签只打在候选信号上 | 在设定标签的函数里，只给 S-03 触发的 K 线赋标签（未来先碰到 +3R 还是 −1R），其余行留空 | — |
| 训练窗口 | train\_period\_days | 365 |
| 重训频率 | backtest\_period\_days（回测）/ live\_retrain\_hours（实盘） | 90 天 / 168 小时 |
| 模型过期 | expiration\_hours | 1,080 小时（45 天） |
| 特征 | include\_timeframes、include\_corr\_pairlist、indicator\_periods\_candles | \["15m", "1d"\]；BTC 作相关标的 |
| 离群数据 | use\_SVM\_to\_remove\_outliers 或 DI\_threshold | 开启；当 do\_predict 不等于 1 时不用模型判断 |
| 时间顺序 | shuffle | false |
| 使用方式 | populate\_entry\_trend 加过滤条件；或在 custom\_stake\_amount 中按预测把风险乘 0.5 | 只减不增 |

**三个必须知道的限制：**

1. **样本仍然不够。** 主手册试跑时，4 个标的在 5 个月内只有 3 个候选事件。换成 FreqAI 不会多出样本，验收下限仍是 300 个事件，达不到就不启用。
2. **标签会跨入下一个测试段。** FreqAI 的标签天然要看未来的 K 线。训练窗口最后一段的标签会用到测试段的价格，而本文查阅的文档没有提到自动清除这段数据。做法：标签最长持有期要远小于重训间隔；或在设定标签时丢弃训练窗口最后 30 天的标签（需自行实现并验证）。
3. **前视偏差检查会报警。** lookahead-analysis 会把 FreqAI 的目标列标记为有偏差，官方说这是误报、可以忽略；但其他特征列仍必须是 No。

**验收与上线：** 沿用主手册第 14 节的 5 条门槛（改进幅度、采纳率、与随机过滤比较的 p 值等），借鉴 Vibe-Trading 的 walk-forward 与 Monte Carlo 做法；需要用导出的回测交易自己计算。FreqAI 的开关（freqai.enabled）只由你改，改完后重新过 G2–G8。

## 10. 30 天启动计划

30 天的终点是“dry-run 在跑、与回测信号一致”，不是实盘。任何一道门不过，后续阶段整体顺延。

&#91;embedded content: 30 天启动计划 · 4 个阶段，3 道门\]

第 3 周的对比是本版的关键分叉点：同一段数据上，freqtrade 的 1 分钟近似实现 A 如果比原生版实现 A 的期望值差 0.1R 以上（示例值），就先查明差异来自哪里（入场延迟、限价未成交、数据源），再决定是否继续用 freqtrade。

## 11. 尚未解决的问题、脆弱假设与 OpenClaw 注意事项

本版最脆弱的假设是“用 1 分钟主框架近似的实现 A，效果接近交易所端的突破单”。入场晚约 1 分钟、限价上限可能导致不成交，都会改变结果。其余问题大多与主手册第 11 节相同，下表只列 freqtrade 版特有或有变化的。

| 问题 / 假设 | 证据标签 | 若不成立的后果 | 推荐做法 |
| --- | --- | --- | --- |
| 1 分钟近似的实现 A 不明显劣于交易所端突破单 | 模型推断 | freqtrade 版结果偏差 | 同一段数据分别跑原生版回放和 freqtrade 回测；期望值差超过 0.1R 就先查原因 |
| 状态机与原生版一致 | 已核验（单元测试 + 日线逐日比对 0 处差异） | 两版信号不可比 | 15 分钟信号时点用 Binance 数据再逐条比对一次 |
| lookahead-analysis 标记了 1 个离场 | 未知 | 可能有未发现的前视偏差 | 疑似分段回测截断数据导致强制离场；在你本机用更长的 Binance 数据重跑，确认前不过 G0 |
| 10 天资金费预留不会扭曲仓位 | 已核验为会扭曲 | 窄止损时实际风险只有约 0.13%–0.24% 权益 | 见第 7 节；作为单变量改动回测 |
| R3 简化为“BTC 强势” | 已核验为与原生版不同 | 少数日子的筛选结果不同 | 需要完全一致时，加入“或 ≥ 2 个标的强势”的跨交易对判断 |
| 维持保证金率估计值足够保守 | 未知 | 强平价比预计更靠近止损 | 上线前对照 Binance 杠杆档位表核对；在 dry-run 里核对 freqtrade 显示的强平价 |
| 分批平仓后，交易所端止损单的数量会同步缩小 | 未知 | 止损数量与剩余仓位不符 | dry-run 验证不了；在 G9 第一周用最小仓位人工核对 |
| 成交到止损挂出的空窗足够短 | 未知 | 空窗内的价格跳动没有保护 | G9 逐笔记录时间差；超过 30 秒就暂停并排查（示例值） |
| 测试环境未用 Binance 数据 | 已核验事实 | Binance 与 OKX 的价格、交易对细节可能不同 | 在你本机用 Binance 数据重跑第 6 节的全部验证 |
| hyperopt 找到的参数是真改进 | 模型推断 | 过拟合 | 样本不足时不用 hyperopt；用了就记录全部尝试并过 G2–G5 |
| FreqAI 标签不会泄漏到测试段 | 未知 | 回测中的模型效果虚高 | 见第 9 节限制 2 |
| freqtrade 升级后行为不变 | 未知 | 回测与实盘口径悄悄改变 | 锁定版本号（Docker 镜像标签）；升级后重跑 G0–G5 |
| 4 个标的信号稀少 | 已核验为偏少（1,852 天中 48 天符合日线条件） | G1、G8 长期达不到 | 与主手册相同：延长验证期，不降低门槛 |

**用 OpenClaw 运行 freqtrade 的注意事项：** 通用原则与主手册第 15 节相同：OpenClaw 只当值班助手，不碰密钥，不装 ClawHub 技能，版本不低于 2026.2.25。freqtrade 版还要注意：

1. **不要把 freqtrade 的 REST API 密码直接给 OpenClaw。** 这个 API 可以强制开仓、平仓和重载配置。做法是中间放一个小的代理脚本，只放行这几个接口：查看状态、查看盈亏、日报、暂停新开仓（stopentry）。强制开仓、重载配置、重新启动一律不放行。
2. **不要把 OpenClaw 拉进 freqtrade 的 Telegram 控制群。** 在那个群里发消息就能下达交易命令，只用于你本人。
3. **config-private.json（密钥）不挂载进 OpenClaw 的容器。** OpenClaw 只读日志和导出的交易文件。
4. **策略文件、主配置、FreqAI 开关不允许 OpenClaw 修改。** 它可以起草改动建议，由你审阅后亲自提交。
5. **重启交给 systemd，启动与故障处理交给 s03-ops。** freqtrade 自己负责循环，systemd 负责崩溃后自动重启；连续失败才唤醒 s03-ops（第 13 节）。大模型服务中断不应影响交易。

我不会替你部署 freqtrade 或 OpenClaw，也不会接触任何交易密钥。

## 12. 行情监控与告警：程序盯盘，OpenClaw 只在告警时分析

行情轮询和阈值判断由程序完成，不消耗 LLM token；只有触发告警时才调用 OpenClaw。S-03 的信号仍由 freqtrade 按 1 分钟主框架和 15 分钟状态机产生，监控进程不参与下单。告警核心 `monitor/alerts_core.py` 与 Hyperliquid 版 `s03/alerts/core.py` 是同一份代码，规则、冷却、限流完全一致（见主手册第 16 节）。

**分工：**

| 层 | 由谁做 | 内容 | 用 LLM |
| --- | --- | --- | --- |
| 1. 策略信号与下单 | freqtrade（`S03Strategy.py`） | 1 分钟触发、15 分钟状态机、风控回调、交易所端止损、Protections | 否 |
| 2. 实时监控 | 监控进程（`monitor/run_monitor.py alerts`） | Binance WebSocket、freqtrade 只读状态、OKX 参考价 | 否 |
| 3. 告警判断 | 同上 | 7 条规则、冷却、去重、每小时上限 | 否 |
| 4. 告警分析 | OpenClaw（官方 webhook `/hooks/agent`） | 只在告警时；只读、只建议 | 是 |
| 5. 自动动作 | OpenClaw 经白名单代理（`run_monitor.py gate`） | 只能调用 freqtrade `/stopentry` | — |
| 6. 决定 | 你 | 恢复（`/start`）、`/forceexit`、上线、改参数 | — |

**数据来源（全部只读，不需要交易所密钥）：**

| 数据 | 接口 | 用途 | 标签 |
| --- | --- | --- | --- |
| 1 分钟、15 分钟 K 线 | WebSocket `<symbol>@kline_1m`、`@kline_15m`，只用 `x = true` 的已收盘 K 线 | shock\_1m、range\_15m | 已核验（官方文档） |
| 标记价与资金费率 | WebSocket `<symbol>@markPrice@1s`（含费率 r 与下次结算时间 T）；T 前进时记录结算前最后一个费率 | funding\_extreme、最新价 | 已核验 + 模型推断（结算口径） |
| 启动预热 | REST `/fapi/v1/klines`（1,500 根 1 分钟、20 根 15 分钟）、`/fapi/v1/fundingRate`（90 期 ≈ 30 天） | 标准差、ATR、资金费分位 | 已核验 |
| 参考价 | OKX 公开 ticker，每 60 秒 | ref\_deviation | 已核验 |
| 持仓与止损 | freqtrade `GET /api/v1/status`，每 10 秒：`open_rate`、`initial_stop_loss_abs`、`stop_loss_abs`、orders 里 `ft_order_side = stoploss` 且未成交的单 | near\_stop、stop\_missing | 已核验（freqtrade 2026.9 源码字段） |

Binance 已把 USDⓈ-M 的 WebSocket 拆成 `/public`、`/market`、`/private` 三个路径，K 线和标记价属于 `/market`，代码默认 `wss://fstream.binance.com/market/stream`。官方页面上旧地址的停用日期前后不一致，以 Binance 公告为准（[WebSocket 变更通知](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Important-WebSocket-Change-Notice)）。

**告警规则**与主手册第 16 节相同（阈值均为示例值），只有一处不同：Binance 每 8 小时结算一次资金费，资金费告警的绝对下限设为 0.03%/期（`monitor/monitor.example.toml`）。限流同样是同一标的同一规则 15 分钟冷却、每小时最多转 6 条给 OpenClaw（critical 不占额度）。告警写入 `monitor/var/alerts.jsonl`。

**OpenClaw 接口与权限：**

- 发送：`POST http://127.0.0.1:18789/hooks/agent`（以你的 gateway 为准），`Authorization: Bearer <OPENCLAW_HOOKS_TOKEN>`，`Idempotency-Key` = 告警 id，agentId = s03-analyst，sessionMode = isolated。200 只表示已受理。后台队列发送，不影响 freqtrade（[OpenClaw Webhooks 文档](https://docs.openclaw.ai/automation/cron-jobs/webhooks)）。
- 消息只有行情与告警数据：不含余额、交易所 API Key、freqtrade 账号。
- 白名单代理只监听 127.0.0.1:8765，只有 `GET /status`（运行状态、dry\_run、持仓的交易对 / 开仓价 / 止损价 / 盈亏比例 / 交易所端止损是否在场、最近 20 条告警）和 `POST /pause`（转发为 freqtrade 的 `/stopentry`，只停新开仓）。这就是第 11 节第 1 条说的“代理脚本”。
- freqtrade API 账号（`FT_API_USER` / `FT_API_PASS`）只给监控进程和代理；OpenClaw 只拿到代理 token（`OPENCLAW_GATE_TOKEN`）。恢复交易（`/start`）只能由你在 FreqUI 或 Telegram 操作。

```bash
cp monitor/monitor.example.toml monitor/monitor.toml
export FT_API_USER=... FT_API_PASS=...       # 在你的服务器上设置，不进文件与对话
python monitor/run_monitor.py alerts          # 监控进程
python monitor/run_monitor.py gate            # OpenClaw 白名单代理
```

**验证状态：**

| 项目 | 结果 | 标签 |
| --- | --- | --- |
| 监控单元测试 | 6 个通过：只用已收盘 K 线、异动告警写入日志、资金费结算口径、freqtrade 持仓转换、配置加载（加上策略测试共 12 个） | 已核验事实 |
| 告警核心 | 与 Hyperliquid 版同一份代码，那边 13 个测试覆盖规则、限流、OpenClaw 请求与代理 | 已核验事实 |
| 连接 Binance | 未测：测试环境连不上 Binance，WebSocket 地址和 REST 预热要在你本机确认 | 未知 |
| dry-run 下的止损单 | 未知：freqtrade 是否在 `/status` 的 orders 里显示模拟止损单。若不显示，dry-run 期间把 `require_exchange_stop` 设为 false，否则会误报 stop\_missing | 未知 |
| 实际调用 OpenClaw | 未测；开启前用你的 gateway 做一次冒烟测试 | 未知 |

## 13. 由 OpenClaw agent 运行整套流程

所有程序都由 OpenClaw agent 启动和运营，原则与主手册第 17 节相同：**agent 负责操作，freqtrade 与监控程序负责交易决策和监控**。freqtrade 是常驻服务，自己按 1 分钟主框架运行；agent 只把它启动起来，不参与信号和下单。

**4 个 agent 与你的分工：**

| 角色 | 形态 | 由谁唤醒 | 能做的 | 不能做的 |
| --- | --- | --- | --- | --- |
| s03-ops 运维 | 单个 agent | 每日定时器；服务进入 failed | `sudo systemctl start/stop/restart` s03-freqtrade、s03-ft-monitor、s03-ft-gate；运行 s03-ft-export；看最近 200 行日志；`s03-gate status` | 改配置或策略、把 dry\_run 改为 false、读密钥、调用 freqtrade REST API、`/start`、`/forceexit`、reset-failed |
| s03-analyst 告警分析 | 单个 agent | 监控进程的告警（第 12 节） | `s03-gate status`、`s03-gate pause <原因>`（转发为 freqtrade 的 `/stopentry`） | 其他所有命令 |
| s03-research 研究 | 主 agent + 子 agent | 你手动发起 | 沙箱里的研究副本中运行 `freqtrade download-data / backtesting / lookahead-analysis / hyperopt / backtesting-analysis`（必须带 `-c user_data/config.json`）与测试；按标的或时间段派生子 agent | `freqtrade trade`；碰实盘目录和密钥 |
| s03-review 复盘 | 主 agent + 子 agent | 每周定时器 | 读导出的交易记录、盈亏、告警副本；写周报草稿 | 执行任何命令 |
| 你 | — | — | dry\_run 改为 false（G9）、恢复（`/start`）、强制平仓、改参数、批准 FreqAI 模型、管理 API Key、首次启动 | — |

**与主手册版本的差异：**

| 项目 | freqtrade 版做法 |
| --- | --- |
| 密钥注入 | Binance API Key 和 freqtrade API 账号用 `FREQTRADE__EXCHANGE__KEY`、`FREQTRADE__API_SERVER__PASSWORD` 等环境变量，放在 `/etc/s03/ft-trader.env`（root 0600）。已在 freqtrade 2026.9 上用 `show-config` 验证可以读取 |
| dry\_run 开关 | 在 `/etc/s03/ft-config.json`（root 所有），agent 能读不能改 |
| REST API | 只监听 127.0.0.1:8080（`/etc/s03/ft-api.json`）；账号只给监控、代理和导出任务，不给任何 agent |
| 复盘数据 | `s03-ft-export` 每天用 GET 接口导出交易记录、盈亏、每日统计、持仓、锁定，再复制告警日志，放到 `/srv/s03-review/inbox` |
| 暂停与恢复 | 暂停 = freqtrade `/stopentry`（不开新仓，持仓与交易所端止损照常）；恢复 = `/start`，只在 FreqUI 或 Telegram 由你执行 |

权限分层（系统用户、密钥文件、root 所有的配置、sudoers、沙箱、OpenClaw 工具与命令白名单）、唤醒方式、剩余风险与主手册第 17 节相同。

**代码包里的部署文件（`deploy/`）：** `systemd/`（3 个常驻服务、导出任务、故障唤醒、每日与每周定时唤醒）、`sudoers/s03-openclaw`、`env/*.env.example`、`ft-api.json`、`openclaw/openclaw.s03.json5`（4 个 agent 的工作区、沙箱、工具、子 agent 设置与 hooks）、`openclaw/exec-approvals.s03.json`（命令白名单）、`openclaw/roles/*.md`（各 agent 的职责与流程）、`bin/`（s03-gate、s03-hook、s03-ft-export）、`README.md`（安装步骤与 7 项上线前演练）。

**验证状态：**

| 项目 | 结果 | 标签 |
| --- | --- | --- |
| 部署检查测试 | 20 个通过（全包共 32 个）：服务以 s03trader 运行、密钥只给 freqtrade、sudoers 与命令白名单一致、研究 agent 不能运行 `freqtrade trade` 或拼接命令 | 已核验事实 |
| sudoers 语法、systemd 文件 | `visudo -c` 通过；`systemd-analyze verify` 只报可执行文件尚未安装 | 已核验事实 |
| freqtrade 从环境变量读取密钥 | `show-config` 通过（用假值） | 已核验事实 |
| OpenClaw 配置细节、真实服务器部署 | 未测；子 agent allowAgents 格式、嵌套层级、沙箱镜像需装 freqtrade | 未知 |

## 14. v1.0 改进：善用子 agent 与现成程序

与主手册第 18 节的 5 项改进相同，这里只列 freqtrade 版的实现方式。

| # | 改进 | freqtrade 版做法 | 验证 |
| --- | --- | --- | --- |
| 1 | 研究矩阵 + 复核 + G0 | `research/matrix.py`：按“交易对 × 年份 × 方案（base / stop\_floor / cost2x）”生成任务，每格调用 `freqtrade backtesting`；`verify` 重跑比对逐笔哈希；`g0` 同时跑 `lookahead-analysis` 和 `recursive-analysis`（检查 400 根启动 K 线是否足够） | 在 SOL 2026 年 10 月（OKX 数据）跑通，复核哈希一致（已核验） |
| 2 | 随机性质测试 + 规格审查 | `tests/test_properties.py`：随机 K 线下状态机给出的止损距离始终在 0.8%–3%；强平距离 ≥ 3 倍止损；仓位风险不超预算且止损用掉 ≥ 70% | 全部通过（已核验） |
| 3 | 复盘指标由程序算 | `s03-ft-export` 每天调用 `monitor/review_metrics.py`，用入场成交量计算逐笔 R（分批平仓后 trade.amount 会变小），输出 metrics.json 与 metrics.md | 单元测试通过 |
| 4 | 事件日历官方来源 | `monitor/events_calendar.py` 与主手册版是同一份代码；策略读取环境变量 `S03_EVENTS_FILE`，文件过旧就拒绝入场；回测时设 `S03_EVENTS_REQUIRED=0` 并指向 `--history` 生成的历史文件 | 离线测试与实时抓取均通过 |
| 5 | 看门狗、心跳、事件推送 | freqtrade 开 `internals.sd_notify`，服务改 `Type=notify` + `WatchdogSec=120`；外部心跳同时检查 freqtrade `/api/v1/ping`；freqtrade 自带 webhook 把成交和状态推给监控进程（127.0.0.1:8766），止损与强平类事件进入告警，普通成交只记日志。freqtrade 的 webhook 不支持自定义认证头，所以不直接发给 OpenClaw | webhook 接收与分级单元测试通过 |

**策略修复：** `stop_min_mode` 默认 skip（当日最低点仍不到 0.8% 就不入场），可用环境变量 `S03_STOP_MIN_MODE=floor` 切换；资金费预留不超过止损距离的 20%。放宽版测试（OKX 2026 年 10 月数据，SOL 与 BTC）：8 笔止损合计亏损 312.9 USDT，平均约 39 USDT（预算 50）；修复前同类测试为 13–24 USDT（已核验事实）。第 7 节中“资金费预留”的已知问题已由此修复。

**测试：** 全包 45 个测试通过（含随机性质测试、事件日历、复盘指标、webhook、部署检查）。

## 15. 可直接交给 OpenClaw 执行的库

freq-trader 仓库现在是一套可部署的库：agent 只通过一个命令 `s03ft` 操作；交易决策仍只由 freqtrade + S03Strategy 完成；上线、恢复、改参数仍只由你操作。详细步骤见仓库根目录的 `OPENCLAW.md`。

**统一命令 `s03ft`（输出 JSON，参数固定）：**

| 命令 | 作用 | 谁用 |
| --- | --- | --- |
| `s03ft daily` | 更新事件日历 → 导出复盘数据 → 一次返回服务、代理、未来事件、改期、指标、24 小时告警和 problems | s03-ops |
| `s03ft status` / `doctor` / `metrics` | 状态、环境自检、复盘指标 | s03-ops |
| `s03ft svc start\|stop\|restart freqtrade\|monitor\|gate` | 启停服务 | s03-ops |
| `s03ft job events\|export`、`s03ft logs …` | 一次性任务、最近 200 行日志 | s03-ops |
| `s03ft gate status` / `gate pause <原因>` | 查看状态 / 暂停新开仓 | s03-analyst（pause）、s03-ops（status） |
| `s03ft research plan\|run\|verify\|aggregate\|g0`、`events build`、`test` | 研究矩阵、事件历史、测试 | s03-research |

没有恢复交易、强制开平仓、修改配置、切换 dry\_run 的命令。`s03ft` 发出的提权命令与 sudoers 一一对应，由测试自动核对。

**OpenClaw 侧：** 每个 agent 一个工作区（`AGENTS.md` + `skills/<技能>/SKILL.md`），配置里用 `skills` 白名单限定每个 agent 只能看到自己的技能；命令白名单只允许 `s03ft` 的固定子命令（研究 agent 另允许 `freqtrade download-data` 等只读命令）。运维与告警 agent 连读文件工具也关闭，所需数据都由 `s03ft` 输出。

**一键部署 `sudo bash deploy/install.sh`：** 建用户与目录 → 安装 freqtrade 2026.9 与 s03ft → 生成配置和随机 token（只在文件不存在时）→ 安装服务、sudoers、小工具 → 放置 4 个工作区 → 启用定时器 → 自检。它不填交易所 API Key、不启动 freqtrade、不改 dry\_run、不改你的 OpenClaw 主配置。

**你需要做的 4 件事：** 填 Binance API Key（不开提现、限制 IP）、BLS 联系邮箱、心跳地址；合并 OpenClaw 配置并导入命令白名单；首次启动服务并运行 `s03ft doctor`；完成 10 项上线前演练。

**验证：** 全包 53 个测试通过，其中新增：命令行 6 个（拒绝白名单外参数、提权命令与 sudoers 一一对应、每日汇总）、部署脚本 1 个（装到假根目录，密钥文件权限正确、重复运行不覆盖密钥）。在真实服务器和真实 OpenClaw 上尚未测试（未知）。

## 来源

- [freqtrade GitHub](https://github.com/freqtrade/freqtrade)
- [freqtrade 交易所说明](https://www.freqtrade.io/en/stable/exchanges/)
- [杠杆与期货](https://www.freqtrade.io/en/stable/leverage/)
- [止损](https://www.freqtrade.io/en/stable/stoploss/)
- [策略回调](https://www.freqtrade.io/en/stable/strategy-callbacks/)
- [策略自定义（信息周期、startup\_candle\_count）](https://www.freqtrade.io/en/stable/strategy-customization/)
- [插件（Protections、Pairlists）](https://www.freqtrade.io/en/stable/plugins/)
- [回测](https://www.freqtrade.io/en/stable/backtesting/)
- [lookahead-analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/)
- [FreqAI](https://www.freqtrade.io/en/stable/freqai/)、[FreqAI 参数表](https://www.freqtrade.io/en/stable/freqai-parameter-table/)
- 主手册：《加密货币交易流程手册（Hyperliquid 永续 · v0.7）》第 4–7、14、15 节

以上文档均于 2026-10-26 查阅。
