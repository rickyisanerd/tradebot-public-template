from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import Settings
from .db import Database
from .etrade import ETradeClient, ETradeError


@dataclass
class MirrorDecision:
    action: str
    reason: str = ""


class ETradeMirrorExecutor:
    def __init__(self, settings: Settings, db: Database, client: Optional[ETradeClient] = None) -> None:
        self.settings = settings
        self.db = db
        self.client = client

    def enabled(self) -> bool:
        return self.settings.etrade_mirror_ready

    def _client(self) -> ETradeClient:
        if self.client is not None:
            return self.client
        return ETradeClient(self.settings.etrade_mirror_env)

    def _last_trade_id(self) -> int:
        raw = self.db.get_bot_state("etrade_mirror_last_trade_id")
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    def _set_last_trade_id(self, trade_id: int) -> None:
        self.db.set_bot_state("etrade_mirror_last_trade_id", str(trade_id))

    def _set_status(self, key: str, value: str) -> None:
        self.db.set_bot_state(f"etrade_mirror_{key}", value)

    def _auth_error(self, message: str) -> bool:
        lowered = (message or "").lower()
        return any(
            marker in lowered
            for marker in (
                "token_expired",
                "oauth_problem",
                "missing access token",
                "no saved",
                "unauthorized",
                "missing e*trade credentials",
            )
        )

    def _transient_error(self, message: str) -> bool:
        lowered = (message or "").lower()
        return any(
            marker in lowered
            for marker in (
                "service is not currently available",
                "timed out your original order request",
                "please resubmit it now",
                "internal server error",
            )
        )

    def _record_auth_failure(self, message: str, trade_id: int | None = None, symbol: str | None = None) -> None:
        details: Dict[str, object] = {"error": message}
        if trade_id is not None:
            details["trade_id"] = trade_id
        if symbol:
            details["symbol"] = symbol
        self.db.record_audit_event("etrade_mirror", "error", "E*TRADE authentication expired", details)
        self._set_status("last_error", message)
        self._set_status("last_result", "reauth required")

    def _record_retryable_failure(self, message: str, trade_id: int, symbol: str) -> None:
        details: Dict[str, object] = {"error": message, "trade_id": trade_id, "symbol": symbol}
        self.db.record_audit_event("etrade_mirror", "warning", "E*TRADE mirror retry pending", details)
        self._set_status("last_error", message)
        self._set_status("last_result", f"retry pending {symbol}")

    def status(self) -> Dict[str, object]:
        last_error = self.db.get_bot_state("etrade_mirror_last_error") or ""
        auth_expired = self._auth_error(last_error)
        return {
            "enabled": self.enabled(),
            "ready": self.enabled() and not bool(last_error),
            "env": self.settings.etrade_mirror_env,
            "preview_only": self.settings.etrade_mirror_preview_only,
            "account_id_key": self.settings.etrade_account_id_key[-6:] if self.settings.etrade_account_id_key else "",
            "last_trade_id": self._last_trade_id(),
            "last_result": self.db.get_bot_state("etrade_mirror_last_result") or "",
            "last_error": last_error,
            "auth_expired": auth_expired,
            "recovery_hint": (
                "Re-auth E*TRADE locally, sync the fresh live token to Railway, then rerun the mirror."
                if auth_expired
                else ""
            ),
        }

    def _eligible_trade(self, trade: Dict[str, object]) -> MirrorDecision:
        side = str(trade.get("side") or "").lower()
        status = str(trade.get("status") or "").lower()
        note = str(trade.get("note") or "").lower()
        if side not in {"buy", "sell"}:
            return MirrorDecision("skip", "unsupported side")
        if status == "error":
            return MirrorDecision("skip", "source trade errored")
        if "reconciled external position" in note:
            return MirrorDecision("skip", "skip historical reconciled positions")
        if status not in {"filled", "submitted", "accepted", "pending_new"}:
            return MirrorDecision("skip", f"unsupported source status: {status}")
        return MirrorDecision("mirror")

    def _buy_allowed(self, client: ETradeClient, trade: Dict[str, object]) -> MirrorDecision:
        qty = int(float(trade.get("qty") or 0))
        price = float(trade.get("price") or 0.0)
        est_value = qty * price
        if qty <= 0 or price <= 0:
            return MirrorDecision("skip", "invalid quantity or price")
        if self.settings.etrade_mirror_max_order_value > 0 and est_value > self.settings.etrade_mirror_max_order_value:
            return MirrorDecision("skip", f"order value ${est_value:.2f} exceeds mirror cap")
        if self.settings.etrade_mirror_max_total_capital > 0:
            current = client.estimated_position_market_value(self.settings.etrade_account_id_key)
            if current + est_value > self.settings.etrade_mirror_max_total_capital:
                return MirrorDecision("skip", "mirror total capital cap would be exceeded")
        return MirrorDecision("mirror")

    def _tradebot_symbol_qty_after_trade(self, symbol: str, trade_id: int) -> float:
        total = 0.0
        for trade in reversed(self.db.recent_trades(1000)):
            current_id = int(trade["id"])
            if current_id > trade_id:
                continue
            if str(trade.get("symbol") or "").upper() != symbol.upper():
                continue
            status = str(trade.get("status") or "").lower()
            note = str(trade.get("note") or "").lower()
            if status == "error" or "reconciled external position" in note:
                continue
            qty = float(trade.get("qty") or 0)
            side = str(trade.get("side") or "").lower()
            if side == "buy":
                total += qty
            elif side == "sell":
                total -= qty
        return round(max(0.0, total), 8)

    def _resolve_sell_qty(self, client: ETradeClient, trade: Dict[str, object], trade_id: int) -> int:
        symbol = str(trade["symbol"]).upper()
        mirrored_qty = int(float(trade["qty"]))
        remaining_qty = self._tradebot_symbol_qty_after_trade(symbol, trade_id)
        if remaining_qty > 0:
            return mirrored_qty
        etrade_qty = client.symbol_quantity(self.settings.etrade_account_id_key, symbol)
        if etrade_qty > 0:
            return etrade_qty
        return mirrored_qty

    def sync_new_trades(self) -> List[Dict[str, object]]:
        if not self.enabled():
            return []
        try:
            client = self._client()
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if self._auth_error(message):
                self._record_auth_failure(message)
                return []
            self.db.record_audit_event("etrade_mirror", "error", "E*TRADE mirror initialization failed", {"error": message})
            self._set_status("last_error", message)
            self._set_status("last_result", "mirror init failed")
            return []
        if self.db.get_bot_state("etrade_mirror_last_trade_id") is None:
            newest_trade = self.db.recent_trades(1)
            if newest_trade:
                seeded_trade_id = int(newest_trade[0]["id"])
                self._set_last_trade_id(seeded_trade_id)
                self.db.record_audit_event(
                    "etrade_mirror",
                    "info",
                    "Seeded E*TRADE mirror cursor to latest existing trade",
                    {"trade_id": seeded_trade_id},
                )
                self._set_status("last_result", f"seeded cursor at trade {seeded_trade_id}")
            return []
        last_trade_id = self._last_trade_id()
        trades = self.db.trades_since_id(last_trade_id, 100)
        results: List[Dict[str, object]] = []
        for trade in trades:
            trade_id = int(trade["id"])
            decision = self._eligible_trade(trade)
            if decision.action == "skip":
                self.db.record_audit_event("etrade_mirror", "info", "Skipped mirror trade", {"trade_id": trade_id, "symbol": trade["symbol"], "reason": decision.reason})
                self._set_last_trade_id(trade_id)
                self._set_status("last_result", f"skipped {trade['symbol']}: {decision.reason}")
                continue
            if str(trade["side"]).lower() == "buy":
                try:
                    decision = self._buy_allowed(client, trade)
                except ETradeError as exc:
                    message = str(exc)
                    if self._auth_error(message):
                        self._record_auth_failure(message, trade_id=trade_id, symbol=str(trade["symbol"]))
                        results.append({"trade_id": trade_id, "symbol": str(trade["symbol"]), "side": "BUY", "status": "reauth-required", "error": message})
                        break
                    raise
                if decision.action == "skip":
                    self.db.record_audit_event("etrade_mirror", "warning", "Skipped mirror buy", {"trade_id": trade_id, "symbol": trade["symbol"], "reason": decision.reason})
                    self._set_last_trade_id(trade_id)
                    self._set_status("last_result", f"skipped {trade['symbol']}: {decision.reason}")
                    continue

            symbol = str(trade["symbol"])
            side = str(trade["side"]).upper()
            qty = int(float(trade["qty"]))
            if side == "SELL":
                try:
                    qty = self._resolve_sell_qty(client, trade, trade_id)
                except ETradeError as exc:
                    message = str(exc)
                    if self._auth_error(message):
                        self._record_auth_failure(message, trade_id=trade_id, symbol=symbol)
                        results.append({"trade_id": trade_id, "symbol": symbol, "side": side, "status": "reauth-required", "error": message})
                        break
                    raise
            try:
                preview = client.preview_equity_order(self.settings.etrade_account_id_key, symbol, side, qty)
                mode = "preview"
                response = preview
                if not self.settings.etrade_mirror_preview_only:
                    response = client.place_equity_order(
                        self.settings.etrade_account_id_key,
                        symbol,
                        side,
                        qty,
                        preview_payload=preview,
                    )
                    mode = "placed"
            except ETradeError as exc:
                message = str(exc)
                if self._auth_error(message):
                    self._record_auth_failure(message, trade_id=trade_id, symbol=symbol)
                    results.append({"trade_id": trade_id, "symbol": symbol, "side": side, "status": "reauth-required", "error": message})
                    break
                if self._transient_error(message):
                    self._record_retryable_failure(message, trade_id=trade_id, symbol=symbol)
                    results.append({"trade_id": trade_id, "symbol": symbol, "side": side, "status": "retry-pending", "error": message})
                    break
                self.db.record_audit_event("etrade_mirror", "error", "E*TRADE mirror failed", {"trade_id": trade_id, "symbol": symbol, "error": message})
                self._set_status("last_error", message)
                self._set_status("last_result", f"failed {symbol}")
                self._set_last_trade_id(trade_id)
                results.append({"trade_id": trade_id, "symbol": symbol, "side": side, "status": "error", "error": message})
                continue

            self.db.record_audit_event(
                "etrade_mirror",
                "info",
                f"E*TRADE mirror {mode} succeeded",
                {"trade_id": trade_id, "symbol": symbol, "side": side, "qty": qty, "preview_only": self.settings.etrade_mirror_preview_only},
            )
            self._set_status("last_error", "")
            self._set_status("last_result", f"{mode} {symbol} {side} x{qty}")
            self._set_last_trade_id(trade_id)
            results.append({"trade_id": trade_id, "symbol": symbol, "side": side, "qty": qty, "status": mode, "response": response})
        return results
