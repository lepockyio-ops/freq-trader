"""freq-trader 框架基类：把“风控与执行”固定下来，策略只写信号。

继承 FrameworkStrategy 的策略只需要：
  1. 设置 timeframe、startup_candle_count；
  2. populate_indicators()：计算你要的指标（可用 @informative 引入其他周期）；
  3. populate_entry_trend()：在入场信号行设置 enter_long = 1，并写入列 fw_stop（绝对止损价）；
     可选写入 fw_entry_cap（限价上限，例如“突破价 × 1.001”），不写则按信号价成交；
  4. 可选：take_profit_legs()（分批止盈）、strategy_exit()（自定义离场）、populate_exit_trend()（离场信号）。

框架负责（策略不能绕过）：
  - 止损护栏：止损距离必须在 stop_min_pct–stop_max_pct；太近时按 stop_min_mode 放弃或改为下限；
  - 仓位：单笔风险 = 权益 × risk_per_trade（含往返成本与资金费预留）；名义价值 ≤ 1 倍权益；
  - 杠杆：强平距离 ≥ liq_to_stop_multiple × 止损距离，且 ≤ max_position_leverage 与交易所上限；
  - 入场前检查：事件窗口、事件文件新鲜度、总在险风险 ≤ max_open_risk、与已持仓相关性 ≤ corr_threshold；
  - 交易所端止损（按绝对价格）、最长持有天数、Protections（连亏暂停、日/周故障保护、冷却）。
所有数值是示例值，不代表适合任何策略；在子类里用 framework_params 覆盖。需要 freqtrade 2026.9。
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta

import pandas as pd
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, stoploss_from_absolute

# ============================================================== 框架参数（示例值）
FW_DEFAULTS = {
    "risk_per_trade": 0.005,
    "max_open_risk": 0.015,
    "corr_threshold": 0.8,
    "max_position_leverage": 30,
    "liq_to_stop_multiple": 3.0,
    "round_trip_cost": 2 * (0.0005 + 0.0003),       # 每边约 0.05% 手续费 + 0.03% 滑点（以你账户实际费率为准）
    "funding_est": 0.0001 * 3 * 10,                 # 每 8 小时 0.01% × 10 天
    "funding_cap_frac_of_stop": 0.2,                # 资金费预留不超过止损距离的 20%
    # Binance 维持保证金率随档位变化；这里取偏保守的估计值，上线前用交易所档位表核对
    "maint_margin_est": {"BTC": 0.004, "ETH": 0.005, "SOL": 0.01, "BNB": 0.01, "_default": 0.025},
    "stop_min_pct": 0.8,
    "stop_max_pct": 3.0,
    "stop_min_mode": os.environ.get("S03_STOP_MIN_MODE", "skip"),   # skip / floor；研究矩阵用环境变量切换
    "max_hold_days": 30,
    "event_times_utc": [],
    "event_before_min": 120,
    "event_after_min": 60,
    "event_file": os.environ.get("S03_EVENTS_FILE", ""),
    "event_file_max_age_days": 3.0,
    "event_file_required": os.environ.get("S03_EVENTS_REQUIRED", "1") == "1",   # 回测时设为 0
}


# ============================================================== 纯函数（便于测试）
def guarded_stop(entry: float, stop: float | None, fw: dict) -> float | None:
    """框架止损护栏：返回合规的止损价，或 None（放弃入场）。"""
    if stop is None or stop != stop or entry <= 0 or stop >= entry:
        return None
    dist = (entry - stop) / entry * 100
    if dist < fw["stop_min_pct"]:
        if fw["stop_min_mode"] != "floor":
            return None
        stop, dist = entry * (1 - fw["stop_min_pct"] / 100), fw["stop_min_pct"]
    if dist > fw["stop_max_pct"]:
        return None
    return stop


def stake_for(equity: float, stop_dist: float, leverage: float, fw: dict = FW_DEFAULTS) -> float:
    """保证金 = 名义价值 / 杠杆；名义价值 = 权益 × 单笔风险 / (止损距离 + 往返成本 + 资金费预留)，且 ≤ 1 倍权益。"""
    funding = min(fw["funding_est"], fw["funding_cap_frac_of_stop"] * stop_dist)
    notional = equity * fw["risk_per_trade"] / (stop_dist + fw["round_trip_cost"] + funding)
    notional = min(notional, equity)
    return notional / max(leverage, 1.0)


def isolated_leverage(stop_dist: float, max_lev: float, mm: float, fw: dict = FW_DEFAULTS) -> int:
    """逐仓杠杆 = min(上限, 交易所上限, floor(1 / (k × 止损距离 × (1 − mm) + mm)))，k = liq_to_stop_multiple。"""
    raw = 1.0 / (fw["liq_to_stop_multiple"] * stop_dist * (1 - mm) + mm)
    return int(max(1, min(fw["max_position_leverage"], max_lev, math.floor(raw))))


# ============================================================== 基类
class FrameworkStrategy(IStrategy):
    INTERFACE_VERSION = 3
    can_short = False
    process_only_new_candles = True
    minimal_roi = {"0": 100}                   # 关掉默认 ROI 离场
    stoploss = -0.99                           # 仅作兜底；实际止损按 fw_stop 的绝对价格
    use_custom_stoploss = True
    trailing_stop = False
    position_adjustment_enable = True          # 分批止盈用部分平仓
    max_entry_position_adjustment = 0          # 不加仓
    use_exit_signal = True                     # 允许策略用 populate_exit_trend 离场

    is_template = False                        # 空白模板设为 True（框架自检会提示“未配置策略”）
    framework_params: dict = {}                # 子类覆盖框架参数，例如 {"risk_per_trade": 0.003}

    @property
    def fw(self) -> dict:
        return {**FW_DEFAULTS, **(self.framework_params or {})}

    @property
    def protections(self):
        return [
            {"method": "StoplossGuard", "lookback_period": 7 * 1440, "trade_limit": 6,
             "stop_duration": 1440, "only_per_pair": False},
            {"method": "MaxDrawdown", "calculation_mode": "equity", "lookback_period": 1440,
             "trade_limit": 1, "stop_duration": 1440, "max_allowed_drawdown": 0.02},
            {"method": "MaxDrawdown", "calculation_mode": "equity", "lookback_period": 7 * 1440,
             "trade_limit": 1, "stop_duration": 7 * 1440, "max_allowed_drawdown": 0.04},
            {"method": "CooldownPeriod", "stop_duration": 60},
        ]

    # ---------------------------------------------------------- 策略可覆盖的钩子
    def take_profit_legs(self, trade: Trade) -> list[tuple[float, float, str]]:
        """分批止盈：[(R 倍数, 占初始仓位比例, 标签)]，例如 [(2.0, 0.5, "tp_2R")]。默认不分批。"""
        return []

    def strategy_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                      last_row: pd.Series | None) -> str | None:
        """自定义离场（返回离场原因字符串）。last_row 为最新一行已分析数据。默认无。"""
        return None

    # ---------------------------------------------------------- 工具
    def _signal_row(self, pair: str, current_time: datetime) -> pd.Series | None:
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty or "enter_long" not in df:
            return None
        df = df[df["date"] <= current_time]
        sig = df[df["enter_long"] == 1]
        return sig.iloc[-1] if len(sig) else None

    def _entry_and_stop(self, row: pd.Series, rate: float | None = None) -> tuple[float, float | None]:
        cap = row.get("fw_entry_cap") if hasattr(row, "get") else None
        entry = float(cap) if cap is not None and cap == cap else float(rate if rate else row["close"])
        return entry, guarded_stop(entry, row.get("fw_stop"), self.fw)

    def _mm(self, pair: str) -> float:
        m = self.fw["maint_margin_est"]
        return m.get(pair.split("/")[0], m["_default"])

    _events_cache: tuple = (None, [])

    def _event_times(self) -> list[str]:
        f = self.fw["event_file"]
        if not f or not os.path.exists(f):
            return list(self.fw["event_times_utc"])
        mt = os.path.getmtime(f)
        if FrameworkStrategy._events_cache[0] != mt:
            try:
                times = [e["time_utc"] for e in json.load(open(f, encoding="utf-8")).get("events", [])]
            except (ValueError, KeyError, OSError):
                times = []
            FrameworkStrategy._events_cache = (mt, times)
        return list(self.fw["event_times_utc"]) + FrameworkStrategy._events_cache[1]

    def _event_calendar_stale(self) -> bool:
        f = self.fw["event_file"]
        if not f or not self.fw["event_file_required"]:
            return False
        return not os.path.exists(f) or time.time() - os.path.getmtime(f) > self.fw["event_file_max_age_days"] * 86400

    def _in_event_window(self, now: datetime) -> bool:
        for s in self._event_times():
            t = pd.Timestamp(s)
            t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
            if t - timedelta(minutes=self.fw["event_before_min"]) <= now <= t + timedelta(minutes=self.fw["event_after_min"]):
                return True
        return False

    # ---------------------------------------------------------- 入场价、杠杆、仓位
    def custom_entry_price(self, pair, trade, current_time, proposed_rate, entry_tag, side, **kwargs) -> float:
        row = self._signal_row(pair, current_time)
        if row is None:
            return proposed_rate
        cap = row.get("fw_entry_cap")
        return min(proposed_rate, float(cap)) if cap is not None and cap == cap else proposed_rate

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side,
                 **kwargs) -> float:
        row = self._signal_row(pair, current_time)
        if row is None:
            return 1.0
        entry, stop = self._entry_and_stop(row, current_rate)
        if stop is None:
            return 1.0
        return float(isolated_leverage((entry - stop) / entry, max_leverage, self._mm(pair), self.fw))

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake, min_stake, max_stake,
                            leverage, entry_tag, side, **kwargs) -> float:
        row = self._signal_row(pair, current_time)
        if row is None:
            return 0
        entry, stop = self._entry_and_stop(row, current_rate)
        if stop is None:
            return 0                                                       # 止损不合规：不入场
        stake = stake_for(self.wallets.get_total_stake_amount(), (entry - stop) / entry, leverage, self.fw)
        if min_stake and stake < min_stake:
            return 0
        return min(stake, max_stake)

    def confirm_trade_entry(self, pair, order_type, amount, rate, time_in_force, current_time, entry_tag, side,
                            **kwargs) -> bool:
        if self.is_template:
            return False
        if self._in_event_window(current_time) or self._event_calendar_stale():
            return False
        row = self._signal_row(pair, current_time)
        if row is None:
            return False
        _, stop = self._entry_and_stop(row, rate)
        if stop is None:
            return False
        equity = self.wallets.get_total_stake_amount()
        new_risk = amount * (rate - stop)
        open_trades = Trade.get_trades_proxy(is_open=True)
        open_risk = sum(max(0.0, (t.open_rate - sp) * t.amount) for t in open_trades
                        if (sp := t.get_custom_data("stop_px")) is not None)
        if open_risk + new_risk > equity * self.fw["max_open_risk"] * 1.0001:
            return False
        if open_trades:                                   # 与任一已持仓 30 日相关系数过高：视为同一笔风险
            closes = {}
            for p in [pair] + [t.pair for t in open_trades]:
                df = self.dp.get_pair_dataframe(p, "1d")
                if df is not None and len(df) > 31:
                    closes[p] = df.set_index("date")["close"].iloc[-31:]
            rets = pd.DataFrame(closes).pct_change()
            for t in open_trades:
                if pair in rets and t.pair in rets:
                    c = rets[pair].corr(rets[t.pair])
                    if c == c and c > self.fw["corr_threshold"]:
                        return False
        return True

    # ---------------------------------------------------------- 成交后：记录止损与初始仓位
    def order_filled(self, pair, trade, order, current_time, **kwargs) -> None:
        if order.ft_order_side == trade.entry_side and trade.get_custom_data("stop_px") is None:
            row = self._signal_row(pair, current_time)
            if row is not None:
                _, stop = self._entry_and_stop(row, trade.open_rate)
                if stop is not None:
                    trade.set_custom_data("stop_px", float(stop))
                    trade.set_custom_data("init_stake", float(trade.stake_amount))
                    trade.set_custom_data("init_amount", float(trade.amount))

    def custom_stoploss(self, pair, trade, current_time, current_rate, current_profit, after_fill,
                        **kwargs) -> float | None:
        sp = trade.get_custom_data("stop_px")
        if sp is None:
            return None
        return stoploss_from_absolute(sp, current_rate, is_short=False, leverage=trade.leverage)

    # ---------------------------------------------------------- 分批与离场
    def adjust_trade_position(self, trade, current_time, current_rate, current_profit, min_stake, max_stake,
                              current_entry_rate, current_exit_rate, current_entry_profit, current_exit_profit,
                              **kwargs):
        sp = trade.get_custom_data("stop_px")
        if sp is None or trade.open_rate <= sp:
            return None
        init_stake = trade.get_custom_data("init_stake") or trade.stake_amount
        done = trade.get_custom_data("tp_done") or []
        r_px = trade.open_rate - sp
        for r_mult, frac, tag in self.take_profit_legs(trade):
            if tag not in done and current_rate >= trade.open_rate + r_mult * r_px:
                trade.set_custom_data("tp_done", done + [tag])
                return -init_stake * frac, tag
        return None

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        if current_time - trade.open_date_utc >= timedelta(days=self.fw["max_hold_days"]):
            return "max_hold"
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        last = None if df is None or df.empty else df.iloc[-1]
        return self.strategy_exit(pair, trade, current_time, current_rate, last)
