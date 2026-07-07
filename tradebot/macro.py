from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

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
    """Tracks upcoming FOMC meeting dates from the Fed's public calendar.

    CPI release-date tracking was removed: the only free schedule source
    (usinflationcalculator.com) rate-limits datacenter IPs, and its failures
    were pushing the bot into degraded mode for a signal with no proven edge.
    """

    _FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

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

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
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
        return self._dedupe(self._fetch_fomc())

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
