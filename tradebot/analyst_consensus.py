from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Optional

import requests

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)


_CONSENSUS_RE = re.compile(
    r"Analyst Consensus:\s*(Strong Buy|Buy|Hold|Sell|Strong Sell)\b",
    re.IGNORECASE,
)
_TARGET_RE = re.compile(r'Price Target:\s*\$?[0-9.,]+\s*\(([+\-]?[0-9.]+)%\)', re.IGNORECASE)
_YAHOO_RECOMMENDATION_RE = re.compile(r'\\?"recommendationKey\\?"\s*:\s*\\?"([^"\\]+)\\?"', re.IGNORECASE)
_YAHOO_TARGET_MEAN_RE = re.compile(r'\\?"targetMeanPrice\\?"\s*:\s*\{\s*\\?"raw\\?"\s*:\s*([0-9.]+)', re.IGNORECASE)
_YAHOO_CURRENT_PRICE_RE = re.compile(r'\\?"currentPrice\\?"\s*:\s*\{\s*\\?"raw\\?"\s*:\s*([0-9.]+)', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_CONSENSUS_LABELS = {
    "strong_buy": "Strong Buy",
    "strong buy": "Strong Buy",
    "buy": "Buy",
    "hold": "Hold",
    "sell": "Sell",
    "strong_sell": "Strong Sell",
    "strong sell": "Strong Sell",
}


@dataclass
class AnalystConsensusTracker:
    settings: Settings
    db: Database

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            }
        )

    def _cache_key(self, symbol: str) -> str:
        return f"analyst_consensus:{symbol.upper()}"

    def _load_cached(self, symbol: str) -> Optional[dict[str, Any]]:
        raw = self.db.get_bot_state(self._cache_key(symbol))
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        fetched_at = payload.get("fetched_at")
        if not fetched_at:
            return None
        try:
            fetched = datetime.fromisoformat(str(fetched_at))
        except ValueError:
            return None
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - fetched > timedelta(hours=self.settings.analyst_consensus_cache_hours):
            return None
        if not str(payload.get("consensus") or "").strip():
            return None
        return payload

    def _save_cached(self, symbol: str, payload: dict[str, Any]) -> None:
        self.db.set_bot_state(self._cache_key(symbol), json.dumps(payload))

    def _forecast_url(self, symbol: str) -> str:
        return f"https://stockanalysis.com/stocks/{symbol.lower()}/forecast/"

    def _yahoo_quote_url(self, symbol: str) -> str:
        return f"https://finance.yahoo.com/quote/{symbol.upper()}"

    def _normalize_consensus(self, value: str) -> str:
        key = unescape(value).strip().lower().replace("-", "_")
        return _CONSENSUS_LABELS.get(key, "")

    def _parse(self, html: str) -> Optional[dict[str, Any]]:
        text = _TAG_RE.sub(" ", html)
        text = re.sub(r"\s+", " ", text)
        consensus_match = _CONSENSUS_RE.search(text)
        if not consensus_match:
            return None
        consensus = consensus_match.group(1).strip()
        if not consensus:
            return None
        upside_match = _TARGET_RE.search(text)
        upside_pct = float(upside_match.group(1)) if upside_match else 0.0
        return {
            "consensus": consensus,
            "target_upside_pct": upside_pct,
        }

    def _parse_yahoo_quote(self, html: str) -> Optional[dict[str, Any]]:
        recommendation_match = _YAHOO_RECOMMENDATION_RE.search(html)
        if not recommendation_match:
            return None
        consensus = self._normalize_consensus(recommendation_match.group(1))
        if not consensus:
            return None

        target_upside_pct = 0.0
        target_match = _YAHOO_TARGET_MEAN_RE.search(html)
        current_match = _YAHOO_CURRENT_PRICE_RE.search(html)
        if target_match and current_match:
            target = float(target_match.group(1))
            current = float(current_match.group(1))
            target_upside_pct = ((target / current) - 1) * 100 if current > 0 else 0.0

        return {
            "consensus": consensus,
            "target_upside_pct": target_upside_pct,
        }

    def _fetch_yahoo_quote(self, symbol: str) -> Optional[dict[str, Any]]:
        url = self._yahoo_quote_url(symbol)
        resp = self.session.get(url, timeout=15)
        if resp.status_code >= 400:
            return None
        parsed = self._parse_yahoo_quote(resp.text)
        if not parsed:
            return None
        return {
            "symbol": symbol.upper(),
            "source": "yahoo_finance",
            "source_url": url,
            "consensus": parsed["consensus"],
            "target_upside_pct": parsed["target_upside_pct"],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def _fetch_stockanalysis_forecast(self, symbol: str) -> Optional[dict[str, Any]]:
        url = self._forecast_url(symbol)
        resp = self.session.get(url, timeout=15)
        if resp.status_code >= 400:
            return None
        parsed = self._parse(resp.text)
        if not parsed:
            return None
        return {
            "symbol": symbol.upper(),
            "source": "stockanalysis",
            "source_url": url,
            "consensus": parsed["consensus"],
            "target_upside_pct": parsed["target_upside_pct"],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def get(self, symbol: str) -> Optional[dict[str, Any]]:
        cached = self._load_cached(symbol)
        if cached:
            return cached
        try:
            payload = self._fetch_yahoo_quote(symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("Yahoo analyst consensus fetch failed for %s: %s", symbol, exc)
            payload = None
        if not payload:
            try:
                payload = self._fetch_stockanalysis_forecast(symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("StockAnalysis analyst consensus fetch failed for %s: %s", symbol, exc)
                payload = None
        if not payload:
            return None
        self._save_cached(symbol, payload)
        return payload
