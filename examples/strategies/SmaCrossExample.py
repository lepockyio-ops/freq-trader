"""示例策略（仅用于验证框架能跑通，不代表有正期望，不要直接用于交易）。

日线：收盘价在上升的 50 日 SMA 之上。信号：15 分钟收盘价上穿 20 EMA。
止损：最近 4 根 15 分钟最低点下方 0.1%。止盈：2R 卖出一半。离场：日线收盘跌破 50 日 SMA。
回测：freqtrade backtesting -c user_data/config.json --strategy SmaCrossExample --strategy-path examples/strategies
"""
from __future__ import annotations

import os
import sys

from pandas import DataFrame

from freqtrade.strategy import informative

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "user_data", "strategies"))
from framework_base import FrameworkStrategy  # noqa: E402


class SmaCrossExample(FrameworkStrategy):
    timeframe = "15m"
    startup_candle_count = 100

    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma50"] = dataframe["close"].rolling(50).mean()
        dataframe["trend_ok"] = ((dataframe["close"] > dataframe["sma50"])
                                 & (dataframe["sma50"] >= dataframe["sma50"].shift(5))).astype(int)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = dataframe["close"].ewm(span=20, adjust=False).mean()
        dataframe["swing_low4"] = dataframe["low"].rolling(4).min()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        cross = (df["close"] > df["ema20"]) & (df["close"].shift(1) <= df["ema20"].shift(1))
        cond = cross & (df["trend_ok_1d"] == 1) & (df["volume"] > 0)
        df["fw_stop"] = df["swing_low4"] * (1 - 0.001)
        df["fw_entry_cap"] = df["close"] * (1 + 0.001)
        df.loc[cond, ["enter_long", "enter_tag"]] = (1, "sma_cross")
        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def take_profit_legs(self, trade):
        return [(2.0, 0.5, "tp_2R")]

    def strategy_exit(self, pair, trade, current_time, current_rate, last_row):
        if last_row is not None and last_row.get("close_1d", 0) < last_row.get("sma50_1d", 0):
            return "below_sma50"
        return None
