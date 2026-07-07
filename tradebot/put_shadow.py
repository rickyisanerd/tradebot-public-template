"""Put-shadow evaluator — earns the right to trade puts before risking a dollar.

This module is the honest "is the bot ready to try puts?" engine. It NEVER
places a real order and is completely OFF the live trading path. Instead it:

    1.  Listens for the same bearish/risk-off conditions the bot already detects
        for inverse-ETF hedging (see engine._market_regime_status).
    2.  When those fire on a liquid, optionable underlying, it logs a SYNTHETIC
        30-day at-the-money put — priced with Black-Scholes off the underlying's
        own realized volatility (we can't pull live option quotes on the current
        Polygon plan, so we model them).
    3.  Tracks each synthetic put to a close (fixed hold window) and books a
        paper P&L *net of modeled commission and slippage*.
    4.  Only when a STRICT track record accumulates — 40+ closed trades AND
        positive expectancy after costs — does it raise a one-time alert telling
        it may be worth funding a real (paper-first) put experiment.

The whole point: a green light is *earned* by evidence, never assumed.
"""

from __future__ import annotations

import json
import math
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Tunables (env-overridable, strict defaults chosen with the user)
# ---------------------------------------------------------------------------

READY_MIN_CLOSED = int(os.getenv("PUT_SHADOW_MIN_CLOSED", "40"))      # strict sample size
HOLD_DAYS = int(os.getenv("PUT_SHADOW_HOLD_DAYS", "10"))               # calendar days to hold
EXPIRY_DAYS = int(os.getenv("PUT_SHADOW_EXPIRY_DAYS", "30"))           # contract tenor at entry
COMMISSION_PER_CONTRACT = float(os.getenv("PUT_SHADOW_COMMISSION", "0.65"))  # each way, per contract
SLIPPAGE_PCT = float(os.getenv("PUT_SHADOW_SLIPPAGE_PCT", "0.05"))     # haircut vs mid, each side
RISK_FREE = float(os.getenv("PUT_SHADOW_RISK_FREE", "0.04"))
MIN_IV = 0.10  # floor so a dead-flat lookback doesn't produce a free option


# ---------------------------------------------------------------------------
# Black-Scholes (same model used in puts_learn.py; kept local so this module
# has no dependency on the repo-root learning script)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(spot: float, strike: float, days: float, iv: float, r: float = RISK_FREE) -> float:
    """Black-Scholes European put price, per share."""
    if days <= 0 or iv <= 0 or spot <= 0:
        return max(strike - spot, 0.0)
    t = days / 365.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def realized_vol(closes: List[float]) -> float:
    """Annualized volatility from a series of daily closes (IV proxy)."""
    if len(closes) < 5:
        return MIN_IV
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 2:
        return MIN_IV
    daily = statistics.pstdev(rets)
    return max(MIN_IV, daily * math.sqrt(252))


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

@dataclass
class ReadinessReport:
    ready: bool
    n_closed: int
    n_open: int
    win_rate: float
    avg_pnl: float
    total_pnl: float
    expectancy: float          # avg net P&L per closed trade, after costs
    reasons: List[str]


class PutShadowLedger:
    """A JSON-backed paper ledger of synthetic put trades. No real money, ever."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"trades": [], "alerted_ready": False}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2))

    @property
    def trades(self) -> List[Dict[str, Any]]:
        return self._data["trades"]

    # -- modeled costs -----------------------------------------------------

    @staticmethod
    def _fill_premium(mid: float, *, side: str) -> float:
        """Apply slippage: you BUY above mid and SELL below mid."""
        if side == "buy":
            return mid * (1 + SLIPPAGE_PCT)
        return max(0.0, mid * (1 - SLIPPAGE_PCT))

    # -- opening -----------------------------------------------------------

    def has_open(self, underlying: str) -> bool:
        u = underlying.upper()
        return any(t["underlying"] == u and t["status"] == "open" for t in self.trades)

    def open_synthetic_put(
        self,
        underlying: str,
        spot: float,
        recent_closes: List[float],
        *,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Log one ATM synthetic put. Skips if one is already open on this name."""
        u = underlying.upper()
        if self.has_open(u) or spot <= 0:
            return None
        now = now or datetime.now(timezone.utc)
        iv = realized_vol(recent_closes)
        strike = round(spot, 2)  # at-the-money
        mid = bs_put(spot, strike, EXPIRY_DAYS, iv)
        entry_fill = self._fill_premium(mid, side="buy")
        trade = {
            "id": f"{u}-{now.strftime('%Y%m%d%H%M%S')}",
            "underlying": u,
            "status": "open",
            "opened_at": now.isoformat(),
            "entry_spot": round(spot, 4),
            "strike": strike,
            "entry_iv": round(iv, 4),
            "expiry_days_at_entry": EXPIRY_DAYS,
            "entry_mid": round(mid, 4),
            "entry_fill": round(entry_fill, 4),
        }
        self.trades.append(trade)
        self._save()
        return trade

    # -- closing -----------------------------------------------------------

    def update_open_trades(
        self,
        prices: Dict[str, float],
        vols: Dict[str, float],
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """Mark open trades to model price; close any past the hold window.

        ``prices`` / ``vols`` map underlying -> current spot / current IV proxy.
        Returns the number of trades closed on this pass.
        """
        now = now or datetime.now(timezone.utc)
        closed = 0
        for t in self.trades:
            if t["status"] != "open":
                continue
            spot = prices.get(t["underlying"])
            if spot is None:
                continue
            opened = datetime.fromisoformat(t["opened_at"])
            held_days = (now - opened).days
            if held_days < HOLD_DAYS:
                continue
            remaining = max(0, t["expiry_days_at_entry"] - held_days)
            iv = vols.get(t["underlying"], t["entry_iv"])
            exit_mid = bs_put(spot, t["strike"], remaining, iv)
            exit_fill = self._fill_premium(exit_mid, side="sell")
            # P&L per 1 contract (100 shares), net of round-trip commission
            gross = (exit_fill - t["entry_fill"]) * 100
            net = gross - 2 * COMMISSION_PER_CONTRACT
            t["status"] = "closed"
            t["closed_at"] = now.isoformat()
            t["exit_spot"] = round(spot, 4)
            t["exit_mid"] = round(exit_mid, 4)
            t["exit_fill"] = round(exit_fill, 4)
            t["held_days"] = held_days
            t["pnl"] = round(net, 2)
            t["pnl_pct"] = round((net / (t["entry_fill"] * 100)) * 100, 2) if t["entry_fill"] else 0.0
            closed += 1
        if closed:
            self._save()
        return closed

    # -- readiness ---------------------------------------------------------

    def readiness(self) -> ReadinessReport:
        closed = [t for t in self.trades if t["status"] == "closed"]
        n_open = sum(1 for t in self.trades if t["status"] == "open")
        n = len(closed)
        if n == 0:
            return ReadinessReport(False, 0, n_open, 0.0, 0.0, 0.0, 0.0,
                                   ["No closed shadow puts yet."])
        pnls = [t["pnl"] for t in closed]
        wins = [p for p in pnls if p > 0]
        total = sum(pnls)
        expectancy = total / n
        win_rate = len(wins) / n * 100
        reasons: List[str] = []
        if n < READY_MIN_CLOSED:
            reasons.append(f"Need {READY_MIN_CLOSED}+ closed trades (have {n}).")
        if expectancy <= 0:
            reasons.append(f"Expectancy after costs is ${expectancy:,.2f} (must be > $0).")
        ready = n >= READY_MIN_CLOSED and expectancy > 0
        if ready:
            reasons.append(
                f"{n} closed shadow puts, {win_rate:.0f}% winners, "
                f"+${expectancy:,.2f}/trade expectancy after fees & slippage."
            )
        return ReadinessReport(
            ready=ready,
            n_closed=n,
            n_open=n_open,
            win_rate=round(win_rate, 1),
            avg_pnl=round(expectancy, 2),
            total_pnl=round(total, 2),
            expectancy=round(expectancy, 2),
            reasons=reasons,
        )

    # -- the alert ---------------------------------------------------------

    def maybe_alert_ready(self) -> bool:
        """Fire a ONE-TIME green-light email when the strict bar is first cleared.

        Returns True if an alert was sent this call. Safe to call every cycle;
        it de-dupes via the ``alerted_ready`` flag persisted in the ledger.
        """
        report = self.readiness()
        if not report.ready or self._data.get("alerted_ready"):
            return False
        try:
            from .email_report import send_failure_alert
        except Exception:  # noqa: BLE001 - never let alerting break the loop
            return False
        sent = send_failure_alert(
            "Put-shadow track record cleared the bar",
            (
                "The paper put-shadow evaluator has met the strict readiness "
                "criteria. This is NOT an instruction to trade — it means a real "
                "(paper-first) put experiment is now worth considering. Review the "
                "numbers before funding anything."
            ),
            {
                "Closed shadow puts": report.n_closed,
                "Win rate": f"{report.win_rate:.0f}%",
                "Expectancy / trade (net)": f"${report.expectancy:,.2f}",
                "Total paper P&L": f"${report.total_pnl:,.2f}",
            },
        )
        if sent:
            self._data["alerted_ready"] = True
            self._save()
        return bool(sent)


def default_ledger_path() -> Path:
    base = os.getenv("DATA_DIR", str(Path.cwd() / "data"))
    return Path(base) / "put_shadow.json"


# Liquid, optionable names where puts actually trade — the universe a real put
# strategy would use (NOT the bot's sub-$10 equities, which mostly lack options).
DEFAULT_BACKTEST_UNIVERSE = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "META", "GOOGL", "AMD",
]


def backtest(
    underlyings: Optional[List[str]] = None,
    *,
    years: float = 3.0,
    regime_symbol: str = "SPY",
) -> Dict[str, Any]:
    """Replay years of historical equity bars through the SAME synthetic-put
    logic, collapsing the "wait for 40 real drops" timeline into seconds.

    This is the fast way to learn: instead of waiting for future risk-off events,
    we test the signal against many *past* market regimes at once. Option prices
    are modeled with Black-Scholes off realized vol (identical to the live paper
    ledger); equity bars come from Polygon, which the current plan supports.

    Returns a dict with a ReadinessReport plus span metadata. It does NOT touch
    the live ledger and never fires the readiness alert.
    """
    import tempfile

    from .config import get_settings
    from .polygon import build_polygon_client

    underlyings = [u.upper() for u in (underlyings or DEFAULT_BACKTEST_UNIVERSE)]
    short_w = max(2, int(os.getenv("MARKET_REGIME_SHORT_WINDOW", "20")))
    long_w = max(short_w + 1, int(os.getenv("MARKET_REGIME_LONG_WINDOW", "50")))

    client = build_polygon_client(get_settings())
    if client is None:
        raise RuntimeError("POLYGON_API_KEY is required for backtest (set it in .env).")

    fetch_days = int(years * 365) + long_w + 15
    symbols = sorted(set([regime_symbol] + underlyings))
    # Polygon's free tier rate-limits to ~5 req/min and caps history near 2y, so
    # fetch sequentially with pacing rather than the concurrent bars_batch (whose
    # threads get 429'd into silent empties). ~12s/symbol keeps us under the cap.
    import sys as _sys
    import time as _time

    bars: Dict[str, List[dict]] = {}
    for n, sym in enumerate(symbols):
        try:
            bars[sym] = client.bars(sym, days=fetch_days)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {sym}: fetch failed ({exc}); skipping", file=_sys.stderr)
            bars[sym] = []
        print(f"  fetched {sym} ({n + 1}/{len(symbols)}): {len(bars[sym])} bars", file=_sys.stderr)
        if n + 1 < len(symbols):
            _time.sleep(12)

    series: Dict[str, List[tuple]] = {}
    for sym in symbols:
        rows = bars.get(sym) or []
        series[sym] = [(str(b["t"])[:10], float(b["c"])) for b in rows if b.get("c") is not None]

    # Market-regime gate, computed point-in-time (no lookahead): SPY weak on date d
    spy = series.get(regime_symbol) or []
    spy_closes = [c for _, c in spy]
    spy_weak: Dict[str, bool] = {}
    for i, (d, _) in enumerate(spy):
        if i + 1 < long_w:
            continue
        w = spy_closes[: i + 1]
        sma = sum(w[-short_w:]) / short_w
        lma = sum(w[-long_w:]) / long_w
        spy_weak[d] = not (w[-1] >= sma >= lma)

    udata: Dict[str, Dict[str, Any]] = {}
    for u in underlyings:
        rows = series.get(u) or []
        dates = [d for d, _ in rows]
        closes = [c for _, c in rows]
        udata[u] = {"dates": dates, "closes": closes, "idx": {d: i for i, d in enumerate(dates)}}

    # Drive the REAL ledger with historical timestamps so the open/hold/close +
    # cost math is byte-for-byte the same as production. In-memory only.
    led = PutShadowLedger(Path(tempfile.gettempdir()) / "put_shadow_backtest.json")
    led._data = {"trades": [], "alerted_ready": True}
    led._save = lambda: None  # type: ignore[method-assign]  # skip disk writes during the sweep

    def to_dt(d: str) -> datetime:
        return datetime.fromisoformat(d).replace(tzinfo=timezone.utc)

    all_dates = sorted({d for u in underlyings for d in udata[u]["dates"]})
    for d in all_dates:
        # 1) mark/close any matured open trades as of this date
        prices: Dict[str, float] = {}
        vols: Dict[str, float] = {}
        for u in underlyings:
            idx = udata[u]["idx"].get(d)
            if idx is None:
                continue
            closes = udata[u]["closes"]
            prices[u] = closes[idx]
            vols[u] = realized_vol(closes[max(0, idx - 30): idx + 1])
        led.update_open_trades(prices, vols, now=to_dt(d))
        # 2) open new synthetic puts: weak SPY regime AND a momentum breakdown
        if not spy_weak.get(d):
            continue
        for u in underlyings:
            idx = udata[u]["idx"].get(d)
            if idx is None or idx + 1 < short_w:
                continue
            closes = udata[u]["closes"]
            sma = sum(closes[idx - short_w + 1: idx + 1]) / short_w
            if closes[idx] < sma:
                led.open_synthetic_put(u, closes[idx], closes[max(0, idx - 30): idx + 1], now=to_dt(d))

    return {
        "report": led.readiness(),
        "underlyings": underlyings,
        "years": years,
        "first_date": all_dates[0] if all_dates else None,
        "last_date": all_dates[-1] if all_dates else None,
        "n_trades": len(led.trades),
    }


# ---------------------------------------------------------------------------
# CLI: inspect the ledger without running the bot
# ---------------------------------------------------------------------------

def _print_status(ledger: PutShadowLedger) -> None:
    r = ledger.readiness()
    print("=" * 60)
    print("  PUT-SHADOW READINESS (paper only - no real trades)")
    print("=" * 60)
    print(f"  Closed shadow puts : {r.n_closed}   (open: {r.n_open})")
    print(f"  Win rate           : {r.win_rate:.0f}%")
    print(f"  Expectancy/trade   : ${r.expectancy:,.2f}  (after fees + slippage)")
    print(f"  Total paper P&L    : ${r.total_pnl:,.2f}")
    print(f"  READY TO FUND?     : {'YES' if r.ready else 'not yet'}")
    print("-" * 60)
    for reason in r.reasons:
        print(f"  - {reason}")
    print()


def _print_backtest(result: Dict[str, Any]) -> None:
    r: ReadinessReport = result["report"]
    print("=" * 64)
    print("  PUT-SHADOW BACKTEST (modeled options, no real trades)")
    print("=" * 64)
    print(f"  Window      : {result['first_date']} -> {result['last_date']}  (~{result['years']:g}y)")
    print(f"  Underlyings : {', '.join(result['underlyings'])}")
    print("-" * 64)
    print(f"  Closed trades      : {r.n_closed}   (still open at end: {r.n_open})")
    print(f"  Win rate           : {r.win_rate:.0f}%")
    print(f"  Expectancy/trade   : ${r.expectancy:,.2f}  (after fees + slippage)")
    print(f"  Total paper P&L    : ${r.total_pnl:,.2f}")
    print(f"  Clears strict bar? : {'YES' if r.ready else 'NO'}")
    print("-" * 64)
    if r.ready:
        print("  Historical edge looks positive. NEXT: confirm on Alpaca PAPER with")
        print("  real option quotes before any real money — a model is not a fill.")
    else:
        verdict = "negative/insufficient edge" if r.n_closed >= READY_MIN_CLOSED else "too few samples"
        print(f"  No green light ({verdict}). The signal did not pay after costs across")
        print("  this history. That is the honest answer: don't fund it.")
    print()


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Inspect or backtest the put-shadow strategy.")
    parser.add_argument("command", choices=["status", "backtest"], nargs="?", default="status")
    parser.add_argument("--path", help="Override ledger path (status only).")
    parser.add_argument("--years", type=float, default=3.0, help="Backtest lookback in years (default 3).")
    parser.add_argument("--tickers", help="Comma-separated underlyings for backtest (default: liquid basket).")
    args = parser.parse_args(argv)

    if args.command == "backtest":
        tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
        result = backtest(tickers, years=args.years)
        _print_backtest(result)
        return 0

    ledger = PutShadowLedger(Path(args.path) if args.path else default_ledger_path())
    _print_status(ledger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
