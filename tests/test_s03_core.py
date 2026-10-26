"""S03Strategy 纯函数测试：无未来信息、状态机、杠杆公式。运行：pytest -q tests/"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "user_data", "strategies"))

import S03Strategy as S  # noqa: E402


def _daily(n=400, seed=3):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.03, n)))
    idx = pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"open": c, "high": c * 1.02, "low": c * 0.98, "close": c,
                         "volume": rng.uniform(1e6, 2e6, n)}, index=idx)


def test_daily_columns_have_no_lookahead():
    """截断历史计算出的每一行，必须与完整历史上同一行的结果完全相同。"""
    d = _daily()
    full = S.daily_s03_columns(d)
    cols = ["eligible", "zone_lo", "zone_hi", "retests", "r1", "r4", "r5", "r6", "notex", "fresh"]
    for cut in (220, 260, 300, 350):
        part = S.daily_s03_columns(d.iloc[:cut])
        a, b = part[cols].iloc[-1], full[cols].iloc[cut - 1]
        for c in cols:
            assert (np.isnan(a[c]) and np.isnan(b[c])) or np.isclose(a[c], b[c], rtol=1e-9), (cut, c, a[c], b[c])


def _bars(rows, zone=(99.0, 100.0), eligible=1):
    idx = pd.date_range("2026-10-26 00:00", periods=len(rows), freq="15min", tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    return df.assign(eligible=eligible, zone_lo=zone[0], zone_hi=zone[1])


def test_state_machine_matches_native_sequence():
    rows = [[101, 101.5, 100.8, 101.2],      # 未进区
            [100.6, 100.7, 99.6, 99.8],      # 进区收阴 → 布防
            [99.8, 100.4, 99.5, 100.3],      # 收阳 → 确认，触发价 100.4
            [100.3, 100.35, 99.9, 100.1],    # 触发价跟随 100.35
            [100, 100, 97.5, 97.8]]          # 跌穿支撑 → 回到等待
    sm = S.run_15m_state_machine(_bars(rows))
    assert list(sm["s03_phase"]) == [0, 1, 2, 2, 0]
    assert sm["s03_trigger"].iloc[2] == 100.4 and sm["s03_trigger"].iloc[3] == 100.35
    assert np.isnan(sm["s03_trigger"].iloc[4])
    assert sm["s03_stop"].iloc[2] < 99.5


def test_stop_wider_than_3pct_gives_no_trigger():
    rows = [[100.6, 100.7, 99.6, 99.8], [96.0, 100.5, 96.0, 100.3]]
    sm = S.run_15m_state_machine(_bars(rows, zone=(96.5, 100.0)))
    assert sm["s03_phase"].iloc[-1] == 2 and np.isnan(sm["s03_trigger"].iloc[-1])


@pytest.mark.parametrize("stop,mx,mm,expect", [(0.028, 20, 0.025, 9), (0.03, 125, 0.004, 10), (0.03, 5, 0.004, 5)])
def test_isolated_leverage(stop, mx, mm, expect):
    lev = S.isolated_leverage(stop, mx, mm)
    assert lev == expect
    assert (1 / lev - mm) / (1 - mm) >= 3 * stop - 1e-9 or lev == mx
