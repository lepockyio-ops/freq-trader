"""FrameworkStrategy 的 BlockBeats 新闻事件守卫：按交易对封禁、只收紧、过期忽略、回测不应用；与 fwresearch 同口径。"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "user_data" / "strategies"))
sys.path.insert(0, str(ROOT))

from fwresearch import blockbeats as bb  # noqa: E402
from framework_base import FrameworkStrategy  # noqa: E402
from MyStrategy import MyStrategy  # noqa: E402

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _blk(target, cat="security", hours=24):
    return {"target": target, "category": cat, "until_utc": (NOW + timedelta(hours=hours)).isoformat(),
            "news_id": 1, "title": "t", "link": "", "received_at": NOW.isoformat()}


def _strat(tmp_path, blocks, generated=NOW, runmode="dry_run"):
    f = tmp_path / f"nb-{runmode}-{generated.timestamp()}-{len(blocks)}.json"
    bb.write_guard(f, blocks, generated, ok=True)
    s = MyStrategy.__new__(MyStrategy)
    s.framework_params = {"news_file": str(f)}

    class _RM:
        value = runmode

    class _DP:
        runmode = _RM()

    s.dp = _DP()
    FrameworkStrategy._news_cache = (None, None)
    return s, str(f)


def test_pair_and_venue_blocks(tmp_path):
    t = NOW + timedelta(minutes=5)
    s, _ = _strat(tmp_path, [_blk("SOL")])
    assert s._news_block("SOL/USDT:USDT", t) == "security:SOL"
    assert s._news_block("BTC/USDT:USDT", t) is None
    s2, _ = _strat(tmp_path, [_blk("@binance", "withdrawal_halt")])
    assert s2._news_block("BTC/USDT:USDT", t) == "withdrawal_halt:@binance"
    s3, _ = _strat(tmp_path, [_blk("@hyperliquid", "withdrawal_halt")])      # 别的交易所出事：不影响
    assert s3._news_block("BTC/USDT:USDT", t) is None


def test_stale_disabled_and_backtest_never_block(tmp_path):
    s, _ = _strat(tmp_path, [_blk("SOL")], generated=NOW - timedelta(hours=2))
    assert s._news_block("SOL/USDT:USDT", NOW) is None
    s2, _ = _strat(tmp_path, [_blk("SOL")], runmode="backtest")
    assert s2._news_block("SOL/USDT:USDT", NOW + timedelta(minutes=1)) is None
    s3 = MyStrategy.__new__(MyStrategy)
    s3.framework_params = {"news_file": ""}
    assert s3._news_block("SOL/USDT:USDT", NOW) is None


def test_same_semantics_as_fwresearch(tmp_path):
    blocks = [_blk("SOL"), _blk("@binance", "outage", 6), _blk("ETH", "unlock", 1)]
    s, f = _strat(tmp_path, blocks)
    for minutes in (5, 61, 400):
        t = NOW + timedelta(minutes=minutes)
        live, _ = bb.load_blocks(f, t, max_age_min=10_000)
        for coin in ("SOL", "ETH", "BTC"):
            ref = bb.blocked(coin, "binance", live)
            s.framework_params = {**s.framework_params, "news_max_age_min": 10_000}
            got = s._news_block(f"{coin}/USDT:USDT", t)
            assert (got is None) == (ref is None), (coin, minutes)
