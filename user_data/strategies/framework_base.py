"""freq-trader 框架基类：把“风控与执行”固定下来，策略只写信号。

继承 FrameworkStrategy 的策略只需要：
  1. 设置 timeframe、startup_candle_count；
  2. populate_indicators()：计算你要的指标（可用 @informative 引入其他周期）；
  3. populate_entry_trend()：在入场信号行设置 enter_long = 1，并写入列 fw_stop（绝对止损价）；
     可选写入 fw_entry_cap（限价上限，例如“突破价 × 1.001”），不写则按信号价成交；
  4. 可选：take_profit_legs()（分批止盈）、strategy_exit()（自定义离场）、populate_exit_trend()（离场信号）。

框架负责（策略只要不覆盖下面这些回调，就不能绕过）：
  - 入场计划随订单保存：信号行的 fw_stop / fw_entry_cap / 信号时间写进 enter_tag，存进数据库；
    定仓、杠杆、入场检查、成交后的止损都只读这一份计划，重启后也一样（v2.6）；
  - 止损护栏：按下单价检查，距离必须在 stop_min_pct–stop_max_pct；太近时按 stop_min_mode 放弃或改为下限；
    成交价比下单价更优、距离跌破下限时，止损放宽到下限（风险仍不超过下单时的预算）；
    止损不低于成交价、超出上限、缺少计划或实际风险超预算：立即市价离场（fw_invalid_stop），不留无止损仓位；
  - 仓位：单笔风险 = 权益 × risk_per_trade（含往返成本与资金费预留）；名义价值 ≤ 1 倍权益；
  - 杠杆：强平距离 ≥ liq_to_stop_multiple × 止损距离，且 ≤ max_position_leverage 与交易所上限；
  - 入场前检查：事件窗口、事件日历有效性（来源状态、时间格式、新鲜度、覆盖区间）、新闻守卫、
    总在险风险 ≤ max_open_risk、与已持仓相关性 ≤ corr_threshold。检查本身出错一律拒绝入场；
  - 交易所端止损（按绝对价格）、最长持有天数、Protections（连亏暂停、日/周故障保护、冷却）。
已知限制见 docs/REFERENCE.md 的“已知问题”。
所有数值是示例值，不代表适合任何策略；在子类里用 framework_params 覆盖。需要 freqtrade 2026.9。
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, stoploss_from_absolute

logger = logging.getLogger(__name__)

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
    "maint_margin_est": {"BTC": 0.004, "ETH": 0.005, "SOL": 0.01, "BNB": 0.01, "HYPE": 0.025, "_default": 0.025},
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
    # 新闻事件守卫（Odaily + BlockBeats，可选，只收紧）：按币种暂停新开仓。文件由 s03-news 定时器生成（fwresearch.blockbeats）。
    # 未设置或文件不存在 = 未启用；文件超过 news_max_age_min 未更新 = 忽略（不卡住交易）并记日志。回测不应用。
    "news_file": os.environ.get("S03_NEWS_FILE", ""),
    "news_venue": "binance",                        # 交易所级事件（该交易所被盗、暂停提现等）会封禁全部交易对
    "news_max_age_min": 30.0,
    # 研究用压力方案 delay1：入场信号推迟 N 根 K 线（只在回测 / hyperopt 里生效，实盘与 dry-run 忽略）
    "research_entry_delay_bars": int(os.environ.get("S03_ENTRY_DELAY_BARS", "0") or 0),
    # 成交价比下单价更优、按成交价算的止损距离低于下限时怎么办（v2.7，由你决定；研究方案 fill_exit / fill_keep 可对比）：
    #   widen：止损放宽到下限 + 0.02 个百分点（风险仍在预算内，但止损不在原结构位）——v2.6 的行为，默认
    #   exit ：立即离场（最贴近“结构太近就不做”，代价是一笔往返成本）
    #   keep ：保留原结构止损（违反距离下限）
    "better_fill_policy": os.environ.get("S03_BETTER_FILL_POLICY") or "widen",
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


PLAN_PREFIX = "fw|"


def encode_plan(stop, cap, ts, user: str = "") -> str:
    """入场计划 → enter_tag（freqtrade 把它存进订单与交易，最长 255 字符）。"""
    sv = f"{float(stop):.10g}" if stop is not None and stop == stop else "nan"
    cv = f"{float(cap):.10g}" if cap is not None and cap == cap else ""
    tag = f"{PLAN_PREFIX}s={sv}|c={cv}|t={int(pd.Timestamp(ts).timestamp())}"
    return (tag + f"|u={user}")[:255] if user else tag


def decode_plan(tag) -> dict | None:
    """enter_tag → {"stop", "cap", "t", "user"}；不是框架写的或止损无效时返回 None。"""
    if not isinstance(tag, str) or not tag.startswith(PLAN_PREFIX):
        return None
    parts = {}
    for p in tag[len(PLAN_PREFIX):].split("|", 3):
        k, _, v = p.partition("=")
        parts[k] = v
    try:
        stop = float(parts["s"])
        cap = float(parts["c"]) if parts.get("c") else None
        t = int(parts.get("t") or 0)
    except (KeyError, ValueError):
        return None
    if stop != stop or stop <= 0:
        return None
    return {"stop": stop, "cap": cap, "t": t, "user": parts.get("u", "")}


def _utc(s) -> pd.Timestamp:
    t = pd.Timestamp(s)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


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
    # 研究：上线判定必须通过的压力方案（随策略而定，v2.7）。可选：cost2x（手续费与滑点加重）、delay1（入场推迟一根 K 线）、
    # stopslip（止损离场再扣额外滑点，事后近似）。例如只用市价入场的策略可以去掉 delay1；止损很窄的策略应保留 stopslip。
    # 改动要写进策略规格（docs/STRATEGY_TEMPLATE.md），并在 research plan / portfolio 之前定好。
    research_stress: tuple = ("cost2x", "delay1", "stopslip")
    research_stop_slip: float = 0.002          # stopslip 方案每次止损额外扣的滑点（名义价值的比例，示例值）
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

    # ---------------------------------------------------------- 入场计划：随订单保存在 enter_tag
    def advise_entry(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """freqtrade 生成入场信号后，把每个信号行的计划止损、限价上限和信号时间写进 enter_tag。
        之后的定仓、杠杆、入场检查、成交后的止损都只读这份计划，不再“重新找最近一次信号”
        （v2.5 之前的做法：挂单期间出现新信号、成交价更优或重启后，止损可能换成别的或丢失）。"""
        df = super().advise_entry(dataframe, metadata)
        if df is None or df.empty or "enter_long" not in df.columns:
            return df
        delay = int(self.fw.get("research_entry_delay_bars") or 0)
        runmode = getattr(getattr(getattr(self, "dp", None), "runmode", None), "value", "")
        if delay > 0 and runmode in ("backtest", "hyperopt"):
            cols = [c for c in ("enter_long", "fw_stop", "fw_entry_cap", "enter_tag") if c in df.columns]
            df[cols] = df[cols].shift(delay)
            df["enter_long"] = df["enter_long"].fillna(0)
        m = df["enter_long"] == 1
        if not m.any():
            return df
        nan = pd.Series(float("nan"), index=df.index)
        stop = (df["fw_stop"] if "fw_stop" in df else nan)[m]
        cap = (df["fw_entry_cap"] if "fw_entry_cap" in df else nan)[m]
        user = df["enter_tag"][m].fillna("").astype(str) if "enter_tag" in df else pd.Series("", index=df.index[m])
        df.loc[m, "enter_tag"] = [encode_plan(sv, cv, tv, "" if uv.startswith(PLAN_PREFIX) else uv)
                                  for sv, cv, tv, uv in zip(stop, cap, df["date"][m], user)]
        return df

    def _plan_stop(self, entry_tag, rate: float) -> float | None:
        """按下单价检查计划止损：返回合规的止损价，或 None（不入场）。"""
        plan = decode_plan(entry_tag)
        if plan is None or not rate or rate <= 0:
            return None
        return guarded_stop(float(rate), plan["stop"], self.fw)

    def _mm(self, pair: str) -> float:
        m = self.fw["maint_margin_est"]
        return m.get(pair.split("/")[0], m["_default"])

    # ---------------------------------------------------------- 事件日历（有效性校验，出问题就拒绝入场）
    _events_cache: tuple = (None, None)

    def _load_calendar_file(self, f: str) -> dict:
        """解析并缓存事件文件（按修改时间）。返回 {"error", "times", "ok", "sources", "generated_at", "horizon_days"}。"""
        mt = os.path.getmtime(f)
        if FrameworkStrategy._events_cache[0] == (f, mt):
            return FrameworkStrategy._events_cache[1]
        info: dict = {"error": None, "times": []}
        try:
            data = json.load(open(f, encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                raise ValueError("缺少 events 列表")
            info["times"] = [_utc(e["time_utc"]) for e in data["events"]]
            info["ok"] = data.get("ok")
            info["sources"] = data.get("sources") or {}
            info["generated_at"] = _utc(data["generated_at"]) if data.get("generated_at") else None
            info["horizon_days"] = data.get("horizon_days")
        except (OSError, ValueError, KeyError, TypeError) as e:          # pandas 的日期解析错误是 ValueError 子类
            info = {"error": f"事件文件无效：{type(e).__name__}", "times": []}
        FrameworkStrategy._events_cache = ((f, mt), info)
        return info

    def _calendar(self, now: datetime | None = None) -> tuple[bool, str, list]:
        """返回 (可以入场, 原因, 事件时间列表)。event_file_required=True 时，以下任一情况都拒绝入场：
        未配置或找不到事件文件、文件损坏或时间格式非法、超过 event_file_max_age_days 未更新、
        所有来源失败或某个失败来源没有沿用到事件（count = 0）、事件覆盖区间已经不包含现在。
        有来源失败但沿用了上一版事件时放行（日志告警）。回测（required=False）只用文件里的时间，不因文件问题拒绝。"""
        now_ts = _utc(now) if now is not None else pd.Timestamp.now(tz="UTC")
        times = [_utc(t) for t in self.fw["event_times_utc"]]
        f, req = self.fw["event_file"], self.fw["event_file_required"]
        if not f or not os.path.exists(f):
            return (not req), ("未配置事件文件" if not f else "事件文件不存在"), times
        info = self._load_calendar_file(f)
        times = times + info["times"]
        reason = info["error"] or ""
        if not reason and time.time() - os.path.getmtime(f) > self.fw["event_file_max_age_days"] * 86400:
            reason = "事件文件过期"
        if not reason and info.get("ok") is not True:
            srcs = info.get("sources") or {}
            failed = [k for k, v in srcs.items() if not v.get("ok")]
            if not srcs or len(failed) == len(srcs) or any(int(srcs[k].get("count") or 0) == 0 for k in failed):
                reason = "事件来源失败：" + (",".join(failed) or "ok≠true")
            elif FrameworkStrategy._events_warned != (f, os.path.getmtime(f)):
                logger.warning("事件来源 %s 本轮失败，沿用上一版事件：%s", ",".join(failed), f)
                FrameworkStrategy._events_warned = (f, os.path.getmtime(f))
        hz, gen = info.get("horizon_days"), info.get("generated_at")
        if not reason and hz and gen is not None and now_ts > gen + timedelta(days=float(hz) - 1):
            reason = "事件覆盖区间不含现在"
        if reason and req:
            return False, reason, times
        return True, reason, times

    _events_warned: tuple = (None, None)

    def _in_event_window(self, now: datetime, times: list | None = None) -> bool:
        now_ts = _utc(now)
        for t in (self._calendar(now)[2] if times is None else times):
            if t - timedelta(minutes=self.fw["event_before_min"]) <= now_ts <= t + timedelta(minutes=self.fw["event_after_min"]):
                return True
        return False

    def _event_calendar_stale(self, now: datetime | None = None) -> bool:
        """兼容旧名：True = 日历不能用（会拒绝入场）。"""
        return not self._calendar(now)[0]

    # ---------------------------------------------------------- 入场价、杠杆、仓位（只读 enter_tag 里的计划）
    def custom_entry_price(self, pair, trade, current_time, proposed_rate, entry_tag, side, **kwargs) -> float:
        plan = decode_plan(entry_tag)
        cap = plan["cap"] if plan else None
        return min(proposed_rate, cap) if cap else proposed_rate

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side,
                 **kwargs) -> float:
        try:
            stop = self._plan_stop(entry_tag, current_rate)
            if stop is None:
                return 1.0
            return float(isolated_leverage((current_rate - stop) / current_rate, max_leverage, self._mm(pair), self.fw))
        except Exception:
            logger.exception("杠杆计算出错，按 1 倍处理：%s", pair)
            return 1.0

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake, min_stake, max_stake,
                            leverage, entry_tag, side, **kwargs) -> float:
        """按下单价（限价，≤ fw_entry_cap）定仓。成交价不会高于下单价，所以实际止损风险不超过这里的预算。
        出错返回 0（不入场）：freqtrade 对这个回调出错时的默认值是 proposed_stake，不能依赖它。"""
        try:
            stop = self._plan_stop(entry_tag, current_rate)
            if stop is None:
                return 0                                                   # 没有计划或止损不合规：不入场
            stake = stake_for(self.wallets.get_total_stake_amount(), (current_rate - stop) / current_rate, leverage, self.fw)
            if min_stake and stake < min_stake:
                return 0
            return min(stake, max_stake)
        except Exception:
            logger.exception("定仓出错，不入场：%s", pair)
            return 0

    _news_cache: tuple = (None, None)
    _news_warned: str = ""

    def _news_block(self, pair: str, now: datetime) -> str | None:
        """返回封禁原因（如 "security:SOL"），没有封禁返回 None。与 fwresearch.blockbeats.load_blocks 同一口径。"""
        f = self.fw.get("news_file")
        runmode = getattr(getattr(getattr(self, "dp", None), "runmode", None), "value", "")
        if not f or not os.path.exists(f) or runmode in ("backtest", "hyperopt"):
            return None
        mt = os.path.getmtime(f)
        if FrameworkStrategy._news_cache[0] != mt:
            try:
                FrameworkStrategy._news_cache = (mt, json.load(open(f, encoding="utf-8")))
            except (OSError, ValueError):
                FrameworkStrategy._news_cache = (mt, None)
        data = FrameworkStrategy._news_cache[1]
        now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        try:
            gen = datetime.fromisoformat(data["generated_at"])
        except (TypeError, KeyError, ValueError):
            gen = None
        if gen is None or now_utc - gen > timedelta(minutes=self.fw["news_max_age_min"]):
            state = "error" if gen is None else "stale"
            if FrameworkStrategy._news_warned != f"{state}:{mt}":
                logger.warning("新闻事件守卫文件%s，已忽略：%s", "无法读取" if gen is None else "过期", f)
                FrameworkStrategy._news_warned = f"{state}:{mt}"
            return None
        failed = sorted(k for k, v in (data.get("sources") or {}).items() if not v.get("ok"))
        if failed and FrameworkStrategy._news_warned != f"degraded:{mt}":
            logger.warning("新闻来源 %s 本轮采集失败，守卫只用剩下的来源：%s", ",".join(failed), f)
            FrameworkStrategy._news_warned = f"degraded:{mt}"
        coin = pair.split("/")[0].upper()
        for b in data.get("blocks", []):
            if b.get("target") in (coin, f"@{self.fw['news_venue']}") and datetime.fromisoformat(b["until_utc"]) > now_utc:
                return f"{b.get('category')}:{b.get('target')}"
        return None

    def confirm_trade_entry(self, pair, order_type, amount, rate, time_in_force, current_time, entry_tag, side,
                            **kwargs) -> bool:
        """下单前最后一道检查。任何异常都按“拒绝入场”处理：freqtrade 对这个回调出错时的默认返回值是 True（放行）。"""
        try:
            return self._confirm_entry(pair, amount, rate, current_time, entry_tag)
        except Exception:
            logger.exception("入场检查出错，按拒绝处理：%s", pair)
            return False

    def _confirm_entry(self, pair, amount, rate, current_time, entry_tag) -> bool:
        if self.is_template:
            return False
        cal_ok, cal_reason, times = self._calendar(current_time)
        if not cal_ok:
            logger.info("事件日历不可用，拒绝入场：%s（%s）", pair, cal_reason)
            return False
        if self._in_event_window(current_time, times):
            return False
        nb = self._news_block(pair, current_time)
        if nb:
            logger.info("新闻事件守卫：%s 暂停新开仓（%s）", pair, nb)
            return False
        stop = self._plan_stop(entry_tag, rate)
        if stop is None or not rate or rate <= stop:
            return False
        equity = self.wallets.get_total_stake_amount()
        new_risk = amount * (rate - stop)
        open_trades = Trade.get_trades_proxy(is_open=True)
        open_risk = self._open_risk(open_trades)
        if open_risk is None:
            logger.info("已有仓位或挂单缺少止损 / 计划，拒绝新增风险：%s", pair)
            return False
        if open_risk + new_risk > equity * self.fw["max_open_risk"] * 1.0001:
            return False
        others = [t for t in open_trades if t.pair != pair]
        if others and not self._corr_ok(pair, others):
            return False
        return True

    @staticmethod
    def _entry_in_flight(t) -> bool:
        """入场单还没走完：没有任何已完成的入场成交，或者还有挂着的入场单（含已部分成交）。"""
        if not getattr(t, "nr_of_successful_entries", 0):
            return True
        side = getattr(t, "entry_side", "buy")
        return any(getattr(o, "ft_order_side", None) == side and getattr(o, "ft_is_open", False)
                   for o in (getattr(t, "orders", None) or []))

    def _open_risk(self, open_trades) -> float | None:
        """已占用的止损风险（v2.7 修正）：
        - 入场已走完：绑定的止损 × 当前持仓数量 trade.amount；
        - 入场还在进行（未成交或部分成交、单子仍挂着）：止损（已绑定的，否则用计划止损）× 下单数量 amount_requested，
          价格用下单价 open_rate_requested。freqtrade 新建交易时 amount = 0，挂单期间不会更新它；
          v2.6 乘的是 trade.amount，挂单风险实际按 0 计算（外部审查 v2.6 P1-1）。
        任何一笔既没有绑定止损、也读不出计划（或已判为无效、正在离场）时返回 None：信息缺失时不再新增风险。"""
        total = 0.0
        for t in open_trades:
            in_flight = self._entry_in_flight(t)
            sp = t.get_custom_data("stop_px")
            if sp is None and not in_flight:
                sp = self._bind_stop(t)                     # 已成交但还没绑定（例如刚重启）：现在绑定
                if sp is None:
                    return None
            if sp is None:                                  # 入场还在进行：按计划止损
                plan = decode_plan(getattr(t, "enter_tag", None))
                if plan is None or t.get_custom_data("fw_invalid"):
                    return None
                sp = plan["stop"]
            if in_flight:
                qty = max(float(getattr(t, "amount_requested", 0) or 0), float(t.amount or 0))
                px = float(getattr(t, "open_rate_requested", None) or t.open_rate)
            else:
                qty, px = float(t.amount), float(t.open_rate)
            if qty <= 0:
                return None                                 # 读不出数量：不能证明风险为 0
            total += max(0.0, (px - float(sp)) * qty)
        return total

    def check_entry_timeout(self, pair, trade, order, current_time, **kwargs) -> bool:
        """入场单一旦部分成交，就撤掉剩余部分（v2.7，外部审查 v2.6 P0）。
        freqtrade 在入场单仍挂着时不更新 trade.amount，has_open_position = False，交易所上不会挂任何止损，
        直到整单成交或超时撤单。撤掉剩余后，已成交部分成为正式仓位，下一轮循环（约 5 秒）绑定止损并挂到交易所。
        freqtrade 每轮都对挂着的入场单调用本回调；返回 True = 按超时撤单（已成交部分保留）。"""
        try:
            filled = float(getattr(order, "filled", 0) or 0)
            if filled > 0 and getattr(order, "status", "open") == "open":
                logger.warning("%s 入场单部分成交 %s：撤掉剩余部分，已成交部分立即挂止损", pair, filled)
                return True
        except Exception:
            logger.exception("检查入场单状态出错：%s", pair)
        return False

    def informative_pairs(self):
        """相关性检查需要白名单全部交易对的日线：在这里固定订阅（实盘 dp 只返回订阅过的数据）。
        子类覆盖 informative_pairs 时要把 super().informative_pairs() 加进去。"""
        try:
            pairs = self.dp.current_whitelist()
        except Exception:
            return []
        return [(p, "1d") for p in pairs]

    def _corr_ok(self, pair: str, others) -> bool:
        """与任一已持仓或挂单的 30 日日收益相关系数 > corr_threshold 视为同一笔风险，拒绝。
        日线缺失、不足 31 根或算不出相关系数时也拒绝（v2.5 及以前会直接跳过检查）。"""
        closes = {}
        for p in [pair] + [t.pair for t in others]:
            df = self.dp.get_pair_dataframe(p, "1d")
            if df is None or len(df) <= 31:
                logger.info("相关性检查缺少 %s 的日线，拒绝入场：%s", p, pair)
                return False
            closes[p] = df.set_index("date")["close"].iloc[-31:]
        rets = pd.DataFrame(closes).pct_change()
        for t in others:
            c = rets[pair].corr(rets[t.pair])
            if c != c or c > self.fw["corr_threshold"]:
                return False
        return True

    # ---------------------------------------------------------- 成交后：按计划绑定止损（成交、部分成交、重启后都走这里）
    def _bind_stop(self, trade) -> float | None:
        """第一次看到有成交的仓位时，把 enter_tag 里的计划止损绑定到交易上并校验：
        - 止损必须低于成交价，距离不超过 stop_max_pct；成交价更优导致距离不足下限时放宽到下限；
        - 实际止损风险不超过 权益 × risk_per_trade（5% 容差，覆盖手续费与取整）；
        不满足（或缺少计划）时记 fw_invalid，custom_exit 立即市价离场。返回止损价或 None。"""
        sp = trade.get_custom_data("stop_px")
        if sp is not None:
            return float(sp)
        if trade.get_custom_data("fw_invalid") or not getattr(trade, "nr_of_successful_entries", 1) or not trade.amount:
            return None                                     # 还没有已完成的入场成交：交易所上也还没有仓位
        fw, fill = self.fw, float(trade.open_rate)
        plan, reason, stop = decode_plan(trade.enter_tag), "", None
        if plan is None:
            reason = "no_plan"
        else:
            stop = plan["stop"]
            dist = (fill - stop) / fill * 100 if fill > 0 else -1
            if stop >= fill:
                reason = "stop_not_below_fill"
            elif dist > fw["stop_max_pct"] + 1e-9:
                reason = "stop_too_far"
            else:
                if dist < fw["stop_min_pct"] + 0.02:          # 多留 0.02 个百分点：按价格最小变动单位取整后仍不低于下限
                    policy = fw.get("better_fill_policy", "widen")
                    if policy == "exit":
                        reason = "stop_too_close_after_fill"
                    elif policy == "keep":
                        trade.set_custom_data("stop_kept_below_min", True)
                    else:
                        stop = fill * (1 - (fw["stop_min_pct"] + 0.02) / 100)
                        trade.set_custom_data("stop_widened", True)
                equity = self.wallets.get_total_stake_amount() if getattr(self, "wallets", None) else None
                if not reason and equity and trade.amount * (fill - stop) > equity * fw["risk_per_trade"] * 1.05:
                    reason = "risk_over_budget"
        if reason:
            trade.set_custom_data("fw_invalid", reason)
            logger.error("交易 %s 的止损无法绑定（%s）：将立即离场", trade.pair, reason)
            return None
        trade.set_custom_data("stop_px", float(stop))
        trade.set_custom_data("plan_stop", float(plan["stop"]))
        trade.set_custom_data("init_stake", float(trade.stake_amount))
        trade.set_custom_data("init_amount", float(trade.amount))
        return float(stop)

    def order_filled(self, pair, trade, order, current_time, **kwargs) -> None:
        if order.ft_order_side == trade.entry_side:
            self._bind_stop(trade)

    def custom_stoploss(self, pair, trade, current_time, current_rate, current_profit, after_fill,
                        **kwargs) -> float | None:
        sp = self._bind_stop(trade)
        if sp is None:
            return None                                   # 无效仓位由 custom_exit 立即离场
        return stoploss_from_absolute(sp, current_rate, is_short=False, leverage=trade.leverage)

    # ---------------------------------------------------------- 分批与离场
    TP_MAX_ATTEMPTS = 3

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit, min_stake, max_stake,
                              current_entry_rate, current_exit_rate, current_entry_profit, current_exit_profit,
                              **kwargs):
        """分批止盈：是否完成按实际成交量判断（初始数量 − 当前数量），不在下单前标记。
        有未完成的订单时不再下新单；同一档下单 TP_MAX_ATTEMPTS 次仍没成交（拒单、低于最小名义等）就跳过并告警。"""
        sp = self._bind_stop(trade)
        legs = self.take_profit_legs(trade)
        if sp is None or trade.open_rate <= sp or not legs or getattr(trade, "has_open_orders", False):
            return None
        init_amount = float(trade.get_custom_data("init_amount") or trade.amount)
        exited = 1.0 - float(trade.amount) / init_amount if init_amount > 0 else 0.0
        r_px = trade.open_rate - sp
        attempts = dict(trade.get_custom_data("tp_attempts") or {})
        skipped = list(trade.get_custom_data("tp_skipped") or [])
        done, cum = [], 0.0
        for r_mult, frac, tag in sorted(legs, key=lambda x: x[0]):
            cum += frac
            if exited >= cum - max(1e-6, 0.02 * frac):     # 允许 2% 的取整误差（交易所按合约最小单位向下取整）
                done.append(tag)
                continue
            if tag in skipped:
                continue
            if current_rate < trade.open_rate + r_mult * r_px:
                break
            if attempts.get(tag, 0) >= self.TP_MAX_ATTEMPTS:
                logger.warning("分批止盈 %s 已下单 %d 次仍未成交，跳过：%s", tag, attempts[tag], trade.pair)
                trade.set_custom_data("tp_skipped", skipped + [tag])
                continue
            target_left = init_amount * (1.0 - min(cum, 1.0))
            amount_out = float(trade.amount) - target_left
            if amount_out <= 0:
                continue
            attempts[tag] = attempts.get(tag, 0) + 1
            trade.set_custom_data("tp_attempts", attempts)
            trade.set_custom_data("tp_done", done)
            return -float(trade.stake_amount) * amount_out / float(trade.amount), tag
        trade.set_custom_data("tp_done", done)
        return None

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        if self._bind_stop(trade) is None and trade.get_custom_data("fw_invalid"):
            return f"fw_invalid_stop:{trade.get_custom_data('fw_invalid')}"
        if current_time - trade.open_date_utc >= timedelta(days=self.fw["max_hold_days"]):
            return "max_hold"
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        last = None if df is None or df.empty else df.iloc[-1]
        return self.strategy_exit(pair, trade, current_time, current_rate, last)
