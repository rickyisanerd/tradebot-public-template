"""Polygon.io / Massive Market Data integration.

Provides helpers that the trading engine can use to:
1.  Discover the *entire* sub-$10 stock universe in a single API call
    (Daily Market Summary), replacing slow per-batch Alpaca screening.
2.  Fetch short-volume data for squeeze-signal scoring.
3.  Query the Fed inflation endpoint so we no longer scrape web pages for CPI.
4.  Check upcoming market holidays to avoid placing orders after hours.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from .config import Settings

log = logging.getLogger(__name__)

_BASE = "https://api.polygon.io"


class PolygonClient:
    """Thin wrapper around Polygon.io REST endpoints used by TradeBot."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("POLYGON_API_KEY is required for Polygon integration")
        self.api_key = api_key
        self.session = requests.Session()
        # Cache for daily market summary (one call covers all stocks)
        self._market_summary_cache: Optional[List[dict]] = None
        self._market_summary_date: Optional[str] = None

    # ------------------------------------------------------------------
    # low-level
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None, retries: int = 2) -> Any:
        params = dict(params or {})
        params["apiKey"] = self.api_key
        url = f"{_BASE}{path}"
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    # Rate limited – wait & retry
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(f"Polygon {resp.status_code}: {resp.text[:300]}")
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < retries:
                    time.sleep(1)
        raise RuntimeError(f"Polygon request failed after {retries + 1} attempts: {last_err}")

    # ------------------------------------------------------------------
    # 1. Full-market universe discovery
    # ------------------------------------------------------------------

    def daily_market_summary(self, date: str) -> List[dict]:
        """GET /v2/aggs/grouped/locale/us/market/stocks/{date}

        Returns OHLCV for *every* US stock on the given date.
        Response items have keys: T (ticker), o, h, l, c, v, vw, t, n.
        """
        if self._market_summary_date == date and self._market_summary_cache is not None:
            return self._market_summary_cache
        payload = self._get(
            f"/v2/aggs/grouped/locale/us/market/stocks/{date}",
            params={"adjusted": "true"},
        )
        results = payload.get("results") or []
        self._market_summary_cache = results
        self._market_summary_date = date
        return results

    def sub10_universe(
        self,
        min_price: float = 2.0,
        max_price: float = 10.0,
        min_volume: int = 200_000,
    ) -> List[dict]:
        """Return all common-stock tickers that closed in the $2-$10 range
        with at least *min_volume* shares traded yesterday.

        Each item is a dict with keys: symbol, close, volume, dollar_volume.
        Sorted by dollar volume descending.
        """
        # Use yesterday (or last Friday if weekend)
        now = datetime.now(timezone.utc)
        # Walk back to find the most recent trading day
        for days_back in range(1, 5):
            candidate_date = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
            try:
                results = self.daily_market_summary(candidate_date)
                if results:
                    break
            except RuntimeError:
                continue
        else:
            log.warning("Polygon: unable to fetch daily market summary for recent dates")
            return []

        filtered: List[dict] = []
        for item in results:
            ticker = item.get("T", "")
            # Skip ETFs, warrants, preferred, etc. (contain ., -, /)
            if not ticker or any(ch in ticker for ch in (".", "-", "/", " ")):
                continue
            close = float(item.get("c", 0))
            volume = float(item.get("v", 0))
            if not (min_price <= close <= max_price):
                continue
            if volume < min_volume:
                continue
            dollar_volume = close * volume
            filtered.append({
                "symbol": ticker,
                "close": close,
                "volume": volume,
                "dollar_volume": dollar_volume,
            })

        filtered.sort(key=lambda x: x["dollar_volume"], reverse=True)
        return filtered

    # ------------------------------------------------------------------
    # 2. Short volume (squeeze signal)
    # ------------------------------------------------------------------

    def short_volume(self, ticker: str, days: int = 5) -> List[dict]:
        """GET /stocks/v1/short-volume — daily short volume for a ticker.

        Returns list of {date, short_volume, total_volume, short_volume_ratio}.
        """
        payload = self._get(
            "/stocks/v1/short-volume",
            params={
                "ticker": ticker,
                "limit": days,
                "sort": "date.desc",
            },
        )
        return payload.get("results") or []

    def short_volume_batch(self, tickers: List[str], days: int = 5) -> Dict[str, List[dict]]:
        """Fetch short volume for multiple tickers. Returns {ticker: [records]}."""
        out: Dict[str, List[dict]] = {}
        for ticker in tickers:
            try:
                out[ticker] = self.short_volume(ticker, days)
            except RuntimeError:
                out[ticker] = []
        return out

    # ------------------------------------------------------------------
    # 3. Fed inflation data (replaces CPI scraping)
    # ------------------------------------------------------------------

    def inflation_data(self, limit: int = 6) -> List[dict]:
        """GET /fed/v1/inflation — CPI, PCE, core variants.

        Returns list of {date, cpi, cpi_core, pce, pce_core, pce_spending}.
        """
        payload = self._get(
            "/fed/v1/inflation",
            params={"limit": limit, "sort": "date.desc"},
        )
        return payload.get("results") or []

    # ------------------------------------------------------------------
    # 4. Market holidays / status
    # ------------------------------------------------------------------

    def upcoming_holidays(self) -> List[dict]:
        """GET /v1/marketstatus/upcoming — next market holidays."""
        payload = self._get("/v1/marketstatus/upcoming")
        # The response is a list directly (not nested under "results")
        return payload if isinstance(payload, list) else []

    def market_status(self) -> dict:
        """GET /v1/marketstatus/now — current market open/closed status."""
        return self._get("/v1/marketstatus/now")

    # ------------------------------------------------------------------
    # 5. Historical bars (per ticker)
    # ------------------------------------------------------------------

    def bars(self, ticker: str, days: int = 80) -> List[dict]:
        """GET /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}

        Returns OHLCV bars in the same format the engine expects:
        [{t, o, h, l, c, v}, ...]
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days + 5)
        payload = self._get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}",
            params={"adjusted": "true", "limit": days + 10},
        )
        results = payload.get("results") or []
        # Normalize to the same bar format used by Alpaca/Demo brokers
        bars = []
        for r in results[-days:]:
            bars.append({
                "t": datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).isoformat(),
                "o": r["o"],
                "h": r["h"],
                "l": r["l"],
                "c": r["c"],
                "v": r["v"],
            })
        return bars

    def bars_batch(self, tickers: List[str], days: int = 80) -> Dict[str, List[dict]]:
        """Fetch bars for multiple tickers using concurrent requests."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        out: Dict[str, List[dict]] = {}

        def _fetch_one(ticker: str) -> tuple[str, List[dict]]:
            return ticker, self.bars(ticker, days)

        # Use up to 8 threads to speed up fetching while respecting rate limits
        max_workers = min(8, len(tickers))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_one, t): t for t in tickers}
            for future in as_completed(futures):
                try:
                    ticker, result = future.result()
                    if result:
                        out[ticker] = result
                except Exception:  # noqa: BLE001
                    continue
        return out


def build_polygon_client(settings: Settings) -> Optional[PolygonClient]:
    """Create a PolygonClient if POLYGON_API_KEY is configured."""
    if not settings.polygon_api_key:
        return None
    return PolygonClient(settings.polygon_api_key)
