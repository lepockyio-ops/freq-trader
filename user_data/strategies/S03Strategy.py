"""S-03 强势整理回调买入 —— freqtrade 版（Binance USDⓈ-M 永续，只做多）

对应《freqtrade 交易流程手册》与主手册第 4、5、7 节。交易风格与原生版一致：
  第 1 步  日线筛强势、找整理（R1–R6）
  第 2 步  支撑区：9/21/50 日 EMA、前期高低点、AVWAP 聚合；只做第一、二次回测
  第 3 步  下探回升后，价格向上突破前一根 15 分钟 K 线高点即入场（实现 A）；止损取前一根低点；仓位按 0.5% 风险
  第 4 步  3R / 5R 各减 1/3，剩余用日线 9 日 EMA 追踪；远离均线减半；强市保留整仓

实现 A 在 freqtrade 中的写法：
  freqtrade 不支持“突破买入”的止损类入场单（回测代码明确限制限价不得高于 K 线高点）。
  因此主时间框架用 1 分钟：触发价 = 最近一根已收盘 15 分钟 K 线的高点；某根 1 分钟 K 线最高价触及触发价，
  就在下一根 1 分钟开盘以限价买入，限价上限 = 触发价 × (1 + 0.1%)。比真正的交易所突破单约慢 1 分钟。

所有阈值均为手册中的示例值，未经数据验证。本文件不构成投资建议。
需要 freqtrade 2026.9（本文件在该版本上加载和回测过）。
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative, stoploss_from_absolute

# ============================================================== 参数（示例值，与主手册一致）
P = {
    # 第一层：日线
    "momentum_lookbacks": (30, 90, 180),
    "momentum_min_pct": 30.0,
    "min_avg_daily_ntl_usd": 50_000_000,
    "pivot_span": 3,
    "pivot_lookback": 120,
    "contraction_ratio": 0.8,
    "ema_slope_days": 5,
    # 第二层：支撑区
    "avwap_anchor_lookback": 60,
    "zone_cluster_pct": 1.5,
    "max_retests": 2,
    # 第 3 步
    "arm_buffer_pct": 0.5,
    "invalidate_buffer_pct": 0.5,
    "entry_limit_offset_pct": 0.1,
    "stop_buffer_pct": 0.1,
    "stop_min_pct": 0.8,
    "stop_max_pct": 3.0,
    # v1.0 修正：改用当日最低点后若仍 < stop_min_pct，"skip" = 放弃本次入场（默认）；"floor" = 止损设在入场价下方 stop_min_pct
    "stop_min_mode": os.environ.get("S03_STOP_MIN_MODE", "skip"),   # 研究矩阵用环境变量切换方案
    # 第 4 步
    "tp1_r": 3.0,
    "tp1_frac": 1 / 3,
    "tp2_r": 5.0,
    "tp2_frac": 1 / 3,
    "extension_atr_mult": 2.0,
    "max_hold_days": 30,
    "strong_market_cushion_r": 2.0,
    # 风险（第 7 节）
    "risk_per_trade": 0.005,
    "max_open_risk": 0.015,
    "corr_threshold": 0.8,
    "max_position_leverage": 30,
    "liq_to_stop_multiple": 3.0,
    "round_trip_cost": 2 * (0.0005 + 0.0003),       # Binance taker 约 0.05%（以你账户实际费率为准）+ 滑点
    "funding_est": 0.0001 * 3 * 10,                 # 每 8 小时 0.01% × 10 天（示例）
    "funding_cap_frac_of_stop": 0.2,                # v1.0 修正：资金费预留不超过止损距离的 20%（示例值）
    # Binance 维持保证金率随档位变化；这里取偏保守的估计值，上线前用交易所档位表核对
    "maint_margin_est": {"BTC": 0.004, "ETH": 0.005, "SOL": 0.01, "BNB": 0.01, "_default": 0.025},
    # 宏观事件（UTC ISO 字符串），前 120 分钟到后 60 分钟不开新仓
    "event_times_utc": [],
    "event_before_min": 120,
    "event_after_min": 60,
    # 官方日程生成的事件文件（s03-events 每天更新）；回测时可指向 --history 生成的文件
    "event_file": os.environ.get("S03_EVENTS_FILE", ""),
    "event_file_max_age_days": 3.0,
    "event_file_required": os.environ.get("S03_EVENTS_REQUIRED", "1") == "1",   # 回测时设为 0
}


# ============================================================== 纯函数（与原生版 signals/s03.py 同一口径）
def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _atr(df: DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _cluster(cands: list[tuple[str, float]], width_pct: float) -> list[tuple[float, float, tuple]]:
    c = sorted([x for x in cands if x[1] == x[1] and x[1] > 0], key=lambda x: x[1])
    zones = []
    for i in range(len(c)):
        g = [c[i]]
        for j in range(i + 1, len(c)):
            if c[j][1] <= c[i][1] * (1 + width_pct / 100):
                g.append(c[j])
            else:
                break
        if len(g) >= 2:
            zones.append((g[0][1], g[-1][1], tuple(n for n, _ in g)))
    return zones


def _pick_zone(zones, close: float):
    ok = [z for z in zones
          if z[0] <= close * (1 + P["arm_buffer_pct"] / 100) and close >= z[0] * (1 - P["invalidate_buffer_pct"] / 100)]
    if not ok:
        return None
    key = {"avwap", "ema_mid", "prior_high"}
    return max(ok, key=lambda z: (len({m.split(":")[0] for m in z[2]} & key) >= 2, z[1]))


def daily_s03_columns(d: DataFrame) -> DataFrame:
    """对每一根日线，只用截至当天（含）已收盘的数据，计算 R1–R6、支撑区、回测次数。无未来信息。"""
    d = d.copy()
    c, h, lo, v = d["close"].to_numpy(), d["high"].to_numpy(), d["low"].to_numpy(), d["volume"].to_numpy()
    n = len(d)
    ef, em, es = _ema(d["close"], 9), _ema(d["close"], 21), _ema(d["close"], 50)
    d["ema9"], d["ema21"], d["ema50"], d["atr14"] = ef, em, es, _atr(d)
    k = P["ema_slope_days"]
    rng = (d["high"] - d["low"]) / d["close"] * 100
    r10, r30 = rng.rolling(10).mean(), rng.shift(10).rolling(30).mean()
    ntl7 = (d["volume"] * d["close"]).rolling(7).mean()
    rets = {m: (d["close"] / d["close"].shift(m) - 1) * 100 for m in P["momentum_lookbacks"]}
    tp = (d["high"] + d["low"] + d["close"]) / 3
    cum_pv, cum_v = (tp * d["volume"]).cumsum().to_numpy(), d["volume"].cumsum().to_numpy()

    span = P["pivot_span"]
    piv_lo, piv_hi = [], []            # (行号, 值)：在 t 行才“确认”的摆动点
    out = {key: np.full(n, np.nan) for key in ("zone_lo", "zone_hi", "retests")}
    elig = np.zeros(n, dtype=bool)
    flags = {key: np.zeros(n, dtype=bool) for key in ("r1", "r2", "r4", "r5", "r6", "notex", "fresh")}
    min_rows = max(P["momentum_lookbacks"]) + 1
    for t in range(n):
        j = t - span                                  # 候选摆动点在 t-span，右侧 span 根在 t 时才完整
        if j - span >= 0:
            if lo[j] == lo[j - span: t + 1].min():
                piv_lo.append((j, lo[j]))
            if h[j] == h[j - span: t + 1].max():
                piv_hi.append((j, h[j]))
        if t < min_rows:
            continue
        pl = [p for p in piv_lo if p[0] >= t - P["pivot_lookback"]]
        ph = [p for p in piv_hi if p[0] >= t - P["pivot_lookback"]]
        higher_low = len(pl) >= 2 and pl[-1][1] > pl[-2][1]
        lower_low = len(pl) >= 2 and pl[-1][1] < pl[-2][1]
        lower_high = len(ph) >= 2 and ph[-1][1] < ph[-2][1]
        sf, sm = ef.iat[t] - ef.iat[t - k], em.iat[t] - em.iat[t - k]
        flags["r1"][t] = any(rets[m].iat[t] >= P["momentum_min_pct"] for m in rets)
        flags["r2"][t] = ntl7.iat[t] >= P["min_avg_daily_ntl_usd"]
        flags["r4"][t] = higher_low
        flags["r5"][t] = r10.iat[t] < r30.iat[t] * P["contraction_ratio"]
        flags["r6"][t] = c[t] > ef.iat[t] and c[t] > em.iat[t] and sf >= 0 and sm >= 0
        flags["notex"][t] = not (lower_high and lower_low and c[t] < em.iat[t] and sm < 0)
        a0 = t - P["avwap_anchor_lookback"] + 1
        anchor = a0 + int(np.argmax(h[a0: t + 1]))
        vv = cum_v[t] - (cum_v[anchor - 1] if anchor > 0 else 0)
        av = (cum_pv[t] - (cum_pv[anchor - 1] if anchor > 0 else 0)) / vv if vv > 0 else np.nan
        cands = [("ema_fast", ef.iat[t]), ("ema_mid", em.iat[t]), ("ema_slow", es.iat[t]), ("avwap", av)]
        cands += [(f"prior_high:{i}", val) for i, val in ph[-4:]] + [(f"prior_low:{i}", val) for i, val in pl[-4:]]
        z = _pick_zone(_cluster(cands, P["zone_cluster_pct"]), c[t])
        if z is None:
            continue
        zhi = z[1]
        above = c[: t + 1] > zhi
        cross = np.where(above[1:] & ~above[:-1])[0]
        retests = int((lo[cross[-1] + 2: t + 1] <= zhi).sum()) if len(cross) else 0
        flags["fresh"][t] = retests < P["max_retests"]
        out["zone_lo"][t], out["zone_hi"][t], out["retests"][t] = z[0], zhi, retests
        elig[t] = all(flags[f][t] for f in flags)
    for key, arr in out.items():
        d[key] = arr
    for key, arr in flags.items():
        d[key] = arr.astype(int)
    d["eligible"] = elig.astype(int)                  # R3（市场背景）在主框架中与 BTC 日线合并判断
    return d


def run_15m_state_machine(bars: DataFrame) -> DataFrame:
    """输入：按时间排序的已收盘 15 分钟 K 线，附带当时可见的 eligible / zone_lo / zone_hi。
    输出：每根 15 分钟 K 线收盘后的状态、触发价（该 K 线高点）、止损价。逻辑与原生版 step_intraday 相同。"""
    phase, rows = "idle", []
    day, day_low = None, np.inf
    for t, b in bars.iterrows():
        dd = t.normalize()
        if dd != day:
            day, day_low = dd, np.inf
        day_low = min(day_low, b["low"])
        trig = stop = np.nan
        lo_z, hi_z = b["zone_lo"], b["zone_hi"]
        if not b["eligible"] or not (lo_z == lo_z):
            phase = "idle"
        elif phase != "idle" and b["close"] < lo_z * (1 - P["invalidate_buffer_pct"] / 100):
            phase = "idle"                             # 一路跌穿支撑：等待
        else:
            in_zone = b["low"] <= hi_z * (1 + P["arm_buffer_pct"] / 100)
            if phase == "idle" and in_zone and b["close"] >= lo_z * (1 - P["invalidate_buffer_pct"] / 100):
                phase = "armed"
            if phase == "armed" and in_zone and b["close"] > b["open"]:
                phase = "confirmed"                    # 下探后回升
            if phase == "confirmed":
                trig = b["high"]
                s = b["low"] * (1 - P["stop_buffer_pct"] / 100)
                dist = (trig - s) / trig * 100
                if dist < P["stop_min_pct"]:
                    s = day_low * (1 - P["stop_buffer_pct"] / 100)
                    dist = (trig - s) / trig * 100
                if dist < P["stop_min_pct"]:               # v1.0：当日最低点仍太近
                    if P["stop_min_mode"] == "floor":
                        s = trig * (1 - P["stop_min_pct"] / 100)
                        dist = P["stop_min_pct"]
                    else:
                        dist = -1.0                        # 放弃本次入场
                if 0 < dist <= P["stop_max_pct"]:
                    stop = s
                else:
                    trig = np.nan                      # 止损 > 3%：本根不触发
        rows.append((t, {"idle": 0, "armed": 1, "confirmed": 2}[phase], trig, stop))
    return pd.DataFrame(rows, columns=["date_15m", "s03_phase", "s03_trigger", "s03_stop"]).set_index("date_15m")


def stake_for(equity: float, stop_dist: float, leverage: float) -> float:
    """保证金 = 名义价值 / 杠杆；名义价值 = 权益 × 0.5% / (止损距离 + 往返成本 + 资金费预留)，且 ≤ 1 倍权益。
    资金费预留不超过止损距离的 20%（v1.0 修正）。"""
    funding = min(P["funding_est"], P["funding_cap_frac_of_stop"] * stop_dist)
    notional = equity * P["risk_per_trade"] / (stop_dist + P["round_trip_cost"] + funding)
    notional = min(notional, equity)                                   # 单标的名义价值 ≤ 1 倍权益
    return notional / max(leverage, 1.0)


def isolated_leverage(stop_dist: float, max_lev: float, mm: float) -> int:
    """逐仓杠杆 = min(30, 交易所上限, floor(1 / (3 × 止损距离 × (1 − mm) + mm)))。"""
    raw = 1.0 / (P["liq_to_stop_multiple"] * stop_dist * (1 - mm) + mm)
    return int(max(1, min(P["max_position_leverage"], max_lev, math.floor(raw))))


# ============================================================== 策略
class S03Strategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1m"
    can_short = False
    process_only_new_candles = True
    startup_candle_count = 400                 # 1 分钟主框架所需；日线另需约 230 根（见 README）
    minimal_roi = {"0": 100}                   # 关掉默认 ROI 离场
    stoploss = -0.99                           # 仅作兜底；实际止损由 custom_stoploss 按价格给出
    use_custom_stoploss = True
    trailing_stop = False
    position_adjustment_enable = True          # 3R / 5R / 乖离减仓用部分平仓
    max_entry_position_adjustment = 0          # 不加仓
    use_exit_signal = False                    # 离场全部由 custom_exit / 止损 / 分批完成

    @property
    def protections(self):
        return [
            # 连亏暂停：7 天内 6 次止损 → 暂停 24 小时
            {"method": "StoplossGuard", "lookback_period": 7 * 1440, "trade_limit": 6,
             "stop_duration": 1440, "only_per_pair": False},
            # 系统故障保护：日亏 2% / 周亏 4% 停止新开仓（你可删除）
            {"method": "MaxDrawdown", "calculation_mode": "equity", "lookback_period": 1440,
             "trade_limit": 1, "stop_duration": 1440, "max_allowed_drawdown": 0.02},
            {"method": "MaxDrawdown", "calculation_mode": "equity", "lookback_period": 7 * 1440,
             "trade_limit": 1, "stop_duration": 7 * 1440, "max_allowed_drawdown": 0.04},
            {"method": "CooldownPeriod", "stop_duration": 60},
        ]

    # ---------------------------------------------------------- 信息周期
    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return daily_s03_columns(dataframe)

    @informative("1d", "BTC/{stake}:{stake}")
    def populate_indicators_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        e21 = _ema(dataframe["close"], 21)
        dataframe["strong"] = ((dataframe["close"] > e21) & (e21 >= e21.shift(5))).astype(int)
        return dataframe

    @informative("15m")
    def populate_indicators_15m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    # ---------------------------------------------------------- 主框架（1 分钟）
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        stake = self.config["stake_currency"]
        btc_col = f"btc_{stake}_strong_1d".lower()
        btc_strong = df[btc_col] if btc_col in df else 1
        # R3：BTC 在上行的 21 日 EMA 之上（原生版的“或 ≥2 个标的同时强势”在此版省略，见 README）
        df["s03_eligible"] = ((df["eligible_1d"] == 1) & (btc_strong == 1)).astype(int)
        bars = (df[["date_15m", "open_15m", "high_15m", "low_15m", "close_15m", "s03_eligible",
                    "zone_lo_1d", "zone_hi_1d"]]
                .dropna(subset=["date_15m", "close_15m"])
                .drop_duplicates("date_15m")
                .set_index("date_15m")
                .rename(columns={"open_15m": "open", "high_15m": "high", "low_15m": "low", "close_15m": "close",
                                 "s03_eligible": "eligible", "zone_lo_1d": "zone_lo", "zone_hi_1d": "zone_hi"}))
        sm = run_15m_state_machine(bars) if len(bars) else pd.DataFrame(
            columns=["s03_phase", "s03_trigger", "s03_stop"])
        df = df.merge(sm, how="left", left_on="date_15m", right_index=True)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        cond = (
            (df["s03_phase"] == 2)
            & df["s03_trigger"].notna()
            & (df["high"] >= df["s03_trigger"])                     # 1 分钟最高价触及触发价
            & (df["high"].shift(1) < df["s03_trigger"])             # 只在首次触及时发信号
            & (df["volume"] > 0)
        )
        df.loc[cond, ["enter_long", "enter_tag"]] = (1, "S03")
        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    # ---------------------------------------------------------- 工具
    def _signal_row(self, pair: str, current_time: datetime) -> pd.Series | None:
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty:
            return None
        df = df[df["date"] <= current_time]
        sig = df[df["enter_long"] == 1] if "enter_long" in df else df.iloc[0:0]
        return sig.iloc[-1] if len(sig) else None

    def _mm(self, pair: str) -> float:
        base = pair.split("/")[0]
        return P["maint_margin_est"].get(base, P["maint_margin_est"]["_default"])

    _events_cache: tuple = (None, [])

    def _event_times(self) -> list[str]:
        f = P["event_file"]
        if not f or not os.path.exists(f):
            return list(P["event_times_utc"])
        mt = os.path.getmtime(f)
        if self._events_cache[0] != mt:
            try:
                times = [e["time_utc"] for e in json.load(open(f, encoding="utf-8")).get("events", [])]
            except (ValueError, KeyError, OSError):
                times = []
            S03Strategy._events_cache = (mt, times)
        return list(P["event_times_utc"]) + self._events_cache[1]

    def _event_calendar_stale(self) -> bool:
        f = P["event_file"]
        if not f or not P["event_file_required"]:
            return False
        return not os.path.exists(f) or time.time() - os.path.getmtime(f) > P["event_file_max_age_days"] * 86400

    def _in_event_window(self, now: datetime) -> bool:
        for s in self._event_times():
            t = pd.Timestamp(s)
            t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
            if t - timedelta(minutes=P["event_before_min"]) <= now <= t + timedelta(minutes=P["event_after_min"]):
                return True
        return False

    # ---------------------------------------------------------- 入场价、杠杆、仓位
    def custom_entry_price(self, pair, trade, current_time, proposed_rate, entry_tag, side, **kwargs) -> float:
        row = self._signal_row(pair, current_time)
        if row is None or not row["s03_trigger"] == row["s03_trigger"]:
            return proposed_rate
        cap = row["s03_trigger"] * (1 + P["entry_limit_offset_pct"] / 100)
        return min(proposed_rate, cap)          # 开盘若高于上限，挂在上限等回落；超时撤单不追价

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side,
                 **kwargs) -> float:
        row = self._signal_row(pair, current_time)
        if row is None:
            return 1.0
        entry = row["s03_trigger"] * (1 + P["entry_limit_offset_pct"] / 100)
        return float(isolated_leverage((entry - row["s03_stop"]) / entry, max_leverage, self._mm(pair)))

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake, min_stake, max_stake,
                            leverage, entry_tag, side, **kwargs) -> float:
        row = self._signal_row(pair, current_time)
        if row is None:
            return 0
        entry = row["s03_trigger"] * (1 + P["entry_limit_offset_pct"] / 100)
        d = (entry - row["s03_stop"]) / entry
        equity = self.wallets.get_total_stake_amount()
        stake = stake_for(equity, d, leverage)
        if min_stake and stake < min_stake:
            return 0                                                       # 低于交易所最小下单额：不做
        return min(stake, max_stake)

    def confirm_trade_entry(self, pair, order_type, amount, rate, time_in_force, current_time, entry_tag, side,
                            **kwargs) -> bool:
        if self._in_event_window(current_time) or self._event_calendar_stale():
            return False
        row = self._signal_row(pair, current_time)
        if row is None:
            return False
        equity = self.wallets.get_total_stake_amount()
        new_risk = amount * (rate - row["s03_stop"])
        open_trades = Trade.get_trades_proxy(is_open=True)
        open_risk = 0.0
        closes = {}
        for t in open_trades:
            sp = t.get_custom_data("stop_px")
            if sp is not None:
                open_risk += max(0.0, (t.open_rate - sp) * t.amount)
        if open_risk + new_risk > equity * P["max_open_risk"] * 1.0001:
            return False
        # 相关性：与任一已持仓 30 日相关系数 > 0.8 → 视为同一笔风险，不再开新仓（主手册第 7 节）
        if open_trades:
            for p in [pair] + [t.pair for t in open_trades]:
                df = self.dp.get_pair_dataframe(p, "1d")
                if df is not None and len(df) > 31:
                    closes[p] = df.set_index("date")["close"].iloc[-31:]
            rets = pd.DataFrame(closes).pct_change()
            for t in open_trades:
                if pair in rets and t.pair in rets:
                    cval = rets[pair].corr(rets[t.pair])
                    if cval == cval and cval > P["corr_threshold"]:
                        return False
        return True

    # ---------------------------------------------------------- 成交后：记录止损与初始仓位
    def order_filled(self, pair, trade, order, current_time, **kwargs) -> None:
        if order.ft_order_side == trade.entry_side and trade.get_custom_data("stop_px") is None:
            row = self._signal_row(pair, current_time)
            if row is not None:
                trade.set_custom_data("stop_px", float(row["s03_stop"]))
                trade.set_custom_data("init_stake", float(trade.stake_amount))
                trade.set_custom_data("init_amount", float(trade.amount))

    def custom_stoploss(self, pair, trade, current_time, current_rate, current_profit, after_fill,
                        **kwargs) -> float | None:
        sp = trade.get_custom_data("stop_px")
        if sp is None:
            row = self._signal_row(pair, current_time)
            sp = None if row is None else float(row["s03_stop"])
        if sp is None:
            return None
        return stoploss_from_absolute(sp, current_rate, is_short=False, leverage=trade.leverage)

    # ---------------------------------------------------------- 分批与追踪
    def _daily_row(self, pair: str) -> pd.Series | None:
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        return None if df is None or df.empty else df.iloc[-1]

    def _strong_market(self, trade: Trade) -> bool:
        cushion = 0.0
        for t in Trade.get_trades_proxy(is_open=True):
            if t.id == trade.id:
                continue
            sp = t.get_custom_data("stop_px")
            last = self._daily_row(t.pair)
            if sp is None or last is None or t.open_rate <= sp:
                continue
            cushion += (last["close"] - t.open_rate) / (t.open_rate - sp)
        last = self._daily_row(trade.pair)
        stake = self.config["stake_currency"]
        btc_col = f"btc_{stake}_strong_1d".lower()
        btc_ok = last is not None and btc_col in last and last[btc_col] == 1
        return bool(btc_ok and cushion >= P["strong_market_cushion_r"])

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit, min_stake, max_stake,
                              current_entry_rate, current_exit_rate, current_entry_profit, current_exit_profit,
                              **kwargs):
        sp = trade.get_custom_data("stop_px")
        init_stake = trade.get_custom_data("init_stake") or trade.stake_amount
        if sp is None or trade.open_rate <= sp:
            return None
        r_px = trade.open_rate - sp
        done = trade.get_custom_data("tp_done") or []
        if "tp1" not in done and current_rate >= trade.open_rate + P["tp1_r"] * r_px:
            trade.set_custom_data("tp_done", done + ["tp1"])
            if self._strong_market(trade):
                return None                                   # 强市且有利润缓冲：3R 不减仓
            return -init_stake * P["tp1_frac"], "tp1_3R"
        if "tp2" not in done and current_rate >= trade.open_rate + P["tp2_r"] * r_px:
            trade.set_custom_data("tp_done", done + ["tp2"])
            return -init_stake * P["tp2_frac"], "tp2_5R"
        last = self._daily_row(trade.pair)
        if last is not None and "ext" not in done:
            if last["close_1d"] > last["ema9_1d"] + P["extension_atr_mult"] * last["atr14_1d"]:
                trade.set_custom_data("tp_done", done + ["ext"])
                return -trade.stake_amount / 2, "extension"
        return None

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        if current_time - trade.open_date_utc >= timedelta(days=P["max_hold_days"]):
            return "max_hold"
        last = self._daily_row(pair)
        if last is not None and last["close_1d"] < last["ema9_1d"]:
            return "ema9_break"                               # 已收盘日线跌破 9 日 EMA（入场时必在其上，故只会在之后触发）
        return None
