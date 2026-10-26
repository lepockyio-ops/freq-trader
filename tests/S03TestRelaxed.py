"""仅用于测试：放宽日线条件（有支撑区即视为合格、BTC 视为强势），让回测产生交易，
以验证仓位、杠杆、止损、分批、离场回调都能运行。不要用于交易。"""
from pandas import DataFrame

from freqtrade.strategy import informative
from S03Strategy import S03Strategy, daily_s03_columns


class S03TestRelaxed(S03Strategy):
    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        d = daily_s03_columns(dataframe)
        d["eligible"] = d["zone_lo"].notna().astype(int)
        return d

    @informative("1d", "BTC/{stake}:{stake}")
    def populate_indicators_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["strong"] = 1
        return dataframe
