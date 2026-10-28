"""freq-trader 的研究前端接入：逐笔 R 口径、s03ft lab 的权限边界、默认数据目录。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))

import matrix  # noqa: E402


def test_r_multiple_uses_entry_fill_and_stop_loss_abs():
    row = pd.Series({"open_rate": 100.0, "stop_loss_abs": 98.0, "amount": 5.0, "profit_abs": 20.0,
                     "orders": [{"amount": 10.0, "ft_is_entry": True}, {"amount": 5.0, "ft_is_entry": False}]})
    assert matrix.r_multiple(row) == 1.0                       # 风险 = 10 × 2 = 20，盈利 20 → 1R（分批后 amount 只剩 5）
    row["stop_loss_abs"] = 100.0
    assert matrix.r_multiple(row) is None


def test_trades_frame_schema():
    df = matrix.trades_frame("SOL-2024-base", None)
    assert list(df.columns) == ["job", "coin", "year", "variant", "entry_time", "time", "R", "pnl"]


def _s03ft(*args, env=None):
    return subprocess.run([sys.executable, "-m", "s03ft.cli", *args], cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=300)


def test_agent_cannot_approve_ideas_through_s03ft():
    r = _s03ft("lab", "idea", "move", "--id", "I-20260101-001", "--to", "approved", "--reason", "自批", "--by", "human")
    assert r.returncode == 2 and "只能由人" in r.stdout
    r = _s03ft("lab", "idea", "move", "--id", "I-20260101-001", "--to", "approved", "--reason", "x", "--approver", "a")
    assert r.returncode == 2


@pytest.mark.skipif(not (ROOT / "user_data/data/okx/futures/BTC_USDT_USDT-1d-futures.feather").exists(),
                    reason="需要本地 OKX 测试数据（freqtrade download-data 生成，不随仓库提交）")
def test_lab_factors_reads_freqtrade_data_dir(tmp_path):
    r = _s03ft("lab", "factors", "--coins", "BTC,SOL", "--ids", "q158_roc20,q158_kmid", "--out-dir", str(tmp_path),
               env={"S03_FT_CONFIG": "user_data/config-test-okx.json"})
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["mode"] == "time_series" and Path(out["markdown"]).exists()
