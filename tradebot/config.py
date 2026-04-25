from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _env_ratio(*names: str, default: float) -> float:
    for name in names:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        value = float(raw)
        return value / 100.0 if value > 1 else value
    return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_str(*names: str, default: str = "") -> str:
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        value = raw.strip()
        if value:
            return value
    return default


@dataclass
class Settings:
    app_name: str = "TradeBot"
    broker_mode: str = field(default_factory=lambda: os.getenv("BROKER_MODE", "demo").lower())
    alpaca_key_id: str = field(default_factory=lambda: _env_str("ALPACA_KEY_ID", "ALPACA_API_KEY"))
    alpaca_secret_key: str = field(default_factory=lambda: _env_str("ALPACA_SECRET_KEY"))
    auto_trade_enabled: bool = field(default_factory=lambda: _env_bool("AUTO_TRADE_ENABLED", True))
    auto_trade_interval_minutes: int = field(default_factory=lambda: int(os.getenv("AUTO_TRADE_INTERVAL_MINUTES", "30")))
    pdt_cooldown_hours: int = field(default_factory=lambda: int(os.getenv("PDT_COOLDOWN_HOURS", "20")))
    rebuy_cooldown_hours: int = field(default_factory=lambda: int(os.getenv("REBUY_COOLDOWN_HOURS", "48")))
    congress_max_price: float = field(default_factory=lambda: float(os.getenv("CONGRESS_MAX_PRICE", os.getenv("MAX_STOCK_PRICE", "10"))))
    congress_trade_limit: int = field(default_factory=lambda: int(os.getenv("CONGRESS_TRADE_LIMIT", "20")))
    congress_signal_window_days: int = field(default_factory=lambda: int(os.getenv("CONGRESS_SIGNAL_WINDOW_DAYS", "45")))
    congress_freshness_hours: int = field(default_factory=lambda: int(os.getenv("CONGRESS_FRESHNESS_HOURS", "24")))
    congress_min_records: int = field(default_factory=lambda: int(os.getenv("CONGRESS_MIN_RECORDS", "1")))
    congress_retry_minutes: int = field(default_factory=lambda: int(os.getenv("CONGRESS_RETRY_MINUTES", "15")))
    congress_override_mode: str = field(default_factory=lambda: os.getenv("CONGRESS_OVERRIDE_MODE", "auto").strip().lower())
    decision_support_congress_weight: float = field(default_factory=lambda: float(os.getenv("DECISION_SUPPORT_CONGRESS_WEIGHT", "1.0")))
    sec_user_agent: str = field(default_factory=lambda: os.getenv("SEC_USER_AGENT", "").strip())
    sec_signal_window_days: int = field(default_factory=lambda: int(os.getenv("SEC_SIGNAL_WINDOW_DAYS", "30")))
    sec_filing_limit_per_symbol: int = field(default_factory=lambda: int(os.getenv("SEC_FILING_LIMIT_PER_SYMBOL", "20")))
    sec_freshness_hours: int = field(default_factory=lambda: int(os.getenv("SEC_FRESHNESS_HOURS", "24")))
    sec_min_records: int = field(default_factory=lambda: int(os.getenv("SEC_MIN_RECORDS", "1")))
    sec_retry_minutes: int = field(default_factory=lambda: int(os.getenv("SEC_RETRY_MINUTES", "15")))
    sec_override_mode: str = field(default_factory=lambda: os.getenv("SEC_OVERRIDE_MODE", "auto").strip().lower())
    decision_support_sec_weight: float = field(default_factory=lambda: float(os.getenv("DECISION_SUPPORT_SEC_WEIGHT", "1.0")))
    alpha_vantage_api_key: str = field(default_factory=lambda: os.getenv("ALPHA_VANTAGE_API_KEY", "").strip())
    earnings_signal_window_days: int = field(default_factory=lambda: int(os.getenv("EARNINGS_SIGNAL_WINDOW_DAYS", "21")))
    earnings_freshness_hours: int = field(default_factory=lambda: int(os.getenv("EARNINGS_FRESHNESS_HOURS", "24")))
    earnings_min_records: int = field(default_factory=lambda: int(os.getenv("EARNINGS_MIN_RECORDS", "1")))
    earnings_retry_minutes: int = field(default_factory=lambda: int(os.getenv("EARNINGS_RETRY_MINUTES", "15")))
    earnings_override_mode: str = field(default_factory=lambda: os.getenv("EARNINGS_OVERRIDE_MODE", "auto").strip().lower())
    decision_support_earnings_weight: float = field(default_factory=lambda: float(os.getenv("DECISION_SUPPORT_EARNINGS_WEIGHT", "1.0")))
    macro_signal_window_days: int = field(default_factory=lambda: int(os.getenv("MACRO_SIGNAL_WINDOW_DAYS", "7")))
    macro_freshness_hours: int = field(default_factory=lambda: int(os.getenv("MACRO_FRESHNESS_HOURS", "24")))
    macro_min_records: int = field(default_factory=lambda: int(os.getenv("MACRO_MIN_RECORDS", "1")))
    macro_retry_minutes: int = field(default_factory=lambda: int(os.getenv("MACRO_RETRY_MINUTES", "15")))
    macro_override_mode: str = field(default_factory=lambda: os.getenv("MACRO_OVERRIDE_MODE", "auto").strip().lower())
    decision_support_macro_weight: float = field(default_factory=lambda: float(os.getenv("DECISION_SUPPORT_MACRO_WEIGHT", "1.0")))
    stop_loss_pct: float = field(default_factory=lambda: _env_ratio("STOP_LOSS_PCT", "STOP_LOSS", default=0.12))
    trailing_stop_pct: float = field(default_factory=lambda: _env_ratio("TRAILING_STOP_PCT", default=0.10))
    partial_profit_enabled: bool = field(default_factory=lambda: _env_bool("PARTIAL_PROFIT_ENABLED", True))
    partial_profit_pct: float = field(default_factory=lambda: float(os.getenv("PARTIAL_PROFIT_PCT", "15")) / 100.0)
    partial_sell_fraction: float = field(default_factory=lambda: float(os.getenv("PARTIAL_SELL_FRACTION", "0.5")))
    use_broker_protective_orders: bool = field(default_factory=lambda: _env_bool("USE_BROKER_PROTECTIVE_ORDERS", True))
    min_hold_days: int = field(default_factory=lambda: int(os.getenv("MIN_HOLD_DAYS", "0")))
    max_hold_days: int = field(default_factory=lambda: int(os.getenv("MAX_HOLD_DAYS", "0")))
    max_total_capital: float = field(default_factory=lambda: float(os.getenv("MAX_TOTAL_CAPITAL", "500")))
    max_open_positions: int = field(default_factory=lambda: int(os.getenv("MAX_OPEN_POSITIONS", "5")))
    max_stock_price: float = field(default_factory=lambda: float(os.getenv("MAX_STOCK_PRICE", "10")))
    min_stock_price: float = field(default_factory=lambda: float(os.getenv("MIN_STOCK_PRICE", "2")))
    scan_limit: int = field(default_factory=lambda: int(os.getenv("SCAN_LIMIT", "200")))
    candidate_limit: int = field(default_factory=lambda: int(os.getenv("CANDIDATE_LIMIT", "30")))
    max_new_positions_per_run: int = field(default_factory=lambda: int(os.getenv("MAX_NEW_POSITIONS_PER_RUN", "3")))
    risk_per_trade_pct: float = field(default_factory=lambda: float(os.getenv("RISK_PER_TRADE_PCT", "0.04")))
    max_position_pct: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_PCT", "0.25")))
    min_reward_risk: float = field(default_factory=lambda: float(os.getenv("MIN_REWARD_RISK", "1.2")))
    min_dollar_volume: float = field(default_factory=lambda: float(os.getenv("MIN_DOLLAR_VOLUME", "1000000")))
    drawdown_soft_limit_pct: float = field(default_factory=lambda: _env_ratio("DRAWDOWN_SOFT_LIMIT_PCT", default=0.10))
    drawdown_hard_limit_pct: float = field(default_factory=lambda: _env_ratio("DRAWDOWN_HARD_LIMIT_PCT", default=0.20))
    daily_loss_limit_pct: float = field(default_factory=lambda: _env_ratio("DAILY_LOSS_LIMIT_PCT", default=0.05))
    max_consecutive_buy_errors: int = field(default_factory=lambda: int(os.getenv("MAX_CONSECUTIVE_BUY_ERRORS", "3")))
    pause_new_buys_on_degraded_signals: bool = field(default_factory=lambda: _env_bool("PAUSE_NEW_BUYS_ON_DEGRADED_SIGNALS", True))
    buy_kill_switch: bool = field(default_factory=lambda: _env_bool("BUY_KILL_SWITCH", False))
    lookback_days: int = field(default_factory=lambda: int(os.getenv("LOOKBACK_DAYS", "80")))
    dashboard_host: str = field(default_factory=lambda: os.getenv("DASHBOARD_HOST", "0.0.0.0"))
    dashboard_port: int = field(default_factory=lambda: int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8008"))))
    analyzer_mode: str = field(default_factory=lambda: os.getenv("ANALYZER_MODE", "embedded").lower())
    starting_cash: float = field(default_factory=lambda: float(os.getenv("STARTING_CASH", "100000")))
    polygon_api_key: str = field(default_factory=lambda: os.getenv("POLYGON_API_KEY", "").strip())
    short_volume_signal_enabled: bool = field(default_factory=lambda: _env_bool("SHORT_VOLUME_SIGNAL_ENABLED", True))
    decision_support_short_volume_weight: float = field(default_factory=lambda: float(os.getenv("DECISION_SUPPORT_SHORT_VOLUME_WEIGHT", "1.0")))
    inverse_etfs_enabled: bool = field(default_factory=lambda: _env_bool("INVERSE_ETFS_ENABLED", True))
    inverse_etfs: List[str] = field(init=False)
    demo_seed: int = field(default_factory=lambda: int(os.getenv("DEMO_SEED", "42")))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", Path.cwd() / "data")))
    db_path: Path = field(init=False)
    demo_state_path: Path = field(init=False)
    scan_universe: List[str] = field(init=False)
    congress_report_urls: List[str] = field(init=False)

    def __post_init__(self) -> None:
        raw_universe = os.getenv("SCAN_UNIVERSE", "")
        raw_congress_urls = os.getenv("CONGRESS_REPORT_URLS", "")
        self.scan_universe = [x.strip().upper() for x in raw_universe.split(",") if x.strip()] if raw_universe.strip() else []
        self.congress_report_urls = [x.strip() for x in raw_congress_urls.split(",") if x.strip()]
        raw_inverse = os.getenv("INVERSE_ETFS", "SPXS,SQQQ,SDOW,SH,PSQ,DOG,SPXU,TECS")
        self.inverse_etfs = [x.strip().upper() for x in raw_inverse.split(",") if x.strip()]
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "tradebot.db"
        self.demo_state_path = self.data_dir / "demo_broker.json"

    @property
    def is_small_account(self) -> bool:
        return self.max_total_capital > 0 and self.max_total_capital < 2000

    @property
    def is_demo(self) -> bool:
        return self.broker_mode == "demo"

    @property
    def is_alpaca(self) -> bool:
        return self.broker_mode in {"paper", "live"}

    @property
    def trading_base_url(self) -> str:
        if self.broker_mode == "paper":
            return "https://paper-api.alpaca.markets"
        return "https://api.alpaca.markets"

    @property
    def data_base_url(self) -> str:
        return "https://data.alpaca.markets"

    def validate_for_broker(self) -> None:
        if self.is_alpaca and (not self.alpaca_key_id or not self.alpaca_secret_key):
            raise ValueError("ALPACA_KEY_ID and ALPACA_SECRET_KEY are required for paper or live mode.")


def get_settings() -> Settings:
    return Settings()
