"""框架测试：空白模板不入场；示例策略的信号带合规止损；freqtrade 实际回测（有本地数据时）。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "user_data" / "strategies"))
sys.path.insert(0, str(ROOT / "examples" / "strategies"))
import framework_base as F  # noqa: E402
from MyStrategy import MyStrategy  # noqa: E402


def _df(n=300, seed=3):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.004, n)))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"date": pd.date_range("2026-10-01", periods=n, freq="15min", tz="UTC"), "open": o,
                         "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.996, "close": c,
                         "volume": 1.0})


def test_template_is_blank():
    s = MyStrategy.__new__(MyStrategy)
    assert s.is_template is True and isinstance(s, F.FrameworkStrategy)
    df = s.populate_entry_trend(s.populate_indicators(_df(), {}), {})
    assert "enter_long" not in df or df["enter_long"].fillna(0).sum() == 0
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100, "GTC", pd.Timestamp.now(tz="UTC"), "", "long") is False


def test_guarded_stop_cases():
    fw = F.FW_DEFAULTS
    assert F.guarded_stop(100, 99.5, {**fw, "stop_min_mode": "skip"}) is None
    assert abs(F.guarded_stop(100, 99.5, {**fw, "stop_min_mode": "floor"}) - 99.2) < 1e-9
    assert F.guarded_stop(100, 96, fw) is None and F.guarded_stop(100, 98, fw) == 98
    assert F.guarded_stop(100, float("nan"), fw) is None and F.guarded_stop(100, None, fw) is None


def test_example_signals_carry_stop():
    from SmaCrossExample import SmaCrossExample

    s = SmaCrossExample.__new__(SmaCrossExample)
    df = s.populate_indicators(_df(), {})
    df["trend_ok_1d"] = 1
    df = s.populate_entry_trend(df, {})
    sig = df[df["enter_long"] == 1]
    assert len(sig) > 0 and (sig["fw_stop"] < sig["close"]).all() and sig["fw_entry_cap"].notna().all()
    assert s.take_profit_legs(None) == [(2.0, 0.5, "tp_2R")]


DATA = ROOT / "user_data" / "data" / "okx" / "futures"


@pytest.mark.skipif(shutil.which("freqtrade") is None or not DATA.exists(), reason="需要 freqtrade 与本地 OKX 测试数据")
@pytest.mark.parametrize("strategy,path,expect_trades", [("MyStrategy", None, False),
                                                         ("SmaCrossExample", "examples/strategies", True)])
def test_freqtrade_backtest_smoke(tmp_path, strategy, path, expect_trades):
    cmd = ["freqtrade", "backtesting", "-c", "user_data/config-test-okx.json", "--strategy", strategy,
           "--timerange", "20261005-20261026", "-p", "SOL/USDT:USDT", "BTC/USDT:USDT", "--export", "trades",
           "--backtest-directory", str(tmp_path)]
    if path:
        cmd += ["--strategy-path", path]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env={**os.environ, "S03_EVENTS_REQUIRED": "0"})
    assert r.returncode == 0, r.stderr[-2000:]
    from freqtrade.data.btanalysis import load_backtest_data

    t = load_backtest_data(tmp_path)
    if not expect_trades:
        assert len(t) == 0
        return
    assert len(t) > 0
    init = t.groupby("trade_id").first() if "trade_id" in t else t
    # 框架止损按价格挂出（stop_loss_abs）；护栏按“计划入场价”（fw_entry_cap）检查，实际成交价不高于它，
    # 所以按成交价算的止损距离可略小于 0.8%，但不会小于 0.8% − 限价偏移 0.1%
    stop_pct = (init["open_rate"] - init["stop_loss_abs"]) / init["open_rate"] * 100
    assert ((stop_pct >= 0.7 - 1e-6) & (stop_pct <= 3.0 + 1e-6)).all()
    assert (init["initial_stop_loss_abs"] < init["stop_loss_abs"]).all()   # initial 是兜底的 −99%，不是策略止损
