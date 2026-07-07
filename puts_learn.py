"""puts_learn.py — a standalone, read-only lesson in how buying a PUT works.

This script does NOT trade anything and is completely separate from the bot.
It exists to make the mechanics of a long put concrete with real numbers:

    * fetches the live stock price + a real put option chain from Polygon
    * picks a near-the-money put roughly N days out
    * prints the contract's premium, breakeven, max loss, and (if available)
      the Greeks (delta / theta / implied volatility)
    * prints a profit/loss table showing what the put is worth at expiration
      across a range of stock prices
    * demonstrates THETA (time decay) — the silent killer of bought options —
      by valuing the same put today vs. a week from now if the stock doesn't move

Usage
-----
    # live chain (needs POLYGON_API_KEY in your .env, and options data on the plan)
    python puts_learn.py --ticker SPY --days 30

    # manual mode (no API needed — plug in numbers from any broker screen)
    python puts_learn.py --manual --price 50 --strike 45 --premium 1.50 --days 30

Notes
-----
* Buying a put = betting the stock FALLS. Most you can lose = the premium.
* Liquid underlyings (SPY, QQQ, AAPL, ...) have real option chains.
  Sub-$10 stocks usually do NOT — that's why this is separate from the bot.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

try:  # reuse the bot's .env loading if available, but don't require the package
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

POLYGON_BASE = "https://api.polygon.io"


# ----------------------------------------------------------------------------
# Option math (no external deps)
# ----------------------------------------------------------------------------

def put_value_at_expiry(stock_price: float, strike: float) -> float:
    """Intrinsic value of a put at expiration, per share."""
    return max(strike - stock_price, 0.0)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf — good enough for a teaching tool."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_put(
    spot: float,
    strike: float,
    days_to_expiry: float,
    iv: float,
    risk_free: float = 0.04,
) -> float:
    """Black-Scholes price of a European put, per share.

    Used only to *illustrate* time decay — real American options differ slightly.
    """
    if days_to_expiry <= 0 or iv <= 0 or spot <= 0:
        return put_value_at_expiry(spot, strike)
    t = days_to_expiry / 365.0
    d1 = (math.log(spot / strike) + (risk_free + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    return strike * math.exp(-risk_free * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


# ----------------------------------------------------------------------------
# Polygon data fetching
# ----------------------------------------------------------------------------

def _get(path: str, api_key: str, params: Optional[dict] = None) -> dict:
    params = dict(params or {})
    params["apiKey"] = api_key
    resp = requests.get(f"{POLYGON_BASE}{path}", params=params, timeout=30)
    if resp.status_code == 403:
        raise PermissionError(
            "Polygon returned 403 (NOT AUTHORIZED). Your plan likely does not "
            "include options data. Re-run with --manual to enter numbers by hand."
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Polygon {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def fetch_underlying_price(ticker: str, api_key: str) -> float:
    payload = _get(f"/v2/aggs/ticker/{ticker.upper()}/prev", api_key, {"adjusted": "true"})
    results = payload.get("results") or []
    if not results:
        raise RuntimeError(f"No recent price for {ticker}.")
    return float(results[0]["c"])


def fetch_put_chain(ticker: str, api_key: str) -> list[dict]:
    """Snapshot of the put option chain for an underlying (one page, ~250 contracts)."""
    payload = _get(
        f"/v3/snapshot/options/{ticker.upper()}",
        api_key,
        {"contract_type": "put", "limit": 250, "order": "asc", "sort": "expiration_date"},
    )
    return payload.get("results") or []


def pick_put(chain: list[dict], spot: float, target_days: int) -> dict:
    """Choose the put whose expiry is closest to target_days and strike closest to spot."""
    today = date.today()
    best = None
    best_score = None
    for c in chain:
        details = c.get("details") or {}
        exp = details.get("expiration_date")
        strike = details.get("strike_price")
        if not exp or strike is None:
            continue
        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (exp_date - today).days
        if dte < 1:
            continue
        # score: prioritize matching the requested horizon, then nearness to the money
        score = abs(dte - target_days) * 2 + abs(float(strike) - spot)
        if best_score is None or score < best_score:
            best_score = score
            best = c
    if best is None:
        raise RuntimeError("No suitable put contracts found in the chain.")
    return best


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------

def print_scenario(
    *,
    ticker: str,
    spot: float,
    strike: float,
    premium: float,
    dte: int,
    iv: Optional[float],
    delta: Optional[float],
    theta: Optional[float],
    contract_symbol: Optional[str],
) -> None:
    contracts = 1
    cost = premium * 100 * contracts
    breakeven = strike - premium
    max_loss = cost

    print("=" * 68)
    print(f"  LONG PUT lesson - {ticker.upper()}")
    if contract_symbol:
        print(f"  Contract:        {contract_symbol}")
    print("=" * 68)
    print(f"  Stock price now: ${spot:,.2f}")
    print(f"  Strike price:    ${strike:,.2f}  (right to SELL at this price)")
    print(f"  Days to expiry:  {dte}")
    print(f"  Premium:         ${premium:,.2f}/share  ->  ${cost:,.2f} for 1 contract (100 sh)")
    if iv is not None:
        print(f"  Implied vol:     {iv * 100:,.1f}%")
    if delta is not None:
        print(f"  Delta:           {delta:+.2f}  (put gains ~${abs(delta)*100:,.0f} per $1 drop in stock)")
    if theta is not None:
        print(f"  Theta:           {theta:+.3f}/share  ->  loses ~${abs(theta)*100:,.2f}/day to time decay")
    print("-" * 68)
    print(f"  Breakeven at expiry: stock must be BELOW ${breakeven:,.2f}")
    print(f"  Max loss:            ${max_loss:,.2f}  (if stock >= ${strike:,.2f} at expiry)")
    print(f"  Max gain:            ${(strike - premium) * 100:,.2f}  (only if stock goes to $0)")
    print("=" * 68)

    # Expiration P&L table across a range of stock prices
    print("\n  WHAT IT'S WORTH AT EXPIRATION:\n")
    print(f"  {'Stock @ expiry':>16} {'Move':>8} {'Put value':>12} {'P&L':>12} {'Return':>9}")
    print("  " + "-" * 60)
    lo = max(0.0, strike * 0.70)
    hi = max(spot, strike) * 1.15
    steps = 11
    for i in range(steps):
        s = lo + (hi - lo) * i / (steps - 1)
        value = put_value_at_expiry(s, strike) * 100
        pnl = value - cost
        ret = (pnl / cost) * 100 if cost else 0.0
        move = (s - spot) / spot * 100 if spot else 0.0
        flag = "  <- breakeven" if abs(s - breakeven) < (hi - lo) / (steps - 1) / 2 else ""
        print(f"  {f'${s:.2f}':>16} {move:>+7.1f}% {f'${value:,.0f}':>12} "
              f"{f'${pnl:+,.0f}':>12} {ret:>+8.0f}%{flag}")

    # Time-decay demonstration (needs IV). We use Black-Scholes to find the
    # *fraction* of value lost over a week, then apply it to the real premium
    # the user paid so the dollars stay consistent with the numbers above.
    if iv is not None and dte > 7:
        today_val = black_scholes_put(spot, strike, dte, iv)
        week_val = black_scholes_put(spot, strike, dte - 7, iv)
        if today_val > 0:
            keep_frac = max(0.0, week_val / today_val)
            cost_now = premium * 100
            cost_week = cost_now * keep_frac
            decay = cost_now - cost_week
            print("\n  THE SILENT KILLER - TIME DECAY (theta):")
            print(f"  If the stock does NOT move at all for one week, this put goes")
            print(f"  from ~${cost_now:,.0f} to ~${cost_week:,.0f}  =>  you lose ${decay:,.0f} "
                  f"({decay / cost_now * 100:,.0f}%) doing nothing.")

    print("\n  REALITY CHECK:")
    print("  - You only profit if the stock falls BELOW the breakeven, and does it")
    print("    BEFORE expiry. Right direction but too slow = you still lose.")
    print("  - The big 'turned $X into $XX,000' stories are the rare tail. The")
    print("    median outcome for a bought option is a loss. Size it like a lottery")
    print("    ticket: only risk money you're fully prepared to see go to zero.")
    print()


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Learn how buying a put works, with real numbers.")
    parser.add_argument("--ticker", default="SPY", help="Underlying symbol (default SPY).")
    parser.add_argument("--days", type=int, default=30, help="Target days to expiration (default 30).")
    parser.add_argument("--manual", action="store_true", help="Skip the API; supply numbers by hand.")
    parser.add_argument("--price", type=float, help="[manual] current stock price.")
    parser.add_argument("--strike", type=float, help="[manual] put strike price.")
    parser.add_argument("--premium", type=float, help="[manual] put premium per share.")
    parser.add_argument("--iv", type=float, help="[manual] implied volatility, e.g. 0.45 for 45%%.")
    args = parser.parse_args(argv)

    if args.manual:
        if args.price is None or args.strike is None or args.premium is None:
            parser.error("--manual requires --price, --strike and --premium.")
        print_scenario(
            ticker=args.ticker,
            spot=args.price,
            strike=args.strike,
            premium=args.premium,
            dte=args.days,
            iv=args.iv,
            delta=None,
            theta=None,
            contract_symbol=None,
        )
        return 0

    api_key = os.getenv("POLYGON_API_KEY", "").strip()
    if not api_key:
        print("No POLYGON_API_KEY found. Either set it in your .env or use --manual.", file=sys.stderr)
        return 1

    try:
        spot = fetch_underlying_price(args.ticker, api_key)
        chain = fetch_put_chain(args.ticker, api_key)
        contract = pick_put(chain, spot, args.days)
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"Could not fetch live data: {exc}\nTry --manual to enter numbers by hand.", file=sys.stderr)
        return 1

    details = contract.get("details") or {}
    greeks = contract.get("greeks") or {}
    day = contract.get("day") or {}
    last_quote = contract.get("last_quote") or {}

    strike = float(details["strike_price"])
    exp_date = datetime.strptime(details["expiration_date"], "%Y-%m-%d").date()
    dte = (exp_date - date.today()).days

    # premium: prefer mid of quote, fall back to last trade / close
    premium = None
    bid, ask = last_quote.get("bid"), last_quote.get("ask")
    if bid and ask:
        premium = (float(bid) + float(ask)) / 2.0
    if premium is None:
        premium = day.get("close") or (contract.get("last_trade") or {}).get("price")
    if not premium:
        print("Contract found but no price available right now (market closed?). "
              "Try again during market hours or use --manual.", file=sys.stderr)
        return 1

    print_scenario(
        ticker=args.ticker,
        spot=spot,
        strike=strike,
        premium=float(premium),
        dte=dte,
        iv=contract.get("implied_volatility"),
        delta=greeks.get("delta"),
        theta=greeks.get("theta"),
        contract_symbol=details.get("ticker"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
