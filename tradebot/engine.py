from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import exchange_calendars as xcals
import logging
import pandas as pd
from zoneinfo import ZoneInfo

from .analytics import compute_metrics
from .analyst_consensus import AnalystConsensusTracker
from .congress import CongressTracker
from .config import Settings
from .mcp_bridge import analyze as analyze_with_mcp
from .db import Database
from .earnings import EarningsTracker
from .macro import MacroTracker
from .models import Candidate
from .polygon import PolygonClient, build_polygon_client
from .providers import BaseBroker, ProviderError
from .sec import SecTracker

log = logging.getLogger(__name__)
NY_TZ = ZoneInfo("America/New_York")


@dataclass
class TradingEngine:
    settings: Settings
    broker: BaseBroker
    db: Database
    polygon: Optional[PolygonClient] = None
    _base_max_total_capital: float = field(init=False, default=0.0)
    _base_max_open_positions: int = field(init=False, default=0)
    _base_max_new_positions_per_run: int = field(init=False, default=0)
    _base_max_stock_price: float = field(init=False, default=0.0)
    _base_congress_max_price: float = field(init=False, default=0.0)
    _base_scan_limit: int = field(init=False, default=0)
    _base_candidate_limit: int = field(init=False, default=0)
    _base_min_dollar_volume: float = field(init=False, default=0.0)
    _base_risk_per_trade_pct: float = field(init=False, default=0.0)
    _base_max_position_pct: float = field(init=False, default=0.0)
    _scale_baseline_equity: float = field(init=False, default=0.0)
    _latest_growth_ratio: float = field(init=False, default=1.0)
    _latest_drawdown_pct: float = field(init=False, default=0.0)
    _latest_drawdown_state: str = field(init=False, default="normal")
    _market_calendar: object = field(init=False, default=None)
    _last_safety_signature: str = field(init=False, default="")
    _last_broker_sync_at: Optional[datetime] = field(init=False, default=None)
    _signals_paused_market_closed: bool = field(init=False, default=False)
    _analyst_tracker: Optional[AnalystConsensusTracker] = field(init=False, default=None)
    failure_callback: Optional[Callable[[str, Exception, Dict[str, object]], None]] = None

    def __post_init__(self) -> None:
        if self.polygon is None:
            self.polygon = build_polygon_client(self.settings)
        self._base_max_total_capital = max(float(self.settings.max_total_capital), 0.0)
        self._base_max_open_positions = max(int(self.settings.max_open_positions), 0)
        self._base_max_new_positions_per_run = max(int(self.settings.max_new_positions_per_run), 0)
        self._base_max_stock_price = max(float(self.settings.max_stock_price), 0.0)
        self._base_congress_max_price = max(float(self.settings.congress_max_price), 0.0)
        self._base_scan_limit = max(int(self.settings.scan_limit), 1)
        self._base_candidate_limit = max(int(self.settings.candidate_limit), 1)
        self._base_min_dollar_volume = max(float(self.settings.min_dollar_volume), 0.0)
        self._base_risk_per_trade_pct = max(float(self.settings.risk_per_trade_pct), 0.0)
        self._base_max_position_pct = max(float(self.settings.max_position_pct), 0.0)
        if self.settings.is_demo and self.settings.starting_cash > 0:
            self._scale_baseline_equity = float(self.settings.starting_cash)
        self._market_calendar = xcals.get_calendar("XNYS")
        if self.settings.analyst_consensus_enabled:
            self._analyst_tracker = AnalystConsensusTracker(self.settings, self.db)

    def learning_weights(self) -> Dict[str, float]:
        raw = self.db.learning_weights()
        return {name: float(payload["weight"]) for name, payload in raw.items()}

    def _parse_timestamp(self, value: object) -> Optional[datetime]:
        if not value:
            return None
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _pdt_pause_until(self) -> Optional[datetime]:
        cooldown = max(1, int(self.settings.pdt_cooldown_hours))
        for trade in self.db.recent_trades(50):
            if trade.get("side") != "buy" or trade.get("status") != "error":
                continue
            note = str(trade.get("note") or "").lower()
            if "pattern day trading protection" not in note:
                continue
            created_at = self._parse_timestamp(trade.get("created_at"))
            if created_at is None:
                continue
            pause_until = created_at + timedelta(hours=cooldown)
            if pause_until > datetime.now(timezone.utc):
                return pause_until
        return None

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def _today_et(self) -> str:
        return self._now_utc().astimezone(NY_TZ).date().isoformat()

    def _is_trading_day_today(self) -> bool:
        """True if today (ET) is an actual NYSE trading session — a weekday
        that is NOT a market holiday. Lets the daily report distinguish a real
        $0.00 P&L problem from a legitimately flat closed-market day."""
        try:
            return bool(self._market_calendar.is_session(pd.Timestamp(self._today_et())))
        except Exception:  # noqa: BLE001
            return self._now_utc().astimezone(NY_TZ).weekday() < 5

    def _market_session_status(self, when: Optional[datetime] = None) -> Dict[str, object]:
        moment = when or self._now_utc()
        ts = pd.Timestamp(moment.astimezone(timezone.utc))
        calendar = self._market_calendar
        is_open = bool(calendar.is_open_on_minute(ts))
        session = calendar.date_to_session(pd.Timestamp(ts.date()), direction="next")
        session_open = calendar.session_open(session).to_pydatetime()
        session_close = calendar.session_close(session).to_pydatetime()
        current_session = calendar.date_to_session(pd.Timestamp(ts.date()), direction="previous")
        current_open = calendar.session_open(current_session).to_pydatetime()
        current_close = calendar.session_close(current_session).to_pydatetime()
        return {
            "is_open": is_open,
            "session_date": str(session.date()),
            "session_open": session_open,
            "session_close": session_close,
            "current_session_date": str(current_session.date()),
            "current_session_open": current_open,
            "current_session_close": current_close,
            "is_early_close": current_close.hour < 20,
            "next_open": session_open if not is_open else current_open,
            "next_close": current_close if is_open else session_close,
        }

    def _market_is_closed(self) -> bool:
        """Check if the US stock market is currently closed.

        Uses Polygon /v1/marketstatus/now when available, otherwise falls
        back to the NYSE calendar.
        """
        if self.polygon:
            try:
                status = self.polygon.market_status()
                market_state = str(status.get("market", "")).lower()
                return market_state != "open"
            except Exception:  # noqa: BLE001
                pass
        return not bool(self._market_session_status()["is_open"])

    def _buying_pause_reason(self) -> str:
        status = self._execution_safety_status()
        reasons = status["reasons"]
        if not reasons:
            return ""
        return str(reasons[0])

    def _stale_after_hours(self) -> Dict[str, int]:
        return {
            "congress": self.settings.congress_freshness_hours,
            "sec": self.settings.sec_freshness_hours,
            "earnings": self.settings.earnings_freshness_hours,
            "macro": self.settings.macro_freshness_hours,
        }

    def _minimum_records(self) -> Dict[str, int]:
        return {
            "congress": self.settings.congress_min_records,
            "sec": self.settings.sec_min_records,
            "earnings": self.settings.earnings_min_records,
            "macro": self.settings.macro_min_records,
        }

    def _retry_minutes(self) -> Dict[str, int]:
        return {
            "congress": self.settings.congress_retry_minutes,
            "sec": self.settings.sec_retry_minutes,
            "earnings": self.settings.earnings_retry_minutes,
            "macro": self.settings.macro_retry_minutes,
        }

    def _override_modes(self) -> Dict[str, str]:
        return {
            "congress": self.settings.congress_override_mode,
            "sec": self.settings.sec_override_mode,
            "earnings": self.settings.earnings_override_mode,
            "macro": self.settings.macro_override_mode,
        }

    def _signal_enabled(self, source: str) -> bool:
        if self._override_modes()[source] == "disabled":
            return False
        if source == "congress":
            return bool(self.settings.congress_auto_fetch or self.settings.congress_report_urls)
        if source == "sec":
            return bool(self.settings.sec_user_agent)
        if source == "earnings":
            return bool(self.settings.alpha_vantage_api_key)
        if source == "macro":
            return True
        return False

    def _scaling_baseline_equity(self, current_equity: float) -> float:
        if self._scale_baseline_equity > 0:
            return self._scale_baseline_equity
        self._scale_baseline_equity = max(float(current_equity), 1.0)
        return self._scale_baseline_equity

    def _round_up(self, value: float, step: float) -> float:
        if step <= 0:
            return round(value, 2)
        return round(math.ceil(value / step) * step, 2)

    def _price_allowed(self, symbol: str, price: float) -> bool:
        if self._is_inverse_etf(symbol):
            return True
        min_price = max(float(self.settings.min_stock_price), 0.0)
        max_price = float(self.settings.max_stock_price)
        if price < min_price:
            return False
        if max_price > 0 and price > max_price:
            return False
        return True

    def _market_regime_status(self) -> Dict[str, object]:
        if not self.settings.market_regime_filter:
            return {
                "enabled": False,
                "allow_long_buys": True,
                "state": "disabled",
                "reason": "disabled",
                "symbols": [],
                "details": [],
            }
        symbols = self.settings.market_regime_symbols or ["SPY", "QQQ"]
        short_window = max(2, int(self.settings.market_regime_short_window))
        long_window = max(short_window + 1, int(self.settings.market_regime_long_window))
        request_days = max(long_window + 5, int(long_window * 2.2))
        try:
            bars_by_symbol = self._fetch_bars(symbols, request_days)
        except Exception as exc:  # noqa: BLE001
            log.warning("Market regime fetch failed: %s", exc)
            bars_by_symbol = {}

        details: List[Dict[str, object]] = []
        missing = False
        uptrend_count = 0
        for symbol in symbols:
            bars = bars_by_symbol.get(symbol) or []
            closes = [float(bar["c"]) for bar in bars if bar.get("c") is not None]
            if len(closes) < long_window:
                missing = True
                details.append({"symbol": symbol, "state": "missing", "uptrend": False})
                continue
            latest = closes[-1]
            short_ma = sum(closes[-short_window:]) / short_window
            long_ma = sum(closes[-long_window:]) / long_window
            uptrend = latest >= short_ma >= long_ma
            if uptrend:
                uptrend_count += 1
            details.append(
                {
                    "symbol": symbol,
                    "state": "uptrend" if uptrend else "weak",
                    "uptrend": uptrend,
                    "latest": round(latest, 2),
                    "short_ma": round(short_ma, 2),
                    "long_ma": round(long_ma, 2),
                }
            )

        if missing and self.settings.market_regime_block_on_missing:
            allow_long_buys = False
            state = "missing"
            reason = "market regime data unavailable"
        else:
            allow_long_buys = bool(details) and uptrend_count == len(details)
            state = "uptrend" if allow_long_buys else "weak"
            reason = "broad market uptrend confirmed" if allow_long_buys else "SPY/QQQ trend filter is weak"

        return {
            "enabled": True,
            "allow_long_buys": allow_long_buys,
            "state": state,
            "reason": reason,
            "symbols": symbols,
            "details": details,
        }

    def _update_regime_persistence(self, market_regime: Dict[str, object], now: Optional[datetime] = None) -> None:
        """Annotate the regime status with whether inverse hedge entries are
        confirmed. A single weak reading is often a one-morning whipsaw, so
        hedges are only allowed once the weak regime has persisted for
        `inverse_confirmation_hours` (which forces it to span sessions)."""
        if not market_regime.get("enabled"):
            market_regime["inverse_buys_confirmed"] = True
            return
        now = now or datetime.now(timezone.utc)
        state = str(market_regime.get("state") or "")
        if bool(market_regime.get("allow_long_buys")):
            self.db.set_bot_state("regime_weak_since", "")
            market_regime["inverse_buys_confirmed"] = False
            return
        if state == "missing":
            # Data outage, not a market signal — keep any running clock but
            # never confirm hedges on missing data.
            market_regime["inverse_buys_confirmed"] = False
            return
        confirm_hours = max(0.0, float(self.settings.inverse_confirmation_hours))
        weak_since_raw = self.db.get_bot_state("regime_weak_since") or ""
        weak_since: Optional[datetime] = None
        if weak_since_raw:
            try:
                weak_since = datetime.fromisoformat(weak_since_raw)
            except ValueError:
                weak_since = None
        if weak_since is None:
            weak_since = now
            self.db.set_bot_state("regime_weak_since", now.isoformat())
        market_regime["inverse_buys_confirmed"] = (now - weak_since) >= timedelta(hours=confirm_hours)

    def _in_earnings_blackout(self, external_inputs: Dict[str, float]) -> bool:
        days_until = float(external_inputs.get("days_until_earnings", float("inf")))
        return (
            self.settings.earnings_blackout_days > 0
            and float(external_inputs.get("has_upcoming_earnings", 0.0)) > 0
            and days_until <= self.settings.earnings_blackout_days
        )

    def _round_share_qty(self, qty: float) -> float:
        if qty <= 0:
            return 0.0
        precision = 6 if self.settings.is_alpaca else 4
        step = 10 ** precision
        return math.floor(qty * step) / step

    def _capital_step(self) -> float:
        if self._base_max_total_capital <= 1_000:
            return 25.0
        if self._base_max_total_capital <= 5_000:
            return 100.0
        return 250.0

    def _load_peak_equity(self) -> float:
        raw = self.db.get_bot_state("peak_equity")
        if raw is None:
            return 0.0
        try:
            return float(raw)
        except ValueError:
            return 0.0

    def _round_volume_step(self, value: float) -> float:
        if value < 1_000_000:
            step = 50_000.0
        elif value < 5_000_000:
            step = 100_000.0
        else:
            step = 250_000.0
        return self._round_up(value, step)

    def _drawdown_profile(self, drawdown_pct: float) -> Dict[str, float | int | str]:
        if drawdown_pct >= self.settings.drawdown_hard_limit_pct:
            return {
                "state": "hard",
                "capital_mult": 0.60,
                "positions_mult": 0.50,
                "risk_mult": 0.35,
                "position_pct_mult": 0.50,
                "new_positions_cap": 1,
            }
        if drawdown_pct >= self.settings.drawdown_soft_limit_pct:
            return {
                "state": "soft",
                "capital_mult": 0.85,
                "positions_mult": 0.75,
                "risk_mult": 0.60,
                "position_pct_mult": 0.75,
                "new_positions_cap": 2,
            }
        return {
            "state": "normal",
            "capital_mult": 1.0,
            "positions_mult": 1.0,
            "risk_mult": 1.0,
            "position_pct_mult": 1.0,
            "new_positions_cap": 0,
        }

    def _consecutive_buy_errors(self) -> int:
        # Only errors from the current ET trading day count, so the pause
        # clears on its own at the next session instead of deadlocking
        # (paused buys can never produce the successful buy that would
        # otherwise break the streak).
        today_et = self._today_et()
        count = 0
        for trade in self.db.recent_trades(25):
            if trade.get("side") != "buy":
                continue
            created_at = self._parse_timestamp(trade.get("created_at"))
            if created_at is not None and created_at.astimezone(NY_TZ).date().isoformat() != today_et:
                break
            if trade.get("status") == "error":
                count += 1
                continue
            break
        return count

    def _daily_equity_anchor(self, equity: float) -> float:
        key = f"daily_equity_anchor:{self._today_et()}"
        raw = self.db.get_bot_state(key)
        if raw is None:
            self.db.set_bot_state(key, f"{equity:.2f}")
            return equity
        try:
            return float(raw)
        except ValueError:
            self.db.set_bot_state(key, f"{equity:.2f}")
            return equity

    def _execution_safety_status(self, account: Optional[object] = None) -> Dict[str, object]:
        account = account or self.broker.account()
        equity = max(float(account.equity), float(account.cash), 0.0)
        local_daily_anchor = self._daily_equity_anchor(equity) if equity > 0 else 0.0
        broker_daily_anchor = 0.0
        try:
            broker_daily_anchor = float(getattr(account, "last_equity", None) or 0.0)
        except (TypeError, ValueError):
            broker_daily_anchor = 0.0
        daily_anchor = broker_daily_anchor if broker_daily_anchor > 0 else local_daily_anchor
        daily_anchor_source = "broker_previous_close" if broker_daily_anchor > 0 else "local_start_of_day"
        daily_pnl_amount = equity - daily_anchor if daily_anchor > 0 else 0.0
        daily_loss_amount = max(0.0, daily_anchor - equity) if daily_anchor > 0 else 0.0
        daily_profit_amount = max(0.0, daily_pnl_amount)
        daily_loss_pct = max(0.0, (daily_anchor - equity) / daily_anchor) if daily_anchor > 0 else 0.0
        consecutive_buy_errors = self._consecutive_buy_errors()
        reasons: List[str] = []

        if self.settings.buy_kill_switch:
            reasons.append("manual buy kill switch is enabled")
        if self.settings.pause_new_buys_on_degraded_signals and self.degraded_mode():
            reasons.append("external signals are degraded")
        if self._latest_drawdown_state == "hard":
            reasons.append("hard drawdown throttle is active")
        if self.settings.daily_loss_limit_dollars > 0 and daily_loss_amount >= self.settings.daily_loss_limit_dollars:
            reasons.append(f"daily dollar loss limit reached (${daily_loss_amount:.2f})")
        if self.settings.daily_loss_limit_pct > 0 and daily_loss_pct >= self.settings.daily_loss_limit_pct:
            reasons.append(f"daily loss limit reached ({daily_loss_pct * 100:.2f}% of equity)")
        if self.settings.profit_lock_dollars > 0 and daily_profit_amount >= self.settings.profit_lock_dollars:
            reasons.append(f"daily profit lock reached (${daily_profit_amount:.2f})")
        if self.settings.max_consecutive_buy_errors > 0 and consecutive_buy_errors >= self.settings.max_consecutive_buy_errors:
            reasons.append(f"consecutive buy errors reached {consecutive_buy_errors}")
        pause_until = self._pdt_pause_until()
        if pause_until is not None:
            reasons.append(f"pattern day trading protection until {pause_until.isoformat()}")
        if not self.settings.is_demo and self._market_is_closed():
            session = self._market_session_status()
            next_open = session["next_open"]
            if isinstance(next_open, datetime):
                reasons.append(f"market is closed until {next_open.isoformat()}")
            else:
                reasons.append("market is closed")

        return {
            "pause_new_buys": bool(reasons),
            "reasons": reasons,
            "daily_equity_anchor": round(daily_anchor, 2),
            "daily_equity_anchor_source": daily_anchor_source,
            "daily_pnl_amount": round(daily_pnl_amount, 2),
            "daily_loss_amount": round(daily_loss_amount, 2),
            "daily_profit_amount": round(daily_profit_amount, 2),
            "daily_loss_pct": round(daily_loss_pct * 100, 2),
            "consecutive_buy_errors": consecutive_buy_errors,
            "drawdown_state": self._latest_drawdown_state,
        }

    def _signal_diagnostics(self) -> Dict[str, Dict[str, object]]:
        diagnostics: Dict[str, Dict[str, object]] = {}
        statuses = self._signal_health()
        for source, item in statuses.items():
            note = "healthy"
            if not item["enabled"]:
                note = "not configured"
            elif item["status"] == "error":
                note = "source refresh failed"
            elif item["status"] == "backoff" or item["in_backoff"]:
                note = "waiting for retry window"
            elif item["stale"]:
                note = "cached data is stale"
            elif item["low_confidence"]:
                note = f"only {item['records_count']} record(s); minimum is {item['minimum_records']}"
            elif item["no_data"]:
                note = "configured but no usable records are cached"
            diagnostics[source] = {
                "status": item["status"],
                "note": note,
                "records_count": item["records_count"],
                "enabled": item["enabled"],
                "override_mode": item["override_mode"],
                "next_retry_at": item.get("next_retry_at"),
                "last_success_at": item.get("last_success_at"),
                "error_message": item.get("error_message") or "",
            }
        return diagnostics

    def _record_audit_event(self, category: str, severity: str, message: str, details: Optional[Dict[str, object]] = None) -> None:
        self.db.record_audit_event(category, severity, message, details)

    def _notify_failure(self, context: str, exc: Exception, details: Optional[Dict[str, object]] = None) -> None:
        payload: Dict[str, object] = {
            "context": context,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        if details:
            payload.update(details)
        if self.failure_callback:
            try:
                self.failure_callback(context, exc, details or {})
                return
            except Exception:  # noqa: BLE001
                log.exception("Failure callback failed for %s", context)
        self._record_audit_event("failure", "error", f"{context} failed", payload)

    def _record_safety_transition(self, status: Dict[str, object]) -> None:
        signature = json.dumps(
            {
                "pause_new_buys": status["pause_new_buys"],
                "reasons": status["reasons"],
                "drawdown_state": status["drawdown_state"],
            },
            sort_keys=True,
        )
        if signature == self._last_safety_signature:
            return
        self._last_safety_signature = signature
        severity = "warning" if status["pause_new_buys"] else "info"
        message = "New buys paused" if status["pause_new_buys"] else "New buys resumed"
        self._record_audit_event(
            "safety",
            severity,
            message,
            {
                "reasons": status["reasons"],
                "drawdown_state": status["drawdown_state"],
                "daily_loss_pct": status["daily_loss_pct"],
                "consecutive_buy_errors": status["consecutive_buy_errors"],
            },
        )
        self._maybe_alert_buy_error_pause(status)

    def _maybe_alert_buy_error_pause(self, status: Dict[str, object]) -> None:
        reasons = [str(r) for r in status.get("reasons") or []]
        if not any(r.startswith("consecutive buy errors") for r in reasons):
            return
        # One email per ET day, persisted so restarts don't resend.
        alert_key = f"buy_error_alert_sent:{self._today_et()}"
        if self.db.get_bot_state(alert_key):
            return
        self.db.set_bot_state(alert_key, self._now_utc().isoformat())
        try:
            from .email_report import send_failure_alert

            send_failure_alert(
                "New buys paused - consecutive buy errors",
                "The buy-error circuit breaker tripped and new buys are paused. "
                "It resets automatically at the next trading session.",
                {
                    "consecutive_buy_errors": status.get("consecutive_buy_errors"),
                    "reasons": ", ".join(reasons),
                    "drawdown_state": status.get("drawdown_state"),
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("Failed to send buy-error pause alert email")

    def _maybe_sync_broker_state(self, force: bool = False) -> None:
        if not self.settings.is_alpaca:
            return
        now = self._now_utc()
        if not force and self._last_broker_sync_at is not None:
            if (now - self._last_broker_sync_at) < timedelta(seconds=60):
                return
        self.reconcile_broker_state()
        self._last_broker_sync_at = now

    def _signal_health(self) -> Dict[str, dict]:
        now = datetime.now(timezone.utc)
        statuses = self.db.signal_statuses()
        health: Dict[str, dict] = {}
        for source in ("congress", "sec", "earnings", "macro"):
            item = statuses.get(
                source,
                {
                    "source": source,
                    "status": "disabled" if not self._signal_enabled(source) else "unknown",
                    "last_attempt_at": None,
                    "last_success_at": None,
                    "error_message": "",
                    "records_count": 0,
                },
            )
            stale = False
            last_success_at = item.get("last_success_at")
            if item["status"] == "ok" and last_success_at:
                last_success = datetime.fromisoformat(str(last_success_at))
                if last_success.tzinfo is None:
                    last_success = last_success.replace(tzinfo=timezone.utc)
                age_hours = (now - last_success).total_seconds() / 3600.0
                stale = age_hours > self._stale_after_hours()[source]
            minimum_records = self._minimum_records()[source]
            records_count = int(item.get("records_count", 0) or 0)
            low_confidence = item["status"] == "ok" and records_count > 0 and records_count < minimum_records
            no_data = item["status"] == "ok" and records_count == 0
            in_backoff = False
            next_retry_at = item.get("next_retry_at")
            if next_retry_at:
                next_retry = datetime.fromisoformat(str(next_retry_at))
                if next_retry.tzinfo is None:
                    next_retry = next_retry.replace(tzinfo=timezone.utc)
                in_backoff = next_retry > now
            override_mode = self._override_modes()[source]
            if override_mode == "trusted":
                stale = False
                low_confidence = False
            if override_mode == "ignore-backoff":
                in_backoff = False
            health[source] = dict(item) | {
                "enabled": self._signal_enabled(source),
                "stale": stale,
                "minimum_records": minimum_records,
                "low_confidence": low_confidence,
                "no_data": no_data,
                "in_backoff": in_backoff,
                "override_mode": override_mode,
            }
        return health

    def degraded_mode(self) -> bool:
        return any(
            item["enabled"] and (item["status"] in {"error", "backoff"} or item["stale"] or item["low_confidence"])
            for item in self._signal_health().values()
        )

    def _refresh_source(
        self,
        source: str,
        callback: Callable[[], List[dict]],
    ) -> List[dict]:
        attempted_at = datetime.now(timezone.utc).isoformat()
        if not self._signal_enabled(source):
            self.db.update_signal_status(
                source,
                "disabled",
                last_attempt_at=attempted_at,
                error_message="",
                records_count=0,
                failure_count=0,
                next_retry_at=None,
            )
            self.db.record_signal_refresh_event(source, "disabled", records_count=0, failure_count=0)
            self._record_audit_event("signal", "info", f"{source} refresh skipped because the source is disabled", {"source": source})
            return []
        current = self.db.signal_statuses().get(source)
        if current and current.get("next_retry_at") and self._override_modes()[source] != "ignore-backoff":
            next_retry = datetime.fromisoformat(str(current["next_retry_at"]))
            if next_retry.tzinfo is None:
                next_retry = next_retry.replace(tzinfo=timezone.utc)
            if next_retry > datetime.now(timezone.utc):
                self.db.update_signal_status(
                    source,
                    "backoff",
                    last_attempt_at=current.get("last_attempt_at"),
                    last_success_at=current.get("last_success_at"),
                    error_message=str(current.get("error_message") or ""),
                    records_count=int(current.get("records_count") or 0),
                    failure_count=int(current.get("failure_count") or 0),
                    next_retry_at=str(current["next_retry_at"]),
                )
                self.db.record_signal_refresh_event(
                    source,
                    "backoff",
                    records_count=int(current.get("records_count") or 0),
                    failure_count=int(current.get("failure_count") or 0),
                    error_message=str(current.get("error_message") or ""),
                    next_retry_at=str(current["next_retry_at"]),
                )
                self._record_audit_event(
                    "signal",
                    "warning",
                    f"{source} refresh skipped during backoff",
                    {"source": source, "next_retry_at": str(current["next_retry_at"])},
                )
                return []
        try:
            records = callback()
        except Exception as exc:  # noqa: BLE001
            failure_count = int(current.get("failure_count") or 0) + 1 if current else 1
            delay_minutes = self._retry_minutes()[source] * min(8, 2 ** (failure_count - 1))
            next_retry_at = (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).isoformat()
            self.db.update_signal_status(
                source,
                "error",
                last_attempt_at=attempted_at,
                error_message=str(exc),
                records_count=0,
                failure_count=failure_count,
                next_retry_at=next_retry_at,
            )
            self.db.record_signal_refresh_event(
                source,
                "error",
                records_count=0,
                failure_count=failure_count,
                error_message=str(exc),
                next_retry_at=next_retry_at,
            )
            self._record_audit_event(
                "signal",
                "error",
                f"{source} refresh failed",
                {"source": source, "error_message": str(exc), "next_retry_at": next_retry_at},
            )
            self._notify_failure(
                f"{source} signal refresh",
                exc,
                {"source": source, "failure_count": failure_count, "next_retry_at": next_retry_at},
            )
            return []
        self.db.update_signal_status(
            source,
            "ok",
            last_attempt_at=attempted_at,
            last_success_at=attempted_at,
            error_message="",
            records_count=len(records),
            failure_count=0,
            next_retry_at=None,
        )
        self.db.record_signal_refresh_event(source, "ok", records_count=len(records), failure_count=0)
        note = "no usable records" if not records else f"{len(records)} record(s) refreshed"
        self._record_audit_event("signal", "info", f"{source} refresh completed: {note}", {"source": source, "records_count": len(records)})
        return records

    def _short_volume_signal(self, symbol: str) -> Dict[str, float]:
        """Fetch short-volume ratio from Polygon for squeeze detection."""
        if not self.polygon or not self.settings.short_volume_signal_enabled:
            return {
                "short_volume_ratio": 0.0,
                "short_volume_available": 0.0,
            }
        try:
            records = self.polygon.short_volume(symbol, days=5)
        except Exception:  # noqa: BLE001
            return {
                "short_volume_ratio": 0.0,
                "short_volume_available": 0.0,
            }
        if not records:
            return {
                "short_volume_ratio": 0.0,
                "short_volume_available": 0.0,
            }
        # Average the short volume ratio over recent days
        ratios = [float(r.get("short_volume_ratio", 0)) for r in records if r.get("short_volume_ratio")]
        avg_ratio = sum(ratios) / len(ratios) if ratios else 0.0
        return {
            "short_volume_ratio": avg_ratio,
            "short_volume_available": 1.0,
        }

    def _external_decision_inputs(self, symbol: str) -> Dict[str, float]:
        return (
            self.db.congress_signal_for_symbol(symbol, self.settings.congress_signal_window_days)
            | self.db.sec_signal_for_symbol(symbol, self.settings.sec_signal_window_days)
            | self.db.earnings_signal_for_symbol(symbol, self.settings.earnings_signal_window_days)
            | self.db.macro_signal(self.settings.macro_signal_window_days)
            | self._short_volume_signal(symbol)
        )

    def _external_signal_controls(self, symbol: str) -> tuple[Dict[str, float], Dict[str, str]]:
        raw = self._external_decision_inputs(symbol)
        health = self._signal_health()
        signal_usage: Dict[str, str] = {}
        effective: Dict[str, float] = dict(raw)

        source_fields = {
            "congress": [
                "congress_buy_count",
                "congress_sell_count",
                "congress_net_count",
                "days_since_congress_trade",
                "days_since_congress_filed",
            ],
            "sec": [
                "sec_form4_count",
                "sec_disclosure_count",
                "sec_offering_filing_count",
                "days_since_sec_filing",
            ],
            "earnings": [
                "days_until_earnings",
                "earnings_before_open_count",
                "earnings_after_close_count",
                "has_upcoming_earnings",
            ],
            "macro": [
                "days_until_macro_event",
                "has_near_macro_event",
                "near_fomc_count",
            ],
        }
        weight_keys = {
            "congress": "congress_weight",
            "sec": "sec_weight",
            "earnings": "earnings_weight",
            "macro": "macro_weight",
        }
        configured_weights = {
            "congress": self.settings.decision_support_congress_weight,
            "sec": self.settings.decision_support_sec_weight,
            "earnings": self.settings.decision_support_earnings_weight,
            "macro": self.settings.decision_support_macro_weight,
        }

        for source, fields in source_fields.items():
            item = health[source]
            if not item["enabled"]:
                signal_usage[source] = "disabled"
                effective[weight_keys[source]] = 0.0
                for field in fields:
                    effective[field] = 0.0
                continue
            if item["override_mode"] == "trusted":
                signal_usage[source] = "trusted"
                effective[weight_keys[source]] = configured_weights[source]
                continue
            if item["status"] == "error":
                signal_usage[source] = "error"
                effective[weight_keys[source]] = 0.0
                for field in fields:
                    effective[field] = 0.0
                continue
            if item["status"] == "backoff" or item["in_backoff"]:
                signal_usage[source] = "backoff"
                effective[weight_keys[source]] = 0.0
                for field in fields:
                    effective[field] = 0.0
                continue
            if item["stale"]:
                signal_usage[source] = "stale"
                effective[weight_keys[source]] = 0.0
                for field in fields:
                    effective[field] = 0.0
                continue
            if item["low_confidence"]:
                signal_usage[source] = "low-confidence"
                effective[weight_keys[source]] = 0.0
                for field in fields:
                    effective[field] = 0.0
                continue
            if item["no_data"]:
                signal_usage[source] = "no-data"
                effective[weight_keys[source]] = 0.0
                for field in fields:
                    effective[field] = 0.0
                continue
            if configured_weights[source] <= 0:
                signal_usage[source] = "weight=0"
                effective[weight_keys[source]] = 0.0
                for field in fields:
                    effective[field] = 0.0
                continue
            signal_usage[source] = "active"
            effective[weight_keys[source]] = configured_weights[source]
        return effective, signal_usage

    def _avg_dollar_volume_from_bars(self, bars: List[dict], window: int = 20) -> float:
        recent = bars[-max(1, window):]
        if not recent:
            return 0.0
        return sum(float(bar["c"]) * float(bar["v"]) for bar in recent) / len(recent)

    def _polygon_universe(self) -> List[str] | None:
        """Use Polygon daily market summary to discover the current stock universe
        in a single API call. Returns None if Polygon is unavailable."""
        if not self.polygon:
            return None
        try:
            items = self.polygon.sub10_universe(
                min_price=self.settings.min_stock_price,
                max_price=self.settings.max_stock_price if self.settings.max_stock_price > 0 else float("inf"),
                min_volume=200_000,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Polygon universe discovery failed, falling back to broker: %s", exc)
            return None
        if not items:
            return None
        # Cap the pool — Polygon bars_batch fetches one ticker at a time,
        # so keep this reasonable to avoid rate limits and slow scans.
        target_pool = max(self.settings.scan_limit, self.settings.candidate_limit * 4)
        symbols = [item["symbol"] for item in items[:target_pool]]
        log.info("Polygon discovered %d eligible stocks (using top %d)", len(items), len(symbols))
        return symbols

    # Inverse ETFs grouped by the index they short. Holding two funds from the
    # same bucket (e.g. SPXS + SPXU) doubles a single bet, not a hedge.
    _INVERSE_ETF_BUCKETS = {
        "SH": "SPX",
        "SPXS": "SPX",
        "SPXU": "SPX",
        "PSQ": "NDX",
        "SQQQ": "NDX",
        "TECS": "NDX",
        "DOG": "DJI",
        "SDOW": "DJI",
    }

    def _is_inverse_etf(self, symbol: str) -> bool:
        """Check if a symbol is in the inverse ETF list."""
        return (
            self.settings.inverse_etfs_enabled
            and symbol.upper() in self.settings.inverse_etfs
        )

    def _is_broad_market_etf(self, symbol: str) -> bool:
        """Broad-market / index / sector ETFs the bot shouldn't pick as stocks.

        These have no single-name edge — buying them is just full-price beta.
        Inverse ETFs are handled separately (hedging) and never count here.
        """
        return (
            self.settings.exclude_broad_market_etfs
            and symbol.upper() in self.settings.broad_market_etfs
            and not self._is_inverse_etf(symbol)
        )

    def _inverse_bucket(self, symbol: str) -> str:
        return self._INVERSE_ETF_BUCKETS.get(symbol.upper(), symbol.upper())

    def _inverse_hedge_headroom(
        self,
        symbol: str,
        held: List[Tuple[str, float]],
        equity: float,
    ) -> float:
        """Dollars still allowed into this inverse ETF given current holdings.

        `held` is (symbol, market_value) for every open position, including
        buys already placed earlier in the same run. Returns 0 when the buy
        should be skipped, math.inf when uncapped.
        """
        if not self._is_inverse_etf(symbol):
            return math.inf
        held_inverse = [(s, mv) for s, mv in held if self._is_inverse_etf(s)]
        bucket = self._inverse_bucket(symbol)
        if any(self._inverse_bucket(s) == bucket for s, _mv in held_inverse):
            return 0.0
        limit = int(self.settings.max_inverse_positions)
        if limit > 0 and len({s.upper() for s, _mv in held_inverse}) >= limit:
            return 0.0
        cap_pct = float(self.settings.max_inverse_exposure_pct)
        if cap_pct <= 0:
            return math.inf
        return max(0.0, cap_pct * max(equity, 0.0) - sum(mv for _s, mv in held_inverse))

    def _candidate_symbol_pool(self) -> List[str]:
        # If user specified a custom universe, honour it.
        if self.settings.scan_universe:
            base = self.settings.scan_universe[: self.settings.scan_limit]
            # Always inject inverse ETFs so the bot can hedge
            if self.settings.inverse_etfs_enabled:
                for etf in self.settings.inverse_etfs:
                    if etf not in base:
                        base.append(etf)
            return base

        if self.settings.is_alpaca and self.settings.live_universe_mode != "dynamic":
            base = self.broker.universe()[: self.settings.scan_limit]
            if self.settings.inverse_etfs_enabled:
                for etf in self.settings.inverse_etfs:
                    if etf not in base:
                        base.append(etf)
            return base

        # Try Polygon first — one API call covers the entire market.
        polygon_symbols = self._polygon_universe()
        if polygon_symbols:
            # Inject inverse ETFs (they may be above max_stock_price)
            if self.settings.inverse_etfs_enabled:
                for etf in self.settings.inverse_etfs:
                    if etf not in polygon_symbols:
                        polygon_symbols.append(etf)
            return polygon_symbols

        # Non-Alpaca (demo) mode without Polygon — use hardcoded universe.
        raw_symbols = self.broker.universe()
        if not self.settings.is_alpaca:
            base = raw_symbols[: self.settings.scan_limit]
            if self.settings.inverse_etfs_enabled:
                for etf in self.settings.inverse_etfs:
                    if etf not in base:
                        base.append(etf)
            return base

        # Fallback: scan Alpaca universe in batches.
        target_pool = max(self.settings.scan_limit * 4, self.settings.candidate_limit * 8)
        batch_size = max(40, min(100, self.settings.scan_limit))
        history_days = min(max(30, self.settings.lookback_days // 3), self.settings.lookback_days)
        baseline_liquidity = max(100_000.0, self.settings.min_dollar_volume * 0.5)
        ranked: List[tuple[float, str]] = []
        seen: set[str] = set()
        max_symbols_to_screen = min(len(raw_symbols), 3000)

        for start in range(0, max_symbols_to_screen, batch_size):
            batch = raw_symbols[start : start + batch_size]
            if not batch:
                break
            try:
                bars = self.broker.bars(batch, history_days)
            except ProviderError:
                continue
            for symbol in batch:
                if symbol in seen:
                    continue
                item = bars.get(symbol) or []
                if len(item) < 20:
                    continue
                price = float(item[-1]["c"])
                if not self._price_allowed(symbol, price):
                    continue
                avg_dollar_volume = self._avg_dollar_volume_from_bars(item)
                if avg_dollar_volume < baseline_liquidity:
                    continue
                ranked.append((avg_dollar_volume, symbol))
                seen.add(symbol)
            if len(ranked) >= target_pool * 2:
                break

        if ranked:
            ranked.sort(key=lambda item: item[0], reverse=True)
            pool = [symbol for _, symbol in ranked[:target_pool]]
        else:
            pool = raw_symbols[: self.settings.scan_limit]
        # Always inject inverse ETFs into the Alpaca fallback pool
        if self.settings.inverse_etfs_enabled:
            for etf in self.settings.inverse_etfs:
                if etf not in pool:
                    pool.append(etf)
        return pool

    def _candidate_from_bars(
        self,
        symbol: str,
        bars: List[dict],
        buying_power: float,
        market_regime: Optional[Dict[str, object]] = None,
    ) -> Candidate | None:
        if len(bars) < 30:
            return None
        metrics = compute_metrics(bars)
        price = metrics["latest"]
        if not self._price_allowed(symbol, price):
            return None
        # Don't pick broad-market/index/sector ETFs as stocks — no edge there.
        if self._is_broad_market_etf(symbol):
            return None

        stop_from_atr = price - (metrics["atr"] * 1.6)
        stop_from_pct = price * (1 - self.settings.stop_loss_pct)
        stop_price = round(max(stop_from_atr, stop_from_pct, self.settings.min_stock_price * 0.5), 2)
        target_from_swing = max(metrics["swing_high20"] * 1.02, price + metrics["atr"] * 2.4)
        target_price = round(target_from_swing, 2)
        reward = max(0.01, target_price - price)
        risk = max(0.01, price - stop_price)
        reward_risk = reward / risk

        external_inputs, signal_usage = self._external_signal_controls(symbol)
        market_regime = market_regime or self._market_regime_status()
        if market_regime.get("enabled"):
            market_allows_longs = bool(market_regime.get("allow_long_buys"))
            signal_usage["market_regime"] = str(market_regime.get("state") or "unknown")
            metrics["market_regime_uptrend"] = 1.0 if market_allows_longs else 0.0
            metrics["market_regime_blocked"] = 0.0
        analyst_snapshot = None
        if self._analyst_tracker is not None:
            metrics["analyst_target_upside_pct"] = 0.0
            metrics["analyst_consensus_blocked"] = 0.0
            metrics["analyst_consensus_buy_signal"] = 0.0
            metrics["analyst_consensus_hold_signal"] = 0.0
            metrics["analyst_consensus_sell_signal"] = 0.0
            if symbol.upper() in self.settings.analyst_consensus_skip_symbols:
                signal_usage["analyst_consensus"] = "skipped"
            else:
                analyst_snapshot = self._analyst_tracker.get(symbol)
            if analyst_snapshot:
                consensus = str(analyst_snapshot.get("consensus", "")).strip()
                normalized_consensus = consensus.lower()
                upside_pct = float(analyst_snapshot.get("target_upside_pct", 0.0) or 0.0)
                metrics["analyst_target_upside_pct"] = upside_pct
                signal_usage["analyst_consensus"] = consensus
                if normalized_consensus in {"buy", "strong buy"}:
                    metrics["analyst_consensus_buy_signal"] = 1.0
                elif normalized_consensus == "hold":
                    metrics["analyst_consensus_hold_signal"] = 1.0
                elif normalized_consensus in {"sell", "strong sell"}:
                    metrics["analyst_consensus_sell_signal"] = 1.0
            elif symbol.upper() not in self.settings.analyst_consensus_skip_symbols:
                signal_usage["analyst_consensus"] = "unavailable"

        analysis_input = dict(metrics)
        analysis_input.update(
            {
                "reward_risk": reward_risk,
                "stop_price": stop_price,
                "target_price": target_price,
                "risk_amount": risk,
                "reward_amount": reward,
                "min_reward_risk": self.settings.min_reward_risk,
            }
        )
        analysis_input.update(external_inputs)
        # Inject short volume weight for the scoring model
        analysis_input["short_volume_weight"] = self.settings.decision_support_short_volume_weight
        analysis = analyze_with_mcp(analysis_input, self.settings.analyzer_mode)
        decision_support_score, decision_support_reasons = analysis["decision_support"]
        momentum_score, momentum_reasons = analysis["momentum"]
        reversion_score, reversion_reasons = analysis["reversion"]
        risk_score, risk_reasons = analysis["risk"]

        weights = self.learning_weights()
        weighted_scores = {
            "decision_support": decision_support_score,
            "momentum": momentum_score,
            "reversion": reversion_score,
            "risk": risk_score,
        }
        total_weight = sum(weights[name] for name in weighted_scores)
        final_score = sum(weighted_scores[name] * weights[name] for name in weighted_scores) / total_weight

        reasons = []
        reasons.extend(decision_support_reasons[:2])
        reasons.extend(momentum_reasons[:2])
        reasons.extend(reversion_reasons[:2])
        reasons.extend(risk_reasons[:2])

        # Small-account overrides for entry thresholds and sizing
        if self.settings.is_small_account:
            min_reward_risk = min(self.settings.min_reward_risk, 1.2)
            risk_per_trade_pct = max(self.settings.risk_per_trade_pct, 0.04)
            max_position_pct = max(self.settings.max_position_pct, 0.25)
        else:
            min_reward_risk = self.settings.min_reward_risk
            risk_per_trade_pct = self.settings.risk_per_trade_pct
            max_position_pct = self.settings.max_position_pct

        limited_long_by_regime = False
        action = "watch"
        if (
            final_score >= 55
            and reward_risk >= min_reward_risk
            and metrics["avg_dollar_volume"] >= self.settings.min_dollar_volume
            and risk_score >= 45
            and decision_support_score >= 40
        ):
            action = "buy"
            reasons.insert(0, "decision support, liquidity, and reward/risk all cleared the bar")

        if (
            action == "buy"
            and market_regime.get("enabled")
            and not bool(market_regime.get("allow_long_buys"))
            and not self._is_inverse_etf(symbol)
        ):
            if (
                self.settings.market_regime_allow_limited_longs
                and final_score >= self.settings.market_regime_limited_long_min_score
            ):
                limited_long_by_regime = True
                metrics["market_regime_blocked"] = 0.5
                signal_usage["market_regime"] = f"{signal_usage.get('market_regime', 'unknown')}-limited"
                reasons.insert(0, f"market regime caution: reduced-size starter allowed despite {market_regime.get('reason')}")
            else:
                action = "watch"
                metrics["market_regime_blocked"] = 1.0
                reasons.insert(0, f"market regime filter: {market_regime.get('reason')}")

        if (
            action == "buy"
            and self._is_inverse_etf(symbol)
            and market_regime.get("enabled")
            and not bool(market_regime.get("inverse_buys_confirmed", True))
        ):
            action = "watch"
            reasons.insert(0, "inverse hedge awaiting multi-session downtrend confirmation")

        if action == "buy" and self._in_earnings_blackout(external_inputs):
            action = "watch"
            reasons.insert(
                0,
                f"earnings expected within {int(float(external_inputs.get('days_until_earnings', 0)))} day(s) - blackout window",
            )

        if limited_long_by_regime and action == "buy":
            risk_per_trade_pct = min(risk_per_trade_pct, self.settings.market_regime_limited_long_risk_pct)
            max_position_pct = min(max_position_pct, self.settings.market_regime_limited_long_max_position_pct)

        risk_budget = max(50.0, buying_power * risk_per_trade_pct)
        position_cap = max(100.0, buying_power * max_position_pct)
        qty_from_risk = risk_budget / risk
        qty_from_value = position_cap / price
        qty = self._round_share_qty(max(0.0, min(qty_from_risk, qty_from_value)))

        if qty <= 0:
            action = "watch"

        if action == "buy" and self._analyst_tracker is not None and not self._is_inverse_etf(symbol):
            require_strong_buy = self.settings.analyst_consensus_require_strong_buy
            if analyst_snapshot:
                consensus = str(analyst_snapshot.get("consensus", "")).strip()
                normalized_consensus = consensus.lower()
                upside_pct = float(analyst_snapshot.get("target_upside_pct", 0.0) or 0.0)
                if normalized_consensus in {"sell", "strong sell"}:
                    action = "watch"
                    metrics["analyst_consensus_blocked"] = 1.0
                    reasons.insert(0, f"analyst consensus is {consensus}, so the bot is standing down")
                elif self.settings.analyst_consensus_block_hold and normalized_consensus == "hold":
                    action = "watch"
                    metrics["analyst_consensus_blocked"] = 1.0
                    reasons.insert(0, "analyst consensus is Hold, so the bot is standing down")
                elif require_strong_buy and normalized_consensus != "strong buy":
                    action = "watch"
                    metrics["analyst_consensus_blocked"] = 1.0
                    reasons.insert(0, f"analyst consensus is {consensus}; only Strong Buy names have shown a live edge")
                elif (
                    self.settings.analyst_consensus_min_upside_pct > 0
                    and upside_pct < self.settings.analyst_consensus_min_upside_pct
                ):
                    action = "watch"
                    metrics["analyst_consensus_blocked"] = 1.0
                    reasons.insert(
                        0,
                        f"analyst upside is only {upside_pct:.2f}%, below the {self.settings.analyst_consensus_min_upside_pct:.2f}% minimum",
                    )
                elif normalized_consensus in {"buy", "strong buy"}:
                    reasons.insert(0, f"analyst consensus is {consensus}")
            elif require_strong_buy:
                action = "watch"
                metrics["analyst_consensus_blocked"] = 1.0
                reasons.insert(0, "no analyst consensus available; Strong Buy is required for new buys")

        return Candidate(
            symbol=symbol,
            price=round(price, 2),
            final_score=round(final_score, 2),
            action=action,
            reasons=reasons[:5],
            stop_price=stop_price,
            target_price=target_price,
            reward_risk=round(reward_risk, 2),
            qty=qty,
            analyst_scores={
                "decision_support": round(decision_support_score, 2),
                "momentum": round(momentum_score, 2),
                "reversion": round(reversion_score, 2),
                "risk": round(risk_score, 2),
            },
            metrics={k: round(v, 4) for k, v in (metrics | external_inputs).items()},
            signal_usage=signal_usage,
        )

    def _fetch_bars(self, symbols: List[str], days: int) -> Dict[str, List[dict]]:
        """Fetch historical bars, using Polygon when available and batching
        large symbol lists so we don't exceed URL-length limits."""
        # Prefer Polygon for bar data when configured — avoids Alpaca
        # URL-length limit when Polygon discovered hundreds of symbols.
        if self.polygon:
            try:
                return self.polygon.bars_batch(symbols, days)
            except Exception:  # noqa: BLE001
                log.warning("Polygon bars_batch failed, falling back to broker")

        # Alpaca (and others) choke on huge symbol lists in a single call.
        # Split into batches of 100 to stay within URL-length limits.
        batch_size = 100
        all_bars: Dict[str, List[dict]] = {}
        for start in range(0, len(symbols), batch_size):
            batch = symbols[start : start + batch_size]
            try:
                result = self.broker.bars(batch, days)
                all_bars.update(result)
            except ProviderError:
                log.warning("Broker bars batch failed for %d symbols starting at offset %d", len(batch), start)
                continue
        return all_bars

    def scan_market(self) -> List[Candidate]:
        try:
            self._auto_scale_limits()
            account = self.broker.account()
            symbols = self._candidate_symbol_pool()
        except ProviderError:
            self.db.record_scan(self.settings.broker_mode, self.broker.name, [])
            return []
        # Check the regime BEFORE the big candidate bar fetch: the ~200-symbol
        # concurrent burst can rate-limit the data API, and a 429 on SPY/QQQ
        # here reads as regime "missing", which blocks every long buy.
        market_regime = self._market_regime_status()
        self._update_regime_persistence(market_regime)
        bars = self._fetch_bars(symbols, self.settings.lookback_days)
        if not bars:
            log.warning("No bar data returned for %d symbols", len(symbols))
            self.db.record_scan(self.settings.broker_mode, self.broker.name, [])
            return []
        candidates: List[Candidate] = []
        for symbol in symbols:
            item = bars.get(symbol)
            if not item:
                continue
            candidate = self._candidate_from_bars(symbol, item, account.buying_power, market_regime=market_regime)
            if candidate:
                candidates.append(candidate)
        candidates.sort(key=lambda x: (x.action == "buy", x.final_score, x.reward_risk), reverse=True)
        trimmed = candidates[: self.settings.candidate_limit]
        self.db.record_scan(self.settings.broker_mode, self.broker.name, [c.model_dump() for c in trimmed])
        return trimmed

    def refresh_congress_trades(self) -> List[dict]:
        def run() -> List[dict]:
            tracker = CongressTracker(self.settings, self.broker.latest_prices)
            trades = [trade.model_dump() for trade in tracker.refresh()]
            self.db.replace_congress_trades(trades)
            return trades

        return self._refresh_source("congress", run)

    def refresh_sec_filings(self) -> List[dict]:
        def run() -> List[dict]:
            tracker = SecTracker(self.settings)
            symbols = self._candidate_symbol_pool()[: self.settings.scan_limit]
            filings = [filing.__dict__ for filing in tracker.refresh(symbols)]
            grouped: Dict[str, List[dict]] = {}
            for filing in filings:
                grouped.setdefault(filing["symbol"], []).append(filing)
            for symbol in symbols:
                self.db.replace_sec_filings_for_symbol(symbol, grouped.get(symbol, []))
            return filings

        return self._refresh_source("sec", run)

    def refresh_earnings_events(self) -> List[dict]:
        def run() -> List[dict]:
            tracker = EarningsTracker(self.settings)
            symbols = self._candidate_symbol_pool()[: self.settings.scan_limit]
            events = [event.__dict__ for event in tracker.refresh(symbols)]
            grouped: Dict[str, List[dict]] = {}
            for event in events:
                grouped.setdefault(event["symbol"], []).append(event)
            for symbol in symbols:
                self.db.replace_earnings_events_for_symbol(symbol, grouped.get(symbol, []))
            return events

        return self._refresh_source("earnings", run)

    def refresh_macro_events(self) -> List[dict]:
        def run() -> List[dict]:
            tracker = MacroTracker(self.settings)
            events = [event.__dict__ for event in tracker.refresh()]
            self.db.replace_macro_events(events)
            return events

        return self._refresh_source("macro", run)

    def refresh_all_signals(self) -> Dict[str, List[dict]]:
        return {
            "congress": self.refresh_congress_trades(),
            "sec": self.refresh_sec_filings(),
            "earnings": self.refresh_earnings_events(),
            "macro": self.refresh_macro_events(),
        }

    def _held_days(self, opened_at: str) -> int:
        opened = datetime.fromisoformat(opened_at)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - opened).days)

    def _loss_stop_price(self, entry_price: float, stored_stop_price: float) -> float:
        percent_stop = round(entry_price * (1 - self.settings.stop_loss_pct), 2)
        return max(float(stored_stop_price), percent_stop)

    def _daily_loss_liquidation_reason(self, account: object) -> str:
        if not self.settings.liquidate_on_daily_loss:
            return ""
        if not self.settings.is_demo and self._market_is_closed():
            return ""
        status = self._execution_safety_status(account)
        daily_loss_amount = float(status.get("daily_loss_amount", 0.0) or 0.0)
        daily_loss_pct = float(status.get("daily_loss_pct", 0.0) or 0.0) / 100.0
        if self.settings.daily_loss_limit_dollars > 0 and daily_loss_amount >= self.settings.daily_loss_limit_dollars:
            return f"daily loss limit (${daily_loss_amount:.2f})"
        if self.settings.daily_loss_limit_pct > 0 and daily_loss_pct >= self.settings.daily_loss_limit_pct:
            return f"daily loss limit ({daily_loss_pct * 100:.2f}%)"
        return ""

    def _open_exit_order_has_stop_at_or_above(self, orders: List[dict], stop_price: float) -> bool:
        best_stop = self._best_open_exit_stop_price(orders)
        return best_stop is not None and best_stop >= stop_price - 0.01

    def _best_open_exit_stop_price(self, orders: List[dict]) -> Optional[float]:
        best_stop: Optional[float] = None
        for order in orders:
            raw_stop = order.get("stop_price")
            if raw_stop in (None, ""):
                continue
            try:
                stop = float(raw_stop)
            except (TypeError, ValueError):
                continue
            best_stop = stop if best_stop is None else max(best_stop, stop)
        return best_stop

    def _protective_stop_replace_min_step(self, stop_price: float) -> float:
        pct = max(float(self.settings.protective_stop_replace_min_step_pct), 0.0)
        return max(0.01, round(float(stop_price) * pct, 2))

    @staticmethod
    def _order_status_is_filled(status: object) -> bool:
        return str(status or "").strip().lower() == "filled"

    @staticmethod
    def _stop_price_above_market_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "stop price must be less than current price" in message

    def _ensure_protective_exit_order(
        self,
        position: object,
        stop_price: float,
        target_price: float,
        current_price: float,
        meta: Optional[Dict[str, object]],
    ) -> Optional[dict]:
        if not (self.settings.is_alpaca and self.settings.use_broker_protective_orders):
            return None
        market_session = self._market_session_status()
        if (
            self.broker.name == "alpaca"
            and not bool(market_session["is_open"])
            and self._now_utc() >= market_session["current_session_close"]
        ):
            self._record_audit_event(
                "broker",
                "info",
                "Protective exit deferred until next session",
                {
                    "symbol": position.symbol,
                    "stop_price": stop_price,
                    "target_price": target_price,
                    "current_session_close": market_session["current_session_close"].isoformat(),
                    "next_open": market_session["next_open"].isoformat()
                    if isinstance(market_session["next_open"], datetime)
                    else None,
                },
            )
            return None
        actionable_price = current_price
        try:
            latest = self.broker.latest_prices([position.symbol]).get(position.symbol)
            if latest:
                actionable_price = float(latest)
        except Exception:  # noqa: BLE001
            actionable_price = current_price
        if actionable_price <= stop_price:
            self._record_audit_event(
                "broker",
                "warning",
                "Protective stop already breached",
                {"symbol": position.symbol, "stop_price": stop_price, "current_price": actionable_price},
            )
            return self._sell_position(position, actionable_price, "protective stop already breached", meta)
        try:
            open_orders = self.broker.open_exit_orders_for_symbol(position.symbol)
            if self._open_exit_order_has_stop_at_or_above(open_orders, stop_price):
                return None
            existing_stop = self._best_open_exit_stop_price(open_orders)
            if existing_stop is not None:
                min_step = self._protective_stop_replace_min_step(stop_price)
                if stop_price - existing_stop < min_step:
                    self._record_audit_event(
                        "broker",
                        "info",
                        "Protective stop ratchet deferred",
                        {
                            "symbol": position.symbol,
                            "existing_stop_price": existing_stop,
                            "desired_stop_price": stop_price,
                            "min_step": min_step,
                        },
                    )
                    return None
            if open_orders:
                self.broker.cancel_open_orders_for_symbol(position.symbol)
            result = self.broker.submit_protective_exit(position.symbol, position.qty, stop_price, target_price)
        except ProviderError as exc:
            if self._stop_price_above_market_error(exc):
                try:
                    latest = self.broker.latest_prices([position.symbol]).get(position.symbol)
                    if latest:
                        actionable_price = float(latest)
                except Exception:  # noqa: BLE001
                    actionable_price = current_price
                self._record_audit_event(
                    "broker",
                    "warning",
                    "Protective stop rejected because stop was already breached",
                    {
                        "symbol": position.symbol,
                        "stop_price": stop_price,
                        "target_price": target_price,
                        "current_price": actionable_price,
                        "error": str(exc),
                    },
                )
                return self._sell_position(position, actionable_price, "protective stop already breached", meta)
            self._record_audit_event(
                "broker",
                "error",
                "Protective exit order failed",
                {"symbol": position.symbol, "stop_price": stop_price, "target_price": target_price, "error": str(exc)},
            )
            self._notify_failure(
                "protective exit order",
                exc,
                {"symbol": position.symbol, "stop_price": stop_price, "target_price": target_price},
            )
            return None
        if result:
            self._record_audit_event(
                "broker",
                "info",
                "Protective exit order submitted",
                {
                    "symbol": position.symbol,
                    "stop_price": stop_price,
                    "target_price": target_price,
                    "status": result.get("status", ""),
                    "order_class": result.get("order_class", ""),
                },
            )
        return None

    def _sell_position(self, position: object, current: float, note: str, meta: Optional[Dict[str, object]]) -> Optional[dict]:
        try:
            if self.settings.is_alpaca and self.settings.use_broker_protective_orders:
                self.broker.cancel_open_orders_for_symbol(position.symbol)
            result = self.broker.sell(position.symbol, position.qty)
        except ProviderError as exc:
            self.db.record_trade(position.symbol, "sell", position.qty, current, "error", str(exc))
            self._notify_failure("position sell", exc, {"symbol": position.symbol, "note": note})
            return None
        raw_qty = result.get("qty")
        raw_price = result.get("filled_avg_price")
        status = result.get("status", "submitted")
        recorded_qty = float(raw_qty) if raw_qty not in (None, "") else float(position.qty)
        recorded_price = float(raw_price) if raw_price not in (None, "") else float(current)
        if not self._order_status_is_filled(status):
            if meta:
                self.db.set_exit_pending(position.symbol, True)
            self.db.record_trade(
                position.symbol,
                "sell",
                recorded_qty,
                recorded_price,
                str(status or "submitted"),
                f"{note} (exit pending)",
            )
            self._record_audit_event(
                "broker",
                "info",
                "Exit order submitted and pending fill",
                {"symbol": position.symbol, "qty": recorded_qty, "price": recorded_price, "status": status, "note": note},
            )
            return {"symbol": position.symbol, "note": f"{note} (exit pending)", "status": str(status or "submitted")}
        closed = self.db.close_position_meta(position.symbol) if meta else None
        entry = float(closed["entry_price"]) if closed else position.avg_entry_price
        pnl_pct = ((recorded_price - entry) / entry) * 100 if entry else 0.0
        pnl_amount = (recorded_price - entry) * recorded_qty
        analysis = closed["analysis"] if closed else (meta.get("analysis", {}) if meta else {})
        self.db.record_trade(
            position.symbol,
            "sell",
            recorded_qty,
            recorded_price,
            "filled",
            note,
            pnl_pct,
            analysis,
            pnl_amount,
        )
        if analysis:
            self.db.update_learning(analysis, pnl_pct)
            log.info("Learning updated for %s: pnl=%.2f%% analysis=%s", position.symbol, pnl_pct, analysis)
        else:
            log.warning("No analysis stored for %s - learning skipped", position.symbol)
        return {"symbol": position.symbol, "pnl_pct": round(pnl_pct, 2), "note": note}

    def reconcile_broker_state(self) -> List[dict]:
        tracked = {item["symbol"]: item for item in self.db.all_position_meta()}
        live_positions = {p.symbol: p for p in self.broker.positions()}
        notes: List[dict] = []
        mismatches: List[Dict[str, object]] = []

        for symbol, position in live_positions.items():
            meta = tracked.get(symbol)
            if not meta:
                stop_price = round(max(position.avg_entry_price * (1 - self.settings.stop_loss_pct), self.settings.min_stock_price * 0.5), 2)
                target_price = round(position.avg_entry_price + (position.avg_entry_price - stop_price) * self.settings.min_reward_risk, 2)
                # Try to recover analyst_scores from the original buy trade event
                recovered_analysis = self.db.recover_analysis_for_symbol(symbol)
                self.db.open_position_meta(symbol, position.qty, position.avg_entry_price, stop_price, target_price, recovered_analysis)
                self.db.record_trade(symbol, "buy", position.qty, position.avg_entry_price, "reconciled", "reconciled external position")
                notes.append({"symbol": symbol, "note": "reconciled external position"})
                mismatches.append({"symbol": symbol, "type": "missing_meta"})
                continue
            if bool(meta.get("exit_pending")):
                try:
                    open_orders = self.broker.open_exit_orders_for_symbol(symbol)
                except ProviderError:
                    open_orders = []
                if not open_orders:
                    self.db.set_exit_pending(symbol, False)
                    meta["exit_pending"] = 0
                    notes.append({"symbol": symbol, "note": "cleared stale pending exit"})
                    mismatches.append({"symbol": symbol, "type": "stale_exit_pending"})
            if abs(float(meta["qty"]) - float(position.qty)) > 1e-9 or abs(float(meta["entry_price"]) - float(position.avg_entry_price)) > 1e-9:
                self.db.open_position_meta(
                    symbol,
                    position.qty,
                    position.avg_entry_price,
                    float(meta["stop_price"]),
                    float(meta["target_price"]),
                    meta["analysis"],
                )
                notes.append({"symbol": symbol, "note": "synced live position metadata"})
                mismatches.append({"symbol": symbol, "type": "position_mismatch"})

        missing_symbols = [symbol for symbol in tracked if symbol not in live_positions]
        if not missing_symbols:
            if mismatches:
                self._record_audit_event(
                    "broker",
                    "warning",
                    "Broker reconciliation detected and handled state mismatches",
                    {"mismatches": mismatches},
                )
            return notes

        recent_sells = self.broker.recent_filled_sell_orders(missing_symbols)
        for symbol in missing_symbols:
            order = recent_sells.get(symbol)
            if not order:
                mismatches.append({"symbol": symbol, "type": "missing_live_position"})
                continue
            closed = self.db.close_position_meta(symbol)
            if not closed:
                continue
            exit_price = float(order.get("filled_avg_price") or order.get("limit_price") or closed["target_price"])
            qty = float(order.get("filled_qty") or order.get("qty") or closed["qty"])
            entry = float(closed["entry_price"])
            pnl_pct = ((exit_price - entry) / entry) * 100 if entry else 0.0
            note = order.get("order_class") or order.get("client_order_id") or "broker managed exit"
            analysis = closed["analysis"]
            pnl_amount = (exit_price - entry) * qty
            self.db.record_trade(symbol, "sell", qty, exit_price, order.get("status", "filled"), note, pnl_pct, analysis, pnl_amount)
            if analysis:
                self.db.update_learning(analysis, pnl_pct)
            notes.append({"symbol": symbol, "pnl_pct": round(pnl_pct, 2), "note": note})
            mismatches.append({"symbol": symbol, "type": "broker_managed_exit"})
        if mismatches:
            self._record_audit_event(
                "broker",
                "warning",
                "Broker reconciliation detected and handled state mismatches",
                {"mismatches": mismatches},
            )
        return notes

    def manage_positions(self) -> List[dict]:
        broker_notes: List[dict] = []
        if self.settings.is_alpaca:
            broker_notes = self.reconcile_broker_state()
        positions = self.broker.positions()
        prices = self.broker.latest_prices([p.symbol for p in positions])
        daily_liquidation_reason = self._daily_loss_liquidation_reason(self.broker.account())
        sold: List[dict] = list(broker_notes)
        rotation_market_open: Optional[bool] = None
        for position in positions:
            meta = self.db.get_position_meta(position.symbol)
            if not meta:
                continue
            if bool(meta.get("exit_pending")):
                continue
            current = prices.get(position.symbol, position.current_price)
            entry_price = float(meta["entry_price"])
            gain_pct = ((current - entry_price) / entry_price) if entry_price else 0.0

            held_days = self._held_days(str(meta["opened_at"]))

            if daily_liquidation_reason:
                sold_item = self._sell_position(position, current, daily_liquidation_reason, meta)
                if sold_item:
                    sold.append(sold_item)
                continue

            # Rotate out positions the exclusion list would refuse to buy today
            # (legacy broad-market ETFs bought before the exclusion deployed).
            # Held >= 1 day so the sell can never count as a PDT day trade.
            if (
                self.settings.exclude_broad_market_etfs
                and position.symbol.upper() in self.settings.broad_market_etfs
                and not self._is_inverse_etf(position.symbol)
                and held_days >= 1
            ):
                if rotation_market_open is None:
                    rotation_market_open = self.settings.is_demo or not self._market_is_closed()
                if rotation_market_open:
                    sold_item = self._sell_position(
                        position, current, "rotating out of excluded broad-market ETF", meta
                    )
                    if sold_item:
                        sold.append(sold_item)
                    continue

            target_price = float(meta["target_price"])
            if target_price > 0 and current >= target_price and held_days >= self.settings.min_hold_days:
                sold_item = self._sell_position(position, current, "target hit", meta)
                if sold_item:
                    sold.append(sold_item)
                continue

            # --- Partial profit-taking ---
            if (
                self.settings.partial_profit_enabled
                and not bool(meta.get("partial_profit_taken"))
                and held_days >= self.settings.min_hold_days
                and gain_pct >= self.settings.partial_profit_pct
                and position.qty >= 0.01
            ):
                sell_qty = self._round_share_qty(max(0.01, position.qty * self.settings.partial_sell_fraction))
                if sell_qty >= position.qty:
                    sell_qty = self._round_share_qty(max(0.0, position.qty - 0.01))
                if sell_qty <= 0:
                    continue
                remaining_qty = position.qty - sell_qty
                try:
                    if self.settings.is_alpaca and self.settings.use_broker_protective_orders:
                        self.broker.cancel_open_orders_for_symbol(position.symbol)
                    result = self.broker.sell(position.symbol, sell_qty)
                except ProviderError as exc:
                    log.warning("Partial profit sell failed for %s: %s", position.symbol, exc)
                    self._notify_failure("partial profit sell", exc, {"symbol": position.symbol})
                else:
                    raw_price = result.get("filled_avg_price")
                    recorded_price = float(raw_price) if raw_price not in (None, "") else float(current)
                    pnl_pct = ((recorded_price - entry_price) / entry_price) * 100
                    self.db.record_trade(
                        position.symbol, "sell", sell_qty, recorded_price,
                        "filled", f"partial profit at +{gain_pct*100:.0f}%", pnl_pct,
                        meta.get("analysis", {}),
                        (recorded_price - entry_price) * sell_qty,
                    )
                    self.db.mark_partial_profit_taken(position.symbol)
                    self.db.update_position_qty(position.symbol, remaining_qty)
                    # Ratchet stop up to breakeven so the remaining shares are house money
                    if entry_price > float(meta["stop_price"]):
                        self.db.update_stop_price(position.symbol, entry_price)
                    log.info(
                        "Partial profit: sold %.4f of %s at $%.2f (+%.1f%%), %.4f shares remain at breakeven stop",
                        sell_qty, position.symbol, recorded_price, pnl_pct, remaining_qty,
                    )
                    sold.append({"symbol": position.symbol, "pnl_pct": round(pnl_pct, 2), "note": f"partial profit ({sell_qty:.4f} shares)"})
                continue  # skip full-sell check this cycle, re-evaluate next cycle

            should_sell = False
            note = ""
            # Track peak price — the highest the stock has reached since we bought
            peak_price = float(meta.get("peak_price") or entry_price)
            if current > peak_price:
                peak_price = current
                self.db.update_peak_price(position.symbol, peak_price)
            # Trailing stop: drop from peak price (default 10%), never moves down.
            # Uses TRAILING_STOP_PCT independently from STOP_LOSS_PCT (the loss cap).
            trail_pct = self.settings.trailing_stop_pct
            trailing_stop = round(peak_price * (1 - trail_pct), 2)
            # Also respect the original stop (for initial downside protection)
            stored_stop_price = self._loss_stop_price(entry_price, float(meta["stop_price"]))
            effective_stop_price = stored_stop_price
            trailing_stop_active = False
            if trailing_stop > effective_stop_price:
                effective_stop_price = trailing_stop
                trailing_stop_active = True
                self.db.update_stop_price(position.symbol, trailing_stop)
            if current <= effective_stop_price:
                should_sell = True
                if trailing_stop_active:
                    drop_from_peak = ((peak_price - current) / peak_price * 100) if peak_price else 0
                    note = f"trailing stop (peak ${peak_price:.2f}, dropped {drop_from_peak:.1f}%)"
                else:
                    note = "stop hit"
            elif self.settings.max_hold_days > 0 and held_days >= self.settings.max_hold_days:
                should_sell = True
                note = "time stop"
            elif position.unrealized_pl_pct <= -(self.settings.stop_loss_pct * 100):
                should_sell = True
                note = "loss cap"
            if should_sell:
                sold_item = self._sell_position(position, current, note, meta)
                if sold_item:
                    sold.append(sold_item)
            else:
                sold_item = self._ensure_protective_exit_order(
                    position,
                    effective_stop_price,
                    target_price,
                    current,
                    meta,
                )
                if sold_item:
                    sold.append(sold_item)
        return sold

    def _position_display_rows(self) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []
        live_positions = self.broker.positions()
        prices = self.broker.latest_prices([p.symbol for p in live_positions]) if live_positions else {}
        for position in live_positions:
            meta = self.db.get_position_meta(position.symbol)
            current_price = float(prices.get(position.symbol, position.current_price))
            payload: Dict[str, object] = position.model_dump()
            if not meta:
                rows.append(payload)
                continue
            entry_price = float(meta["entry_price"])
            peak_price = max(float(meta.get("peak_price") or entry_price), current_price)
            stored_stop_price = self._loss_stop_price(entry_price, float(meta["stop_price"]))
            trailing_stop = round(peak_price * (1 - self.settings.trailing_stop_pct), 2)
            active_stop_price = max(stored_stop_price, trailing_stop)
            payload.update(
                {
                    "peak_price": round(peak_price, 2),
                    "stored_stop_price": round(stored_stop_price, 2),
                    "trailing_stop_price": round(trailing_stop, 2),
                    "active_stop_price": round(active_stop_price, 2),
                    "target_price": round(float(meta["target_price"]), 2),
                    "distance_to_stop_pct": round(((current_price - active_stop_price) / current_price) * 100, 2) if current_price else 0.0,
                    "opened_at": meta["opened_at"],
                }
            )
            rows.append(payload)
        return rows

    def _performance_summary(self, account: object, positions: List[Dict[str, object]], trades: List[Dict[str, object]]) -> Dict[str, object]:
        open_positions: List[Dict[str, object]] = []
        unrealized_pnl = 0.0
        open_cost_basis = 0.0
        open_winners = 0
        open_losers = 0

        for position in positions:
            qty = float(position.get("qty", 0) or 0)
            entry = float(position.get("avg_entry_price", position.get("entry_price", 0)) or 0)
            current = float(position.get("current_price", entry) or entry)
            cost_basis = qty * entry
            market_value = qty * current
            pnl_amount = market_value - cost_basis
            pnl_pct = (pnl_amount / cost_basis * 100) if cost_basis > 0 else 0.0
            unrealized_pnl += pnl_amount
            open_cost_basis += cost_basis
            if pnl_amount > 0:
                open_winners += 1
            elif pnl_amount < 0:
                open_losers += 1
            open_positions.append(
                {
                    "symbol": str(position.get("symbol", "")),
                    "pnl_amount": round(pnl_amount, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "market_value": round(market_value, 2),
                    "cost_basis": round(cost_basis, 2),
                }
            )

        realized_pnl = 0.0
        closed_winners = 0
        closed_losers = 0
        for trade in trades:
            if trade.get("side") != "sell":
                continue
            raw_amount = trade.get("pnl_amount")
            if raw_amount is None:
                continue
            pnl_amount = float(raw_amount)
            realized_pnl += pnl_amount
            if pnl_amount > 0:
                closed_winners += 1
            elif pnl_amount < 0:
                closed_losers += 1

        baseline_equity = self._scaling_baseline_equity(max(float(account.equity), float(account.cash), 0.0))
        peak_equity = max(self._load_peak_equity(), float(account.equity), baseline_equity)
        total_pnl = realized_pnl + unrealized_pnl
        tracked_basis = float(account.equity) - total_pnl
        if tracked_basis > 0:
            total_return_pct = total_pnl / tracked_basis * 100
        else:
            total_return_pct = ((float(account.equity) - baseline_equity) / baseline_equity * 100) if baseline_equity > 0 else 0.0
        open_positions.sort(key=lambda item: float(item["pnl_amount"]), reverse=True)
        winning_positions = [item for item in open_positions if float(item["pnl_amount"]) > 0]
        losing_positions = [item for item in open_positions if float(item["pnl_amount"]) < 0]

        return {
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_return_pct, 2),
            "baseline_equity": round(baseline_equity, 2),
            "tracked_basis": round(tracked_basis, 2),
            "peak_equity": round(peak_equity, 2),
            "open_cost_basis": round(open_cost_basis, 2),
            "open_market_value": round(open_cost_basis + unrealized_pnl, 2),
            "closed_winners": closed_winners,
            "closed_losers": closed_losers,
            "open_winners": open_winners,
            "open_losers": open_losers,
            "top_winners": winning_positions[:3],
            "top_losers": sorted(losing_positions, key=lambda item: float(item["pnl_amount"]))[:3],
        }

    def buy_candidates(self, candidates: List[Candidate]) -> List[dict]:
        pause_reason = self._buying_pause_reason()
        if pause_reason:
            return []
        positions = self.broker.positions()
        existing = {p.symbol for p in positions}
        recently_sold_losses = self.db.recently_sold_symbols(self.settings.rebuy_cooldown_hours)
        recently_sold_any = (
            self.db.recently_sold_symbols(self.settings.rebuy_after_sell_cooldown_hours)
            if self.settings.rebuy_after_sell_cooldown_hours > 0
            else {}
        )
        account = self.broker.account()
        bought: List[dict] = []
        open_position_limit = self.settings.max_open_positions or (len(positions) + self.settings.max_new_positions_per_run)
        slots = min(self.settings.max_new_positions_per_run, max(0, open_position_limit - len(positions)))
        cash_left = account.buying_power
        if self.settings.cash_buffer_pct > 0:
            cash_left = max(0.0, cash_left - account.equity * self.settings.cash_buffer_pct)
        deployed_capital = sum(p.market_value for p in positions)
        capital_limit = self.settings.max_total_capital if self.settings.max_total_capital > 0 else max(account.equity, deployed_capital + cash_left)
        capital_left = max(0.0, capital_limit - deployed_capital)
        # Dynamic equity-based position sizing
        if self.settings.is_small_account:
            effective_risk_per_trade_pct = max(self.settings.risk_per_trade_pct, 0.04)
            effective_max_position_pct = max(self.settings.max_position_pct, 0.25)
        else:
            effective_risk_per_trade_pct = self.settings.risk_per_trade_pct
            effective_max_position_pct = self.settings.max_position_pct
        risk_budget = max(50.0, account.equity * effective_risk_per_trade_pct)
        position_cap = max(100.0, account.equity * effective_max_position_pct)
        held_values: List[Tuple[str, float]] = [(p.symbol, float(p.market_value)) for p in positions]
        for candidate in candidates:
            if slots <= 0:
                break
            if candidate.action != "buy" or candidate.symbol in existing:
                continue
            sold_info = recently_sold_any.get(candidate.symbol)
            if sold_info:
                log.info(
                    "Skipping %s - sold recently within %dh cooldown",
                    candidate.symbol,
                    self.settings.rebuy_after_sell_cooldown_hours,
                )
                continue
            # Longer cooldown for loss sells to avoid buy/sell loops.
            sold_info = recently_sold_losses.get(candidate.symbol)
            if sold_info:
                sold_pnl = sold_info.get("pnl_pct") or 0
                if sold_pnl < 0:
                    log.info(
                        "Skipping %s - sold at %.1f%% within %dh cooldown",
                        candidate.symbol, sold_pnl, self.settings.rebuy_cooldown_hours,
                    )
                    continue
            max_affordable_qty = self._round_share_qty(min(cash_left, capital_left) / candidate.price) if candidate.price > 0 else 0.0
            qty = min(candidate.qty, max_affordable_qty)
            if self._is_inverse_etf(candidate.symbol):
                headroom = self._inverse_hedge_headroom(candidate.symbol, held_values, account.equity)
                if headroom <= 0:
                    log.info(
                        "Skipping %s - inverse hedge limits reached (bucket, count, or %.0f%% exposure cap)",
                        candidate.symbol,
                        self.settings.max_inverse_exposure_pct * 100,
                    )
                    continue
                if candidate.price > 0 and math.isfinite(headroom):
                    qty = min(qty, self._round_share_qty(headroom / candidate.price))
            est_cost = qty * candidate.price
            if qty <= 0 or est_cost > cash_left or est_cost > capital_left:
                continue
            if est_cost < self.settings.min_buy_notional:
                # Alpaca rejects fractional orders under $1 notional; skip instead
                # of submitting an order that is guaranteed to error out.
                log.info(
                    "Skipping %s - order notional $%.2f below minimum $%.2f",
                    candidate.symbol, est_cost, self.settings.min_buy_notional,
                )
                continue
            try:
                result = self.broker.buy(
                    candidate.symbol,
                    qty,
                    stop_price=candidate.stop_price,
                    target_price=candidate.target_price,
                )
            except ProviderError as exc:
                self.db.record_trade(candidate.symbol, "buy", qty or candidate.qty, candidate.price, "error", str(exc), analysis=candidate.analyst_scores)
                continue
            status = str(result.get("status", "submitted") or "submitted")
            raw_fill_price = result.get("filled_avg_price")
            raw_filled_qty = result.get("filled_qty") or result.get("qty")
            if not self._order_status_is_filled(status) or raw_fill_price in (None, ""):
                recorded_qty = float(raw_filled_qty) if raw_filled_qty not in (None, "") else float(qty)
                self.db.record_trade(
                    candidate.symbol,
                    "buy",
                    recorded_qty,
                    candidate.price,
                    status,
                    "entry pending",
                    analysis=candidate.analyst_scores,
                )
                bought.append({"symbol": candidate.symbol, "qty": recorded_qty, "price": candidate.price, "status": status})
                held_values.append((candidate.symbol, est_cost))
                cash_left -= est_cost
                capital_left -= est_cost
                slots -= 1
                continue
            fill_price = float(raw_fill_price)
            applied_stop_price = self._loss_stop_price(fill_price, candidate.stop_price)
            filled_qty = float(raw_filled_qty) if raw_filled_qty not in (None, "") else float(qty)
            self.db.record_trade(candidate.symbol, "buy", filled_qty, fill_price, status, "entry", analysis=candidate.analyst_scores)
            self.db.open_position_meta(candidate.symbol, filled_qty, fill_price, applied_stop_price, candidate.target_price, candidate.analyst_scores)
            bought.append({"symbol": candidate.symbol, "qty": filled_qty, "price": fill_price})
            held_values.append((candidate.symbol, est_cost))
            cash_left -= est_cost
            capital_left -= est_cost
            slots -= 1
        return bought

    def _auto_scale_limits(self) -> None:
        """Scale search, liquidity, and risk controls as account equity compounds."""
        if self._base_max_total_capital <= 0:
            return
        try:
            account = self.broker.account()
        except Exception:
            return
        equity = max(float(account.equity), float(account.cash), 0.0)
        if equity <= 0:
            return
        baseline = self._scaling_baseline_equity(equity)
        growth_ratio = max(1.0, equity / baseline)
        gentle_ratio = growth_ratio ** 0.35
        search_ratio = growth_ratio ** 0.25
        liquidity_ratio = growth_ratio ** 0.60
        risk_taper_ratio = growth_ratio ** 0.20

        peak_equity = max(self._load_peak_equity(), equity, baseline)
        if peak_equity > 0:
            self.db.set_bot_state("peak_equity", f"{peak_equity:.2f}")
        drawdown_pct = max(0.0, (peak_equity - equity) / peak_equity) if peak_equity > 0 else 0.0
        drawdown_profile = self._drawdown_profile(drawdown_pct)
        self._latest_growth_ratio = growth_ratio
        self._latest_drawdown_pct = drawdown_pct
        self._latest_drawdown_state = str(drawdown_profile["state"])

        scaled_capital = self._round_up(self._base_max_total_capital * growth_ratio, self._capital_step())
        self.settings.max_total_capital = max(
            100.0,
            round(scaled_capital * float(drawdown_profile["capital_mult"]), 2),
        )

        scaled_open_positions = self._base_max_open_positions
        if self._base_max_open_positions > 0:
            scaled_open_positions = max(
                self._base_max_open_positions,
                math.ceil(self._base_max_open_positions * gentle_ratio),
            )
            scaled_open_positions = max(1, math.ceil(scaled_open_positions * float(drawdown_profile["positions_mult"])))
            self.settings.max_open_positions = scaled_open_positions
        if self._base_max_new_positions_per_run > 0 and scaled_open_positions > 0:
            scaled_new_positions = max(
                self._base_max_new_positions_per_run,
                math.ceil(self._base_max_new_positions_per_run * gentle_ratio),
            )
            new_positions_cap = int(drawdown_profile["new_positions_cap"])
            if new_positions_cap > 0:
                scaled_new_positions = min(scaled_new_positions, new_positions_cap)
            scaled_new_positions = min(scaled_new_positions, scaled_open_positions)
            self.settings.max_new_positions_per_run = scaled_new_positions
        if self._base_max_stock_price > 0:
            self.settings.max_stock_price = max(
                self._base_max_stock_price,
                self._round_up(self._base_max_stock_price * gentle_ratio, 0.5),
            )
        if self._base_congress_max_price > 0:
            self.settings.congress_max_price = max(
                self._base_congress_max_price,
                self._round_up(self._base_congress_max_price * gentle_ratio, 0.5),
            )
        self.settings.scan_limit = max(
            self._base_scan_limit,
            math.ceil(self._base_scan_limit * min(search_ratio, 3.0)),
        )
        self.settings.candidate_limit = max(
            self._base_candidate_limit,
            math.ceil(self._base_candidate_limit * min(search_ratio, 2.5)),
        )
        self.settings.min_dollar_volume = max(
            self._base_min_dollar_volume,
            self._round_volume_step(self._base_min_dollar_volume * liquidity_ratio),
        )
        self.settings.risk_per_trade_pct = max(
            self._base_risk_per_trade_pct * 0.35,
            (self._base_risk_per_trade_pct / risk_taper_ratio) * float(drawdown_profile["risk_mult"]),
        )
        self.settings.max_position_pct = max(
            self._base_max_position_pct * 0.40,
            (self._base_max_position_pct / (growth_ratio ** 0.15)) * float(drawdown_profile["position_pct_mult"]),
        )

    # ------------------------------------------------------------------
    # Retroactive scan learning: check how past scan picks performed
    # and feed the outcome back into the learning weights, even for
    # stocks we only watched but didn't buy.
    # ------------------------------------------------------------------
    def _record_shadow_candidates(self, candidates: List[Candidate], bought: List[dict]) -> List[dict]:
        if not self.settings.shadow_mode_strategies:
            return []
        bought_symbols = {str(item.get("symbol", "")).upper() for item in bought}
        held_symbols = {position.symbol for position in self.broker.positions()}
        eligible: List[dict] = []
        for candidate in candidates:
            if candidate.symbol in bought_symbols or candidate.symbol in held_symbols:
                continue
            if candidate.final_score < self.settings.shadow_min_score:
                continue
            payload = candidate.model_dump()
            payload["shadow_reason"] = (
                "would-buy candidate not executed"
                if candidate.action == "buy"
                else "high-score watch candidate"
            )
            eligible.append(payload)
            if len(eligible) >= max(1, self.settings.shadow_max_picks_per_cycle):
                break
        inserted = self.db.record_shadow_picks("candidate_score", eligible)
        if inserted:
            self._record_audit_event(
                "shadow",
                "info",
                f"Recorded {inserted} shadow candidate(s)",
                {"strategy": "candidate_score", "count": inserted},
            )
        return eligible

    def _new_performance_bucket(self, name: str) -> Dict[str, object]:
        return {"name": name, "count": 0, "wins": 0, "losses": 0, "total_pnl_pct": 0.0, "avg_pnl_pct": 0.0}

    def _add_performance_result(self, buckets: Dict[str, Dict[str, object]], name: str, pnl_pct: float) -> None:
        bucket = buckets.setdefault(name, self._new_performance_bucket(name))
        bucket["count"] = int(bucket["count"]) + 1
        bucket["wins"] = int(bucket["wins"]) + (1 if pnl_pct > 0 else 0)
        bucket["losses"] = int(bucket["losses"]) + (1 if pnl_pct <= 0 else 0)
        bucket["total_pnl_pct"] = float(bucket["total_pnl_pct"]) + pnl_pct
        bucket["avg_pnl_pct"] = float(bucket["total_pnl_pct"]) / int(bucket["count"])

    def _performance_bucket_rows(self, buckets: Dict[str, Dict[str, object]]) -> List[Dict[str, object]]:
        rows = list(buckets.values())
        for row in rows:
            row["total_pnl_pct"] = round(float(row["total_pnl_pct"]), 2)
            row["avg_pnl_pct"] = round(float(row["avg_pnl_pct"]), 2)
        rows.sort(key=lambda item: (int(item["count"]), float(item["avg_pnl_pct"])), reverse=True)
        return rows

    def _weekly_signal_performance(self) -> Dict[str, object]:
        days = max(1, int(self.settings.weekly_report_days))
        realized_buckets: Dict[str, Dict[str, object]] = {}
        for trade in self.db.recent_realized_sells(days=days):
            pnl_pct = float(trade.get("pnl_pct") or 0.0)
            for strategy, score in (trade.get("analysis") or {}).items():
                if float(score or 0.0) <= 0:
                    continue
                self._add_performance_result(realized_buckets, str(strategy), pnl_pct)

        shadow_picks = self.db.recent_shadow_picks(days=days, limit=200)
        symbols = sorted({str(item.get("symbol", "")).upper() for item in shadow_picks if item.get("symbol")})
        try:
            latest_prices = self.broker.latest_prices(symbols) if symbols else {}
        except Exception:  # noqa: BLE001
            latest_prices = {}

        shadow_strategy_buckets: Dict[str, Dict[str, object]] = {}
        shadow_signal_buckets: Dict[str, Dict[str, object]] = {}
        enriched_shadow: List[Dict[str, object]] = []
        for pick in shadow_picks:
            symbol = str(pick.get("symbol", "")).upper()
            entry_price = float(pick.get("price") or 0.0)
            current_price = float(latest_prices.get(symbol) or 0.0)
            if entry_price <= 0 or current_price <= 0:
                pnl_pct = 0.0
            else:
                pnl_pct = ((current_price - entry_price) / entry_price) * 100
            for strategy, score in (pick.get("analysis") or {}).items():
                if float(score or 0.0) <= 0:
                    continue
                self._add_performance_result(shadow_strategy_buckets, str(strategy), pnl_pct)
            for source, state in (pick.get("signal_usage") or {}).items():
                self._add_performance_result(shadow_signal_buckets, f"{source}:{state}", pnl_pct)
            enriched = dict(pick)
            enriched["current_price"] = round(current_price, 2) if current_price > 0 else None
            enriched["pnl_pct"] = round(pnl_pct, 2) if current_price > 0 else None
            enriched_shadow.append(enriched)

        return {
            "days": days,
            "realized_strategies": self._performance_bucket_rows(realized_buckets),
            "shadow_strategies": self._performance_bucket_rows(shadow_strategy_buckets),
            "shadow_signals": self._performance_bucket_rows(shadow_signal_buckets),
            "shadow_picks": enriched_shadow[:20],
        }

    def _run_put_shadow(self) -> None:
        """Feed bearish/risk-off signals into the paper put-shadow ledger.

        This is completely OFF the live trading path: it only writes to a JSON
        paper ledger and may send a one-time readiness email. Gated by
        PUT_SHADOW_ENABLED (default off), and wrapped so it can NEVER break the
        live trade cycle. See tradebot/put_shadow.py for the readiness logic.
        """
        if not self.settings.put_shadow_enabled:
            return
        try:
            from .put_shadow import PutShadowLedger, realized_vol

            ledger = PutShadowLedger(self.settings.data_dir / "put_shadow.json")
            regime = self._market_regime_status()
            self._update_regime_persistence(regime)
            symbols = [str(s).upper() for s in (regime.get("symbols") or [])]
            if not symbols:
                return
            short_w = max(2, int(self.settings.market_regime_short_window))
            bars = self._fetch_bars(symbols, max(60, int(self.settings.market_regime_long_window) + 10))

            prices: Dict[str, float] = {}
            vols: Dict[str, float] = {}
            closes_by_symbol: Dict[str, List[float]] = {}
            for sym in symbols:
                closes = [float(b["c"]) for b in (bars.get(sym) or []) if b.get("c") is not None]
                if len(closes) < short_w:
                    continue
                closes_by_symbol[sym] = closes
                prices[sym] = closes[-1]
                vols[sym] = realized_vol(closes)

            # Only OPEN a new synthetic put when the weak regime has persisted
            # (same confirmation gate the bot uses for inverse-ETF hedges) AND
            # the name is below its short MA — i.e. an actual momentum breakdown.
            if (
                regime.get("enabled")
                and not regime.get("allow_long_buys")
                and bool(regime.get("inverse_buys_confirmed"))
            ):
                for sym, closes in closes_by_symbol.items():
                    short_ma = sum(closes[-short_w:]) / short_w
                    if closes[-1] < short_ma:
                        ledger.open_synthetic_put(sym, closes[-1], closes)

            # Always mark/close existing open trades, then check the strict bar.
            ledger.update_open_trades(prices, vols)
            ledger.maybe_alert_ready()
        except Exception as exc:  # noqa: BLE001 - paper feature must never break trading
            log.warning("put-shadow hook skipped: %s", exc)

    def trade_once(self) -> Dict[str, List[dict]]:
        self._auto_scale_limits()
        self.broker.advance_market()
        sold = self.manage_positions()
        candidates = self.scan_market()
        bought = self.buy_candidates(candidates)
        shadow = self._record_shadow_candidates(candidates, bought)
        self._run_put_shadow()
        payload = {"sold": sold, "bought": bought, "candidates": [c.model_dump() for c in candidates], "shadow": shadow}
        pause_reason = self._buying_pause_reason()
        if pause_reason:
            payload["buying_paused_reason"] = pause_reason
        return payload

    def trade_once_with_congress_refresh(self) -> Dict[str, List[dict]]:
        return self.trade_once_with_signal_refresh()

    def trade_once_with_signal_refresh(self) -> Dict[str, List[dict]]:
        if self._should_refresh_signals_now():
            self.refresh_all_signals()
        return self.trade_once()

    def _should_refresh_signals_now(self) -> bool:
        """Scheduled signal refreshes only run while the market is open.

        The macro/congress/SEC feeds are external sites that rate-limit
        around-the-clock scraping (HTTP 429), and their calendars don't
        change while the market is closed. Demo mode always refreshes so
        local runs and tests stay deterministic. Manual refreshes via the
        CLI or dashboard endpoints bypass this gate entirely.
        """
        if self.settings.is_demo or not self._market_is_closed():
            self._signals_paused_market_closed = False
            return True
        if not self._signals_paused_market_closed:
            self._signals_paused_market_closed = True
            self._record_audit_event(
                "signal",
                "info",
                "signal refresh paused until the market reopens",
                {"market_closed": True},
            )
        return False

    def dashboard_snapshot(self) -> dict:
        self._auto_scale_limits()
        self._maybe_sync_broker_state()
        account = self.broker.account()
        positions = self._position_display_rows()
        trades = self.db.recent_trades(25)
        market_session = self._market_session_status()
        safety_status = self._execution_safety_status(account)
        self._record_safety_transition(safety_status)
        signal_diagnostics = self._signal_diagnostics()
        performance = self._performance_summary(account, positions, trades)
        market_regime = self._market_regime_status()
        weekly_signal_performance = self._weekly_signal_performance()
        return {
            "account": account.model_dump(),
            "candidates": self.db.latest_candidates(),
            "congress_trades": self.db.recent_congress_trades(self.settings.congress_trade_limit),
            "positions": positions,
            "trades": trades,
            "audit_events": self.db.recent_audit_events(20),
            "learning": self.db.learning_weights(),
            "performance": performance,
            "signal_health": self._signal_health(),
            "signal_diagnostics": signal_diagnostics,
            "signal_refresh_history": self.db.recent_signal_refresh_history(12),
            "shadow_picks": weekly_signal_performance["shadow_picks"],
            "weekly_signal_performance": weekly_signal_performance,
            "degraded_mode": self.degraded_mode(),
            "buying_paused_reason": self._buying_pause_reason(),
            "mode": self.settings.broker_mode,
            "provider": self.broker.name,
            "polygon_enabled": self.polygon is not None,
            "market_closed": self._market_is_closed(),
            "is_trading_day": self._is_trading_day_today(),
            "market_session": {
                "is_open": market_session["is_open"],
                "current_session_date": market_session["current_session_date"],
                "current_session_open": market_session["current_session_open"].isoformat(),
                "current_session_close": market_session["current_session_close"].isoformat(),
                "next_open": market_session["next_open"].isoformat() if isinstance(market_session["next_open"], datetime) else None,
                "next_close": market_session["next_close"].isoformat() if isinstance(market_session["next_close"], datetime) else None,
                "is_early_close": market_session["is_early_close"],
            },
            "market_regime": market_regime,
            "safety_status": safety_status,
            "inverse_etfs_enabled": self.settings.inverse_etfs_enabled,
            "inverse_etfs": self.settings.inverse_etfs,
            "dynamic_controls": {
                "growth_ratio": round(self._latest_growth_ratio, 3),
                "drawdown_pct": round(self._latest_drawdown_pct * 100, 2),
                "drawdown_state": self._latest_drawdown_state,
                "max_total_capital": round(self.settings.max_total_capital, 2),
                "max_open_positions": self.settings.max_open_positions,
                "max_new_positions_per_run": self.settings.max_new_positions_per_run,
                "max_stock_price": round(self.settings.max_stock_price, 2),
                "min_dollar_volume": round(self.settings.min_dollar_volume, 2),
                "scan_limit": self.settings.scan_limit,
                "candidate_limit": self.settings.candidate_limit,
                "risk_per_trade_pct": round(self.settings.risk_per_trade_pct, 4),
                "max_position_pct": round(self.settings.max_position_pct, 4),
            },
        }
