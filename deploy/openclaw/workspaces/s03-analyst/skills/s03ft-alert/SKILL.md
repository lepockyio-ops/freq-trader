---
name: s03ft-alert
description: S-03 告警分析：读取状态、按固定条件决定是否暂停新开仓、用固定格式回复。
metadata: { "openclaw": { "requires": { "bins": ["s03ft"] } } }
---

# S-03 告警分析技能

## 可用命令（只有这两条）
- `s03ft gate status`：运行状态、持仓、交易所端止损是否在场、最近告警。
- `s03ft gate pause <原因>`：暂停新开仓（转为 freqtrade 的 /stopentry；不撤止损、不平仓）。原因必须写明。

## 什么时候暂停新开仓（满足任一条，否则只给建议）
- `stop_missing`（critical）告警，且 status 显示该持仓的交易所端止损不在场。
- `ft_exit_fill_liquidation` 或 `ft_exit_fill_emergency_exit` 告警。
- 同一小时内 `feed_stale` 或 `ref_deviation` 告警涉及 ≥ 2 个标的。
- 你判断数据可能有误，继续开仓会基于错误数据。
恢复只能由 Pocky 在 FreqUI 或 Telegram 操作，你没有恢复的方法，也不要请求获得。

## 回复格式（固定 4 段）
1. 发生了什么（只依据告警数据与 status；不知道写“未知”，不要编造新闻或数字）
2. 对持仓和挂单的影响
3. 建议：无需操作 / 建议人工查看 / 已暂停新开仓（三选一，附一句理由）
4. 证据标签：每条判断标注 已核验事实 / 模型推断 / 未知
