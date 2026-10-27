# freq-trader-framework

基于 **freqtrade 的 Binance USDⓈ-M 永续交易框架**。v2.0 将交易规则与执行、风控、监控和运维分开，供你接入自己的做多策略。

默认策略为 `MyStrategy` 空白模板，**不会产生入场信号**；默认配置 `dry_run: true`。均线示例仅用于检查框架流程，收益表现需要另行验证。仓库中的 `s03` / `s03ft` 是沿用的包名、命令和服务名。

## 文档入口

- [v2.0 中文完整手册](%E4%BA%A4%E6%98%93%E6%A1%86%E6%9E%B6%E6%A8%A1%E6%9D%BF%E6%89%8B%E5%86%8C%EF%BC%88freqtrade%20%C2%B7%20%E7%AD%96%E7%95%A5%E7%95%99%E7%A9%BA%20v2.0%EF%BC%89.md)：交易流程、数据、验证门槛、风险与运维。
- [框架说明](docs/FRAMEWORK.md)：框架与策略各自负责什么。
- [策略规格模板](docs/STRATEGY_TEMPLATE.md)：编写策略前先明确规则、参数来源与验收标准。
- [部署说明](deploy/README.md)：Linux 服务、权限、监控与密钥配置。
- [OpenClaw 操作入口](OPENCLAW.md)：部署和日常命令。

## 框架提供什么

| 模块 | 内容 |
| --- | --- |
| 策略基类 | `FrameworkStrategy`：策略接入接口、止损护栏、仓位与杠杆计算、入场检查 |
| 风险与执行 | 总在险风险、相关性检查、事件窗口、交易所端止损、分批止盈、最长持有与 Protections |
| 监控 | 行情、持仓、资金费率、参考价和数据中断告警；本机状态与暂停代理 |
| 研究 | 按交易对、年份和方案拆分回测，重跑复核、汇总与前视/递归偏差检查 |
| 事件与复盘 | 美联储、BLS、BEA 事件日历；程序计算复盘指标 |
| 运维 | `s03ft` 命令、systemd 服务、安装脚本和 OpenClaw 工作区模板 |

## 本地安装

以下命令使用 Linux / macOS 的 Bash。正式部署需要 Linux 与 systemd。

仓库的命令行包要求 Python ≥ 3.11；freqtrade 部分固定依赖 `2026.9`，完整交易环境建议按手册使用 Python 3.12+。

```bash
git clone https://github.com/lepockyio-ops/freq-trader-framework.git
cd freq-trader-framework
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[trade,test]"
python -m pytest -q
```

测试覆盖框架、模板、命令行、监控、事件和部署。涉及 freqtrade 或回测数据的测试，请按测试自身要求准备依赖和数据；测试通过也不等于策略有效。

## 接入你的策略

1. 填写[策略规格模板](docs/STRATEGY_TEMPLATE.md)，确定市场条件、信号、止损、仓位、离场和验证标准。
2. 修改 [MyStrategy.py](user_data/strategies/MyStrategy.py)，继承 [FrameworkStrategy](user_data/strategies/framework_base.py)：
   - 用 `populate_indicators()` 计算指标。
   - 用 `populate_entry_trend()` 设置 `enter_long` 和绝对止损价 `fw_stop`，可选 `fw_entry_cap` 限定计划入场价。
   - 按需实现 `take_profit_legs()`、`strategy_exit()` 或 `populate_exit_trend()`。
3. 完成规则后移除 `is_template = True` 或设为 `False`。若更换类名，同步修改 `user_data/config.json` 的 `strategy`。
4. 核对策略周期与配置：模板类使用 `15m`，当前配置文件写的是 `1m`，运行前应按策略需求统一。
5. 完成测试、数据准备、回测和偏差检查，再进入 dry-run 验证。

[均线示例](examples/strategies/SmaCrossExample.py)可用于了解接口。准备所需历史数据后，可单独回测：

```bash
freqtrade backtesting -c user_data/config.json \
  --strategy SmaCrossExample --strategy-path examples/strategies
```

## 研究与验证

在仓库根目录执行；先准备对应市场、周期和时间范围的历史数据。下面的 `--job` 是矩阵任务编号示例。

```bash
s03ft research plan --pairs BTC,ETH,SOL,BNB --years 2022-2025 --variants base,stop_floor,cost2x
s03ft research run --job SOL-2023-base
s03ft research verify --job SOL-2023-base
s03ft research aggregate
s03ft research g0 --timerange 20250101-
```

策略名默认读取 `user_data/config.json`；也可用 `FW_STRATEGY`、`FW_STRATEGY_PATH` 指定策略与路径，用 `S03_FT_CONFIG` 指定配置。

验证按手册的 G0–G9 推进，包括偏差检查、样本量、样本外与滚动验证、参数稳健性、成本压力、极端情景、代码一致性和模拟运行。实盘是独立阶段，需要人工授权。

## 监控与部署

本地监控配置从 [monitor.example.toml](monitor/monitor.example.toml) 复制，并配置 freqtrade API 账号：

```bash
cp monitor/monitor.example.toml monitor/monitor.toml
python monitor/run_monitor.py alerts
# 在另一个终端运行本机代理
python monitor/run_monitor.py gate
```

告警分析可选接入 OpenClaw，默认关闭。告警分析端经代理可查看状态、暂停新开仓；运维角色的启停权限由 systemd、sudoers 和工作区配置限定。密钥按部署文档在本机配置，勿提交到 Git。

Linux 服务器部署入口：

```bash
sudo bash deploy/install.sh
s03ft doctor --json
s03ft status --json
```

安装脚本要求已存在 OpenClaw 用户，可用 `--openclaw-user` 指定。它安装代码、服务与定时任务，生成缺失配置和 token；交易所密钥、OpenClaw 主配置合并和交易服务启动仍需按[部署说明](deploy/README.md)完成。

## 目录导航

| 路径 | 用途 |
| --- | --- |
| [user_data/strategies/](user_data/strategies/) | 框架基类与空白策略 |
| [user_data/config.json](user_data/config.json) | Binance 逐仓期货、默认策略与 dry-run 配置 |
| [examples/strategies/](examples/strategies/) | 示例策略 |
| [monitor/](monitor/) | 监控、代理、事件日历与复盘指标 |
| [research/matrix.py](research/matrix.py) | 回测矩阵与偏差检查 |
| [s03ft/](s03ft/) | 统一命令行 |
| [deploy/](deploy/) | 安装、服务、权限与 OpenClaw 模板 |
| [tests/](tests/) | 测试与离线日历样本 |

## 使用边界

- 当前模板面向做多策略。风险阈值是待校准的配置起点，不能替代策略验证。
- 止损护栏按计划入场价检查；实际成交价格会影响最终止损距离。
- 杠杆计算中的维持保证金率估计应与交易所档位核对；资金费与参考价告警不等于策略入场过滤。
- Binance 行情连接、API 返回的止损状态和交易所端订单行为仍需在目标环境验证。
- 本仓库提供研究与执行工具，不保证策略收益。
