# S-03 freqtrade 策略（Binance USDⓈ-M 永续 · 只做多 · 实现 A）

> **交给 OpenClaw 直接执行：见 [OPENCLAW.md](OPENCLAW.md)。** 一键部署 `sudo bash deploy/install.sh`，agent 通过统一命令 `s03ft` 操作。

对应《freqtrade 交易流程手册》v0.2。交易风格与原生版一致（R1–R6 日线筛选、支撑区、下探回升后突破前一根
15 分钟高点入场、0.5% 风险、3R/5R 分批、9 日 EMA 追踪）。默认 `dry_run = true`，不会下真实订单。
已在 freqtrade 2026.9 上加载、回测并做过前视偏差检查。所有阈值是示例值，不代表策略有效。

## v1.0 改进（2026-10-26）

| 改进 | 文件 |
| --- | --- |
| 事件日历改用官方来源（美联储、BLS、BEA），策略读取 `S03_EVENTS_FILE`，文件过旧即停新仓 | `monitor/events_calendar.py`、`deploy/systemd/s03-events.*` |
| 修复最小止损未生效（`stop_min_mode`）与资金费预留过度压缩仓位（`funding_cap_frac_of_stop`） | `user_data/strategies/S03Strategy.py` |
| 随机性质测试（Hypothesis） | `tests/test_properties.py` |
| 研究矩阵 + 复核 + G0（lookahead + recursive） | `research/matrix.py` |
| 复盘指标由程序计算 | `monitor/review_metrics.py`、`deploy/bin/s03-ft-export` |
| systemd 看门狗（freqtrade `internals.sd_notify`）、外部心跳、freqtrade webhook 事件推送 | `deploy/ft-api.json`、`monitor/binance_monitor.py`、`deploy/bin/s03-heartbeat` |

## 文件

| 路径 | 作用 |
| --- | --- |
| `user_data/strategies/S03Strategy.py` | 策略（单文件，不依赖 TA-Lib） |
| `user_data/config.json` | Binance 期货、逐仓、最多 3 笔、交易所端市价止损、dry_run |
| `user_data/config-private.example.json` | 密钥与 API 服务器模板：复制为 `config-private.json` 后在你本机填写 |
| `user_data/config-test-okx.json` | 我在测试环境里用的 OKX 配置（含本测试环境专用的代理与证书设置，你用不到） |
| `tests/test_s03_core.py` | 6 个测试：无未来信息、状态机、杠杆公式 |
| `monitor/` | 行情监控与告警（独立进程）：Binance WebSocket → 告警 → OpenClaw；白名单代理 |
| `deploy/` | 由 OpenClaw agent 运行整套流程：systemd 服务、sudoers 白名单、4 个 agent 的配置与职责说明、命令白名单（见 deploy/README.md） |
| `tests/test_deploy.py` | 14 个部署检查：服务权限、密钥只给需要的服务、sudoers 与命令白名单一致 |
| `tests/test_monitor.py` | 6 个测试：Binance 消息解析、资金费率结算、freqtrade 持仓转换、配置 |
| `tests/S03TestRelaxed.py` | 仅测试用：放宽日线条件以产生交易，验证回调能运行；不可用于交易 |

## 实现 A 在 freqtrade 中怎么做

freqtrade 不支持“突破买入”的止损类入场单（其回测代码注释写明：限价不得高于 K 线高点，否则就是 freqtrade 实盘不支持的止损限价入场）。
因此主时间框架用 **1 分钟**：触发价 = 最近一根已收盘 15 分钟 K 线高点；1 分钟最高价首次触及触发价 → 下一根 1 分钟开盘以限价买入，
限价上限 = 触发价 × 1.001，3 分钟未成交撤单。与交易所突破单相比约慢 1 分钟；回测、dry-run、实盘口径一致。

## 使用顺序

```bash
# 1. 安装（建议 Docker，并固定版本标签；或 pip install freqtrade==2026.9）
# 2. 下载数据：日线需要比回测起点再早约 230 天
freqtrade download-data -c user_data/config.json --timeframes 1d --timerange 20200101-
freqtrade download-data -c user_data/config.json --timeframes 1m 15m --timerange 20210101-
# 3. 测试
pytest -q tests/
# 4. 回测（G1–G5）与前视偏差检查（G0）
freqtrade backtesting -c user_data/config.json --timerange 20210101-20241231 --enable-protections --export trades
freqtrade lookahead-analysis -c user_data/config.json --timerange 20250101-
# 5. dry-run（G8：≥ 4 周且 ≥ 20 笔）
freqtrade trade -c user_data/config.json -c user_data/config-private.json
# 6. 实盘（G9）：由你本人把 dry_run 改为 false，资金 ≤ 1,000 USDT
```

## 测试环境里已验证的内容（2026-10-26，OKX 永续数据代替 Binance）

| 项目 | 结果 |
| --- | --- |
| 策略加载与回测（2026-10-08 至 10-26） | 正常运行；0 笔交易，符合当前 4 个标的都不满足日线条件 |
| 日线筛选与原生版逐日比对（4 个标的、1,852 个交易日） | 0 处差异；其中 48 个交易日符合条件（约 2.6%） |
| 放宽版回测（仅测试用） | 24 笔；3R / 5R 分批、交易所端止损、强制离场都按设计成交 |
| freqtrade lookahead-analysis（放宽版，6 个信号） | 入场 0 个有偏差、指标 0 个有偏差；离场 1 个被标记，原因未确认（见下） |
| 单元测试 | 6 个全部通过，含“截断历史与完整历史结果一致”的无未来信息测试 |

Binance 公开接口在测试环境所在地区被限制，因此没能用 Binance 数据测试；Binance 与 OKX 的交易对写法相同，策略本身与交易所无关。

## 行情监控与告警（手册第 12 节）

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

- **离场偏差标记未确认**：关掉跨交易对规则后仍有 1 个离场被标记。疑似分段回测在持仓未结束时截断数据导致强制离场；请在你本机用更长数据重跑。
- **资金费预留（v1.0 已修复）**：预留不超过止损距离的 20%；同类放宽版测试中，单笔止损亏损约 39 USDT（预算 50），修复前约 13–24 USDT。
- **R3 简化**：只用“BTC 在上行的 21 日 EMA 之上”，未实现原生版的“或 ≥ 2 个标的同时强势”。
- **维持保证金率**：杠杆公式用的是代码里的估计值，上线前用 Binance 的杠杆档位表核对，或直接调高以更保守。
- **资金费率与参考价交叉核对**未在策略里实现（主手册第 3、4 节的规则），需要另写监控。
- startup_candle_count = 400 时，实盘需 2 次请求取 1 分钟 K 线（freqtrade 会提示），属正常。

本代码是研究与流程工具，不构成投资建议，不保证收益。
