from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from .config import Settings
from .models import AccountSnapshot, PositionSnapshot
from .universe import DEFAULT_UNIVERSE, LIQUID_LARGE_CAP_UNIVERSE


class ProviderError(RuntimeError):
    pass


class BaseBroker:
    name = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def universe(self) -> List[str]:
        return self.settings.scan_universe

    def advance_market(self) -> None:
        return None

    def account(self) -> AccountSnapshot:
        raise NotImplementedError

    def positions(self) -> List[PositionSnapshot]:
        raise NotImplementedError

    def bars(self, symbols: List[str], days: int) -> Dict[str, List[dict]]:
        raise NotImplementedError

    def latest_prices(self, symbols: List[str]) -> Dict[str, float]:
        raise NotImplementedError

    def buy(self, symbol: str, qty: float, stop_price: Optional[float] = None, target_price: Optional[float] = None) -> dict:
        raise NotImplementedError

    def sell(self, symbol: str, qty: Optional[float] = None) -> dict:
        raise NotImplementedError

    def recent_filled_sell_orders(self, symbols: List[str]) -> Dict[str, dict]:
        return {}

    def open_exit_orders_for_symbol(self, symbol: str) -> List[dict]:
        return []

    def submit_protective_exit(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
        target_price: Optional[float] = None,
    ) -> Optional[dict]:
        return None

    def cancel_open_orders_for_symbol(self, symbol: str) -> int:
        return 0


class DemoBroker(BaseBroker):
    name = "demo"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.state_path = Path(settings.demo_state_path)
        self._ensure_state()

    def _ensure_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            payload = {
                "cash": self.settings.starting_cash,
                "tick": 0,
                "positions": {},
                "orders": [],
            }
            self.state_path.write_text(json.dumps(payload, indent=2))

    def universe(self) -> List[str]:
        return self.settings.scan_universe or DEFAULT_UNIVERSE

    def _load(self) -> dict:
        self._ensure_state()
        return json.loads(self.state_path.read_text())

    def _save(self, payload: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(payload, indent=2))

    def advance_market(self) -> None:
        state = self._load()
        state["tick"] += 1
        self._save(state)

    def _base_price(self, symbol: str) -> float:
        return 2.5 + (sum(ord(c) for c in symbol) % 700) / 100.0

    def _bars_for_symbol(self, symbol: str, days: int, tick: int) -> List[dict]:
        rng = random.Random(f"{self.settings.demo_seed}:{symbol}:{tick}")
        price = self._base_price(symbol)
        bars: List[dict] = []
        now = datetime.now(timezone.utc)
        trend = ((sum(ord(c) for c in symbol) % 11) - 5) / 1200.0
        season = math.sin((tick + len(symbol)) / 4.0) / 300.0
        for offset in range(days, 0, -1):
            drift = trend + season + math.sin((days - offset) / 6.0) / 500.0
            shock = rng.gauss(0, 0.025)
            price = max(1.5, min(9.90, price * (1 + drift + shock)))
            high = price * (1 + abs(rng.gauss(0.015, 0.01)))
            low = price * max(0.86, 1 - abs(rng.gauss(0.015, 0.01)))
            open_ = max(low, min(high, price * (1 + rng.gauss(0, 0.01))))
            close = max(low, min(high, price))
            volume = int((250_000 + (sum(ord(c) for c in symbol) % 700_000)) * (0.7 + abs(rng.gauss(1.0, 0.3))))
            bars.append(
                {
                    "t": (now - timedelta(days=offset)).isoformat(),
                    "o": round(open_, 4),
                    "h": round(high, 4),
                    "l": round(low, 4),
                    "c": round(close, 4),
                    "v": volume,
                }
            )
        return bars

    def bars(self, symbols: List[str], days: int) -> Dict[str, List[dict]]:
        state = self._load()
        tick = int(state["tick"])
        return {symbol: self._bars_for_symbol(symbol, days, tick) for symbol in symbols}

    def latest_prices(self, symbols: List[str]) -> Dict[str, float]:
        state = self._load()
        tick = int(state["tick"])
        out: Dict[str, float] = {}
        for symbol in symbols:
            out[symbol] = self._bars_for_symbol(symbol, max(30, self.settings.lookback_days), tick)[-1]["c"]
        return out

    def account(self) -> AccountSnapshot:
        state = self._load()
        prices = self.latest_prices(list(state["positions"].keys())) if state["positions"] else {}
        market_value = sum(float(pos["qty"]) * prices.get(symbol, float(pos["avg_entry_price"])) for symbol, pos in state["positions"].items())
        equity = float(state["cash"]) + market_value
        return AccountSnapshot(
            cash=round(float(state["cash"]), 2),
            equity=round(equity, 2),
            buying_power=round(float(state["cash"]), 2),
            mode="demo",
            last_equity=None,
        )

    def positions(self) -> List[PositionSnapshot]:
        state = self._load()
        if not state["positions"]:
            return []
        prices = self.latest_prices(list(state["positions"].keys()))
        positions: List[PositionSnapshot] = []
        for symbol, pos in state["positions"].items():
            qty = float(pos["qty"])
            avg_entry = float(pos["avg_entry_price"])
            current = float(prices.get(symbol, avg_entry))
            mv = qty * current
            pnl_pct = ((current - avg_entry) / avg_entry) * 100 if avg_entry else 0.0
            positions.append(
                PositionSnapshot(
                    symbol=symbol,
                    qty=qty,
                    avg_entry_price=round(avg_entry, 4),
                    current_price=round(current, 4),
                    market_value=round(mv, 2),
                    unrealized_pl_pct=round(pnl_pct, 2),
                )
            )
        return sorted(positions, key=lambda x: x.market_value, reverse=True)

    def buy(self, symbol: str, qty: float, stop_price: Optional[float] = None, target_price: Optional[float] = None) -> dict:
        if qty <= 0:
            raise ProviderError("Quantity must be positive.")
        state = self._load()
        price = self.latest_prices([symbol])[symbol]
        cost = price * qty
        if cost > float(state["cash"]):
            raise ProviderError("Insufficient demo cash.")
        state["cash"] = round(float(state["cash"]) - cost, 2)
        pos = state["positions"].get(symbol)
        if pos:
            existing_qty = float(pos["qty"])
            existing_cost = existing_qty * float(pos["avg_entry_price"])
            new_qty = existing_qty + qty
            pos["qty"] = new_qty
            pos["avg_entry_price"] = round((existing_cost + cost) / new_qty, 4)
        else:
            state["positions"][symbol] = {"qty": qty, "avg_entry_price": round(price, 4)}
        state["orders"].append({"symbol": symbol, "side": "buy", "qty": qty, "price": price, "timestamp": time.time()})
        self._save(state)
        return {"symbol": symbol, "side": "buy", "qty": qty, "filled_avg_price": price, "status": "filled"}

    def sell(self, symbol: str, qty: Optional[float] = None) -> dict:
        state = self._load()
        pos = state["positions"].get(symbol)
        if not pos:
            raise ProviderError(f"No demo position for {symbol}.")
        current_qty = float(pos["qty"])
        sell_qty = current_qty if qty is None else min(current_qty, float(qty))
        price = self.latest_prices([symbol])[symbol]
        proceeds = round(price * sell_qty, 2)
        state["cash"] = round(float(state["cash"]) + proceeds, 2)
        remaining = current_qty - sell_qty
        if remaining <= 0:
            del state["positions"][symbol]
        else:
            state["positions"][symbol]["qty"] = remaining
        state["orders"].append({"symbol": symbol, "side": "sell", "qty": sell_qty, "price": price, "timestamp": time.time()})
        self._save(state)
        return {"symbol": symbol, "side": "sell", "qty": sell_qty, "filled_avg_price": price, "status": "filled"}


class AlpacaBroker(BaseBroker):
    name = "alpaca"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        settings.validate_for_broker()
        self._universe_cache: list[str] | None = None
        self._universe_cached_at: datetime | None = None
        self.session = requests.Session()
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": settings.alpaca_key_id,
                "APCA-API-SECRET-KEY": settings.alpaca_secret_key,
                "Content-Type": "application/json",
            }
        )

    def universe(self) -> List[str]:
        if self.settings.scan_universe:
            return self.settings.scan_universe
        if self.settings.live_universe_mode != "dynamic":
            return self.settings.liquid_scan_universe or LIQUID_LARGE_CAP_UNIVERSE
        now = datetime.now(timezone.utc)
        if self._universe_cache and self._universe_cached_at and (now - self._universe_cached_at) < timedelta(hours=6):
            return self._universe_cache
        payload = self._request(
            "GET",
            f"{self.settings.trading_base_url}/v2/assets",
            params={"status": "active", "asset_class": "us_equity"},
        )
        tradable = [
            str(item["symbol"]).upper()
            for item in payload if isinstance(payload, list)
            if item.get("tradable") and not str(item.get("symbol", "")).startswith("$")
        ]
        # Rotate the available symbol set by day so a blank SCAN_UNIVERSE does not
        # keep feeding the same small alphabetical slice into each scan.
        rng = random.Random(int(now.strftime("%Y%m%d")))
        rng.shuffle(tradable)
        self._universe_cache = tradable
        self._universe_cached_at = now
        return tradable

    def _request(self, method: str, url: str, **kwargs) -> dict | list:
        last_error: Optional[Exception] = None
        for _ in range(3):
            try:
                resp = self.session.request(method, url, timeout=20, **kwargs)
                if resp.status_code >= 400:
                    raise ProviderError(f"Alpaca error {resp.status_code}: {resp.text[:300]}")
                return resp.json() if resp.text else {}
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(1)
        raise ProviderError(f"Alpaca request failed: {last_error}")

    def account(self) -> AccountSnapshot:
        payload = self._request("GET", f"{self.settings.trading_base_url}/v2/account")
        return AccountSnapshot(
            cash=float(payload.get("cash", 0)),
            equity=float(payload.get("equity", 0)),
            buying_power=float(payload.get("buying_power", 0)),
            mode=self.settings.broker_mode,
            last_equity=float(payload["last_equity"]) if payload.get("last_equity") not in (None, "") else None,
        )

    def positions(self) -> List[PositionSnapshot]:
        payload = self._request("GET", f"{self.settings.trading_base_url}/v2/positions")
        positions = []
        for item in payload:
            positions.append(
                PositionSnapshot(
                    symbol=item["symbol"],
                    qty=float(item.get("qty", 0)),
                    avg_entry_price=float(item.get("avg_entry_price", 0)),
                    current_price=float(item.get("current_price", 0)),
                    market_value=float(item.get("market_value", 0)),
                    unrealized_pl_pct=float(item.get("unrealized_plpc", 0)) * 100.0,
                )
            )
        return positions

    def bars(self, symbols: List[str], days: int) -> Dict[str, List[dict]]:
        end = datetime.now(timezone.utc) - timedelta(minutes=20)
        start = end - timedelta(days=days + 5)
        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": days * max(1, len(symbols)),
            "adjustment": "raw",
            "feed": "iex",
        }
        payload = self._request("GET", f"{self.settings.data_base_url}/v2/stocks/bars", params=params)
        out = payload.get("bars", {})
        normalized: Dict[str, List[dict]] = {}
        for symbol, items in out.items():
            normalized[symbol] = [
                {"t": bar["t"], "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar["v"]}
                for bar in items[-days:]
            ]
        return normalized

    def latest_prices(self, symbols: List[str]) -> Dict[str, float]:
        if not symbols:
            return {}
        params = {"symbols": ",".join(symbols), "feed": "iex"}
        payload = self._request("GET", f"{self.settings.data_base_url}/v2/stocks/snapshots", params=params)
        prices: Dict[str, float] = {}
        for symbol, item in payload.items():
            latest_trade = item.get("latestTrade") or {}
            daily_bar = item.get("dailyBar") or {}
            minute_bar = item.get("minuteBar") or {}
            price = latest_trade.get("p") or minute_bar.get("c") or daily_bar.get("c")
            if price is not None:
                prices[symbol] = float(price)
        return prices

    def buy(self, symbol: str, qty: float, stop_price: Optional[float] = None, target_price: Optional[float] = None) -> dict:
        order = {
            "symbol": symbol,
            "qty": qty,
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        }
        whole_share_qty = float(qty).is_integer()
        if (
            self.settings.use_broker_protective_orders
            and stop_price is not None
            and target_price is not None
            and target_price > stop_price
            and whole_share_qty
        ):
            order["time_in_force"] = "gtc"
            order["order_class"] = "bracket"
            order["take_profit"] = {"limit_price": round(float(target_price), 2)}
            order["stop_loss"] = {"stop_price": round(float(stop_price), 2)}
        return self._request("POST", f"{self.settings.trading_base_url}/v2/orders", json=order)

    def sell(self, symbol: str, qty: Optional[float] = None) -> dict:
        if qty is None:
            return self._request("DELETE", f"{self.settings.trading_base_url}/v2/positions/{symbol}")
        order = {
            "symbol": symbol,
            "qty": qty,
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
        }
        return self._request("POST", f"{self.settings.trading_base_url}/v2/orders", json=order)

    def open_exit_orders_for_symbol(self, symbol: str) -> List[dict]:
        payload = self._request(
            "GET",
            f"{self.settings.trading_base_url}/v2/orders",
            params={"status": "open", "limit": 100, "nested": "true", "direction": "desc"},
        )
        symbol = symbol.upper()
        orders: List[dict] = []

        def visit(order: dict) -> None:
            if str(order.get("symbol", "")).upper() != symbol:
                return
            if order.get("side") == "sell":
                orders.append(order)

        for order in payload if isinstance(payload, list) else []:
            visit(order)
            for leg in order.get("legs") or []:
                visit(leg)
        return orders

    def submit_protective_exit(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
        target_price: Optional[float] = None,
    ) -> Optional[dict]:
        if qty <= 0:
            raise ProviderError("Quantity must be positive.")
        qty_value = float(qty)
        whole_share_qty = qty_value.is_integer()
        rounded_stop = round(float(stop_price), 2)
        if whole_share_qty and target_price is not None and float(target_price) > rounded_stop:
            order = {
                "symbol": symbol,
                "qty": int(qty_value),
                "side": "sell",
                "type": "limit",
                "time_in_force": "gtc",
                "order_class": "oco",
                "take_profit": {"limit_price": round(float(target_price), 2)},
                "stop_loss": {"stop_price": rounded_stop},
            }
        else:
            order = {
                "symbol": symbol,
                "qty": qty,
                "side": "sell",
                "type": "stop",
                "time_in_force": "day",
                "stop_price": rounded_stop,
            }
        return self._request("POST", f"{self.settings.trading_base_url}/v2/orders", json=order)

    def recent_filled_sell_orders(self, symbols: List[str]) -> Dict[str, dict]:
        if not symbols:
            return {}
        payload = self._request(
            "GET",
            f"{self.settings.trading_base_url}/v2/orders",
            params={"status": "closed", "limit": 100, "nested": "true", "direction": "desc"},
        )
        symbol_set = set(symbols)
        matched: Dict[str, dict] = {}

        def visit(order: dict) -> None:
            symbol = order.get("symbol")
            if symbol not in symbol_set or order.get("side") != "sell":
                return
            if order.get("status") != "filled":
                return
            if symbol not in matched:
                matched[symbol] = order

        for order in payload if isinstance(payload, list) else []:
            visit(order)
            for leg in order.get("legs") or []:
                visit(leg)
        return matched

    def cancel_open_orders_for_symbol(self, symbol: str) -> int:
        payload = self._request(
            "GET",
            f"{self.settings.trading_base_url}/v2/orders",
            params={"status": "open", "limit": 100, "direction": "desc"},
        )
        canceled = 0
        for order in payload if isinstance(payload, list) else []:
            if order.get("symbol") != symbol:
                continue
            order_id = order.get("id")
            if not order_id:
                continue
            try:
                self._request("DELETE", f"{self.settings.trading_base_url}/v2/orders/{order_id}")
                canceled += 1
            except ProviderError:
                continue
        return canceled


def build_broker(settings: Settings) -> BaseBroker:
    if settings.is_alpaca:
        return AlpacaBroker(settings)
    return DemoBroker(settings)
