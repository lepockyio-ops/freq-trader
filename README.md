# freq-trader：freqtrade 自动交易框架（Binance USDⓈ-M 永续 · 策略留空，可接入任意策略）

> **交给 OpenClaw 直接执行：见 [OPENCLAW.md](OPENCLAW.md)。** 一键部署 `sudo bash deploy/install.sh`，agent 通过统一命令 `s03ft` 操作。

v2.0 起，这是一个**不含交易策略的框架**：止损护栏、仓位与杠杆、事件窗口、总风险与相关性、交易所端止损、
Protections、监控告警、官方事件日历、复盘指标、研究矩阵、OpenClaw 运维与一键部署都已就绪；交易规则由你写成一个
继承 `FrameworkStrategy` 的 freqtrade 策略接入。默认策略是空白模板 `MyStrategy`：可以运行，但不会产生任何入场。
原先的 S-03 策略保留在 git 历史中（v1.x）。默认 `dry_run = true`。需要 freqtrade 2026.9。

## 接入你的策略（3 步）

1. 按 `docs/STRATEGY_TEMPLATE.md` 写好策略规格（适用市场、信号、止损、仓位、离场、验证门槛）。
2. 修改 `user_data/strategies/MyStrategy.py`：设置 `timeframe`；在 `populate_entry_trend()` 写 `enter_long = 1` 和
   `fw_stop`（绝对止损价）；按需实现 `take_profit_legs()`、`strategy_exit()`；删掉 `is_template = True`。
   改了类名就同步修改 `user_data/config.json` 的 `"strategy"`。
3. `pytest -q` → `s03ft research plan/run/aggregate`（研究矩阵）→ `s03ft research g0`（偏差检查）→ 按 G1–G9 逐级上线。

框架说明见 `docs/FRAMEWORK.md`。示例 `examples/strategies/SmaCrossExample.py` 只用于验证框架能跑通，不代表可交易：
`freqtrade backtesting -c user_data/config.json --strategy SmaCrossExample --strategy-path examples/strategies`

## 文件

| 路径 | 作用 |
| --- | --- |
| `user_data/strategies/framework_base.py` | **框架基类** `FrameworkStrategy`：止损护栏、仓位、杠杆、入场前检查、交易所端止损、分批、最长持有、Protections |
| `user_data/strategies/MyStrategy.py` | **空白策略模板**（默认） |
| `examples/strategies/SmaCrossExample.py` | 示例策略（只用于验证框架） |
| `docs/STRATEGY_TEMPLATE.md`、`docs/FRAMEWORK.md` | 策略规格填写模板、框架说明 |
| `user_data/config.json` | Binance 期货、逐仓、最多 3 笔、交易所端市价止损、dry_run、`"strategy"` |
| `user_data/config-private.example.json` | 密钥与 API 服务器模板（本地调试用） |
| `user_data/config-test-okx.json` | 测试环境用的 OKX 配置（你用不到） |
| `monitor/` | 行情监控与告警、白名单代理、官方事件日历、复盘指标 |
| `research/matrix.py` | 研究矩阵（策略名读配置或 `FW_STRATEGY`） |
| `s03ft/` | 给 OpenClaw agent 的统一命令行 |
| `deploy/` | 一键部署、systemd、sudoers、4 个 agent 的工作区与技能 |
| `tests/` | 53 个测试：框架护栏（随机性质测试）、空白模板、示例策略、freqtrade 实际回测、监控、部署、命令行 |

## 测试环境里已验证（2026-10-27，OKX 永续数据代替 Binance）

| 项目 | 结果 |
| --- | --- |
| 空白模板回测（SOL、BTC，2026-10-05 至 10-26） | 正常运行，0 笔交易 |
| 示例策略回测（同上） | 7 笔，全部为止损离场；单笔亏损约 34–37 USDT（风险预算 50）；止损按价格挂出 |
| 框架护栏随机测试 | 止损距离、强平距离、风险预算三条规则在数千组随机输入下都成立 |

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

- **护栏按计划入场价检查**：实际成交价不高于 `fw_entry_cap`，按成交价算的止损距离可能略小于下限（至多差一个限价偏移）。
- **复盘 R 的口径（v2.0 修正）**：R 用 `stop_loss_abs` 计算。v1.x 用的是 `initial_stop_loss_abs`（兜底的 −99% 止损），会把 R 算得过小；v2.0 已改正。
- **维持保证金率**：杠杆公式用的是 `framework_base.FW_DEFAULTS` 里的估计值，上线前用 Binance 的杠杆档位表核对。
- **资金费率与参考价**只在监控进程里告警，入场前不检查；需要的话在你的策略里加条件。
- **前视偏差**：每个新策略都要跑 `s03ft research g0`（lookahead-analysis + recursive-analysis）。

本代码是研究与流程工具，不构成投资建议，不保证收益。
