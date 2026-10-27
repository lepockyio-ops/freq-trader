"""s03ft 命令行测试：只执行白名单命令、系统命令与 sudoers 完全一致、每日汇总、事件新鲜度。"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def cli(monkeypatch, tmp_path):
    monkeypatch.setenv("S03FT_DRY_RUN", "1")
    monkeypatch.setenv("S03FT_INBOX", str(tmp_path / "inbox"))
    monkeypatch.setenv("S03_EVENTS_FILE", str(tmp_path / "events.json"))
    import importlib

    import s03ft.cli as c

    importlib.reload(c)
    return c, tmp_path


def _sudoers_cmds() -> set[str]:
    text = re.sub(r"#.*", "", (ROOT / "deploy" / "sudoers" / "s03-openclaw").read_text("utf-8"))
    return {c.strip() for c in re.findall(r"(/usr/bin/(?:systemctl|journalctl) [^,\n\\]+)", text)}


def test_every_privileged_command_is_in_sudoers(cli):
    c, _ = cli
    allowed = _sudoers_cmds()
    cmds = [[c.SUDO, "-n", c.SYSTEMCTL, a, c.UNITS[n]] for a in ("start", "stop", "restart") for n in c.UNITS]
    cmds += [[c.SUDO, "-n", c.SYSTEMCTL, "start", j] for j in c.JOBS.values()]
    cmds += [[c.SUDO, "-n", c.JOURNALCTL, "-u", u, "-n", "200", "--no-pager"] for u in c.UNITS.values()]
    for cmd in cmds:
        assert " ".join(cmd[2:]) in allowed, cmd
    assert len(cmds) == len(allowed)          # CLI 发出的提权命令与 sudoers 一一对应


def test_svc_rejects_unknown_names(cli):
    c, _ = cli
    with pytest.raises(SystemExit):
        c.main(["svc", "start", "freqtrade; rm -rf /"])
    with pytest.raises(SystemExit):
        c.main(["svc", "disable", "freqtrade"])


def test_gate_pause_requires_reason(cli, capsys):
    c, _ = cli
    assert c.main(["gate", "pause"]) == 1
    assert "必须写明" in capsys.readouterr().out
    assert c.main(["gate", "pause", "资金费率异常"]) == 0


def test_events_freshness(cli):
    c, tmp = cli
    f = tmp / "events.json"
    assert not c.events_info(f)["ok"]
    f.write_text(json.dumps({"ok": True, "sources": {"fed": {"ok": True, "issues": []}},
                             "events": [{"time_utc": "2099-01-01T00:00:00Z", "kind": "US_CPI"}], "changes": []}))
    assert c.events_info(f)["ok"] and c.events_info(f)["next_events"][0]["kind"] == "US_CPI"
    os.utime(f, (time.time() - 80 * 3600,) * 2)
    assert not c.events_info(f)["ok"]


def test_daily_summary_reports_problems(cli, capsys):
    c, tmp = cli
    (tmp / "inbox").mkdir()
    (tmp / "inbox" / "metrics.json").write_text(json.dumps({"total": {"n": 3}, "by_coin": {}}))
    (tmp / "inbox" / "alerts.jsonl").write_text(json.dumps(
        {"kind": "alert", "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "severity": "warning",
         "rule": "shock_1m"}) + "\n")
    rc = c.main(["daily"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and "事件文件不新鲜或有来源异常" in out["problems"]
    assert out["metrics"]["total"] == {"n": 3} and out["alerts_24h"] == {"warning:shock_1m": 1}


def test_no_forbidden_commands_in_cli():
    src = (ROOT / "s03ft" / "cli.py").read_text("utf-8")
    for bad in ("forceexit", "forceenter", "/start", "dry_run\"] =", "reset-failed", "daemon-reload"):
        assert bad not in src, bad


def test_strategy_info_detects_blank_template(cli, monkeypatch):
    c, _ = cli
    monkeypatch.delenv("FW_STRATEGY", raising=False)
    info = c.strategy_info(research=True)
    assert info["name"] == "MyStrategy" and info["is_template"] and info["file"].endswith("MyStrategy.py")
    monkeypatch.setenv("FW_STRATEGY", "SmaCrossExample")
    info = c.strategy_info(research=True)
    assert not info["is_template"] and info["file"].endswith("SmaCrossExample.py")
