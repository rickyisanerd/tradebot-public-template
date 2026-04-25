from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import requests

from .config import Settings


class SecTrackerError(RuntimeError):
    pass


@dataclass
class SecFiling:
    symbol: str
    cik: str
    form: str
    filing_date: str
    accession_number: str
    primary_document: str
    sec_url: str


class SecTracker:
    _TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
    _SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
    _POSITIVE_FORMS = {"4"}
    _DISCLOSURE_FORMS = {"8-K", "10-K", "10-Q"}
    _NEGATIVE_FORMS = {"S-1", "S-1/A", "S-3", "S-3ASR", "424B1", "424B2", "424B3", "424B4", "424B5"}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        if settings.sec_user_agent:
            self.session.headers.update({"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"})

    def refresh(self, symbols: List[str]) -> List[SecFiling]:
        if not self.settings.sec_user_agent:
            return []
        ticker_map = self._ticker_map()
        filings: List[SecFiling] = []
        for symbol in symbols:
            cik = ticker_map.get(symbol.upper())
            if not cik:
                continue
            filings.extend(self._fetch_symbol_filings(symbol.upper(), cik))
        return filings

    def _ticker_map(self) -> Dict[str, str]:
        payload = self._get_json(self._TICKER_URL)
        mapping: Dict[str, str] = {}
        for item in payload.values():
            ticker = str(item.get("ticker", "")).upper()
            cik_str = str(item.get("cik_str", "")).strip()
            if ticker and cik_str:
                mapping[ticker] = cik_str.zfill(10)
        return mapping

    def _fetch_symbol_filings(self, symbol: str, cik: str) -> List[SecFiling]:
        payload = self._get_json(self._SUBMISSIONS_URL.format(cik=cik))
        recent = payload.get("filings", {}).get("recent", {})
        forms = list(recent.get("form", []))
        filing_dates = list(recent.get("filingDate", []))
        accession_numbers = list(recent.get("accessionNumber", []))
        primary_documents = list(recent.get("primaryDocument", []))
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=max(1, self.settings.sec_signal_window_days))
        filings: List[SecFiling] = []
        for form, filing_date, accession_number, primary_document in zip(forms, filing_dates, accession_numbers, primary_documents):
            if form not in self._interesting_forms():
                continue
            parsed_date = datetime.strptime(filing_date, "%Y-%m-%d").date()
            if parsed_date < cutoff:
                continue
            accession_compact = str(accession_number).replace("-", "")
            sec_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_compact}/{primary_document}"
            filings.append(
                SecFiling(
                    symbol=symbol,
                    cik=cik,
                    form=form,
                    filing_date=filing_date,
                    accession_number=str(accession_number),
                    primary_document=str(primary_document),
                    sec_url=sec_url,
                )
            )
            if len(filings) >= self.settings.sec_filing_limit_per_symbol:
                break
        return filings

    def _interesting_forms(self) -> set[str]:
        return self._POSITIVE_FORMS | self._DISCLOSURE_FORMS | self._NEGATIVE_FORMS

    def _get_json(self, url: str) -> dict:
        try:
            response = self.session.get(url, timeout=20)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            raise SecTrackerError(f"Unable to fetch SEC data: {url}") from exc
