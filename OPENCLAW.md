# 交给 OpenClaw 直接执行：落地指南（freq-trader 模板版 v2.2 · 策略留空）

这个仓库是一套可直接部署、由 OpenClaw agent 操作的库。原则：**agent 通过一个命令 `s03ft` 操作一切；交易决策只由 freqtrade 里你接入的策略完成；上线、恢复、改参数只由你操作。**

> 默认策略是空白模板 `MyStrategy`：整套流程可以部署和运行，但不会产生任何入场。接入策略见 `docs/STRATEGY_TEMPLATE.md` 与 README。

## 组成

| 部分 | 位置 | 作用 |
| --- | --- | --- |
| 命令行库 `s03ft` | `s03ft/`、`pyproject.toml` | agent 唯一使用的命令：自检、状态、每日例行、启停服务、日志、暂停新开仓、事件日历、研究矩阵、复盘指标、测试；输出 JSON |
| 策略 | `user_data/strategies/framework_base.py`、`MyStrategy.py` | 框架基类（风控与执行）+ 你的策略（默认空白模板） |
| 监控与代理 | `monitor/` | Binance WebSocket 告警、freqtrade webhook 接收、OpenClaw 白名单代理、官方事件日历、复盘指标 |
| 研究 | `research/matrix.py`、`fwresearch/` | 回测矩阵、复核、G0；想法登记簿、筛因子（跨币种比较）、统计检验、Vibe-Trading 白名单代理 |
| 部署 | `deploy/install.sh`、`deploy/systemd/`、`deploy/sudoers/` | 一键部署、服务、看门狗、心跳、提权白名单 |
| OpenClaw | `deploy/openclaw/` | 4 个 agent 的工作区（AGENTS.md + 技能 SKILL.md）、配置片段、命令白名单 |

## 4 个 agent

| agent | 技能 | 能执行的命令 | 被谁唤醒 |
| --- | --- | --- | --- |
| s03-ops | `s03ft-ops` | `s03ft daily / status / doctor / metrics / svc / job / logs / gate status` | 每日定时器；服务失败 |
| s03-analyst | `s03ft-alert` | `s03ft gate status`、`s03ft gate pause <原因>` | 监控告警 |
| s03-research | `s03ft-research` | `s03ft research … / lab factors|stats|matrix|idea … / events build … / test / doctor --research`、`freqtrade download-data …`；MCP 服务器 `vibe-trading`（经白名单代理，可选） | 你 |
| s03-review | `s03ft-review` | 不执行命令，只读 inbox | 每周定时器 |

## 落地步骤

```bash
# 在服务器上（Ubuntu / Debian，已按官方方式装好 OpenClaw，用户名 openclaw）
git clone https://github.com/lepockyio-ops/freq-trader-template.git freq-trader && cd freq-trader
sudo bash deploy/install.sh                     # 可重复运行；不会覆盖已有的密钥与配置；加 --with-vibe-trading 同时装研究前端（可选）
sudo nano /etc/s03/ft-trader.env                # 填 Binance API Key（不开提现、限制 IP）
sudo nano /etc/s03/events.env                   # 填 BLS 要求的联系邮箱
sudo nano /etc/s03/heartbeat.env                # 填另一台机器上的心跳地址
# OpenClaw 2026.10.8 需要 Node ≥ 24.22.1 或 ≥ 26.12.1
openclaw config patch --file /etc/s03/openclaw-templates/openclaw.s03.json5 --dry-run   # 校验（未知键会报错）
openclaw config patch --file /etc/s03/openclaw-templates/openclaw.s03.json5             # 合并；hooks.token 填 /etc/s03/openclaw-hooks.env 的值
openclaw approvals set --file /etc/s03/openclaw-templates/exec-approvals.s03.json        # 命令白名单
openclaw doctor
sudo systemctl start s03-events s03-ft-gate s03-ft-monitor s03-freqtrade
sudo -u openclaw s03ft doctor                   # 全部 ✓ 后，日常运维交给 s03-ops
```

默认 `dry_run = true`。完成 deploy/README.md 中的 10 项上线前演练、并通过 G1–G8 之前，不要改为实盘。

## 安全边界（为什么可以交给 agent）

- agent 只能运行 `s03ft` 的固定子命令；`s03ft` 发出的提权命令与 sudoers 一一对应（测试 `tests/test_cli.py` 自动核对）。
- 密钥在 root 0600 的环境文件里，由 systemd 加载；agent 读不到，也没有任何命令会输出它们。
- 没有恢复交易、强制开平仓、修改配置、切换 dry_run 的命令。
- 运维与告警 agent 连 `read` 工具也关闭；研究与复盘 agent 在沙箱里，只接触副本。
- 即使 agent 被提示注入，最坏只能停服务或暂停新开仓；交易所端止损始终有效。

## 研究前端（v2.1 起；v2.2 已实测）

- 研究 agent 用 `s03ft lab …` 登记想法、筛因子、做统计检验；批准想法（`approved`）只能由你在终端执行，`s03ft lab` 会拒绝。
- 可选接入 Vibe-Trading（`--with-vibe-trading`）：OpenClaw 启动 `/usr/local/lib/s03/s03-vt-mcp`，它经 `mcp-guard` 只放行 26 个研究工具。
  配置里另有 `toolFilter`、`codex.agents`、各 agent 的 `vibe-trading__*` 规则，以及研究 agent 的沙箱闸门 `tools.sandbox.tools.alsoAllow`。
- 验证：`s03ft lab vt-check`（全部通过时 `ok: true`）、`openclaw mcp probe vibe-trading`（26 个工具）。
  2026-10-28 的实测记录见 `docs/RESEARCH_LAB.md`。
