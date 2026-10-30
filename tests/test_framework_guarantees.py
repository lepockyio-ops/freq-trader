"""框架保证的故障与边界测试（v2.6，针对外部审查报告的 P0 问题）。

每个测试对应一条“框架保证”在出错或边界条件下的行为：
- 止损随订单绑定（enter_tag），不再在成交时重新找信号；
- 成交价更优、成交价低于止损、缺计划、风险超预算、重启后恢复；
- 入场检查出错一律拒绝；事件日历无效（来源失败、时间非法、过期、缺文件、覆盖不足）一律拒绝。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "user_data" / "strategies"))
import framework_base as F  # noqa: E402

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
EQUITY = 10_000.0


class S(F.FrameworkStrategy):
    timeframe = "15m"
    is_template = False

    def populate_indicators(self, df, md):
        return df

    def populate_entry_trend(self, df, md):
        return df

    def populate_exit_trend(self, df, md):
        return df


class FakeTrade:
    def __init__(self, enter_tag, open_rate, amount, pair="BTC/USDT:USDT", leverage=10.0, entries=1):
        self.enter_tag, self.open_rate, self.amount, self.pair = enter_tag, open_rate, amount, pair
        self.leverage, self.nr_of_successful_entries, self.entry_side = leverage, entries, "buy"
        self.stake_amount = amount * open_rate / leverage
        self.open_date_utc = NOW - timedelta(hours=1)
        self.cd: dict = {}

    def get_custom_data(self, k):
        return self.cd.get(k)

    def set_custom_data(self, k, v):
        self.cd[k] = v


def strat(params=None, open_trades=(), signal_rows=None):
    s = S.__new__(S)
    s.framework_params = {"event_file_required": False, **(params or {})}
    df = pd.DataFrame(signal_rows or [])
    s.dp = SimpleNamespace(get_analyzed_dataframe=lambda p, tf: (df, None), get_pair_dataframe=lambda p, tf: pd.DataFrame(),
                           runmode=SimpleNamespace(value="dry_run"))
    s.wallets = SimpleNamespace(get_total_stake_amount=lambda: EQUITY)
    F.Trade.get_trades_proxy = staticmethod(lambda is_open=True: list(open_trades))
    F.FrameworkStrategy._events_cache = (None, None)
    return s


def tag(stop, cap=None, user=""):
    return F.encode_plan(stop, cap, NOW - timedelta(minutes=15), user)


def sized_amount(s, rate, stop):
    """按框架自己的定仓与杠杆算出的数量（模拟 freqtrade 下单）。"""
    lev = s.leverage("BTC/USDT:USDT", NOW, rate, 10, 75, tag(stop), "long")
    stake = s.custom_stake_amount("BTC/USDT:USDT", NOW, rate, 0, 0, 1e9, lev, tag(stop), "long")
    return stake * lev / rate, lev


# ------------------------------------------------------------------ 计划随订单保存
def test_plan_roundtrip_and_advise_entry_keeps_user_tag():
    p = F.decode_plan(F.encode_plan(97.5, 100.1, NOW, "my|tag"))
    assert p["stop"] == 97.5 and p["cap"] == 100.1 and p["user"] == "my|tag"
    assert F.decode_plan("sma_cross") is None and F.decode_plan(F.encode_plan(float("nan"), None, NOW)) is None
    s = strat()
    df = pd.DataFrame({"date": pd.date_range(NOW, periods=3, freq="15min"), "close": [100.0] * 3,
                       "enter_long": [0, 1, 1], "fw_stop": [99.0, 98.0, 97.0], "fw_entry_cap": [None, 100.1, None]})
    S.populate_entry_trend = lambda self, d, md: d.assign(enter_tag=["", "u1", ""])
    try:
        out = s.advise_entry(df, {"pair": "BTC/USDT:USDT"})
    finally:
        S.populate_entry_trend = lambda self, d, md: d
    plans = [F.decode_plan(t) for t in out["enter_tag"]]
    assert plans[0] is None and plans[1]["stop"] == 98.0 and plans[1]["cap"] == 100.1 and plans[1]["user"] == "u1"
    assert plans[2]["stop"] == 97.0 and plans[2]["cap"] is None


def test_stop_comes_from_the_order_not_from_a_newer_signal():
    """审查 P0：挂单期间出现止损 3% 的新信号，旧实现成交时换成新止损（风险 1.10%，止损贴近强平价）。"""
    newer = [{"date": NOW - timedelta(minutes=15), "close": 100.0, "enter_long": 1, "fw_stop": 97.0}]
    s = strat(signal_rows=newer)
    amount, lev = sized_amount(s, 100.0, 99.0)
    t = FakeTrade(tag(99.0), 100.0, amount, leverage=lev)
    s.order_filled(t.pair, t, SimpleNamespace(ft_order_side="buy"), NOW)
    assert t.cd["stop_px"] == 99.0
    assert t.amount * (100.0 - t.cd["stop_px"]) <= EQUITY * 0.005 + 1e-9


def test_better_fill_widens_stop_to_floor_within_budget():
    """审查 P0：不设上限、计划 100 / 止损 99、实际成交 99.5，旧实现 stop_px = None（只剩 −99% 兜底）。"""
    s = strat()
    amount, lev = sized_amount(s, 100.0, 99.0)
    t = FakeTrade(tag(99.0), 99.5, amount, leverage=lev)
    s.order_filled(t.pair, t, SimpleNamespace(ft_order_side="buy"), NOW)
    sp = t.cd["stop_px"]
    assert sp is not None and (99.5 - sp) / 99.5 * 100 >= 0.8 and t.cd.get("stop_widened")
    assert t.amount * (99.5 - sp) <= EQUITY * 0.005 * 1.05


def test_order_price_below_stop_is_rejected_before_ordering():
    """审查 P0：上限 100、止损 99、下单价 98，旧实现 confirm 返回 True、新增风险为负。"""
    s = strat()
    t = tag(99.0, cap=100.0)
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 10, 98.0, "GTC", NOW, t, "long") is False
    assert s.custom_stake_amount("BTC/USDT:USDT", NOW, 98.0, 100, 0, 1e9, 10, t, "long") == 0
    assert s.custom_entry_price("BTC/USDT:USDT", None, NOW, 101.0, t, "long") == 100.0


@pytest.mark.parametrize("enter_tag,fill,amount,reason", [
    ("manual", 100.0, 1.0, "no_plan"),                        # 没有框架计划（例如手动开仓）
    (None, 100.0, 1.0, "no_plan"),
    ("PLAN:99", 98.5, 1.0, "stop_not_below_fill"),            # 跳空成交在止损之下
    ("PLAN:99", 100.0, 1000.0, "risk_over_budget"),           # 数量远超预算
])
def test_invalid_positions_exit_immediately(enter_tag, fill, amount, reason):
    s = strat()
    t = FakeTrade(tag(99.0) if enter_tag == "PLAN:99" else enter_tag, fill, amount)
    s.order_filled(t.pair, t, SimpleNamespace(ft_order_side="buy"), NOW)
    assert t.cd.get("stop_px") is None and t.cd["fw_invalid"] == reason
    assert s.custom_exit(t.pair, t, NOW, fill, 0.0) == f"fw_invalid_stop:{reason}"
    assert s.custom_stoploss(t.pair, t, NOW, fill, 0.0, False) is None


def test_restart_rebinds_from_enter_tag():
    """重启后 order_filled 不会重放；custom_stoploss 第一次调用时从 enter_tag 绑定同一份计划。"""
    s = strat()
    amount, lev = sized_amount(s, 100.0, 98.5)
    t = FakeTrade(tag(98.5), 100.0, amount, leverage=lev)
    fresh = strat()                                       # 新进程：没有任何内存状态
    sl = fresh.custom_stoploss(t.pair, t, NOW, 100.0, 0.0, False)
    assert t.cd["stop_px"] == 98.5 and sl is not None and abs(sl) > 0


def test_unfilled_trade_is_not_bound_or_exited():
    s = strat()
    t = FakeTrade(tag(99.0), 100.0, 1.0, entries=0)
    assert s._bind_stop(t) is None and "fw_invalid" not in t.cd


# ------------------------------------------------------------------ 入场检查出错一律拒绝
def test_exceptions_in_entry_checks_reject(monkeypatch):
    from freqtrade.strategy.strategy_wrapper import strategy_safe_wrapper

    s = strat()
    monkeypatch.setattr(S, "_news_block", lambda self, p, n: (_ for _ in ()).throw(RuntimeError("boom")))
    logging.disable(logging.CRITICAL)
    try:
        assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100.0, "GTC", NOW, tag(99.0), "long") is False
        wrapped = strategy_safe_wrapper(s.confirm_trade_entry, default_retval=True)     # freqtrade 的调用方式
        assert wrapped(pair="BTC/USDT:USDT", order_type="limit", amount=1, rate=100.0, time_in_force="GTC",
                       current_time=NOW, entry_tag=tag(99.0), side="long") is False
        monkeypatch.setattr(S, "_plan_stop", lambda self, t, r: (_ for _ in ()).throw(RuntimeError("boom")))
        assert s.custom_stake_amount("BTC/USDT:USDT", NOW, 100.0, 500, 0, 1e9, 10, tag(99.0), "long") == 0
        assert s.leverage("BTC/USDT:USDT", NOW, 100.0, 10, 75, tag(99.0), "long") == 1.0
    finally:
        logging.disable(logging.NOTSET)


# ------------------------------------------------------------------ 事件日历有效性
def _cal(tmp_path, ok=True, events=None, sources=None, generated=None, horizon=35, name="events.json"):
    f = tmp_path / name
    data = {"generated_at": (generated or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "horizon_days": horizon, "ok": ok, "events": events if events is not None else [],
            "sources": sources if sources is not None else {"fed": {"ok": ok, "count": 1}}}
    f.write_text(json.dumps(data), "utf-8")
    return str(f)


def _confirm(s, now=None):
    return s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100.0, "GTC", now or datetime.now(timezone.utc),
                                 tag(98.0), "long")


@pytest.mark.parametrize("case", ["ok_false_empty", "all_failed", "failed_no_fallback", "bad_time", "bad_json",
                                  "missing_file", "no_file_configured", "stale", "horizon"])
def test_calendar_problems_reject_entry(tmp_path, case):
    now = datetime.now(timezone.utc)
    f = {"ok_false_empty": lambda: _cal(tmp_path, ok=False, sources={"fed": {"ok": False, "count": 0}}),
         "all_failed": lambda: _cal(tmp_path, ok=False, sources={"fed": {"ok": False, "count": 3},
                                                                  "bls": {"ok": False, "count": 2}}),
         "failed_no_fallback": lambda: _cal(tmp_path, ok=False, sources={"fed": {"ok": True, "count": 3},
                                                                          "bls": {"ok": False, "count": 0}}),
         "bad_time": lambda: _cal(tmp_path, events=[{"time_utc": "not-a-time"}]),
         "bad_json": lambda: (tmp_path / "e.json").write_text("{broken", "utf-8") and str(tmp_path / "e.json"),
         "missing_file": lambda: str(tmp_path / "nope.json"),
         "no_file_configured": lambda: "",
         "stale": lambda: _cal(tmp_path),
         "horizon": lambda: _cal(tmp_path, generated=now - timedelta(days=40)),
         }[case]()
    if case == "stale":
        os.utime(f, (time.time() - 5 * 86400,) * 2)
    s = strat({"event_file": f, "event_file_required": True})
    assert _confirm(s, now) is False
    ok, reason, _ = s._calendar(now)
    assert not ok and reason


def test_calendar_partial_failure_with_fallback_allows(tmp_path):
    f = _cal(tmp_path, ok=False, sources={"fed": {"ok": True, "count": 8}, "bls": {"ok": False, "count": 5}})
    s = strat({"event_file": f, "event_file_required": True})
    assert _confirm(s) is True


def test_calendar_window_blocks_and_backtest_mode_never_rejects_on_file_problems(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    f = _cal(tmp_path, events=[{"time_utc": (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}])
    s = strat({"event_file": f, "event_file_required": True})
    assert _confirm(s, now) is False and _confirm(s, now + timedelta(hours=5)) is True
    bad = _cal(tmp_path, ok=False, sources={"fed": {"ok": False, "count": 0}}, name="bad.json")
    s2 = strat({"event_file": bad, "event_file_required": False})                      # 回测：只用时间，不因文件拒绝
    assert _confirm(s2, now) is True


# ------------------------------------------------------------------ 第 2 批：分批止盈按成交确认；总风险含挂单；缺数据即拒绝
class TPStrat(S):
    def take_profit_legs(self, trade):
        return [(2.0, 0.5, "tp_2R")]


def tp_trade():
    s = TPStrat.__new__(TPStrat)
    s.framework_params = {"event_file_required": False}
    s.wallets = SimpleNamespace(get_total_stake_amount=lambda: EQUITY)
    t = FakeTrade(tag(99.0), 100.0, 10.0)
    t.has_open_orders = False
    s._bind_stop(t)
    return s, t


def adjust(s, t, px):
    return s.adjust_trade_position(t, NOW, px, 0, 0, 1e9, px, 0, px, 0)


def test_take_profit_is_done_only_after_the_fill():
    """审查 P1：旧实现下单前就把这一档标记完成，订单没成交时永久跳过。"""
    s, t = tp_trade()
    first = adjust(s, t, 102.5)
    assert first is not None and first[1] == "tp_2R" and abs(first[0] + t.stake_amount * 0.5) < 1e-9
    second = adjust(s, t, 102.5)                         # 订单被拒 / 取消：数量没变 → 再下一次
    assert second is not None and "tp_2R" not in (t.cd.get("tp_done") or [])
    t.has_open_orders = True                             # 有挂着的单：等待，不重复下单
    assert adjust(s, t, 102.5) is None
    t.has_open_orders, t.amount = False, 5.0              # 成交了一半仓位
    assert adjust(s, t, 103.0) is None and t.cd["tp_done"] == ["tp_2R"]


def test_partial_fill_requests_only_the_rest():
    s, t = tp_trade()
    t.amount = 8.0                                       # 第一次只成交了 2（初始 10，目标留 5）
    req = adjust(s, t, 102.5)
    assert req is not None and abs(-req[0] / t.stake_amount * t.amount - 3.0) < 1e-9


def test_take_profit_gives_up_after_max_attempts():
    s, t = tp_trade()
    for _ in range(TPStrat.TP_MAX_ATTEMPTS):
        assert adjust(s, t, 102.5) is not None
    assert adjust(s, t, 102.5) is None and t.cd["tp_skipped"] == ["tp_2R"]


def _open(stop_px=None, enter_tag=None, entries=1, amount=10.0, pair="ETH/USDT:USDT", invalid=None):
    t = FakeTrade(enter_tag, 100.0, amount, pair=pair, entries=entries)
    if stop_px is not None:
        t.cd["stop_px"] = stop_px
    if invalid:
        t.cd["fw_invalid"] = invalid
    return t


@pytest.mark.parametrize("trade,expect", [
    (lambda: _open(stop_px=99.0), True),                                     # 正常持仓，风险 10
    (lambda: _open(enter_tag=tag(99.0), entries=0), True),                  # 挂单：按计划止损算
    (lambda: _open(enter_tag=tag(95.0), entries=0, amount=40.0), False),    # 挂单风险 200 → 总风险超 1.5%
    (lambda: _open(enter_tag="manual", entries=1), False),                  # 已成交但没有止损 / 计划
    (lambda: _open(enter_tag=None, entries=0), False),                      # 挂单没有计划
    (lambda: _open(enter_tag=tag(99.0), invalid="stop_not_below_fill"), False),
])
def test_open_risk_counts_pending_and_rejects_missing_info(trade, expect):
    d1 = pd.DataFrame({"date": pd.date_range("2026-09-01", periods=40, freq="D", tz="UTC"),
                       "close": [100 + (i % 3) for i in range(40)]})
    d2 = d1.assign(close=[100 + (i % 5) * (-1) ** i for i in range(40)])
    s = strat(open_trades=[trade()])
    s.dp = SimpleNamespace(get_pair_dataframe=lambda p, tf: d1 if p.startswith("BTC") else d2,
                           current_whitelist=lambda: ["BTC/USDT:USDT", "ETH/USDT:USDT"],
                           runmode=SimpleNamespace(value="dry_run"))
    amount, _ = sized_amount(s, 100.0, 99.0)
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", amount, 100.0, "GTC", NOW, tag(99.0), "long") is expect


def test_correlation_check_rejects_when_daily_data_is_missing():
    """审查 P1：日线缺失时旧实现直接跳过相关性检查。现在拒绝，并在 informative_pairs 里固定订阅日线。"""
    s = strat(open_trades=[_open(stop_px=99.0)])
    s.dp = SimpleNamespace(get_pair_dataframe=lambda p, tf: pd.DataFrame(), current_whitelist=lambda: ["BTC/USDT:USDT"],
                           runmode=SimpleNamespace(value="dry_run"))
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100.0, "GTC", NOW, tag(99.0), "long") is False
    assert s.informative_pairs() == [("BTC/USDT:USDT", "1d")]
    same = pd.DataFrame({"date": pd.date_range("2026-09-01", periods=40, freq="D", tz="UTC"),
                         "close": [100 * 1.01 ** i * (1 + 0.02 * (i % 2)) for i in range(40)]})
    s.dp = SimpleNamespace(get_pair_dataframe=lambda p, tf: same, runmode=SimpleNamespace(value="dry_run"))
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100.0, "GTC", NOW, tag(99.0), "long") is False  # 相关 = 1


def test_take_profit_tolerates_contract_rounding():
    s, t = tp_trade()
    t.amount = 10.0 * (1 - 0.4987)                       # 交易所把 0.01855 取整成 0.0185：只差一点也算完成
    assert adjust(s, t, 102.5) is None and t.cd["tp_done"] == ["tp_2R"]
