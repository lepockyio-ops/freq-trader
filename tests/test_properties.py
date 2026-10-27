"""随机性质测试（Hypothesis）：框架护栏对任何策略都必须永远成立。"""
from __future__ import annotations

import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "user_data", "strategies"))
import framework_base as F  # noqa: E402


@settings(max_examples=3000, deadline=None)
@given(entry=st.floats(1.0, 200_000.0), drop=st.floats(0.0, 0.06), mode=st.sampled_from(["skip", "floor"]))
def test_stop_guard_always_within_limits(entry, drop, mode):
    fw = {**F.FW_DEFAULTS, "stop_min_mode": mode}
    stop = F.guarded_stop(entry, entry * (1 - drop), fw)
    if stop is not None:
        dist = (entry - stop) / entry * 100
        assert fw["stop_min_pct"] - 1e-9 <= dist <= fw["stop_max_pct"] + 1e-9


@settings(max_examples=2000, deadline=None)
@given(stop=st.floats(0.008, 0.03), mx=st.sampled_from([5, 10, 20, 50, 75, 125]),
       mm=st.sampled_from([0.004, 0.005, 0.01, 0.025]))
def test_liquidation_at_least_3x_stop(stop, mx, mm):
    fw = F.FW_DEFAULTS
    lev = F.isolated_leverage(stop, mx, mm)
    assert 1 <= lev <= min(mx, fw["max_position_leverage"])
    if lev > 1:
        assert (1 / lev - mm) / (1 - mm) >= fw["liq_to_stop_multiple"] * stop - 1e-12


@settings(max_examples=2000, deadline=None)
@given(equity=st.floats(1_000, 1_000_000), stop=st.floats(0.008, 0.03), lev=st.integers(1, 30))
def test_risk_budget_used_but_never_exceeded(equity, stop, lev):
    fw = F.FW_DEFAULTS
    notional = F.stake_for(equity, stop, lev) * lev
    budget = equity * fw["risk_per_trade"]
    total = notional * (stop + fw["round_trip_cost"] + min(fw["funding_est"], fw["funding_cap_frac_of_stop"] * stop))
    assert total <= budget * (1 + 1e-9)
    if notional < equity * (1 - 1e-9):
        assert notional * stop >= 0.7 * budget
