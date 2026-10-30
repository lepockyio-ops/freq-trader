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


def test_status_reports_news_guard_and_lab_news(tmp_path):
    r = _s03ft("lab", "news", "blocks", "--guard-out", str(tmp_path / "none.json"))
    assert r.returncode == 0 and json.loads(r.stdout)["status"] == "disabled"
    sys.path.insert(0, str(ROOT))
    from s03ft import cli

    assert cli.news_info(tmp_path / "none.json")["status"] == "disabled"


# ------------------------------------------------------------------ v2.7：必需压力方案随策略声明
def test_required_stress_follows_the_strategy(tmp_path, monkeypatch):
    sdir = tmp_path / "strats"
    sdir.mkdir()
    (sdir / "MarketEntry.py").write_text(
        "from framework_base import FrameworkStrategy\n"
        "class MarketEntry(FrameworkStrategy):\n"
        "    timeframe = '15m'\n"
        "    research_stress = ('cost2x', 'stopslip')     # 市价入场：不需要 delay1\n"
        "    research_stop_slip = 0.003\n", "utf-8")
    (sdir / "Bad.py").write_text(
        "from framework_base import FrameworkStrategy\n"
        "class Bad(FrameworkStrategy):\n"
        "    research_stress = ('cost2x', 'moon')\n", "utf-8")
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("FW_STRATEGY_PATH", str(sdir))
    monkeypatch.setenv("FW_STRATEGY", "MarketEntry")
    req = matrix.strategy_requirements()
    assert req["required_stress"] == ["cost2x", "stopslip"] and req["stop_slip"] == 0.003
    monkeypatch.setenv("FW_STRATEGY", "Bad")
    with pytest.raises(SystemExit, match="moon"):
        matrix.strategy_requirements()
    monkeypatch.setenv("FW_STRATEGY_PATH", str(ROOT / "examples" / "strategies"))
    monkeypatch.setenv("FW_STRATEGY", "SmaCrossExample")
    assert matrix.strategy_requirements()["required_stress"] == ["cost2x", "delay1", "stopslip"]   # 框架默认


def test_portfolio_failure_replaces_latest(tmp_path, monkeypatch):
    """审查 v2.6 P1-4：失败的运行也要成为 latest（状态 error），旧的成功结果不能再被读到。"""
    monkeypatch.setattr(matrix, "PORTFOLIO", tmp_path / "portfolio")
    monkeypatch.setattr(matrix, "run_freqtrade", lambda v, tr, p, o: ({"status": "error", "stderr_tail": "boom"}, None))
    res = matrix.portfolio("base", "20240101-", {"id": "fp"})
    latest = json.loads((tmp_path / "portfolio" / "base" / "latest.json").read_text("utf-8"))
    assert res["status"] == "error" and latest["status"] == "error" and "csv" not in latest
