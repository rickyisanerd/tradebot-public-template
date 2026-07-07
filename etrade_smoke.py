from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Dict, Tuple

from dotenv import load_dotenv
from requests_oauthlib import OAuth1Session

load_dotenv()


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


def _env_name(prefix: str, env_name: str) -> str:
    return f"ETRADE_{env_name.upper()}_{prefix}"


def _credentials(env_name: str) -> Tuple[str, str]:
    key = os.getenv(_env_name("CONSUMER_KEY", env_name), "").strip()
    secret = os.getenv(_env_name("CONSUMER_SECRET", env_name), "").strip()
    if not key or not secret:
        raise SystemExit(
            f"Missing credentials for {env_name}. Set "
            f"{_env_name('CONSUMER_KEY', env_name)} and {_env_name('CONSUMER_SECRET', env_name)} in your local .env."
        )
    return key, secret


def _callback_url() -> str:
    return os.getenv("ETRADE_OAUTH_CALLBACK", "oob").strip() or "oob"


def _token_path(env_name: str) -> Path:
    base = Path(os.getenv("ETRADE_TOKEN_DIR", ".etrade"))
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{env_name}.tokens.json"


def _load_tokens(env_name: str) -> Dict[str, Any]:
    path = _token_path(env_name)
    if not path.exists():
        raise SystemExit(
            f"No saved {env_name} tokens found at {path}. Run auth-start and auth-complete first."
        )
    return json.loads(path.read_text())


def _save_tokens(env_name: str, payload: Dict[str, Any]) -> None:
    path = _token_path(env_name)
    path.write_text(json.dumps(payload, indent=2))


def _saved_access_tokens(env_name: str) -> Tuple[str, str]:
    tokens = _load_tokens(env_name)
    access_token = str(tokens.get("access_token") or "").strip()
    access_secret = str(tokens.get("access_token_secret") or "").strip()
    if not access_token or not access_secret:
        raise SystemExit(f"Missing saved access token for {env_name}. Run auth-complete first.")
    return access_token, access_secret


def _railway_cli() -> str:
    executable = shutil.which("railway") or shutil.which("railway.cmd") or shutil.which("railway.exe")
    if executable:
        return executable
    raise SystemExit(
        "Railway CLI was not found in PATH. Install it or run the token sync manually with "
        "`railway variable set ...`."
    )


def _oauth_session(env_name: str, token: str | None = None, token_secret: str | None = None) -> OAuth1Session:
    consumer_key, consumer_secret = _credentials(env_name)
    return OAuth1Session(
        consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=token,
        resource_owner_secret=token_secret,
        callback_uri=_callback_url(),
        signature_type="AUTH_HEADER",
    )


def _api_url(env_name: str, path: str) -> str:
    base = ENV_URLS[env_name]["api_base"]
    return f"{base}{path}"


def _authorize_url(env_name: str, request_token: str) -> str:
    base = ENV_URLS[env_name]["authorize_base"]
    consumer_key, _ = _credentials(env_name)
    return f"{base}/e/t/etws/authorize?key={consumer_key}&token={request_token}"


def cmd_auth_start(args: argparse.Namespace) -> None:
    env_name = args.env
    session = _oauth_session(env_name)
    request_token_url = _api_url(env_name, "/oauth/request_token")
    tokens = session.fetch_request_token(request_token_url)
    payload = {
        "request_token": tokens["oauth_token"],
        "request_token_secret": tokens["oauth_token_secret"],
    }
    _save_tokens(env_name, payload)
    url = _authorize_url(env_name, tokens["oauth_token"])
    print(f"Saved request token to {_token_path(env_name)}")
    print(f"Authorize this app in your browser:\n{url}")
    if args.open_browser:
        webbrowser.open(url)


def cmd_auth_complete(args: argparse.Namespace) -> None:
    env_name = args.env
    payload = _load_tokens(env_name)
    request_token = payload.get("request_token")
    request_token_secret = payload.get("request_token_secret")
    if not request_token or not request_token_secret:
        raise SystemExit("Missing request token data. Run auth-start first.")
    verifier = args.verifier.strip()
    session = _oauth_session(env_name, request_token, request_token_secret)
    access = session.fetch_access_token(_api_url(env_name, "/oauth/access_token"), verifier=verifier)
    payload.update(
        {
            "access_token": access["oauth_token"],
            "access_token_secret": access["oauth_token_secret"],
        }
    )
    _save_tokens(env_name, payload)
    print(f"Saved access token to {_token_path(env_name)}")
    if args.sync_railway:
        _sync_tokens_to_railway(env_name, service=args.service, environment=args.environment)


def _authorized_session(env_name: str) -> OAuth1Session:
    tokens = _load_tokens(env_name)
    access_token = tokens.get("access_token")
    access_secret = tokens.get("access_token_secret")
    if not access_token or not access_secret:
        raise SystemExit("Missing access token. Run auth-complete first.")
    return _oauth_session(env_name, access_token, access_secret)


def _request_json(env_name: str, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
    session = _authorized_session(env_name)
    response = session.request(method, _api_url(env_name, path), timeout=30, **kwargs)
    if response.status_code >= 400:
        raise SystemExit(f"{response.status_code} {response.reason}\n{response.text}")
    if not response.text:
        return {}
    return response.json()


def cmd_accounts(args: argparse.Namespace) -> None:
    payload = _request_json(args.env, "GET", "/v1/accounts/list.json")
    print(json.dumps(payload, indent=2))


def cmd_balance(args: argparse.Namespace) -> None:
    path = f"/v1/accounts/{args.account_id_key}/balance.json"
    payload = _request_json(args.env, "GET", path, params={"instType": "BROKERAGE", "realTimeNAV": "true"})
    print(json.dumps(payload, indent=2))


def cmd_positions(args: argparse.Namespace) -> None:
    path = f"/v1/accounts/{args.account_id_key}/portfolio.json"
    payload = _request_json(args.env, "GET", path)
    print(json.dumps(payload, indent=2))


def _equity_preview_payload(symbol: str, side: str, qty: int) -> Dict[str, Any]:
    return {
        "PreviewOrderRequest": {
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
    }


def cmd_preview_equity(args: argparse.Namespace) -> None:
    payload = _equity_preview_payload(args.symbol, args.side, args.qty)
    path = f"/v1/accounts/{args.account_id_key}/orders/preview.json"
    response = _request_json(args.env, "POST", path, json=payload)
    print(json.dumps(response, indent=2))


def cmd_renew(args: argparse.Namespace) -> None:
    session = _authorized_session(args.env)
    response = session.get(_api_url(args.env, "/oauth/renew_access_token"), timeout=30)
    if response.status_code >= 400:
        raise SystemExit(f"{response.status_code} {response.reason}\n{response.text}")
    print("Access token renewed.")
    if response.text:
        print(response.text)


def cmd_revoke(args: argparse.Namespace) -> None:
    session = _authorized_session(args.env)
    response = session.get(_api_url(args.env, "/oauth/revoke_access_token"), timeout=30)
    if response.status_code >= 400:
        raise SystemExit(f"{response.status_code} {response.reason}\n{response.text}")
    print("Access token revoked.")


def _sync_tokens_to_railway(env_name: str, service: str | None = None, environment: str | None = None) -> None:
    access_token, access_secret = _saved_access_tokens(env_name)
    token_var = _env_name("ACCESS_TOKEN", env_name)
    secret_var = _env_name("ACCESS_TOKEN_SECRET", env_name)
    command = [
        _railway_cli(),
        "variable",
        "set",
        f"{token_var}={access_token}",
        f"{secret_var}={access_secret}",
    ]
    if service:
        command.extend(["--service", service])
    if environment:
        command.extend(["--environment", environment])
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    stdout = completed.stdout.strip()
    if stdout:
        print(stdout)
    print(f"Synced {env_name} E*TRADE access token vars to Railway.")


def cmd_sync_railway_tokens(args: argparse.Namespace) -> None:
    try:
        _sync_tokens_to_railway(args.env, service=args.service, environment=args.environment)
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        raise SystemExit(f"Railway token sync failed: {message}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal E*TRADE smoke-test helper for auth, account reads, and equity order preview.")
    parser.add_argument("--env", choices=["sandbox", "live"], default=os.getenv("ETRADE_ENV", "sandbox").strip().lower())
    sub = parser.add_subparsers(dest="command", required=True)

    auth_start = sub.add_parser("auth-start", help="Fetch a request token and print the E*TRADE authorization URL.")
    auth_start.add_argument("--open-browser", action="store_true", help="Open the authorization URL in your browser.")
    auth_start.set_defaults(func=cmd_auth_start)

    auth_complete = sub.add_parser("auth-complete", help="Exchange the verifier code for an access token.")
    auth_complete.add_argument("--verifier", required=True, help="OAuth verifier code from E*TRADE.")
    auth_complete.add_argument("--sync-railway", action="store_true", help="After saving the token locally, sync it to the linked Railway service.")
    auth_complete.add_argument("--service", default=os.getenv("ETRADE_RAILWAY_SERVICE", "tradebot").strip() or "tradebot", help="Railway service name for token sync.")
    auth_complete.add_argument("--environment", default=os.getenv("ETRADE_RAILWAY_ENVIRONMENT", "").strip(), help="Optional Railway environment name for token sync.")
    auth_complete.set_defaults(func=cmd_auth_complete)

    accounts = sub.add_parser("accounts", help="List accessible E*TRADE accounts.")
    accounts.set_defaults(func=cmd_accounts)

    balance = sub.add_parser("balance", help="Fetch balance for an accountIdKey.")
    balance.add_argument("--account-id-key", required=True)
    balance.set_defaults(func=cmd_balance)

    positions = sub.add_parser("positions", help="Fetch positions for an accountIdKey.")
    positions.add_argument("--account-id-key", required=True)
    positions.set_defaults(func=cmd_positions)

    preview = sub.add_parser("preview-equity", help="Preview a simple market equity order.")
    preview.add_argument("--account-id-key", required=True)
    preview.add_argument("--symbol", required=True)
    preview.add_argument("--side", choices=["BUY", "SELL"], required=True)
    preview.add_argument("--qty", type=int, required=True)
    preview.set_defaults(func=cmd_preview_equity)

    renew = sub.add_parser("renew", help="Renew the current access token.")
    renew.set_defaults(func=cmd_renew)

    revoke = sub.add_parser("revoke", help="Revoke the current access token.")
    revoke.set_defaults(func=cmd_revoke)

    sync_railway = sub.add_parser("sync-railway-tokens", help="Push the saved E*TRADE access token for this env into Railway env vars.")
    sync_railway.add_argument("--service", default=os.getenv("ETRADE_RAILWAY_SERVICE", "tradebot").strip() or "tradebot", help="Railway service name.")
    sync_railway.add_argument("--environment", default=os.getenv("ETRADE_RAILWAY_ENVIRONMENT", "").strip(), help="Optional Railway environment name.")
    sync_railway.set_defaults(func=cmd_sync_railway_tokens)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
