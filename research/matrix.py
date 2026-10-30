"""研究矩阵与组合回测（freqtrade 版）。所有数字来自 freqtrade 输出。

  python research/matrix.py plan --pairs BTC,ETH,SOL,BNB,HYPE --years 2022-2025 --variants base,stop_floor,cost2x,delay1
  python research/matrix.py run --job SOL-2023-base
  python research/matrix.py aggregate                    # → research/matrix_report.md（只汇总清单里、当前版本、成功的任务）
  python research/matrix.py verify --job SOL-2023-base   # 重跑并比对逐笔哈希（含盈亏、费用、资金费、止损、订单）
  python research/matrix.py portfolio --variant base --timerange 20220101-   # 五币同时、连续时间、共享余额（主证据）
  python research/matrix.py g0 --timerange 20250101-     # lookahead-analysis + recursive-analysis
  s03ft lab matrix --holdout-start 2025-01-01            # 统计检验（docs/RESEARCH_LAB.md）

两种回测的用途不同（v2.6）：
- 研究矩阵：单币 × 单年，便于并行和拆解“哪个币、哪一年”贡献了结果。它测不到组合约束（同时持仓数、总风险、相关性、
  共享余额），年末会强制平仓，合并后的逐笔记录也不是同一个账户能走出的路径——只作诊断。
- 组合回测（portfolio）：白名单全部交易对、连续时间、一个账户。上线证据以它为准。

可复核性（v2.6）：
- plan 写入任务清单 research/jobs.json 和“版本指纹”（框架基类、策略文件、配置、本脚本、历史事件文件、数据文件清单、
  相关环境变量的哈希）。run 先删掉这个任务的旧结果再跑，并记录当时的指纹；aggregate 与 lab matrix 只用清单里、
  状态成功、指纹一致的任务（旧实验和失败重跑的残留不会混进来）。
- freqtrade 的回测缓存只看配置、策略参数和策略文件，不包含框架基类和外部文件，所以一律加 --cache none。

方案（只改变一个因素）：
  base        当前参数（固定 S03_STOP_MIN_MODE=skip，不继承外部环境变量）
  stop_floor  止损太近时改为下限，而不是放弃
  cost2x      每边手续费 0.19%（0.05% × 2 + 滑点 0.03% × 3 折算成手续费，示例值）
  delay1      入场信号推迟一根 K 线（只在回测里生效），检验对执行延迟的敏感度
未实现（freqtrade 回测不支持，结论里要写明）：止损单的额外滑点与跳空、资金费 × 2、未成交与排队。

环境变量：S03_FT_CONFIG（默认 user_data/config.json）、S03_FT_EXTRA_CONFIG（本机专属的附加配置，如代理证书，不进仓库）、
          FW_STRATEGY、FW_STRATEGY_PATH、S03_RESEARCH_EVENTS（历史事件文件，默认 research/events_history.json，
          由 s03ft events --history --out research/events_history.json 生成）。
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
import time
from pathlib import Path

ROOT = Path("research")
RES = ROOT / "results"
CONFIG = os.environ.get("S03_FT_CONFIG", "user_data/config.json")
EVENTS = Path(os.environ.get("S03_RESEARCH_EVENTS", str(ROOT / "events_history.json")))
VARIANTS = {
    "base": ({}, []),
    "stop_floor": ({"S03_STOP_MIN_MODE": "floor"}, []),
    "cost2x": ({}, ["--fee", "0.0019"]),
    "delay1": ({"S03_ENTRY_DELAY_BARS": "1"}, []),
}
FINGERPRINT_ENV = ("FW_STRATEGY", "FW_STRATEGY_PATH", "S03_FT_CONFIG", "S03_RESEARCH_EVENTS")


def strategy_name() -> str:
    name = os.environ.get("FW_STRATEGY")
    if not name:
        try:
            name = json.loads(Path(CONFIG).read_text("utf-8")).get("strategy")
        except (OSError, ValueError):
            name = None
    return name or "MyStrategy"


def strategy_args() -> list[str]:
    """策略名：环境变量 FW_STRATEGY，否则读配置文件里的 "strategy"；FW_STRATEGY_PATH 可指向 examples/strategies。"""
    args = ["--strategy", strategy_name()]
    if os.environ.get("FW_STRATEGY_PATH"):
        args += ["--strategy-path", os.environ["FW_STRATEGY_PATH"]]
    return args


def config_args() -> list[str]:
    extra = ["-c", os.environ["S03_FT_EXTRA_CONFIG"]] if os.environ.get("S03_FT_EXTRA_CONFIG") else []
    return ["-c", CONFIG, *extra]


def _sha(paths) -> str:
    h = hashlib.sha256()
    for p in sorted(str(x) for x in paths):
        h.update(p.encode())
        try:
            h.update(Path(p).read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:16]


def fingerprint() -> dict:
    """结果属于哪个版本：代码、配置、事件文件、数据文件清单、相关环境变量。任何一项变了，旧结果就不再被汇总。"""
    code = [Path("user_data/strategies/framework_base.py"), Path(__file__), Path(CONFIG)]
    code += list(Path("user_data/strategies").glob("*.py"))
    if os.environ.get("FW_STRATEGY_PATH"):
        code += list(Path(os.environ["FW_STRATEGY_PATH"]).glob("*.py"))
    data_dir = Path("user_data/data")
    data = hashlib.sha256()
    for f in sorted(data_dir.rglob("*.feather")) if data_dir.exists() else []:
        st = f.stat()
        data.update(f"{f}:{st.st_size}:{int(st.st_mtime)}".encode())
    parts = {"code": _sha(code), "events": _sha([EVENTS]) if EVENTS.exists() else "none",
             "data": data.hexdigest()[:16], "env": hashlib.sha256(json.dumps(
                 {k: os.environ.get(k) for k in FINGERPRINT_ENV}, sort_keys=True).encode()).hexdigest()[:16]}
    parts["id"] = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]
    return parts


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


HASH_COLS = ("pair", "open_date", "close_date", "open_rate", "close_rate", "amount", "profit_abs", "fee_open",
             "fee_close", "funding_fees", "stop_loss_abs", "exit_reason", "enter_tag", "leverage")


def trades_hash(trades) -> str:
    """复核用哈希：逐笔的价格、数量、净盈亏、费用、资金费、止损、离场原因，以及每笔的全部订单。"""
    if trades is None or len(trades) == 0:
        return hashlib.sha256(b"").hexdigest()[:16]
    cols = [c for c in HASH_COLS if c in trades.columns]
    rows = []
    for _, r in trades.iterrows():
        rec = {c: str(r[c]) for c in cols}
        rec["orders"] = [(o.get("ft_order_side"), o.get("amount"), o.get("safe_price"), o.get("ft_order_tag"))
                         for o in (r.get("orders") or [])]
        rows.append(rec)
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:16]


def freqtrade_bin() -> str:
    return shutil.which("freqtrade") or "freqtrade"


def research_env(variant: str) -> dict:
    env_extra, _ = VARIANTS[variant]
    env = {**os.environ, "S03_STOP_MIN_MODE": "skip", "S03_ENTRY_DELAY_BARS": "0",    # base 不继承外部环境
           "S03_EVENTS_REQUIRED": "0", "S03_NEWS_FILE": "",                           # 新闻守卫没有历史，回测不用
           "S03_EVENTS_FILE": str(EVENTS) if EVENTS.exists() else "", **env_extra}
    return env


def run_freqtrade(variant: str, timerange: str, pairs: list[str] | None, outdir: Path) -> tuple[dict, object]:
    _, args = VARIANTS[variant]
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [freqtrade_bin(), "backtesting", *config_args(), *strategy_args(), "--timerange", timerange,
           "--enable-protections", "--cache", "none", "--export", "trades", "--backtest-directory", str(outdir), *args]
    if pairs:
        cmd += ["-p", *pairs]
    r = subprocess.run(cmd, env=research_env(variant), capture_output=True, text=True)
    info = {"cmd": " ".join(cmd), "events_file": str(EVENTS) if EVENTS.exists() else None}
    if r.returncode != 0:
        return {**info, "status": "error", "stderr_tail": r.stderr[-1500:]}, None
    from freqtrade.data.btanalysis import load_backtest_data

    return {**info, "status": "ok"}, load_backtest_data(outdir)


def backtest(jid: str, outdir: Path) -> dict:
    base, year, variant = parse_job(jid)
    info, trades = run_freqtrade(variant, f"{year}0101-{year + 1}0101", [f"{base}/USDT:USDT"], outdir)
    out = {"job": jid, "pair": base, "year": year, "variant": variant, "fingerprint": fingerprint()["id"], **info}
    if trades is None:
        return out
    n = int(len(trades))
    prof = float(trades["profit_abs"].sum()) if n else 0.0
    win = float((trades["profit_abs"] > 0).mean()) if n else None
    return {"_trades": trades_frame(jid, trades), **out, "n_trades": n, "profit_abs": round(prof, 4),
            "win_rate": None if win is None else round(win, 4), "trades_hash": trades_hash(trades)}


def equity_stats(pnl_by_close, start_equity: float) -> dict:
    """按平仓顺序的已实现权益曲线：最大回撤（不含持仓期间的浮亏，结论里要写明）。"""
    eq, peak, mdd = start_equity, start_equity, 0.0
    for p in pnl_by_close:
        eq += p
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak if peak > 0 else 0.0)
    return {"final_equity": round(eq, 2), "max_drawdown_closed": round(mdd, 4)}


def portfolio(variant: str, timerange: str) -> dict:
    import pandas as pd

    outdir = RES / "portfolio" / variant
    shutil.rmtree(outdir, ignore_errors=True)
    info, trades = run_freqtrade(variant, timerange, None, outdir)
    res = {"kind": "portfolio", "variant": variant, "timerange": timerange, "fingerprint": fingerprint(), **info}
    if trades is not None:
        try:
            wallet = float(json.loads(Path(CONFIG).read_text("utf-8")).get("dry_run_wallet") or 10_000)
        except (OSError, ValueError):
            wallet = 10_000.0
        t = trades.sort_values("close_date")
        tf = pd.DataFrame({"job": f"portfolio-{variant}", "coin": t["pair"].str.split("/").str[0], "year": 0,
                           "variant": variant, "entry_time": t["open_date"].values, "time": t["close_date"].values,
                           "R": [r_multiple(r) for _, r in t.iterrows()], "pnl": t["profit_abs"].values})
        tf.to_csv(RES / "portfolio" / f"{variant}.csv", index=False)
        res.update(n_trades=int(len(t)), profit_abs=round(float(t["profit_abs"].sum()), 4) if len(t) else 0.0,
                   by_coin=tf.groupby("coin")["pnl"].agg(["count", "sum"]).round(4).to_dict("index"),
                   max_concurrent=_max_concurrent(t), trades_hash=trades_hash(trades),
                   **equity_stats(t["profit_abs"].tolist(), wallet),
                   exits=t["exit_reason"].value_counts().to_dict(),
                   note="组合回测的逐笔 CSV 可交给 s03ft lab stats --trades research/results/portfolio/<方案>.csv --holdout-start …")
    (RES / "portfolio" / f"{variant}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), "utf-8")
    return res


def _max_concurrent(t) -> int:
    ev = sorted([(o, 1) for o in t["open_date"]] + [(c, -1) for c in t["close_date"]], key=lambda x: (x[0], x[1]))
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best


def load_manifest() -> dict:
    p = ROOT / "jobs.json"
    m = json.loads(p.read_text("utf-8"))
    return m if isinstance(m, dict) else {"jobs": m, "fingerprint": None}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--pairs", default="BTC,ETH,SOL,BNB,HYPE")
    p.add_argument("--years", default="2022-2025")
    p.add_argument("--variants", default="base,stop_floor,cost2x,delay1")
    for n in ("run", "verify"):
        sub.add_parser(n).add_argument("--job", required=True)
    sub.add_parser("aggregate")
    pf = sub.add_parser("portfolio")
    pf.add_argument("--variant", default="base", choices=sorted(VARIANTS))
    pf.add_argument("--timerange", default="20220101-")
    g = sub.add_parser("g0")
    g.add_argument("--timerange", default="20250101-")
    a = ap.parse_args()

    if a.cmd == "plan":
        y0, y1 = (int(x) for x in a.years.split("-"))
        jobs = [f"{b}-{y}-{v}" for b in a.pairs.split(",") for y in range(y0, y1 + 1) for v in a.variants.split(",")]
        for j in jobs:
            parse_job(j)
        ROOT.mkdir(exist_ok=True)
        fp = fingerprint()
        (ROOT / "jobs.json").write_text(json.dumps({"jobs": jobs, "fingerprint": fp["id"], "fingerprint_parts": fp,
                                                    "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                                   indent=1), "utf-8")
        print(f"{len(jobs)} 个任务 → research/jobs.json（版本指纹 {fp['id']}）")
        return 0
    if a.cmd == "run":
        RES.mkdir(parents=True, exist_ok=True)
        for old in (RES / f"{a.job}.json", RES / "trades" / f"{a.job}.csv"):   # 先删旧结果：失败时不会留下旧数据
            old.unlink(missing_ok=True)
        out = backtest(a.job, RES / a.job)
        tf = out.pop("_trades", None)
        if tf is not None:
            (RES / "trades").mkdir(parents=True, exist_ok=True)
            tf.to_csv(RES / "trades" / f"{a.job}.csv", index=False)
        (RES / f"{a.job}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
        print(json.dumps({k: out.get(k) for k in ("job", "status", "n_trades", "trades_hash", "fingerprint")},
                         ensure_ascii=False))
        return 0 if out["status"] == "ok" else 1
    if a.cmd == "verify":
        stored = json.loads((RES / f"{a.job}.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as d:
            fresh = backtest(a.job, Path(d))
            fresh.pop("_trades", None)
        same = stored.get("trades_hash") == fresh.get("trades_hash") and stored.get("fingerprint") == fresh.get("fingerprint")
        print(json.dumps({"job": a.job, "match": same, "stored": stored.get("trades_hash"), "fresh": fresh.get("trades_hash"),
                          "same_version": stored.get("fingerprint") == fresh.get("fingerprint")}, ensure_ascii=False))
        return 0 if same else 1
    if a.cmd == "aggregate":
        m = load_manifest()
        rows, skipped = [], []
        for j in m["jobs"]:
            try:
                r = json.loads((RES / f"{j}.json").read_text("utf-8"))
            except (OSError, ValueError):
                skipped.append((j, "没有结果"))
                continue
            if r.get("status") != "ok":
                skipped.append((j, f"状态 {r.get('status')}"))
            elif m.get("fingerprint") and r.get("fingerprint") != m["fingerprint"]:
                skipped.append((j, "版本指纹不一致"))
            else:
                rows.append(r)
        lines = ["# 研究矩阵汇总（freqtrade，程序生成）", "", f"清单 {len(m['jobs'])} 个任务，纳入 {len(rows)} 个，"
                 f"排除 {len(skipped)} 个；版本指纹 {m.get('fingerprint')}。", "",
                 "| 任务 | 笔数 | 盈亏（USDT） | 胜率 | 逐笔哈希 |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {r['job']} | {r['n_trades']} | {r['profit_abs']} | {r['win_rate']} | {r['trades_hash']} |" for r in rows]
        if skipped:
            lines += ["", "排除的任务：", ""] + [f"- {j}：{why}" for j, why in skipped]
        base_n = sum(r["n_trades"] for r in rows if r["variant"] == "base")
        lines += ["", f"base 方案合计 {base_n} 笔；G1 要求 ≥ 100 笔（30–99 笔为“证据不足”）。",
                  "矩阵是单币 × 单年的诊断，合计笔数不等于一个账户的交易；上线证据看 portfolio。"]
        (ROOT / "matrix_report.md").write_text("\n".join(lines), "utf-8")
        print("\n".join(lines))
        return 0
    if a.cmd == "portfolio":
        res = portfolio(a.variant, a.timerange)
        print(json.dumps({k: res.get(k) for k in ("variant", "status", "n_trades", "profit_abs", "max_drawdown_closed",
                                                  "max_concurrent", "trades_hash")}, ensure_ascii=False, default=str))
        return 0 if res.get("status") == "ok" else 1
    if a.cmd == "g0":
        env = {**os.environ, "S03_EVENTS_REQUIRED": "0"}
        rc = 0
        for sub_cmd in (["lookahead-analysis"], ["recursive-analysis", "--startup-candle", "199", "400", "999"]):
            cmd = [freqtrade_bin(), *sub_cmd, *config_args(), *strategy_args(), "--timerange", a.timerange]
            print("$", " ".join(cmd), flush=True)
            rc |= subprocess.run(cmd, env=env).returncode
        return rc
    return 2


if __name__ == "__main__":
    sys.exit(main())
