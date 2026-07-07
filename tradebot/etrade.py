from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Tuple

from requests_oauthlib import OAuth1Session


ENV_URLS = {
    "live": {
        "api_base": "https://api.etrade.com",
        "authorize_base": "https://us.etrade.com",
    },
    "sandbox": {
        "api_base": "https://apisb.etrade.com",
        "authorize_base": "https://us.etrade.com",
    },
}


class ETradeError(RuntimeError):
    pass


def _env_name(prefix: str, env_name: str) -> str:
    return f"ETRADE_{env_name.upper()}_{prefix}"


def etrade_credentials(env_name: str) -> Tuple[str, str]:
    key = os.getenv(_env_name("CONSUMER_KEY", env_name), "").strip()
    secret = os.getenv(_env_name("CONSUMER_SECRET", env_name), "").strip()
    if not key or not secret:
        raise ETradeError(
            f"Missing E*TRADE credentials for {env_name}. Set "
            f"{_env_name('CONSUMER_KEY', env_name)} and {_env_name('CONSUMER_SECRET', env_name)}."
        )
    return key, secret


def etrade_token_path(env_name: str) -> Path:
    base = Path(os.getenv("ETRADE_TOKEN_DIR", ".etrade"))
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{env_name}.tokens.json"


def load_etrade_tokens(env_name: str) -> Dict[str, Any]:
    access_token = os.getenv(_env_name("ACCESS_TOKEN", env_name), "").strip()
    access_secret = os.getenv(_env_name("ACCESS_TOKEN_SECRET", env_name), "").strip()
    if access_token and access_secret:
        return {
            "access_token": access_token,
            "access_token_secret": access_secret,
        }
    path = etrade_token_path(env_name)
    if not path.exists():
        raise ETradeError(
            f"No saved {env_name} E*TRADE tokens found at {path}, and "
            f"{_env_name('ACCESS_TOKEN', env_name)} / {_env_name('ACCESS_TOKEN_SECRET', env_name)} are not set."
        )
    return json.loads(path.read_text())


def equity_order_payload(symbol: str, side: str, qty: int) -> Dict[str, Any]:
    return {
        "orderType": "EQ",
        "clientOrderId": f"tradebot-etrade-{uuid.uuid4().hex[:12]}",
        "Order": [
            {
                "allOrNone": False,
                "priceType": "MARKET",
                "orderTerm": "GOOD_FOR_DAY",
                "marketSession": "REGULAR",
                "Instrument": [
                    {
                        "Product": {
                            "securityType": "EQ",
                            "symbol": symbol.upper(),
                        },
                        "orderAction": side.upper(),
                        "quantityType": "QUANTITY",
                        "quantity": qty,
                    }
                ],
            }
        ],
    }


def extract_preview_id(payload: Dict[str, Any]) -> str:
    response = payload.get("PreviewOrderResponse", payload)
    preview_ids = response.get("PreviewIds")
    if isinstance(preview_ids, dict):
        preview_id = preview_ids.get("previewId")
        if preview_id is not None:
            return str(preview_id)
    if isinstance(preview_ids, list):
        for item in preview_ids:
            if isinstance(item, dict) and item.get("previewId") is not None:
                return str(item["previewId"])
    raise ETradeError("Preview response did not include a previewId required for order placement.")


class ETradeClient:
    def __init__(self, env_name: str) -> None:
        if env_name not in ENV_URLS:
            raise ETradeError(f"Unsupported E*TRADE environment: {env_name}")
        self.env_name = env_name
        self.api_base = ENV_URLS[env_name]["api_base"]
        consumer_key, consumer_secret = etrade_credentials(env_name)
        tokens = load_etrade_tokens(env_name)
        access_token = tokens.get("access_token")
        access_secret = tokens.get("access_token_secret")
        if not access_token or not access_secret:
            raise ETradeError(f"Missing access token for {env_name}. Run the auth flow in etrade_smoke.py first.")
        self.session = OAuth1Session(
            consumer_key,
            client_secret=consumer_secret,
            resource_owner_key=access_token,
            resource_owner_secret=access_secret,
            signature_type="AUTH_HEADER",
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        response = self.session.request(method, f"{self.api_base}{path}", timeout=30, **kwargs)
        if response.status_code >= 400:
            raise ETradeError(f"{response.status_code} {response.reason}: {response.text}")
        if not response.text:
            return {}
        return response.json()

    def accounts(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/accounts/list.json")

    def balance(self, account_id_key: str) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/accounts/{account_id_key}/balance.json",
            params={"instType": "BROKERAGE", "realTimeNAV": "true"},
        )

    def positions(self, account_id_key: str) -> Dict[str, Any]:
        return self._request("GET", f"/v1/accounts/{account_id_key}/portfolio.json")

    def symbol_quantity(self, account_id_key: str, symbol: str) -> int:
        payload = self.positions(account_id_key)
        wanted = symbol.upper()
        total = 0.0

        def maybe_symbol(node: Dict[str, Any]) -> str:
            product = node.get("Product")
            if isinstance(product, dict):
                for key in ("symbol", "securityTypeDescription", "symbolDescription"):
                    value = product.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip().upper()
            for key in ("symbol", "symbolDescription"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip().upper()
            return ""

        def walk(node: Any) -> None:
            nonlocal total
            if isinstance(node, dict):
                node_symbol = maybe_symbol(node)
                if node_symbol == wanted:
                    try:
                        total += float(node.get("quantity", 0) or 0)
                    except (TypeError, ValueError):
                        pass
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        return max(0, int(round(total)))

    def preview_equity_order(self, account_id_key: str, symbol: str, side: str, qty: int) -> Dict[str, Any]:
        payload = {"PreviewOrderRequest": equity_order_payload(symbol, side, qty)}
        return self._request("POST", f"/v1/accounts/{account_id_key}/orders/preview.json", json=payload)

    def place_equity_order(
        self,
        account_id_key: str,
        symbol: str,
        side: str,
        qty: int,
        preview_payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        order_payload = equity_order_payload(symbol, side, qty)
        if preview_payload is not None:
            order_payload["PreviewIds"] = [{"previewId": extract_preview_id(preview_payload)}]
        payload = {"PlaceOrderRequest": order_payload}
        return self._request("POST", f"/v1/accounts/{account_id_key}/orders/place.json", json=payload)

    def estimated_position_market_value(self, account_id_key: str) -> float:
        payload = self.positions(account_id_key)
        total = 0.0

        def walk(node: Any) -> None:
            nonlocal total
            if isinstance(node, dict):
                if "marketValue" in node:
                    try:
                        total += float(node["marketValue"])
                    except (TypeError, ValueError):
                        pass
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        return round(total, 2)
