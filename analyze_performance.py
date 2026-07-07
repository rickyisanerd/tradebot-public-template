#!/usr/bin/env python3
"""Performance analyzer for TradeBot.

Answers the questions that matter for *learning*: where is the money actually
going, and which signals actually have an edge? Reads the live dashboard's
read-only ``/api/status`` snapshot (no credentials, places no orders) and prints
a ranked, plain-English report. Optionally writes a Markdown copy.

Usage:
    python analyze_performance.py
    python analyze_performance.py --url https://your-deployment.example.com
    python analyze_performance.py --json status.json          # analyze a saved snapshot
    python analyze_performance.py --out report.md             # also write Markdown

Nothing here mutates state or trades; it only GETs /api/status.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_URL = os.getenv("TRADEBOT_DASHBOARD_URL", "http://127.0.0.1:8008")

# ETFs / index funds the bot keeps buying. Used to separate "broad-market beta"
# bets (which should be matched against just holding the index) from real
# single-name stock picks where the bot is supposed to add value.
BROAD_MARKET = {
    "SPY", "VOO", "VTI", "QQQ", "DIA", "IWM", "IVV",
    "XLF", "XLK", "XLE", "XLV", "XLY", "XLI", "XLU", "XLB", "XLP", "XLRE", "XLC",
}
INVERSE = {"SH", "PSQ", "DOG", "SPXS", "SQQQ", "SDOW", "SPXU", "TECS", "SOXS", "LABU"}


def _fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "  n/a"
    return f"{x:+6.2f}%"


def _fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"${x:,.2f}"


def fetch_status(url: str, timeout: float = 90.0) -> Dict[str, Any]:
    # /api/status recomputes the shadow study (prices every shadow symbol), so
    # it can take a while. Give it room rather than failing on a slow cycle.
    endpoint = url.rstrip("/") + "/api/status"
    with urllib.request.urlopen(endpoint, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def classify(symbol: str) -> str:
    s = symbol.upper()
    if s in INVERSE:
        return "inverse-hedge"
    if s in BROAD_MARKET:
        return "broad-market ETF"
    return "single-name"


def section(title: str) -> List[str]:
    return ["", f"## {title}", ""]


def analyze(status: Dict[str, Any]) -> str:
    out: List[str] = []
    out.append("# TradeBot performance review")

    # --- Account & headline P&L ---
    acct = status.get("account", {})
    perf = status.get("performance", {})
    safety = status.get("safety_status", {})
    regime = status.get("market_regime", {})
    out += section("Account")
    equity = acct.get("equity")
    peak = perf.get("peak_equity")
    baseline = perf.get("baseline_equity")
    out.append(f"- Equity: {_fmt_money(equity)}   (peak {_fmt_money(peak)}, baseline {_fmt_money(baseline)})")
    if equity and peak:
        out.append(f"- Drawdown from peak: {(equity / peak - 1) * 100:+.2f}%")
    out.append(f"- Cash / buying power: {_fmt_money(acct.get('cash'))} / {_fmt_money(acct.get('buying_power'))}")
    out.append(
        f"- Realized P&L: {_fmt_money(perf.get('realized_pnl'))}   "
        f"Unrealized: {_fmt_money(perf.get('unrealized_pnl'))}   "
        f"Total: {_fmt_money(perf.get('total_pnl'))} ({_fmt_pct(perf.get('total_return_pct'))})"
    )
    out.append(
        f"- Closed: {perf.get('closed_winners', 0)}W / {perf.get('closed_losers', 0)}L   "
        f"Open: {perf.get('open_winners', 0)}W / {perf.get('open_losers', 0)}L"
    )
    daily_amt = safety.get("daily_pnl_amount")
    daily_pct = safety.get("daily_loss_pct")
    if daily_amt is not None and daily_amt < 0 and daily_pct:
        daily_pct = -abs(daily_pct)
    out.append(f"- Today: {_fmt_money(daily_amt)} ({_fmt_pct(daily_pct)})")
    out.append(f"- Market regime: {regime.get('state')} ({regime.get('reason')}); longs allowed = {regime.get('allow_long_buys')}")

    # --- Capital allocation: where is the money? ---
    out += section("Where the capital actually is")
    positions = status.get("positions", [])
    buckets: Dict[str, Dict[str, float]] = {}
    for p in positions:
        kind = classify(str(p.get("symbol", "")))
        b = buckets.setdefault(kind, {"count": 0, "mv": 0.0})
        b["count"] += 1
        b["mv"] += float(p.get("market_value") or 0.0)
    total_mv = sum(b["mv"] for b in buckets.values()) or 1.0
    for kind, b in sorted(buckets.items(), key=lambda kv: kv[1]["mv"], reverse=True):
        out.append(f"- {kind:18s}: {int(b['count'])} pos, {_fmt_money(b['mv'])} ({b['mv'] / total_mv * 100:.0f}% of book)")
    single = buckets.get("single-name", {}).get("mv", 0.0)
    if single / total_mv < 0.34:
        out.append(
            f"  -> NOTE: only {single / total_mv * 100:.0f}% of the book is in single-name picks. "
            f"The rest is broad-market/hedge beta the bot can't add edge to."
        )

    # --- Signal edge leaderboard (the core "which signals work") ---
    weekly = status.get("weekly_signal_performance", {})
    signals = weekly.get("shadow_signals", [])
    days = weekly.get("days", "?")
    out += section(f"Signal edge ({days}-day shadow study, sorted by avg P&L)")
    if not signals:
        out.append("- No shadow-signal data yet.")
    else:
        out.append("  signal:state                     n     win%    avg P&L    total")
        out.append("  " + "-" * 64)
        for s in sorted(signals, key=lambda r: r.get("avg_pnl_pct", 0.0), reverse=True):
            n = int(s.get("count", 0))
            wins = int(s.get("wins", 0))
            winp = (wins / n * 100) if n else 0.0
            name = str(s.get("name", ""))[:30]
            flag = "  <-- EDGE" if s.get("avg_pnl_pct", 0) > 0.3 else ("  <-- drag" if s.get("avg_pnl_pct", 0) < -0.5 else "")
            out.append(
                f"  {name:30s} {n:4d}   {winp:4.0f}%   {_fmt_pct(s.get('avg_pnl_pct'))}   "
                f"{_fmt_pct(s.get('total_pnl_pct'))}{flag}"
            )

    # --- Realized losers/winners ---
    out += section("Biggest open winners / losers")
    for label, key in (("Winners", "top_winners"), ("Losers", "top_losers")):
        rows = perf.get(key, [])
        out.append(f"- {label}:")
        for r in rows:
            out.append(
                f"    {str(r.get('symbol','')):6s} {_fmt_pct(r.get('pnl_pct'))}  "
                f"({_fmt_money(r.get('pnl_amount'))})  [{classify(str(r.get('symbol','')))}]"
            )

    # --- Learning health check ---
    out += section("Learning health check")
    learning = status.get("learning", {})
    realized_trades = sum(
        1 for t in status.get("trades", []) if str(t.get("side")) == "sell" and str(t.get("status")) == "filled"
    )
    total_recorded = 0
    weights = []
    identical = True
    first_wl = None
    for name, row in learning.items():
        w = float(row.get("weight", 1.0))
        weights.append((name, w, int(row.get("wins", 0)), int(row.get("losses", 0))))
        total_recorded = max(total_recorded, int(row.get("wins", 0)) + int(row.get("losses", 0)))
        wl = (int(row.get("wins", 0)), int(row.get("losses", 0)))
        if first_wl is None:
            first_wl = wl
        elif wl != first_wl:
            identical = False
    for name, w, wins, losses in weights:
        out.append(f"- {name:18s} weight={w:.2f}  wins={wins:,}  losses={losses:,}")
    problems = []
    if identical and len(weights) > 1:
        problems.append(
            "All strategy weights are IDENTICAL -> no differentiation. update_learning credits every "
            "strategy the same pnl, so the bot can't tell which strategy is predictive."
        )
    if weights and all(abs(w - weights[0][1]) < 1e-9 for _, w, _, _ in weights) and abs(weights[0][1] - 0.25) < 1e-6:
        problems.append("Weights are pinned at the 0.25 floor -> learning has collapsed/saturated.")
    if total_recorded > max(50, realized_trades * 20):
        problems.append(
            f"Learning table shows ~{total_recorded:,} outcomes but the bot has only a handful of real "
            f"closed trades. The retroactive scan-learning loop is inflating it with phantom outcomes."
        )
    if problems:
        out.append("")
        out.append("  PROBLEMS DETECTED:")
        for p in problems:
            out.append(f"   !! {p}")
    else:
        out.append("  Learning table looks plausible.")

    # --- Bottom line ---
    out += section("Bottom line")
    edges = [s for s in signals if s.get("avg_pnl_pct", 0) > 0.3 and s.get("count", 0) >= 5]
    drags = [s for s in signals if s.get("avg_pnl_pct", 0) < -0.5 and s.get("count", 0) >= 5]
    if edges:
        out.append("- Working signals (positive avg, decent sample):")
        for s in sorted(edges, key=lambda r: r["avg_pnl_pct"], reverse=True):
            out.append(f"    {s['name']}  ({s['count']} picks, {_fmt_pct(s['avg_pnl_pct'])} avg)")
    if drags:
        out.append("- Money-losing signals (negative avg, decent sample):")
        for s in sorted(drags, key=lambda r: r["avg_pnl_pct"]):
            out.append(f"    {s['name']}  ({s['count']} picks, {_fmt_pct(s['avg_pnl_pct'])} avg)")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help="dashboard base URL")
    ap.add_argument("--json", help="analyze a saved /api/status JSON file instead of fetching")
    ap.add_argument("--out", help="also write the report to this Markdown file")
    args = ap.parse_args(argv)

    try:
        if args.json:
            with open(args.json, "r", encoding="utf-8") as fh:
                status = json.load(fh)
        else:
            status = fetch_status(args.url)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to load status: {exc}", file=sys.stderr)
        return 1

    report = analyze(status)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"\n(written to {args.out})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
