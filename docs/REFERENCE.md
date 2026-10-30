# freq-trader 参考手册（文件、已验证项、监控、已知问题）

快速上手见仓库根目录的 `README.md`；给 AI agent 的守则见 `AGENTS.md`；OpenClaw 落地见 `OPENCLAW.md`。

## 文件

| 路径 | 作用 |
| --- | --- |
| `user_data/strategies/framework_base.py` | **框架基类** `FrameworkStrategy`：止损护栏、仓位、杠杆、入场前检查、交易所端止损、分批、最长持有、Protections |
| `user_data/strategies/MyStrategy.py` | **空白策略模板**（默认） |
| `examples/strategies/SmaCrossExample.py` | 示例策略（只用于验证框架） |
| `docs/STRATEGY_TEMPLATE.md`、`docs/FRAMEWORK.md` | 策略规格填写模板、框架说明 |
| `user_data/config.json` | Binance 期货、逐仓、5 个交易对（含 HYPE）、最多 3 笔、交易所端市价止损、dry_run、`"strategy"` |
| `user_data/config-private.example.json` | 密钥与 API 服务器模板（本地调试用） |
| `user_data/config-test-okx.json` | OKX 配置：本地试跑、或所在地区访问不了 Binance 时下载数据 / 回测用 |
| `monitor/` | 行情监控与告警、白名单代理、官方事件日历、复盘指标 |
| `research/matrix.py` | 研究矩阵（策略名读配置或 `FW_STRATEGY`） |
| `s03ft/` | 给 OpenClaw agent 的统一命令行 |
| `deploy/` | 一键部署、systemd、sudoers、4 个 agent 的工作区与技能 |
| `fwresearch/` | 研究前端：想法登记簿、因子库与检验、统计检验、Vibe-Trading 桥接与白名单代理（与 hyper-trader 共用） |
| `tests/` | 约 145 个测试：框架护栏（随机性质测试）、空白模板、示例策略、freqtrade 实际回测、监控、部署、命令行、研究前端 |

## 测试环境里已验证（v2.7，2026-10-30，OKX 永续数据代替 Binance）

| 项目 | 结果 |
| --- | --- |
| 周期 | 回测日志不再出现 “Override strategy 'timeframe'”；入场都在 15 分钟 K 线开盘时刻 |
| 示例策略回测（SOL、BTC，2026-10-05 至 10-26） | 9 笔；成交后止损距离 0.82%–1.70%（v2.5 被配置改成 1m 时为 7 笔、0.72%–0.98%） |
| 组合回测 `research portfolio --all`（同一区间） | base、cost2x、delay1 三个方案都成功运行；base 8 笔；上线判定为“不通过”（压力方案期望 R ≤ 0），并记录了“没有历史事件文件”的数据缺口 |
| 框架保证的故障测试 | `tests/test_framework_guarantees.py`：用 freqtrade 自己的 `LocalTrade` / `Order` 对象覆盖挂单、部分成交、撤单后绑定、风险预算、日历失效、回调出错等情形 |

示例结果只说明框架能跑通，不说明示例策略有效。

## 行情监控与告警

```bash
cp monitor/monitor.example.toml monitor/monitor.toml
export FT_API_USER=... FT_API_PASS=...          # freqtrade api_server 账号，只给监控进程
python monitor/run_monitor.py alerts            # Binance WebSocket 盯盘，按阈值告警；不消耗 LLM token
python monitor/run_monitor.py gate              # OpenClaw 白名单代理（127.0.0.1:8765）：GET /status、POST /pause
```

- 告警核心 `monitor/alerts_core.py` 与 Hyperliquid 版 `s03/alerts/core.py` 是同一份代码。
- 告警只在触发时交给 OpenClaw（官方 webhook `/hooks/agent`），每小时最多 6 次（示例值），critical 不占额度。
- OpenClaw 唯一能做的动作是经代理调用 freqtrade 的 `/stopentry`（只停新开仓）。它拿不到 freqtrade API 账号和交易所密钥。
- `[openclaw] enabled` 默认为 false；打开前设置 `OPENCLAW_HOOKS_TOKEN` 和 `OPENCLAW_GATE_TOKEN`。
- 未测：测试环境连不上 Binance，WebSocket 地址（`/market/stream`）和 REST 预热需在你本机确认；
  dry-run 下 freqtrade 是否在 `/status` 的 orders 里显示模拟止损单也要确认，否则把 `require_exchange_stop` 设为 false。

## 已知问题

- **部分成交与挂单风险（v2.7 已修）**：v2.6 在入场单部分成交、仍挂着时交易所上没有任何止损；挂单风险按 0 计算（freqtrade 挂单期间 `amount = 0`）。现在部分成交就撤掉剩余部分并立即挂止损；挂单按下单数量计风险。
- **研究流程（v2.7 已修）**：上线判定只有 `research verdict` 一个入口；必需压力方案由策略声明；样本外起点须事先登记、按入场时间归属；失败的组合回测不会留下可用的旧结果；版本指纹覆盖解析后的最终配置与数据内容。见 `docs/RESEARCH_LAB.md`。
- **`enter_tag` 被框架占用**：每笔交易的标记都不同，freqtrade 回测报告、FreqUI 里按入场标记分组会失效。复盘指标（`s03ft metrics`）按 `u=` 之后的策略标记分组。
- **止损绑定（v2.6 已修）**：v2.5 及以前在成交时重新寻找“最近一次信号”，挂单期间出现新信号、成交价更优或重启后，止损可能换成别的或缺失（只剩 −99% 兜底）；按计划入场价检查护栏，成交后距离可能低于下限。现在计划随订单保存，成交后按成交价校验，不合格立即离场，见 `docs/FRAMEWORK.md`。
- **复盘 R 的口径（v2.0 修正）**：R 用 `stop_loss_abs` 计算。v1.x 用的是 `initial_stop_loss_abs`（兜底的 −99% 止损），会把 R 算得过小；v2.0 已改正。
- **维持保证金率**：杠杆公式用的是 `framework_base.FW_DEFAULTS` 里的估计值，上线前用 Binance 的杠杆档位表核对。
- **资金费率与参考价**只在监控进程里告警，入场前不检查；需要的话在你的策略里加条件。
- **前视偏差**：每个新策略都要跑 `s03ft research g0`（lookahead-analysis + recursive-analysis）。

本代码是研究与流程工具，不构成投资建议，不保证收益。
