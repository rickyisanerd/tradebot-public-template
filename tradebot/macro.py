from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests

from .config import Settings

log = logging.getLogger(__name__)


class MacroTrackerError(RuntimeError):
    pass


@dataclass
class MacroEvent:
    event_type: str
    event_date: str
    source: str


class MacroTracker:
    _CPI_URL = "https://www.usinflationcalculator.com/inflation/consumer-price-index-release-schedule/"
    _FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

    # Matches dates like "Apr. 10, 2026" or "Jun. 10, 2026" from usinflationcalculator
    _CPI_DATE_RE = re.compile(
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    )

    # Parses the Fed calendar HTML which uses fomc-meeting__month and
    # fomc-meeting__date CSS classes inside year-headed panels.
    _FOMC_YEAR_RE = re.compile(r"(\d{4})\s+FOMC\s+Meetings")
    _FOMC_MONTH_TAG_RE = re.compile(
        r'fomc-meeting__month[^>]*>(?:<[^>]+>)*\s*'
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    )
    _FOMC_DATE_TAG_RE = re.compile(
        r'fomc-meeting__date[^>]*>\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s*\*?'
    )

    def __init__(self, settings: Settings, polygon_client: Optional[object] = None) -> None:
        self.settings = settings
        self._polygon = polygon_client
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    settings.sec_user_agent
                    if settings.sec_user_agent
                    else "TradeBot/1.0 (macro calendar fetcher)"
                ),
            }
        )

    def refresh(self) -> List[MacroEvent]:
        cpi = self._fetch_cpi()
        fomc = self._fetch_fomc()
        return self._dedupe(cpi + fomc)

    def _fetch_cpi_from_polygon(self) -> List[MacroEvent]:
        """Use Polygon /fed/v1/inflation API for CPI data.

        The inflation endpoint returns monthly observations with CPI values.
        We use the observation dates as approximate CPI release dates.
        """
        if not self._polygon:
            return []
        try:
            records = self._polygon.inflation_data(limit=12)
        except Exception as exc:  # noqa: BLE001
            log.warning("Polygon inflation API failed: %s", exc)
            return []
        events: List[MacroEvent] = []
        today = datetime.now(timezone.utc).date()
        for record in records:
            date_str = record.get("date")
            if not date_str:
                continue
            try:
                # Polygon returns the observation month (e.g. 2026-02-01).
                # CPI is typically released ~2 weeks into the following month.
                obs_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                # Approximate CPI release: 13th of the month after observation
                if obs_date.month == 12:
                    release_date = obs_date.replace(year=obs_date.year + 1, month=1, day=13)
                else:
                    release_date = obs_date.replace(month=obs_date.month + 1, day=13)
            except ValueError:
                continue
            if release_date < today:
                continue
            events.append(
                MacroEvent(
                    event_type="cpi",
                    event_date=release_date.isoformat(),
                    source="polygon.io/fed/v1/inflation",
                )
            )
        return events

    def _fetch_cpi(self) -> List[MacroEvent]:
        # Try Polygon first (reliable API), fall back to web scraping
        polygon_events = self._fetch_cpi_from_polygon()
        if polygon_events:
            return polygon_events

        html = self._get_text(self._CPI_URL)
        events: List[MacroEvent] = []
        today = datetime.now(timezone.utc).date()
        for raw_date in self._CPI_DATE_RE.findall(html):
            cleaned = raw_date.replace(".", "").replace(",", ",").strip()
            for fmt in ("%b %d, %Y", "%b %d %Y", "%B %d, %Y", "%B %d %Y"):
                try:
                    event_date = datetime.strptime(cleaned, fmt).date()
                    break
                except ValueError:
                    continue
            else:
                continue
            if event_date < today:
                continue
            events.append(
                MacroEvent(
                    event_type="cpi",
                    event_date=event_date.isoformat(),
                    source=self._CPI_URL,
                )
            )
        return events

    def _fetch_fomc(self) -> List[MacroEvent]:
        html = self._get_text(self._FOMC_URL)
        events: List[MacroEvent] = []
        today = datetime.now(timezone.utc).date()
        current_year: int | None = None

        # Walk through the HTML looking for year headers, then month/date pairs
        # within fomc-meeting divs.
        lines = html.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]

            year_match = self._FOMC_YEAR_RE.search(line)
            if year_match:
                current_year = int(year_match.group(1))
                i += 1
                continue

            if current_year:
                month_match = self._FOMC_MONTH_TAG_RE.search(line)
                if month_match:
                    month_name = month_match.group(1)
                    # Look ahead in nearby lines for the date
                    for j in range(i, min(i + 5, len(lines))):
                        date_match = self._FOMC_DATE_TAG_RE.search(lines[j])
                        if date_match:
                            day = int(date_match.group(2) or date_match.group(1))
                            try:
                                event_date = datetime.strptime(
                                    f"{month_name} {day} {current_year}", "%B %d %Y"
                                ).date()
                            except ValueError:
                                break
                            if event_date >= today:
                                events.append(
                                    MacroEvent(
                                        event_type="fomc",
                                        event_date=event_date.isoformat(),
                                        source=self._FOMC_URL,
                                    )
                                )
                            break
            i += 1
        return events

    def _dedupe(self, events: List[MacroEvent]) -> List[MacroEvent]:
        seen: set[tuple[str, str]] = set()
        unique: List[MacroEvent] = []
        for event in sorted(events, key=lambda item: (item.event_type, item.event_date)):
            key = (event.event_type, event.event_date)
            if key in seen:
                continue
            seen.add(key)
            unique.append(event)
        return unique

    def _get_text(self, url: str) -> str:
        try:
            response = self.session.get(url, timeout=20)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            raise MacroTrackerError(f"Unable to fetch macro calendar: {url}") from exc
