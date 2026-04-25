from __future__ import annotations

import io
import re
from typing import Callable, Dict, Iterable, List
from urllib.parse import urlparse

import requests
from pypdf import PdfReader

from .config import Settings
from .models import CongressTrade


class CongressTrackerError(RuntimeError):
    pass


class CongressTracker:
    _TRADE_RE = re.compile(
        r"(?P<asset>.+?)\((?P<symbol>[A-Z.\-]+)\)\s*\[ST\]\s*"
        r"(?P<side>P|S(?:\s*\(partial\))?)\s*"
        r"(?P<trade_date>\d{2}/\d{2}/\d{4})\s*"
        r"(?P<filed_date>\d{2}/\d{2}/\d{4})\s*"
        r"(?P<amount>\$[\d,]+(?:\s*-\s*\$[\d,]+|\+))"
    )

    def __init__(self, settings: Settings, price_lookup: Callable[[List[str]], Dict[str, float]]) -> None:
        self.settings = settings
        self.price_lookup = price_lookup
        self.session = requests.Session()

    def refresh(self) -> List[CongressTrade]:
        if not self.settings.congress_report_urls:
            return []
        trades: List[CongressTrade] = []
        for url in self.settings.congress_report_urls:
            trades.extend(self._fetch_report(url))
        prices = self.price_lookup(sorted({trade.symbol for trade in trades}))
        filtered: List[CongressTrade] = []
        for trade in trades:
            current_price = prices.get(trade.symbol)
            payload = trade.model_copy(
                update={
                    "current_price": round(float(current_price), 2) if current_price is not None else None,
                    "under_price_cap": current_price is not None and float(current_price) <= self.settings.congress_max_price,
                }
            )
            if payload.under_price_cap:
                filtered.append(payload)
        return filtered[: self.settings.congress_trade_limit]

    def _fetch_report(self, url: str) -> List[CongressTrade]:
        try:
            response = self.session.get(url, timeout=20)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise CongressTrackerError(f"Unable to download congressional report: {url}") from exc
        reader = PdfReader(io.BytesIO(response.content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        chamber = self._infer_chamber(url)
        return self.parse_ptr_text(text, url, chamber)

    def parse_ptr_text(self, text: str, source_url: str, chamber: str) -> List[CongressTrade]:
        member_match = re.search(r"Name:\s*(.+)", text)
        member = (member_match.group(1).strip() if member_match else "Unknown member").replace("  ", " ")
        trades: List[CongressTrade] = []
        for chunk in self._trade_chunks(text.splitlines()):
            match = self._TRADE_RE.search(chunk)
            if not match:
                continue
            side = match.group("side").strip()
            trades.append(
                CongressTrade(
                    member=member,
                    chamber=chamber,
                    symbol=match.group("symbol").upper(),
                    asset=" ".join(match.group("asset").split()),
                    side="buy" if side.startswith("P") else "sell",
                    trade_date=match.group("trade_date"),
                    filed_date=match.group("filed_date"),
                    amount_range=" ".join(match.group("amount").split()),
                    source_url=source_url,
                )
            )
        return trades

    def _trade_chunks(self, lines: Iterable[str]) -> Iterable[str]:
        buffer = ""
        for raw_line in lines:
            line = " ".join(raw_line.split())
            if not line:
                continue
            if "[ST]" in line and buffer:
                buffer = ""
            buffer = f"{buffer} {line}".strip()
            if "[ST]" not in buffer:
                continue
            if len(re.findall(r"\d{2}/\d{2}/\d{4}", buffer)) < 2:
                continue
            if "$" not in buffer:
                continue
            yield buffer
            buffer = ""

    def _infer_chamber(self, url: str) -> str:
        host = urlparse(url).netloc.lower()
        if "senate" in host:
            return "Senate"
        if "house" in host:
            return "House"
        return "Congress"
