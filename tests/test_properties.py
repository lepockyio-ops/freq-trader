"""随机性质测试（Hypothesis）：策略文件里的纯函数必须永远满足手册第 5、7 节的规则。"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "user_data", "strategies"))
import S03Strategy as S  # noqa: E402


@settings(max_examples=300, deadline=None)
@given(seed=st.integers(0, 10_000), vol=st.floats(0.0005, 0.02), mode=st.sampled_from(["skip", "floor"]))
def test_state_machine_stops_always_within_limits(seed, vol, mode):
    S.P["stop_min_mode"] = mode
    rng = np.random.default_rng(seed)
    n = 96
    c = 100 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, vol, n))
    lo = np.minimum(o, c) * (1 - rng.uniform(0, vol, n))
    idx = pd.date_range("2026-10-26", periods=n, freq="15min", tz="UTC")
    df = pd.DataFrame({"open": o, "high": h, "low": lo, "close": c}, index=idx)
    df = df.assign(eligible=1, zone_lo=float(np.quantile(lo, 0.2)), zone_hi=float(np.quantile(c, 0.5)))
    sm = S.run_15m_state_machine(df)
    ok = sm["s03_trigger"].notna()
    dist = (sm.loc[ok, "s03_trigger"] - sm.loc[ok, "s03_stop"]) / sm.loc[ok, "s03_trigger"] * 100
    assert ((dist >= S.P["stop_min_pct"] - 1e-9) & (dist <= S.P["stop_max_pct"] + 1e-9)).all()
    S.P["stop_min_mode"] = "skip"


@settings(max_examples=2000, deadline=None)
@given(stop=st.floats(0.008, 0.03), mx=st.sampled_from([5, 10, 20, 50, 75, 125]),
       mm=st.sampled_from([0.004, 0.005, 0.01, 0.025]))
def test_liquidation_at_least_3x_stop(stop, mx, mm):
    lev = S.isolated_leverage(stop, mx, mm)
    assert 1 <= lev <= min(mx, S.P["max_position_leverage"])
    if lev > 1:
        assert (1 / lev - mm) / (1 - mm) >= S.P["liq_to_stop_multiple"] * stop - 1e-12


@settings(max_examples=2000, deadline=None)
@given(equity=st.floats(1_000, 1_000_000), stop=st.floats(0.008, 0.03), lev=st.integers(1, 30))
def test_risk_budget_used_but_never_exceeded(equity, stop, lev):
    stake = S.stake_for(equity, stop, lev)
    notional = stake * lev
    budget = equity * S.P["risk_per_trade"]
    total = notional * (stop + S.P["round_trip_cost"] + min(S.P["funding_est"], S.P["funding_cap_frac_of_stop"] * stop))
    assert total <= budget * (1 + 1e-9)
    if notional < equity * (1 - 1e-9):
        assert notional * stop >= 0.7 * budget
