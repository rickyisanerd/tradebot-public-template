from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List

import requests

from .config import Settings


class EarningsTrackerError(RuntimeError):
    pass


@dataclass
class EarningsEvent:
    symbol: str
    earnings_date: str
    report_time: str
    fiscal_date_ending: str
    estimate: str
    currency: str


class EarningsTracker:
    _URL = "https://www.alphavantage.co/query"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()

    def refresh(self, symbols: List[str]) -> List[EarningsEvent]:
        if not self.settings.alpha_vantage_api_key:
            return []
        params = {
            "function": "EARNINGS_CALENDAR",
            "horizon": "3month",
            "apikey": self.settings.alpha_vantage_api_key,
        }
        try:
            response = self.session.get(self._URL, params=params, timeout=20)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise EarningsTrackerError("Unable to fetch earnings calendar.") from exc
        return self._parse_csv(response.text, symbols)

    def _parse_csv(self, text: str, symbols: List[str]) -> List[EarningsEvent]:
        wanted = {symbol.upper() for symbol in symbols}
        today = datetime.now(timezone.utc).date()
        cutoff = today + timedelta(days=max(1, self.settings.earnings_signal_window_days))
        reader = csv.DictReader(io.StringIO(text))
        events: List[EarningsEvent] = []
        for row in reader:
            symbol = str(row.get("symbol", "")).upper()
            earnings_date = str(row.get("reportDate", "")).strip()
            if symbol not in wanted or not earnings_date:
                continue
            parsed_date = datetime.strptime(earnings_date, "%Y-%m-%d").date()
            if parsed_date > cutoff:
                continue
            events.append(
                EarningsEvent(
                    symbol=symbol,
                    earnings_date=earnings_date,
                    report_time=str(row.get("reportTime", "")).strip(),
                    fiscal_date_ending=str(row.get("fiscalDateEnding", "")).strip(),
                    estimate=str(row.get("estimate", "")).strip(),
                    currency=str(row.get("currency", "")).strip(),
                )
            )
        return events
