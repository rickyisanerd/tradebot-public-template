from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from pypdf import PdfReader

from .config import Settings
from .models import CongressTrade

log = logging.getLogger("tradebot.congress")

HOUSE_INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
SENATE_BASE_URL = "https://efdsearch.senate.gov"
USER_AGENT = "tradebot/1.0 (congressional disclosure tracker)"

_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")
_SYMBOL_RE = re.compile(r"[A-Z][A-Z.\-]{0,9}")
_HREF_RE = re.compile(r'href="([^"]+)"')


class CongressTrackerError(RuntimeError):
    pass


class _TableParser(HTMLParser):
    """Collects the text of every table cell, grouped by row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join(" ".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_html_table_rows(html: str) -> List[List[str]]:
    parser = _TableParser()
    parser.feed(html)
    return parser.rows


def parse_house_index(index_text: str, cutoff: date) -> List[Tuple[str, str, date]]:
    """Parse the Clerk's tab-separated filing index into (member, doc_id, filed)
    tuples for periodic transaction reports filed on or after the cutoff."""
    entries: List[Tuple[str, str, date]] = []
    for line in index_text.splitlines():
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        prefix, last, first, suffix, filing_type, _district, _year, filing_date, doc_id = (
            part.strip() for part in parts[:9]
        )
        if filing_type.upper() != "P" or not doc_id:
            continue
        try:
            filed = datetime.strptime(filing_date, "%m/%d/%Y").date()
        except ValueError:
            continue
        if filed < cutoff:
            continue
        member = " ".join(part for part in (prefix, first, last, suffix) if part)
        entries.append((member or "Unknown member", doc_id, filed))
    entries.sort(key=lambda entry: entry[2], reverse=True)
    return entries


class CongressTracker:
    _TRADE_RE = re.compile(
        r"(?P<asset>.+?)\((?P<symbol>[A-Z.\-]+)\)\s*\[ST\]\s*"
        r"(?P<side>P|S(?:\s*\(partial\))?)\s*"
        r"(?P<trade_date>\d{2}/\d{2}/\d{4})\s*"
        r"(?P<filed_date>\d{2}/\d{2}/\d{4})\s*"
        r"(?P<amount>\$[\d,]+(?:\s*-\s*\$[\d,]+|\s*\+))"
    )
    # Buffers longer than this without a completed trade are reflowed page
    # furniture, not a transaction row.
    _MAX_CHUNK_CHARS = 600

    def __init__(self, settings: Settings, price_lookup: Callable[[List[str]], Dict[str, float]]) -> None:
        self.settings = settings
        self.price_lookup = price_lookup
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def refresh(self) -> List[CongressTrade]:
        trades: List[CongressTrade] = []
        sources_attempted = 0
        sources_succeeded = 0
        if self.settings.congress_auto_fetch:
            sources_attempted += 1
            try:
                trades.extend(self._house_trades())
                sources_succeeded += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("House disclosure refresh failed: %s", exc)
            if self.settings.congress_include_senate:
                sources_attempted += 1
                try:
                    trades.extend(self._senate_trades())
                    sources_succeeded += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("Senate disclosure refresh failed: %s", exc)
        for url in self.settings.congress_report_urls:
            sources_attempted += 1
            try:
                trades.extend(self._fetch_report(url))
                sources_succeeded += 1
            except CongressTrackerError as exc:
                log.warning("%s", exc)
        if sources_attempted and not sources_succeeded:
            raise CongressTrackerError("All congressional disclosure sources failed")
        if not trades:
            return []
        trades = self._dedupe(trades)
        prices = self.price_lookup(sorted({trade.symbol for trade in trades}))
        # Matching MAX_STOCK_PRICE semantics, a cap of 0 (or below) disables
        # the price filter entirely.
        max_price = float(self.settings.congress_max_price)
        filtered: List[CongressTrade] = []
        for trade in trades:
            current_price = prices.get(trade.symbol)
            under_cap = max_price <= 0 or (current_price is not None and float(current_price) <= max_price)
            payload = trade.model_copy(
                update={
                    "current_price": round(float(current_price), 2) if current_price is not None else None,
                    "under_price_cap": under_cap,
                }
            )
            if payload.under_price_cap:
                filtered.append(payload)
        filtered.sort(key=self._freshness_key, reverse=True)
        return filtered[: self.settings.congress_trade_limit]

    # ── House of Representatives ───────────────────────────────

    def _house_trades(self) -> List[CongressTrade]:
        today = datetime.now(timezone.utc).date()
        cutoff = today - timedelta(days=max(1, self.settings.congress_lookback_days))
        entries: List[Tuple[str, str, date, int]] = []
        for year in sorted({cutoff.year, today.year}):
            try:
                index_text = self._download_house_index(year)
            except Exception as exc:  # noqa: BLE001
                # The current year's index may not exist in early January.
                log.warning("House filing index unavailable for %s: %s", year, exc)
                continue
            for member, doc_id, filed in parse_house_index(index_text, cutoff):
                entries.append((member, doc_id, filed, year))
        if not entries:
            return []
        entries.sort(key=lambda entry: entry[2], reverse=True)
        trades: List[CongressTrade] = []
        for member, doc_id, _filed, year in entries[: max(1, self.settings.congress_max_reports)]:
            url = HOUSE_PTR_URL.format(year=year, doc_id=doc_id)
            try:
                trades.extend(self._fetch_report(url, fallback_member=member))
            except CongressTrackerError as exc:
                # Paper filings are scanned images with no extractable text.
                log.debug("Skipping house PTR %s: %s", url, exc)
        return trades

    def _download_house_index(self, year: int) -> str:
        url = HOUSE_INDEX_URL.format(year=year)
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            for name in archive.namelist():
                if name.lower().endswith(".txt"):
                    return archive.read(name).decode("utf-8", errors="replace")
        raise CongressTrackerError(f"No filing index found inside {url}")

    # ── Senate ─────────────────────────────────────────────────

    def _senate_trades(self) -> List[CongressTrade]:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=max(1, self.settings.congress_lookback_days))
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        session.get(f"{SENATE_BASE_URL}/search/home/", timeout=30).raise_for_status()
        csrf = session.cookies.get("csrftoken", "")
        session.post(
            f"{SENATE_BASE_URL}/search/home/",
            data={"csrfmiddlewaretoken": csrf, "prohibition_agreement": "1"},
            headers={"Referer": f"{SENATE_BASE_URL}/search/home/"},
            timeout=30,
        ).raise_for_status()
        csrf = session.cookies.get("csrftoken", "") or csrf
        response = session.post(
            f"{SENATE_BASE_URL}/search/report/data/",
            data={
                "start": "0",
                "length": str(max(1, self.settings.congress_max_reports)),
                "report_types": "[11]",
                "filer_types": "[]",
                "submitted_start_date": cutoff.strftime("%m/%d/%Y") + " 00:00:00",
                "submitted_end_date": "",
                "candidate_state": "",
                "senator_state": "",
                "office_id": "",
                "first_name": "",
                "last_name": "",
            },
            headers={"Referer": f"{SENATE_BASE_URL}/search/", "X-CSRFToken": csrf},
            timeout=30,
        )
        response.raise_for_status()
        trades: List[CongressTrade] = []
        for row in response.json().get("data", []):
            cells = [cell for cell in row if isinstance(cell, str)]
            link = next((match.group(1) for cell in cells for match in [_HREF_RE.search(cell)] if match), "")
            if "/search/view/ptr/" not in link:
                continue  # paper filings are scanned images with no parseable table
            filed_date = next(
                (match.group(0) for cell in reversed(cells) for match in [_DATE_RE.fullmatch(cell.strip())] if match),
                "",
            )
            member = self._senate_member_name(cells)
            url = SENATE_BASE_URL + link
            try:
                page = session.get(url, timeout=30)
                page.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                log.warning("Unable to download Senate PTR %s: %s", url, exc)
                continue
            trades.extend(self.parse_senate_ptr_html(page.text, url, member, filed_date))
        return trades

    @staticmethod
    def _senate_member_name(cells: List[str]) -> str:
        parts = " ".join(cells[:2]).split() if len(cells) >= 2 else []
        cleaned = [part.title() if part.isupper() else part for part in parts]
        return " ".join(cleaned) or "Unknown senator"

    def parse_senate_ptr_html(self, html: str, source_url: str, member: str, filed_date: str) -> List[CongressTrade]:
        trades: List[CongressTrade] = []
        for cells in parse_html_table_rows(html):
            if len(cells) < 8:
                continue
            tx_date, _owner, ticker, asset, asset_type, tx_type, amount = (cell.strip() for cell in cells[1:8])
            if not _DATE_RE.fullmatch(tx_date):
                continue
            symbol = ticker.upper()
            if not _SYMBOL_RE.fullmatch(symbol):
                continue  # non-equity rows show "--"
            kind = asset_type.lower()
            if not kind.startswith("stock") or "option" in kind:
                continue
            side_text = tx_type.lower()
            if side_text.startswith("purchase"):
                side = "buy"
            elif side_text.startswith("sale"):
                side = "sell"
            else:
                continue  # exchanges and other transaction types
            trades.append(
                CongressTrade(
                    member=member,
                    chamber="Senate",
                    symbol=symbol,
                    asset=" ".join(asset.split()),
                    side=side,
                    trade_date=self._pad_date(tx_date),
                    filed_date=self._pad_date(filed_date or tx_date),
                    amount_range=" ".join(amount.split()),
                    source_url=source_url,
                )
            )
        return trades

    @staticmethod
    def _pad_date(value: str) -> str:
        try:
            return datetime.strptime(value, "%m/%d/%Y").strftime("%m/%d/%Y")
        except ValueError:
            return value

    # ── Shared PDF parsing ─────────────────────────────────────

    def _fetch_report(self, url: str, fallback_member: str | None = None) -> List[CongressTrade]:
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            reader = PdfReader(io.BytesIO(response.content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:  # noqa: BLE001
            raise CongressTrackerError(f"Unable to download congressional report: {url}") from exc
        chamber = self._infer_chamber(url)
        return self.parse_ptr_text(text, url, chamber, fallback_member=fallback_member)

    def parse_ptr_text(
        self,
        text: str,
        source_url: str,
        chamber: str,
        fallback_member: str | None = None,
    ) -> List[CongressTrade]:
        member_match = re.search(r"Name:\s*(.+)", text)
        if member_match:
            member = " ".join(member_match.group(1).split())
        else:
            member = fallback_member or "Unknown member"
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
        # Asset names often wrap onto the line(s) before the "[ST]" marker, and
        # dollar ranges onto the line after the dates, so a trade is complete
        # only once the full pattern matches — not as soon as "[ST]" appears.
        pending: List[str] = []
        buffer = ""
        for raw_line in lines:
            line = " ".join(raw_line.split())
            if not line:
                continue
            if buffer and "[ST]" in line:
                buffer = ""  # the previous trade never completed; drop it
            if buffer:
                buffer = f"{buffer} {line}"
            elif "[ST]" in line:
                buffer = " ".join(pending + [line])
                pending = []
            else:
                # Hold potential asset-name fragments; labels, amounts, and
                # column headers are never part of an asset name.
                if ":" not in line and "$" not in line and "?" not in line:
                    pending.append(line)
                    pending = pending[-2:]
                else:
                    pending = []
                continue
            if self._TRADE_RE.search(buffer):
                yield buffer
                buffer = ""
            elif len(buffer) > self._MAX_CHUNK_CHARS:
                buffer = ""

    # ── Helpers ────────────────────────────────────────────────

    @staticmethod
    def _dedupe(trades: List[CongressTrade]) -> List[CongressTrade]:
        seen = set()
        unique: List[CongressTrade] = []
        for trade in trades:
            key = (trade.source_url, trade.symbol, trade.side, trade.trade_date, trade.amount_range)
            if key in seen:
                continue
            seen.add(key)
            unique.append(trade)
        return unique

    @staticmethod
    def _freshness_key(trade: CongressTrade) -> Tuple[date, date]:
        def parse(value: str) -> date:
            try:
                return datetime.strptime(value, "%m/%d/%Y").date()
            except ValueError:
                return date.min

        return (parse(trade.filed_date), parse(trade.trade_date))

    def _infer_chamber(self, url: str) -> str:
        host = urlparse(url).netloc.lower()
        if "senate" in host:
            return "Senate"
        if "house" in host:
            return "House"
        return "Congress"
