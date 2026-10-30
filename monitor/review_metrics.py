"""复盘指标（程序计算，可复现）。复盘 agent 只读本模块的输出，不自己数笔数、算胜率。
Hyperliquid 版与 freqtrade 版共用同一个 compute()（freqtrade 版：monitor/review_metrics.py，由 s03-ft-export 调用）。"""
from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trade_risk import r_multiple  # noqa: E402


def compute(trades: list[dict]) -> dict:
    """trades: [{coin, pnl, r (可为 None), exit_reason, close_time}]，按平仓时间排序。"""
    def stats(ts: list[dict]) -> dict:
        n = len(ts)
        rs = [t["r"] for t in ts if t.get("r") is not None]
        wins = [t for t in ts if (t.get("pnl") or 0) > 0]
        streak = worst = 0
        for t in ts:
            streak = streak + 1 if (t.get("pnl") or 0) <= 0 else 0
            worst = max(worst, streak)
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in ts if (t.get("pnl") or 0) <= 0)
        return {
            "n": n,
            "win_rate": round(len(wins) / n, 3) if n else None,
            "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
            "r_known": len(rs),
            "r_unknown": n - len(rs),                                    # v2.11：初始风险未知的笔数与净盈亏，不能静默略过
            "r_unknown_pnl": round(sum(t.get("pnl") or 0 for t in ts if t.get("r") is None), 2),
            "r_estimated": sum(1 for t in ts if t.get("r") is None and t.get("r_est") is not None),
            "expectancy_r_est_only": (round(sum(t["r_est"] for t in ts if t.get("r") is None and t.get("r_est") is not None) /
                                            max(1, sum(1 for t in ts if t.get("r") is None and t.get("r_est") is not None)), 3)
                                      if any(t.get("r") is None and t.get("r_est") is not None for t in ts) else None),
            "pnl": round(sum(t.get("pnl") or 0 for t in ts), 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "max_consecutive_losses": worst,
            "exit_reasons": dict(Counter(t.get("exit_reason") or "unknown" for t in ts)),
        }

    ordered = sorted(trades, key=lambda x: str(x.get("close_time")))
    by = defaultdict(list)
    for t in ordered:
        by[t["coin"]].append(t)
    # 合计按全账户的平仓顺序计算（v2.5 及以前先按币分组再拼接，打乱了顺序，最长连亏会算少）
    by_tag = defaultdict(list)
    for t in ordered:
        if "tag" in t:
            by_tag[t["tag"]].append(t)
    return {"total": stats(ordered), "by_coin": {c: stats(v) for c, v in sorted(by.items())},
            "by_tag": {k: stats(v) for k, v in sorted(by_tag.items())},
            "evidence": "已核验事实（由日志或交易记录逐笔计算）；n < 30 时任何结论都属证据不足"}


def to_markdown(m: dict, title: str) -> str:
    rows = [("合计", m["total"])] + list(m["by_coin"].items())
    lines = [f"# {title}", "", "| 标的 | 笔数 | 胜率 | 期望值（R） | 盈亏 | 盈亏比 | 最长连亏 | 离场原因 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, s in rows:
        lines.append(f"| {name} | {s['n']} | {s['win_rate']} | {s['expectancy_r']} | {s['pnl']} | {s['profit_factor']} | "
                     f"{s['max_consecutive_losses']} | {s['exit_reasons']} |")
    if m.get("by_tag"):
        lines += ["", "| 策略标记 | 笔数 | 胜率 | 期望值（R） | 盈亏 |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {k} | {v['n']} | {v['win_rate']} | {v['expectancy_r']} | {v['pnl']} |" for k, v in m["by_tag"].items()]
    t = m["total"]
    if t.get("r_unknown"):
        lines += ["", f"初始风险没有记录 {t['r_unknown']} 笔（净盈亏 {t['r_unknown_pnl']}）：期望值（R）只按有记录的 {t['r_known']} 笔计算，"
                      "不能代表全部交易。"]
        if t.get("r_estimated"):
            lines += [f"其中 {t['r_estimated']} 笔按策略实际止损参数**估算**，估算部分的期望值 {t['expectancy_r_est_only']} R（不计入上表）。"]
    ev = m.get("risk_evidence") or {}
    if ev.get("db_error") or ev.get("estimate_error"):
        lines += [f"风险记录来源：{ev.get('db_error') or '数据库正常'}；估算参数：{ev.get('estimate_error') or ev.get('estimate_params')}"]
    lines += ["", m["evidence"]]
    return "\n".join(lines)


def from_journal(records: list[dict]) -> list[dict]:
    """Hyperliquid 版：从 journal 的 position_closed 记录取逐笔数据（v1.0 起记录 r）。"""
    out = []
    for r in records:
        if r.get("kind") != "position_closed":
            continue
        out.append({"coin": r.get("coin"), "pnl": float(r.get("pnl") or 0), "r": r.get("r"),
                    "exit_reason": r.get("exit_reason"), "close_time": r.get("ts")})
    return out


def alert_counts(records: list[dict]) -> dict:
    return dict(Counter(f"{r.get('coin')}:{r.get('rule')}" for r in records if r.get("kind") == "alert"))


def load_init_risk_db(db_path: str) -> tuple[dict[int, float], str | None]:
    """只读打开 freqtrade 的数据库，取框架在绑定止损时写下的初始风险（trade_custom_data 表，键 fw_init_risk；v2.11）。
    freqtrade 的 REST 接口不返回 custom data。返回（{交易 id: 初始风险}, 读不到的原因或 None）（v2.12：原因写进复盘报告）。"""
    import sqlite3

    if not db_path:
        return {}, "没有配置数据库路径"
    if not os.path.exists(db_path):
        return {}, f"数据库不存在：{db_path}"
    if not os.access(db_path, os.R_OK):
        return {}, f"没有读取权限：{db_path}"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT ft_trade_id, cd_value FROM trade_custom_data WHERE cd_key = 'fw_init_risk'").fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        return {}, f"读取失败：{type(e).__name__}: {e}"
    out = {}
    for tid, v in rows:
        try:
            out[int(tid)] = float(v)
        except (TypeError, ValueError):
            continue
    return out, None


def strategy_stop_params(strategy_dir: str, name: str) -> tuple[dict | None, str | None]:
    """读服务器上策略类实际生效的止损参数（FW_DEFAULTS + framework_params），供“估算 R”使用（v2.12）。"""
    import importlib.util

    try:
        sys.path.insert(0, strategy_dir)
        for f in sorted(os.listdir(strategy_dir)):
            path = os.path.join(strategy_dir, f)
            if not f.endswith(".py") or f"class {name}(" not in open(path, encoding="utf-8", errors="ignore").read():
                continue
            spec = importlib.util.spec_from_file_location(f"_s03_review_{f[:-3]}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            import framework_base as fb

            fw = {**fb.FW_DEFAULTS, **(getattr(getattr(mod, name), "framework_params", None) or {})}
            return {"stop_min_pct": float(fw["stop_min_pct"]), "stop_max_pct": float(fw["stop_max_pct"]),
                    "stop_min_mode": fw["stop_min_mode"]}, None
        return None, f"找不到策略类 {name}"
    except Exception as e:                                   # noqa: BLE001
        return None, f"读取策略参数失败：{type(e).__name__}"


def from_freqtrade(trades: list[dict], init_risk: dict[int, float] | None = None,
                   estimate_params: dict | None = None) -> list[dict]:
    """freqtrade 版：已平仓交易的逐笔 R。口径与研究端相同（monitor/trade_risk.py）：入场成交量 × (开仓价 − 初始止损)。
    v2.12（外部审查 v2.11 P2-1）：r 只用成交时记录的初始风险（freqtrade 数据库里的 fw_init_risk）；缺记录时 r 为空（未知）。
    给了 estimate_params（服务器上策略实际的止损参数）时另算一个估算值 r_est，标为“估算”，不计入主期望值；
    v2.11 缺记录时用默认的 0.8% / 3% 推算，并与有记录的交易混在一起。"""
    out = []
    for t in trades:
        if t.get("is_open"):
            continue
        rec = (init_risk or {}).get(t.get("trade_id"))
        if rec is not None:
            r, src = r_multiple(t, recorded={"init_risk": rec})
            r_est = None
        else:
            r, src = None, "no_record"
            r_est = None
            if estimate_params:
                kw = {k: estimate_params[k] for k in ("stop_min_pct", "stop_max_pct") if k in estimate_params}
                r_est, _ = r_multiple(t, **kw)
        pnl = float(t.get("profit_abs") or 0)
        out.append({"coin": str(t.get("pair", "")).split("/")[0], "pnl": pnl,
                    "r": round(r, 3) if r is not None else None, "risk_source": src,
                    "r_est": round(r_est, 3) if r_est is not None else None, "exit_reason": t.get("exit_reason"),
                    "close_time": t.get("close_date"), "tag": strategy_tag(t.get("enter_tag"))})
    return out


def strategy_tag(enter_tag) -> str:
    """框架把入场计划写进 enter_tag（fw|s=…|c=…|t=…|u=<策略自己的标记>），每笔都不同，freqtrade 自带的按标记分组因此失效。
    这里取回策略自己的标记（u= 之后）；没有时记为 "(无)"。"""
    s = str(enter_tag or "")
    if not s.startswith("fw|"):
        return s or "(无)"
    return s.split("|u=", 1)[1] if "|u=" in s else "(无)"
