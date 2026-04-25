from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init_db(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    broker_mode TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    candidates_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trade_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT DEFAULT '',
                    pnl_pct REAL,
                    pnl_amount REAL,
                    analysis_json TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS position_meta (
                    symbol TEXT PRIMARY KEY,
                    opened_at TEXT NOT NULL,
                    qty REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    analysis_json TEXT NOT NULL,
                    exit_pending INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS learning (
                    strategy TEXT PRIMARY KEY,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    total_return REAL NOT NULL DEFAULT 0,
                    weight REAL NOT NULL DEFAULT 1.0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS congress_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    member TEXT NOT NULL,
                    chamber TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    side TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    filed_date TEXT NOT NULL,
                    amount_range TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    current_price REAL,
                    under_price_cap INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(source_url, symbol, side, trade_date, amount_range)
                );

                CREATE TABLE IF NOT EXISTS sec_filings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    cik TEXT NOT NULL,
                    form TEXT NOT NULL,
                    filing_date TEXT NOT NULL,
                    accession_number TEXT NOT NULL,
                    primary_document TEXT NOT NULL,
                    sec_url TEXT NOT NULL,
                    UNIQUE(symbol, accession_number, form)
                );

                CREATE TABLE IF NOT EXISTS earnings_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    earnings_date TEXT NOT NULL,
                    report_time TEXT NOT NULL,
                    fiscal_date_ending TEXT NOT NULL,
                    estimate TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    UNIQUE(symbol, earnings_date, report_time)
                );

                CREATE TABLE IF NOT EXISTS macro_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    UNIQUE(event_type, event_date)
                );

                CREATE TABLE IF NOT EXISTS signal_status (
                    source TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    last_attempt_at TEXT,
                    last_success_at TEXT,
                    error_message TEXT DEFAULT '',
                    records_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT
                );

                CREATE TABLE IF NOT EXISTS signal_refresh_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    records_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT DEFAULT '',
                    next_retry_at TEXT
                );

                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT DEFAULT '{}'
                );
                """
            )
            columns = {row["name"] for row in con.execute("PRAGMA table_info(position_meta)").fetchall()}
            trade_columns = {row["name"] for row in con.execute("PRAGMA table_info(trade_events)").fetchall()}
            if "exit_pending" not in columns:
                con.execute("ALTER TABLE position_meta ADD COLUMN exit_pending INTEGER NOT NULL DEFAULT 0")
            signal_columns = {row["name"] for row in con.execute("PRAGMA table_info(signal_status)").fetchall()}
            if "failure_count" not in signal_columns:
                con.execute("ALTER TABLE signal_status ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0")
            if "next_retry_at" not in signal_columns:
                con.execute("ALTER TABLE signal_status ADD COLUMN next_retry_at TEXT")
            if "pnl_amount" not in trade_columns:
                con.execute("ALTER TABLE trade_events ADD COLUMN pnl_amount REAL")
            if "partial_profit_taken" not in columns:
                con.execute("ALTER TABLE position_meta ADD COLUMN partial_profit_taken INTEGER NOT NULL DEFAULT 0")
            if "peak_price" not in columns:
                con.execute("ALTER TABLE position_meta ADD COLUMN peak_price REAL NOT NULL DEFAULT 0")
            for strategy in ("decision_support", "momentum", "reversion", "risk"):
                con.execute(
                    """
                    INSERT INTO learning(strategy, wins, losses, total_return, weight, updated_at)
                    VALUES (?, 0, 0, 0, 1.0, ?)
                    ON CONFLICT(strategy) DO NOTHING
                    """,
                    (strategy, utc_now()),
                )

    def record_scan(self, broker_mode: str, provider: str, candidates: List[Dict[str, Any]]) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO scans(created_at, broker_mode, provider, candidates_json) VALUES (?, ?, ?, ?)",
                (utc_now(), broker_mode, provider, json.dumps(candidates)),
                )

    def replace_congress_trades(self, trades: List[Dict[str, Any]]) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM congress_trades")
            for trade in trades:
                con.execute(
                    """
                    INSERT INTO congress_trades(
                        created_at, member, chamber, symbol, asset, side, trade_date, filed_date,
                        amount_range, source_url, current_price, under_price_cap
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        utc_now(),
                        trade["member"],
                        trade["chamber"],
                        trade["symbol"],
                        trade["asset"],
                        trade["side"],
                        trade["trade_date"],
                        trade["filed_date"],
                        trade["amount_range"],
                        trade["source_url"],
                        trade.get("current_price"),
                        1 if trade.get("under_price_cap") else 0,
                    ),
                )

    def recent_congress_trades(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT member, chamber, symbol, asset, side, trade_date, filed_date, amount_range,
                       source_url, current_price, under_price_cap
                FROM congress_trades
                ORDER BY id DESC
                """
            ).fetchall()
            items = [dict(row) for row in rows]
            items.sort(
                key=lambda item: (
                    datetime.strptime(item["filed_date"], "%m/%d/%Y"),
                    datetime.strptime(item["trade_date"], "%m/%d/%Y"),
                ),
                reverse=True,
            )
            return items[:limit]

    def congress_signal_for_symbol(self, symbol: str, window_days: int) -> Dict[str, float]:
        today = datetime.now(timezone.utc).date()
        cutoff = today - timedelta(days=max(1, window_days))
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT side, trade_date, filed_date
                FROM congress_trades
                WHERE symbol = ?
                ORDER BY id DESC
                """,
                (symbol.upper(),),
            ).fetchall()

        buy_count = 0
        sell_count = 0
        latest_trade_days: int | None = None
        latest_filed_days: int | None = None
        for row in rows:
            trade_date = datetime.strptime(row["trade_date"], "%m/%d/%Y").date()
            if trade_date < cutoff:
                continue
            filed_date = datetime.strptime(row["filed_date"], "%m/%d/%Y").date()
            age_trade = max(0, (today - trade_date).days)
            age_filed = max(0, (today - filed_date).days)
            if row["side"] == "buy":
                buy_count += 1
            else:
                sell_count += 1
            latest_trade_days = age_trade if latest_trade_days is None else min(latest_trade_days, age_trade)
            latest_filed_days = age_filed if latest_filed_days is None else min(latest_filed_days, age_filed)

        return {
            "congress_buy_count": float(buy_count),
            "congress_sell_count": float(sell_count),
            "congress_net_count": float(buy_count - sell_count),
            "days_since_congress_trade": float(latest_trade_days if latest_trade_days is not None else window_days + 1),
            "days_since_congress_filed": float(latest_filed_days if latest_filed_days is not None else window_days + 1),
        }

    def replace_sec_filings_for_symbol(self, symbol: str, filings: List[Dict[str, Any]]) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM sec_filings WHERE symbol = ?", (symbol.upper(),))
            for filing in filings:
                con.execute(
                    """
                    INSERT INTO sec_filings(
                        created_at, symbol, cik, form, filing_date, accession_number, primary_document, sec_url
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        utc_now(),
                        filing["symbol"].upper(),
                        filing["cik"],
                        filing["form"],
                        filing["filing_date"],
                        filing["accession_number"],
                        filing["primary_document"],
                        filing["sec_url"],
                    ),
                )

    def sec_signal_for_symbol(self, symbol: str, window_days: int) -> Dict[str, float]:
        today = datetime.now(timezone.utc).date()
        cutoff = today - timedelta(days=max(1, window_days))
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT form, filing_date
                FROM sec_filings
                WHERE symbol = ?
                ORDER BY filing_date DESC
                """,
                (symbol.upper(),),
            ).fetchall()

        form4_count = 0
        disclosure_count = 0
        offering_count = 0
        latest_filing_days: int | None = None
        for row in rows:
            filing_date = datetime.strptime(row["filing_date"], "%Y-%m-%d").date()
            if filing_date < cutoff:
                continue
            age_days = max(0, (today - filing_date).days)
            latest_filing_days = age_days if latest_filing_days is None else min(latest_filing_days, age_days)
            form = row["form"]
            if form == "4":
                form4_count += 1
            elif form in {"8-K", "10-K", "10-Q"}:
                disclosure_count += 1
            elif form in {"S-1", "S-1/A", "S-3", "S-3ASR", "424B1", "424B2", "424B3", "424B4", "424B5"}:
                offering_count += 1

        return {
            "sec_form4_count": float(form4_count),
            "sec_disclosure_count": float(disclosure_count),
            "sec_offering_filing_count": float(offering_count),
            "days_since_sec_filing": float(latest_filing_days if latest_filing_days is not None else window_days + 1),
        }

    def replace_earnings_events_for_symbol(self, symbol: str, events: List[Dict[str, Any]]) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM earnings_events WHERE symbol = ?", (symbol.upper(),))
            for event in events:
                con.execute(
                    """
                    INSERT INTO earnings_events(
                        created_at, symbol, earnings_date, report_time, fiscal_date_ending, estimate, currency
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        utc_now(),
                        event["symbol"].upper(),
                        event["earnings_date"],
                        event["report_time"],
                        event["fiscal_date_ending"],
                        event["estimate"],
                        event["currency"],
                    ),
                )

    def earnings_signal_for_symbol(self, symbol: str, window_days: int) -> Dict[str, float]:
        today = datetime.now(timezone.utc).date()
        cutoff = today + timedelta(days=max(1, window_days))
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT earnings_date, report_time
                FROM earnings_events
                WHERE symbol = ?
                ORDER BY earnings_date ASC
                """,
                (symbol.upper(),),
            ).fetchall()

        next_earnings_days: int | None = None
        before_open = 0
        after_close = 0
        for row in rows:
            earnings_date = datetime.strptime(row["earnings_date"], "%Y-%m-%d").date()
            if earnings_date > cutoff:
                continue
            days_until = (earnings_date - today).days
            if days_until < 0:
                continue
            next_earnings_days = days_until if next_earnings_days is None else min(next_earnings_days, days_until)
            report_time = (row["report_time"] or "").lower()
            if report_time == "pre-market":
                before_open += 1
            elif report_time == "post-market":
                after_close += 1

        return {
            "days_until_earnings": float(next_earnings_days if next_earnings_days is not None else window_days + 1),
            "earnings_before_open_count": float(before_open),
            "earnings_after_close_count": float(after_close),
            "has_upcoming_earnings": 1.0 if next_earnings_days is not None else 0.0,
        }

    def replace_macro_events(self, events: List[Dict[str, Any]]) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM macro_events")
            for event in events:
                con.execute(
                    """
                    INSERT INTO macro_events(created_at, event_type, event_date, source)
                    VALUES (?, ?, ?, ?)
                    """,
                    (utc_now(), event["event_type"], event["event_date"], event["source"]),
                )

    def macro_signal(self, window_days: int) -> Dict[str, float]:
        today = datetime.now(timezone.utc).date()
        cutoff = today + timedelta(days=max(1, window_days))
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT event_type, event_date
                FROM macro_events
                ORDER BY event_date ASC
                """
            ).fetchall()

        next_macro_days: int | None = None
        cpi_count = 0
        fomc_count = 0
        for row in rows:
            event_date = datetime.strptime(row["event_date"], "%Y-%m-%d").date()
            if event_date > cutoff:
                continue
            days_until = (event_date - today).days
            if days_until < 0:
                continue
            next_macro_days = days_until if next_macro_days is None else min(next_macro_days, days_until)
            if row["event_type"] == "cpi":
                cpi_count += 1
            elif row["event_type"] == "fomc":
                fomc_count += 1

        return {
            "days_until_macro_event": float(next_macro_days if next_macro_days is not None else window_days + 1),
            "has_near_macro_event": 1.0 if next_macro_days is not None else 0.0,
            "near_cpi_count": float(cpi_count),
            "near_fomc_count": float(fomc_count),
        }

    def update_signal_status(
        self,
        source: str,
        status: str,
        *,
        last_attempt_at: str | None = None,
        last_success_at: str | None = None,
        error_message: str = "",
        records_count: int = 0,
        failure_count: int | None = None,
        next_retry_at: str | None = None,
    ) -> None:
        with self.connect() as con:
            existing = con.execute(
                "SELECT last_attempt_at, last_success_at, failure_count, next_retry_at FROM signal_status WHERE source = ?",
                (source,),
            ).fetchone()
            current_attempt = existing["last_attempt_at"] if existing else None
            current_success = existing["last_success_at"] if existing else None
            current_failure_count = existing["failure_count"] if existing else 0
            current_next_retry_at = existing["next_retry_at"] if existing else None
            con.execute(
                """
                INSERT INTO signal_status(
                    source, status, last_attempt_at, last_success_at, error_message, records_count, failure_count, next_retry_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    status=excluded.status,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=excluded.last_success_at,
                    error_message=excluded.error_message,
                    records_count=excluded.records_count,
                    failure_count=excluded.failure_count,
                    next_retry_at=excluded.next_retry_at
                """,
                (
                    source,
                    status,
                    last_attempt_at or current_attempt,
                    last_success_at or current_success,
                    error_message,
                    records_count,
                    current_failure_count if failure_count is None else failure_count,
                    current_next_retry_at if next_retry_at is None else next_retry_at,
                ),
            )

    def signal_statuses(self) -> Dict[str, Dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM signal_status ORDER BY source").fetchall()
            return {row["source"]: dict(row) for row in rows}

    def record_signal_refresh_event(
        self,
        source: str,
        status: str,
        *,
        records_count: int = 0,
        failure_count: int = 0,
        error_message: str = "",
        next_retry_at: str | None = None,
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO signal_refresh_history(
                    created_at, source, status, records_count, failure_count, error_message, next_retry_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (utc_now(), source, status, records_count, failure_count, error_message, next_retry_at),
            )

    def recent_signal_refresh_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT created_at, source, status, records_count, failure_count, error_message, next_retry_at
                FROM signal_refresh_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_bot_state(self, key: str) -> Optional[str]:
        with self.connect() as con:
            row = con.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_bot_state(self, key: str, value: str) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO bot_state(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, value, utc_now()),
            )

    def record_audit_event(
        self,
        category: str,
        severity: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO audit_events(created_at, category, severity, message, details_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (utc_now(), category, severity, message, json.dumps(details or {})),
            )

    def recent_audit_events(self, limit: int = 25) -> List[Dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT created_at, category, severity, message, details_json
                FROM audit_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            items: List[Dict[str, Any]] = []
            for row in rows:
                payload = dict(row)
                payload["details"] = json.loads(payload.pop("details_json") or "{}")
                items.append(payload)
            return items

    def latest_candidates(self) -> List[Dict[str, Any]]:
        with self.connect() as con:
            row = con.execute(
                "SELECT candidates_json FROM scans ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return json.loads(row[0]) if row else []

    def recent_trades(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT id, created_at, symbol, side, qty, price, status, note, pnl_pct, pnl_amount, analysis_json "
                "FROM trade_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) | {"analysis": json.loads(r["analysis_json"] or "{}")} for r in rows]

    def trades_since_id(self, last_trade_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT id, created_at, symbol, side, qty, price, status, note, pnl_pct, pnl_amount, analysis_json "
                "FROM trade_events WHERE id > ? ORDER BY id ASC LIMIT ?",
                (last_trade_id, limit),
            ).fetchall()
            return [dict(r) | {"analysis": json.loads(r["analysis_json"] or "{}")} for r in rows]

    def record_trade(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        status: str,
        note: str = "",
        pnl_pct: Optional[float] = None,
        analysis: Optional[Dict[str, float]] = None,
        pnl_amount: Optional[float] = None,
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO trade_events(created_at, symbol, side, qty, price, status, note, pnl_pct, pnl_amount, analysis_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (utc_now(), symbol, side, qty, price, status, note, pnl_pct, pnl_amount, json.dumps(analysis or {})),
            )

    def recently_sold_symbols(self, hours: int = 48) -> Dict[str, Dict[str, Any]]:
        """Return symbols sold within the last N hours, with their sell details.

        Returns a dict of {symbol: {"sold_at": str, "pnl_pct": float, "note": str}}
        so callers can decide whether to rebuy based on context.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT symbol, created_at, pnl_pct, note
                FROM trade_events
                WHERE side = 'sell' AND created_at >= ?
                ORDER BY created_at DESC
                """,
                (cutoff,),
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            sym = row["symbol"]
            if sym not in result:
                result[sym] = {
                    "sold_at": row["created_at"],
                    "pnl_pct": row["pnl_pct"],
                    "note": row["note"],
                }
        return result

    def recover_analysis_for_symbol(self, symbol: str) -> Dict[str, float]:
        """Try to find analyst_scores from a previous buy trade event for this symbol."""
        with self.connect() as con:
            row = con.execute(
                "SELECT analysis_json FROM trade_events WHERE symbol = ? AND side = 'buy' "
                "AND analysis_json != '{}' ORDER BY created_at DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            if row:
                return json.loads(row["analysis_json"] or "{}")
            return {}

    def open_position_meta(
        self,
        symbol: str,
        qty: float,
        entry_price: float,
        stop_price: float,
        target_price: float,
        analysis: Dict[str, float],
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO position_meta(symbol, opened_at, qty, entry_price, stop_price, target_price, analysis_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    opened_at=excluded.opened_at,
                    qty=excluded.qty,
                    entry_price=excluded.entry_price,
                    stop_price=excluded.stop_price,
                    target_price=excluded.target_price,
                    analysis_json=excluded.analysis_json
                """,
                (symbol, utc_now(), qty, entry_price, stop_price, target_price, json.dumps(analysis)),
            )

    def get_position_meta(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM position_meta WHERE symbol = ?", (symbol,)).fetchone()
            if not row:
                return None
            payload = dict(row)
            payload["analysis"] = json.loads(payload.pop("analysis_json"))
            return payload

    def all_position_meta(self) -> List[Dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM position_meta ORDER BY opened_at DESC").fetchall()
            items = []
            for row in rows:
                payload = dict(row)
                payload["analysis"] = json.loads(payload.pop("analysis_json"))
                items.append(payload)
            return items

    def close_position_meta(self, symbol: str) -> Optional[Dict[str, Any]]:
        existing = self.get_position_meta(symbol)
        if not existing:
            return None
        with self.connect() as con:
            con.execute("DELETE FROM position_meta WHERE symbol = ?", (symbol,))
        return existing

    def update_stop_price(self, symbol: str, stop_price: float) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE position_meta SET stop_price = ? WHERE symbol = ?",
                (round(stop_price, 2), symbol),
            )

    def mark_partial_profit_taken(self, symbol: str) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE position_meta SET partial_profit_taken = 1 WHERE symbol = ?",
                (symbol,),
            )

    def update_peak_price(self, symbol: str, peak_price: float) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE position_meta SET peak_price = ? WHERE symbol = ?",
                (round(peak_price, 4), symbol),
            )

    def update_position_qty(self, symbol: str, new_qty: float) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE position_meta SET qty = ? WHERE symbol = ?",
                (new_qty, symbol),
            )

    def set_exit_pending(self, symbol: str, pending: bool) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE position_meta SET exit_pending = ? WHERE symbol = ?",
                (1 if pending else 0, symbol),
            )

    def learning_weights(self) -> Dict[str, Dict[str, float]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM learning ORDER BY strategy").fetchall()
            return {row["strategy"]: dict(row) for row in rows}

    def update_learning(self, analysis: Dict[str, float], pnl_pct: float) -> None:
        with self.connect() as con:
            for strategy, score in analysis.items():
                row = con.execute(
                    "SELECT wins, losses, total_return FROM learning WHERE strategy = ?",
                    (strategy,),
                ).fetchone()
                if not row:
                    continue
                wins = row["wins"] + (1 if pnl_pct > 0 else 0)
                losses = row["losses"] + (1 if pnl_pct <= 0 else 0)
                # Adaptive learning: strategies that consistently pick winners
                # get boosted faster; losers decay faster.  Wider bounds let the
                # bot express stronger conviction once it has enough data.
                bounded_pnl = max(-25.0, min(25.0, float(pnl_pct)))
                contribution = (bounded_pnl / 20.0) * (float(score) / 100.0)
                total_return = row["total_return"] + contribution
                total_trades = wins + losses
                # Scale the per-trade delta up once we have enough history so
                # early noise doesn't over-steer, but mature weights move faster.
                maturity_factor = min(1.5, 0.8 + total_trades * 0.02)
                weight = 1.0 + (wins - losses) * 0.08 * maturity_factor + total_return * 0.35
                weight = max(0.25, min(3.0, weight))
                con.execute(
                    """
                    UPDATE learning
                    SET wins = ?, losses = ?, total_return = ?, weight = ?, updated_at = ?
                    WHERE strategy = ?
                    """,
                    (wins, losses, total_return, weight, utc_now(), strategy),
                )
