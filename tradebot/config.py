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
    rebuy_after_sell_cooldown_hours: int = field(default_factory=lambda: int(os.getenv("REBUY_AFTER_SELL_COOLDOWN_HOURS", "4")))
    congress_max_price: float = field(default_factory=lambda: float(os.getenv("CONGRESS_MAX_PRICE", os.getenv("MAX_STOCK_PRICE", "10"))))
    congress_auto_fetch: bool = field(default_factory=lambda: _env_bool("CONGRESS_AUTO_FETCH", True))
    congress_include_senate: bool = field(default_factory=lambda: _env_bool("CONGRESS_INCLUDE_SENATE", True))
    congress_lookback_days: int = field(default_factory=lambda: int(os.getenv("CONGRESS_LOOKBACK_DAYS", "30")))
    congress_max_reports: int = field(default_factory=lambda: int(os.getenv("CONGRESS_MAX_REPORTS", "50")))
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
    analyst_consensus_enabled: bool = field(default_factory=lambda: _env_bool("ANALYST_CONSENSUS_ENABLED", True))
    analyst_consensus_block_hold: bool = field(default_factory=lambda: _env_bool("ANALYST_CONSENSUS_BLOCK_HOLD", True))
    # Shadow study (2026-07-01): Strong Buy picks +1.67%/trade at 82% win rate;
    # plain Buy -1.06% and no-consensus -1.12%. Only Strong Buy has earned buys.
    analyst_consensus_require_strong_buy: bool = field(default_factory=lambda: _env_bool("ANALYST_CONSENSUS_REQUIRE_STRONG_BUY", True))
    analyst_consensus_min_upside_pct: float = field(default_factory=lambda: float(os.getenv("ANALYST_CONSENSUS_MIN_UPSIDE_PCT", "0")))
    analyst_consensus_cache_hours: int = field(default_factory=lambda: int(os.getenv("ANALYST_CONSENSUS_CACHE_HOURS", "24")))
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
    protective_stop_replace_min_step_pct: float = field(default_factory=lambda: _env_ratio("PROTECTIVE_STOP_REPLACE_MIN_STEP_PCT", default=0.01))
    min_hold_days: int = field(default_factory=lambda: int(os.getenv("MIN_HOLD_DAYS", "0")))
    max_hold_days: int = field(default_factory=lambda: int(os.getenv("MAX_HOLD_DAYS", "0")))
    max_total_capital: float = field(default_factory=lambda: float(os.getenv("MAX_TOTAL_CAPITAL", "500")))
    # Fraction of equity always held back in cash: shallower drawdowns, the
    # daily-loss breaker has less to dump, and there's dry powder for new picks.
    cash_buffer_pct: float = field(default_factory=lambda: float(os.getenv("CASH_BUFFER_PCT", "0.15")))
    max_open_positions: int = field(default_factory=lambda: int(os.getenv("MAX_OPEN_POSITIONS", "5")))
    max_stock_price: float = field(default_factory=lambda: float(os.getenv("MAX_STOCK_PRICE", "10")))
    min_stock_price: float = field(default_factory=lambda: float(os.getenv("MIN_STOCK_PRICE", "2")))
    scan_limit: int = field(default_factory=lambda: int(os.getenv("SCAN_LIMIT", "200")))
    candidate_limit: int = field(default_factory=lambda: int(os.getenv("CANDIDATE_LIMIT", "30")))
    live_universe_mode: str = field(default_factory=lambda: os.getenv("LIVE_UNIVERSE_MODE", "liquid").strip().lower())
    market_regime_filter: bool = field(default_factory=lambda: _env_bool("MARKET_REGIME_FILTER", False))
    market_regime_short_window: int = field(default_factory=lambda: int(os.getenv("MARKET_REGIME_SHORT_WINDOW", "20")))
    market_regime_long_window: int = field(default_factory=lambda: int(os.getenv("MARKET_REGIME_LONG_WINDOW", "50")))
    market_regime_block_on_missing: bool = field(default_factory=lambda: _env_bool("MARKET_REGIME_BLOCK_ON_MISSING", True))
    market_regime_allow_limited_longs: bool = field(default_factory=lambda: _env_bool("MARKET_REGIME_ALLOW_LIMITED_LONGS", True))
    market_regime_limited_long_min_score: float = field(default_factory=lambda: float(os.getenv("MARKET_REGIME_LIMITED_LONG_MIN_SCORE", "75")))
    market_regime_limited_long_max_position_pct: float = field(default_factory=lambda: _env_ratio("MARKET_REGIME_LIMITED_LONG_MAX_POSITION_PCT", default=0.18))
    market_regime_limited_long_risk_pct: float = field(default_factory=lambda: _env_ratio("MARKET_REGIME_LIMITED_LONG_RISK_PCT", default=0.025))
    max_new_positions_per_run: int = field(default_factory=lambda: int(os.getenv("MAX_NEW_POSITIONS_PER_RUN", "3")))
    max_inverse_positions: int = field(default_factory=lambda: int(os.getenv("MAX_INVERSE_POSITIONS", "2")))
    max_inverse_exposure_pct: float = field(default_factory=lambda: _env_ratio("MAX_INVERSE_EXPOSURE_PCT", default=0.30))
    inverse_confirmation_hours: float = field(default_factory=lambda: float(os.getenv("INVERSE_CONFIRMATION_HOURS", "4")))
    earnings_blackout_days: int = field(default_factory=lambda: int(os.getenv("EARNINGS_BLACKOUT_DAYS", "2")))
    risk_per_trade_pct: float = field(default_factory=lambda: float(os.getenv("RISK_PER_TRADE_PCT", "0.04")))
    max_position_pct: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_PCT", "0.25")))
    min_reward_risk: float = field(default_factory=lambda: float(os.getenv("MIN_REWARD_RISK", "1.2")))
    min_dollar_volume: float = field(default_factory=lambda: float(os.getenv("MIN_DOLLAR_VOLUME", "1000000")))
    drawdown_soft_limit_pct: float = field(default_factory=lambda: _env_ratio("DRAWDOWN_SOFT_LIMIT_PCT", default=0.10))
    drawdown_hard_limit_pct: float = field(default_factory=lambda: _env_ratio("DRAWDOWN_HARD_LIMIT_PCT", default=0.20))
    daily_loss_limit_pct: float = field(default_factory=lambda: _env_ratio("DAILY_LOSS_LIMIT_PCT", default=0.05))
    daily_loss_limit_dollars: float = field(default_factory=lambda: float(os.getenv("DAILY_LOSS_LIMIT_DOLLARS", "0")))
    liquidate_on_daily_loss: bool = field(default_factory=lambda: _env_bool("LIQUIDATE_ON_DAILY_LOSS", False))
    profit_lock_dollars: float = field(default_factory=lambda: float(os.getenv("PROFIT_LOCK_DOLLARS", "0")))
    max_consecutive_buy_errors: int = field(default_factory=lambda: int(os.getenv("MAX_CONSECUTIVE_BUY_ERRORS", "3")))
    pause_new_buys_on_degraded_signals: bool = field(default_factory=lambda: _env_bool("PAUSE_NEW_BUYS_ON_DEGRADED_SIGNALS", True))
    buy_kill_switch: bool = field(default_factory=lambda: _env_bool("BUY_KILL_SWITCH", False))
    lookback_days: int = field(default_factory=lambda: int(os.getenv("LOOKBACK_DAYS", "80")))
    dashboard_host: str = field(default_factory=lambda: os.getenv("DASHBOARD_HOST", "0.0.0.0"))
    dashboard_port: int = field(default_factory=lambda: int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8008"))))
    analyzer_mode: str = field(default_factory=lambda: os.getenv("ANALYZER_MODE", "embedded").lower())
    starting_cash: float = field(default_factory=lambda: float(os.getenv("STARTING_CASH", "100000")))
    polygon_api_key: str = field(default_factory=lambda: os.getenv("POLYGON_API_KEY", "").strip())
    etrade_mirror_enabled: bool = field(default_factory=lambda: _env_bool("ETRADE_MIRROR_ENABLED", False))
    etrade_mirror_env: str = field(default_factory=lambda: os.getenv("ETRADE_MIRROR_ENV", "sandbox").strip().lower())
    etrade_account_id_key: str = field(default_factory=lambda: os.getenv("ETRADE_ACCOUNT_ID_KEY", "").strip())
    etrade_mirror_preview_only: bool = field(default_factory=lambda: _env_bool("ETRADE_MIRROR_PREVIEW_ONLY", True))
    etrade_mirror_max_order_value: float = field(default_factory=lambda: float(os.getenv("ETRADE_MIRROR_MAX_ORDER_VALUE", "250")))
    etrade_mirror_max_total_capital: float = field(default_factory=lambda: float(os.getenv("ETRADE_MIRROR_MAX_TOTAL_CAPITAL", "500")))
    etrade_mirror_retry_interval_minutes: int = field(default_factory=lambda: int(os.getenv("ETRADE_MIRROR_RETRY_INTERVAL_MINUTES", "5")))
    short_volume_signal_enabled: bool = field(default_factory=lambda: _env_bool("SHORT_VOLUME_SIGNAL_ENABLED", True))
    decision_support_short_volume_weight: float = field(default_factory=lambda: float(os.getenv("DECISION_SUPPORT_SHORT_VOLUME_WEIGHT", "1.0")))
    shadow_mode_strategies: bool = field(default_factory=lambda: _env_bool("SHADOW_MODE_STRATEGIES", False))
    shadow_min_score: float = field(default_factory=lambda: float(os.getenv("SHADOW_MIN_SCORE", "50")))
    shadow_max_picks_per_cycle: int = field(default_factory=lambda: int(os.getenv("SHADOW_MAX_PICKS_PER_CYCLE", "5")))
    shadow_review_days: int = field(default_factory=lambda: int(os.getenv("SHADOW_REVIEW_DAYS", "5")))
    # Put-shadow paper evaluator (tradebot/put_shadow.py). Default OFF: when off
    # the engine hook is fully inert and never touches the paper ledger.
    put_shadow_enabled: bool = field(default_factory=lambda: _env_bool("PUT_SHADOW_ENABLED", False))
    weekly_report_days: int = field(default_factory=lambda: int(os.getenv("WEEKLY_REPORT_DAYS", "7")))
    inverse_etfs_enabled: bool = field(default_factory=lambda: _env_bool("INVERSE_ETFS_ENABLED", True))
    exclude_broad_market_etfs: bool = field(default_factory=lambda: _env_bool("EXCLUDE_BROAD_MARKET_ETFS", True))
    inverse_etfs: List[str] = field(init=False)
    broad_market_etfs: List[str] = field(init=False)
    market_regime_symbols: List[str] = field(init=False)
    liquid_scan_universe: List[str] = field(init=False)
    demo_seed: int = field(default_factory=lambda: int(os.getenv("DEMO_SEED", "42")))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", Path.cwd() / "data")))
    db_path: Path = field(init=False)
    demo_state_path: Path = field(init=False)
    scan_universe: List[str] = field(init=False)
    congress_report_urls: List[str] = field(init=False)
    analyst_consensus_skip_symbols: List[str] = field(init=False)

    def __post_init__(self) -> None:
        raw_universe = os.getenv("SCAN_UNIVERSE", "")
        raw_congress_urls = os.getenv("CONGRESS_REPORT_URLS", "")
        raw_regime_symbols = os.getenv("MARKET_REGIME_SYMBOLS", "SPY,QQQ")
        raw_liquid_universe = os.getenv("LIQUID_SCAN_UNIVERSE", "")
        raw_analyst_skip_symbols = os.getenv(
            "ANALYST_CONSENSUS_SKIP_SYMBOLS",
            "SPY,QQQ,IWM,DIA,VTI,VOO,XLK,XLF,XLE,XLV,XLY,XLI,ARKK,SOXS,LABU,SPXS,SQQQ,SDOW,SH,PSQ,DOG,SPXU,TECS",
        )
        if raw_universe.strip():
            self.scan_universe = [x.strip().upper() for x in raw_universe.split(",") if x.strip()]
        else:
            self.scan_universe = []
        self.market_regime_symbols = [x.strip().upper() for x in raw_regime_symbols.split(",") if x.strip()]
        self.liquid_scan_universe = [x.strip().upper() for x in raw_liquid_universe.split(",") if x.strip()]
        self.analyst_consensus_skip_symbols = [x.strip().upper() for x in raw_analyst_skip_symbols.split(",") if x.strip()]
        self.congress_report_urls = [x.strip() for x in raw_congress_urls.split(",") if x.strip()]
        # Inverse ETFs: bet against the market when it trends down.
        # Default set covers major indices at various price points.
        raw_inverse = os.getenv("INVERSE_ETFS", "SPXS,SQQQ,SDOW,SH,PSQ,DOG,SPXU,TECS")
        self.inverse_etfs = [x.strip().upper() for x in raw_inverse.split(",") if x.strip()]
        # Broad-market / index / sector ETFs the bot should NOT treat as stock
        # picks: it has no single-name edge there, it just buys beta at full
        # price (often the local top). Excluded from buy candidates by default.
        raw_broad = os.getenv(
            "BROAD_MARKET_ETFS",
            "SPY,VOO,VTI,IVV,QQQ,QQQM,DIA,IWM,IWB,IWV,VEA,VWO,VUG,VTV,SCHB,SCHX,"
            "XLF,XLK,XLE,XLV,XLY,XLI,XLU,XLB,XLP,XLRE,XLC,SMH,SOXX",
        )
        self.broad_market_etfs = [x.strip().upper() for x in raw_broad.split(",") if x.strip()]
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
            raise ValueError("ALPACA_KEY_ID and ALPACA_SECRET_KEY are required for paper/live mode.")

    @property
    def etrade_mirror_ready(self) -> bool:
        return self.etrade_mirror_enabled and bool(self.etrade_account_id_key)


def get_settings() -> Settings:
    return Settings()
