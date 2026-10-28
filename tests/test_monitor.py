"""freqtrade 版监控测试：Binance 消息解析、freqtrade 持仓转换、配置加载。核心阈值逻辑与 HL 版共用（见 HL 版 tests/test_alerts.py）。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "monitor"))

from binance_monitor import BinanceMonitor, MonitorConfig, load_config, positions_from_ft, symbol  # noqa: E402


def _kline(sym, iv, t, o, h, l, c, closed):
    return json.dumps({"stream": f"{sym.lower()}@kline_{iv}", "data": {"e": "kline", "s": sym, "k": {
        "t": t, "i": iv, "o": str(o), "h": str(h), "l": str(l), "c": str(c), "x": closed}}})


def test_symbol():
    assert symbol("BTC/USDT:USDT") == "BTCUSDT"


def test_only_closed_klines_count(tmp_path):
    m = BinanceMonitor(MonitorConfig(journal_path=str(tmp_path / "a.jsonl")))
    m.on_message(_kline("BTCUSDT", "1m", 0, 1, 1, 1, 100, False))
    assert m.engine.stats["BTC"].last_1m_open_ts is None and m.engine.stats["BTC"].last_price == 100
    m.on_message(_kline("BTCUSDT", "1m", 0, 1, 1, 1, 100, True))
    assert m.engine.stats["BTC"].last_1m_open_ts == 0


def test_shock_alert_written_to_journal(tmp_path):
    m = BinanceMonitor(MonitorConfig(journal_path=str(tmp_path / "a.jsonl")))
    px = 100.0
    for i in range(300):
        px *= 1.0005 if i % 2 else 0.9995
        m.on_message(_kline("ETHUSDT", "1m", i * 60_000, px, px, px, px, True))
    m.on_message(_kline("ETHUSDT", "1m", 300 * 60_000, px, px, px, px * 1.01, True))
    recs = [r for r in m.journal.read() if r["kind"] == "alert"]
    assert recs and recs[0]["rule"] == "shock_1m" and recs[0]["to_openclaw"] is False   # OpenClaw 默认关闭


def test_funding_recorded_on_settlement(tmp_path):
    m = BinanceMonitor(MonitorConfig(journal_path=str(tmp_path / "a.jsonl")))
    m.engine.seed_funding("SOL", [0.0001] * 90)

    def mp(r, T):
        return json.dumps({"data": {"e": "markPriceUpdate", "s": "SOLUSDT", "p": "100", "i": "100", "r": str(r), "T": T}})

    m.on_message(mp(0.0001, 1000))
    m.on_message(mp(0.002, 1000))      # 结算前的预测费率
    m.on_message(mp(0.0001, 2000))     # 结算：应记录 0.002（上一条）
    assert list(m.engine.stats["SOL"].funding_hist)[-1] == 0.002
    assert any(r["rule"] == "funding_extreme" for r in m.journal.read() if r["kind"] == "alert")


def test_positions_from_freqtrade_status():
    trades = [{"pair": "SOL/USDT:USDT", "is_open": True, "is_short": False, "open_rate": 100.0,
               "initial_stop_loss_abs": 97.2, "stop_loss_abs": 97.2, "open_fill_timestamp": 1_800_000_000_000,
               "orders": [{"ft_order_side": "buy", "is_open": False},
                          {"ft_order_side": "stoploss", "is_open": True}]},
              {"pair": "BTC/USDT:USDT", "is_open": True, "is_short": False, "open_rate": 80000.0,
               "initial_stop_loss_abs": 78000.0, "stop_loss_abs": 78000.0, "orders": []}]
    pos = positions_from_ft(trades)
    assert [(p.coin, p.has_exchange_stop) for p in pos] == [("SOL", True), ("BTC", False)]
    assert positions_from_ft(trades, require_exchange_stop=False)[1].has_exchange_stop


def test_example_config_loads():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = load_config(os.path.join(root, "monitor", "monitor.example.toml"))
    assert cfg.alerts.funding_abs_floor == 0.0003 and cfg.openclaw.enabled is False
    assert cfg.openclaw.venue.startswith("Binance") and len(cfg.pairs) == 5


def test_freqtrade_webhook_events(tmp_path):
    m = BinanceMonitor(MonitorConfig(journal_path=str(tmp_path / "a.jsonl"), webhook_port=0))
    a1 = m.on_ft_event({"type": "entry_fill", "pair": "SOL/USDT:USDT", "open_rate": "100.1"})
    a2 = m.on_ft_event({"type": "exit_fill", "pair": "SOL/USDT:USDT", "exit_reason": "stoploss_on_exchange",
                        "profit_amount": "-48.2"})
    a3 = m.on_ft_event({"type": "exit_fill", "pair": "BTC/USDT:USDT", "exit_reason": "liquidation", "profit_amount": "-90"})
    assert (a1.severity, a2.severity, a3.severity) == ("info", "warning", "critical")
    assert m.on_ft_event({"type": "entry"}) is None
    recs = [r for r in m.journal.read() if r["kind"] == "alert"]
    assert [r["rule"] for r in recs] == ["ft_entry_fill", "ft_exit_fill_stoploss_on_exchange", "ft_exit_fill_liquidation"]
    assert all(r["to_openclaw"] is False for r in recs)        # OpenClaw 默认关闭


def test_webhook_server_roundtrip(tmp_path):
    import socket
    import urllib.request

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    m = BinanceMonitor(MonitorConfig(journal_path=str(tmp_path / "a.jsonl"), webhook_port=port))
    m.start_webhook_server()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/ft-webhook", method="POST",
                                 data=json.dumps({"type": "status", "status": "Error: exchange timeout"}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
    rec = [r for r in m.journal.read() if r["kind"] == "alert"][0]
    assert rec["rule"] == "ft_status" and rec["severity"] == "warning"
