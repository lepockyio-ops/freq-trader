"""研究矩阵（freqtrade 版）：把回测拆成“交易对 × 年份 × 方案”的独立任务，便于 s03-research 派生子 agent 并行；
另有复核命令供“复核子 agent”重跑比对，以及 G0 检查命令。所有数字来自 freqtrade 输出。

  python research/matrix.py plan --pairs BTC,ETH,SOL,BNB,HYPE --years 2022-2025 --variants base,stop_floor,cost2x
  python research/matrix.py run --job SOL-2023-base
  python research/matrix.py aggregate                    # → research/matrix_report.md
  python research/matrix.py verify --job SOL-2023-base   # 重跑并比对逐笔哈希
  python research/matrix.py g0 --timerange 20250101-     # lookahead-analysis + recursive-analysis
  s03ft lab matrix                                       # 统计检验：DSR、PBO、Bootstrap（docs/RESEARCH_LAB.md）

每个任务的逐笔交易另存 research/results/trades/<任务>.csv（列 job, coin, year, variant, entry_time, time, R, pnl），
R = 盈亏 ÷（入场成交数量 ×（开仓价 − stop_loss_abs）），与 monitor/review_metrics.py 同一口径。

环境变量：S03_FT_CONFIG（默认 user_data/config.json）、FW_STRATEGY（策略名，默认读配置）、FW_STRATEGY_PATH、
          S03_EVENTS_FILE（回测用 events_calendar.py --history 生成的历史事件文件）。
方案 stop_floor 只改变框架的止损护栏处理方式（S03_STOP_MIN_MODE=floor），对任何策略都适用。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path("research")
RES = ROOT / "results"
CONFIG = os.environ.get("S03_FT_CONFIG", "user_data/config.json")


def strategy_args() -> list[str]:
    """策略名：环境变量 FW_STRATEGY，否则读配置文件里的 "strategy"；FW_STRATEGY_PATH 可指向 examples/strategies。"""
    name = os.environ.get("FW_STRATEGY")
    if not name:
        try:
            name = json.loads(Path(CONFIG).read_text("utf-8")).get("strategy")
        except (OSError, ValueError):
            name = None
    args = ["--strategy", name or "MyStrategy"]
    if os.environ.get("FW_STRATEGY_PATH"):
        args += ["--strategy-path", os.environ["FW_STRATEGY_PATH"]]
    return args
VARIANTS = {
    "base": ({}, []),
    "stop_floor": ({"S03_STOP_MIN_MODE": "floor"}, []),
    "cost2x": ({}, ["--fee", "0.0019"]),          # (0.05% 手续费 × 2 + 0.03% 滑点 × 3) 每边，示例值
}


def parse_job(jid: str):
    base, year, variant = jid.split("-", 2)
    if variant not in VARIANTS:
        raise ValueError(f"无效方案：{variant}")
    return base, int(year), variant


def r_multiple(row) -> float | None:
    """逐笔 R。分批平仓后 amount 会变小，所以用入场订单的成交量；止损用 stop_loss_abs（initial_stop_loss_abs 是兜底的 −99%）。"""
    orders = row.get("orders") if hasattr(row, "get") else None
    amt = sum(float(o.get("amount") or 0) for o in (orders or []) if o.get("ft_is_entry")) or float(row["amount"])
    risk = amt * (float(row["open_rate"]) - float(row["stop_loss_abs"]))
    return float(row["profit_abs"]) / risk if risk > 0 else None


def trades_frame(jid: str, trades) -> "pd.DataFrame":
    import pandas as pd

    base, year, variant = parse_job(jid)
    cols = ["job", "coin", "year", "variant", "entry_time", "time", "R", "pnl"]
    if trades is None or len(trades) == 0:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame({"job": jid, "coin": base, "year": year, "variant": variant,
                        "entry_time": trades["open_date"].values, "time": trades["close_date"].values,
                        "R": [r_multiple(r) for _, r in trades.iterrows()], "pnl": trades["profit_abs"].values})
    return out[cols]


def freqtrade_bin() -> str:
    return shutil.which("freqtrade") or "freqtrade"


def backtest(jid: str, outdir: Path) -> dict:
    base, year, variant = parse_job(jid)
    env_extra, args = VARIANTS[variant]
    env = {**os.environ, "S03_EVENTS_REQUIRED": "0", "S03_NEWS_FILE": "", **env_extra}   # 回测不应用新闻守卫（没有历史）
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [freqtrade_bin(), "backtesting", "-c", CONFIG, *strategy_args(),
           "--timerange", f"{year}0101-{year + 1}0101", "-p", f"{base}/USDT:USDT", "--enable-protections",
           "--export", "trades", "--backtest-directory", str(outdir), *args]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        return {"job": jid, "status": "error", "cmd": " ".join(cmd), "stderr_tail": r.stderr[-1500:]}
    from freqtrade.data.btanalysis import load_backtest_data

    trades = load_backtest_data(outdir)
    cols = [c for c in ("pair", "open_date", "close_date", "open_rate", "close_rate", "amount", "exit_reason")
            if c in trades.columns]
    csv = trades[cols].to_csv(index=False).encode() if len(trades) else b""
    n = int(len(trades))
    prof = float(trades["profit_abs"].sum()) if n else 0.0
    win = float((trades["profit_abs"] > 0).mean()) if n else None
    return {"_trades": trades_frame(jid, trades), "job": jid, "status": "ok", "pair": base, "year": year, "variant": variant, "cmd": " ".join(cmd),
            "n_trades": n, "profit_abs": round(prof, 4), "win_rate": None if win is None else round(win, 4),
            "trades_hash": hashlib.sha256(csv).hexdigest()[:16]}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--pairs", default="BTC,ETH,SOL,BNB,HYPE")
    p.add_argument("--years", default="2022-2025")
    p.add_argument("--variants", default="base,stop_floor,cost2x")
    for n in ("run", "verify"):
        sub.add_parser(n).add_argument("--job", required=True)
    sub.add_parser("aggregate")
    g = sub.add_parser("g0")
    g.add_argument("--timerange", default="20250101-")
    a = ap.parse_args()

    if a.cmd == "plan":
        y0, y1 = (int(x) for x in a.years.split("-"))
        jobs = [f"{b}-{y}-{v}" for b in a.pairs.split(",") for y in range(y0, y1 + 1) for v in a.variants.split(",")]
        for j in jobs:
            parse_job(j)
        ROOT.mkdir(exist_ok=True)
        (ROOT / "jobs.json").write_text(json.dumps(jobs, indent=1), "utf-8")
        print(f"{len(jobs)} 个任务 → research/jobs.json")
        return 0
    if a.cmd == "run":
        out = backtest(a.job, RES / a.job)
        tf = out.pop("_trades", None)
        if tf is not None:
            (RES / "trades").mkdir(parents=True, exist_ok=True)
            tf.to_csv(RES / "trades" / f"{a.job}.csv", index=False)
        (RES / f"{a.job}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
        print(json.dumps({k: out.get(k) for k in ("job", "status", "n_trades", "trades_hash")}, ensure_ascii=False))
        return 0 if out["status"] == "ok" else 1
    if a.cmd == "verify":
        stored = json.loads((RES / f"{a.job}.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as d:
            fresh = backtest(a.job, Path(d))
            fresh.pop("_trades", None)
        same = stored.get("trades_hash") == fresh.get("trades_hash")
        print(json.dumps({"job": a.job, "match": same, "stored": stored.get("trades_hash"),
                          "fresh": fresh.get("trades_hash")}, ensure_ascii=False))
        return 0 if same else 1
    if a.cmd == "aggregate":
        rows = [json.loads(p.read_text("utf-8")) for p in sorted(RES.glob("*.json"))]
        ok = [r for r in rows if r.get("status") == "ok"]
        lines = ["# 研究矩阵汇总（freqtrade，程序生成）", "", f"任务 {len(rows)} 个，成功 {len(ok)} 个。", "",
                 "| 任务 | 笔数 | 盈亏（USDT） | 胜率 | 逐笔哈希 |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {r['job']} | {r['n_trades']} | {r['profit_abs']} | {r['win_rate']} | {r['trades_hash']} |" for r in ok]
        base_n = sum(r["n_trades"] for r in ok if r["variant"] == "base")
        lines += ["", f"base 方案合计 {base_n} 笔；G1 要求 ≥ 100 笔，< 30 笔为“证据不足”。"]
        (ROOT / "matrix_report.md").write_text("\n".join(lines), "utf-8")
        print("\n".join(lines))
        return 0
    if a.cmd == "g0":
        env = {**os.environ, "S03_EVENTS_REQUIRED": "0"}
        rc = 0
        for sub_cmd in (["lookahead-analysis"], ["recursive-analysis", "--startup-candle", "199", "400", "999"]):
            cmd = [freqtrade_bin(), *sub_cmd, "-c", CONFIG, *strategy_args(), "--timerange", a.timerange]
            print("$", " ".join(cmd), flush=True)
            rc |= subprocess.run(cmd, env=env).returncode
        return rc
    return 2


if __name__ == "__main__":
    sys.exit(main())
