import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "monitor"))
from review_metrics import compute, from_freqtrade  # noqa: E402


def test_r_uses_entry_fill_amount_after_partial_exits():
    t = {"pair": "SOL/USDT:USDT", "is_open": False, "open_rate": 100.0, "stop_loss_abs": 98.0, "amount": 5.0,
         "profit_abs": 60.0, "exit_reason": "ema9_break", "close_date": "2026-10-03",
         "orders": [{"ft_is_entry": True, "filled": 10.0}, {"ft_is_entry": False, "filled": 5.0}]}
    rows = from_freqtrade([t, {**t, "is_open": True}])
    assert len(rows) == 1 and rows[0]["r"] == 3.0          # 60 / (10 × 2)
    assert compute(rows)["by_coin"]["SOL"]["expectancy_r"] == 3.0


def test_total_streak_uses_account_close_order():
    """审查 P2：合计先按币分组再拼接，真实 2 连亏被报告成 1。"""
    trs = [{"coin": "BTC", "close_time": "2026-01-01", "r": -1, "pnl": -1, "exit_reason": "sl"},
           {"coin": "ETH", "close_time": "2026-01-02", "r": -1, "pnl": -1, "exit_reason": "sl"},
           {"coin": "BTC", "close_time": "2026-01-03", "r": 1, "pnl": 1, "exit_reason": "tp"},
           {"coin": "ETH", "close_time": "2026-01-04", "r": 1, "pnl": 1, "exit_reason": "tp"}]
    m = compute(trs)
    assert m["total"]["max_consecutive_losses"] == 2 and m["by_coin"]["BTC"]["max_consecutive_losses"] == 1
