from __future__ import annotations

from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    binance_api_key: str = Field(default="", alias="BINANCE_API_KEY")
    binance_api_secret: str = Field(default="", alias="BINANCE_API_SECRET")
    binance_base_url: str = Field(default="https://demo-fapi.binance.com", alias="BINANCE_BASE_URL")

    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")
    enable_trading: bool = Field(default=False, alias="ENABLE_TRADING")

    allowed_symbols: str = Field(default="BTCUSDT,ETHUSDT,SOLUSDT", alias="ALLOWED_SYMBOLS")
    position_mode: str = Field(default="ONE_WAY", alias="POSITION_MODE")

    sqlite_path: str = Field(default="signals.db", alias="SQLITE_PATH")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    recv_window: int = Field(default=30000, alias="RECV_WINDOW")
    request_timeout: int = Field(default=10, alias="REQUEST_TIMEOUT")

    # ===== V4 entry execution =====
    # Global switches for entry execution. Signal JSON can choose entry_type=market/limit,
    # but these switches can disable either mode at runtime.
    allow_market_entry: bool = Field(default=True, alias="ALLOW_MARKET_ENTRY")
    allow_limit_entry: bool = Field(default=True, alias="ALLOW_LIMIT_ENTRY")
    default_entry_type: str = Field(default="market", alias="DEFAULT_ENTRY_TYPE")
    default_limit_timeout_sec: int = Field(default=60, alias="DEFAULT_LIMIT_TIMEOUT_SEC")
    default_limit_fallback_to_market: bool = Field(default=False, alias="DEFAULT_LIMIT_FALLBACK_TO_MARKET")
    default_max_slippage_pct: float = Field(default=0.30, alias="DEFAULT_MAX_SLIPPAGE_PCT")
    limit_poll_interval_sec: float = Field(default=2.0, alias="LIMIT_POLL_INTERVAL_SEC")

    # ===== Risk-based sizing =====
    # Asset used to calculate futures account balance. Single-asset mode usually uses USDT.
    risk_balance_asset: str = Field(default="USDT", alias="RISK_BALANCE_ASSET")
    # When risk_mode=fixed_pct, choose whether to use `availableBalance` or `balance` from /fapi/v3/balance.
    risk_balance_field: str = Field(default="availableBalance", alias="RISK_BALANCE_FIELD")
    # If signal.risk_mode is fixed_pct/fixed_usdt and signal.margin_usdt is omitted,
    # margin budget = account_balance * AUTO_MARGIN_BALANCE_PCT.
    auto_margin_balance_pct: float = Field(default=0.20, alias="AUTO_MARGIN_BALANCE_PCT")
    # Bot-level leverage cap. Actual leverage is also limited by Binance leverage brackets.
    max_auto_leverage: int = Field(default=20, alias="MAX_AUTO_LEVERAGE")
    # Existing-position handling before a new signal opens a position.
    # replace: cancel old orders, close any current position, then open the new one.
    # reverse_only: only close when current position direction is opposite; ignore same-side signals.
    # ignore_same_side: ignore same-side signals, but reverse opposite-side positions.
    # add: keep the old position and submit the new order. Use carefully.
    default_position_policy: str = Field(default="replace", alias="DEFAULT_POSITION_POLICY")
    # Fallback taker fee if /fapi/v1/commissionRate is unavailable on testnet/demo.
    default_taker_fee_rate: float = Field(default=0.0005, alias="DEFAULT_TAKER_FEE_RATE")
    # Extra safety padding for estimated fees, e.g. 1.1 = add 10% buffer.
    fee_safety_multiplier: float = Field(default=1.10, alias="FEE_SAFETY_MULTIPLIER")

    # When true, close the open position if stop-loss / take-profit placement fails after entry.
    emergency_close_on_protection_fail: bool = Field(default=False, alias="EMERGENCY_CLOSE_ON_PROTECTION_FAIL")

    # ===== V5 account risk =====
    account_risk_enabled: bool = Field(default=False, alias="ACCOUNT_RISK_ENABLED")
    daily_max_loss_usdt: float = Field(default=0, alias="DAILY_MAX_LOSS_USDT")
    daily_max_trades: int = Field(default=0, alias="DAILY_MAX_TRADES")
    max_open_positions: int = Field(default=0, alias="MAX_OPEN_POSITIONS")
    symbol_cooldown_sec: int = Field(default=0, alias="SYMBOL_COOLDOWN_SEC")
    max_total_risk_usdt: float = Field(default=0, alias="MAX_TOTAL_RISK_USDT")
    account_risk_no_sl_penalty_pct: float = Field(default=0.02, alias="ACCOUNT_RISK_NO_SL_PENALTY_PCT")
    account_risk_day_timezone: str = Field(default="UTC", alias="ACCOUNT_RISK_DAY_TIMEZONE")

    # ===== V5 Dashboard (read-only) =====
    dashboard_enabled: bool = Field(default=False, alias="DASHBOARD_ENABLED")
    dashboard_require_token: bool = Field(default=True, alias="DASHBOARD_REQUIRE_TOKEN")
    dashboard_token: str = Field(default="", alias="DASHBOARD_TOKEN")
    dashboard_auto_refresh_sec: int = Field(default=10, alias="DASHBOARD_AUTO_REFRESH_SEC")
    protect_journal_api: bool = Field(default=False, alias="PROTECT_JOURNAL_API")
    protect_stats_api: bool = Field(default=False, alias="PROTECT_STATS_API")

    # ===== V5 Runtime control =====
    runtime_control_enabled: bool = Field(default=False, alias="RUNTIME_CONTROL_ENABLED")
    runtime_control_require_token: bool = Field(default=True, alias="RUNTIME_CONTROL_REQUIRE_TOKEN")
    runtime_control_token: str = Field(default="", alias="RUNTIME_CONTROL_TOKEN")
    runtime_status_allow_dashboard_token: bool = Field(
        default=True, alias="RUNTIME_STATUS_ALLOW_DASHBOARD_TOKEN"
    )

    # ===== V5.9 TradingView Signal Sandbox =====
    tv_signal_sandbox_enabled: bool = Field(default=True, alias="TV_SIGNAL_SANDBOX_ENABLED")
    tv_signal_require_source: bool = Field(default=True, alias="TV_SIGNAL_REQUIRE_SOURCE")
    tv_signal_allowed_sources: str = Field(
        default="tradingview,tv_sandbox", alias="TV_SIGNAL_ALLOWED_SOURCES"
    )
    tv_signal_id_prefix: str = Field(default="TV-", alias="TV_SIGNAL_ID_PREFIX")
    tv_signal_max_risk_usdt: float = Field(default=5, alias="TV_SIGNAL_MAX_RISK_USDT")
    tv_signal_max_margin_usdt: float = Field(default=100, alias="TV_SIGNAL_MAX_MARGIN_USDT")
    tv_signal_allowed_entry_types: str = Field(
        default="market,limit", alias="TV_SIGNAL_ALLOWED_ENTRY_TYPES"
    )
    tv_signal_reject_live_binance: bool = Field(default=True, alias="TV_SIGNAL_REJECT_LIVE_BINANCE")

    # ===== V6.0 TradingView Alert Observation =====
    tv_alert_observation_enabled: bool = Field(default=True, alias="TV_ALERT_OBSERVATION_ENABLED")
    tv_alert_public_base_url: str = Field(default="", alias="TV_ALERT_PUBLIC_BASE_URL")
    tv_alert_stale_minutes: int = Field(default=120, alias="TV_ALERT_STALE_MINUTES")
    tv_alert_observation_window_hours: int = Field(default=24, alias="TV_ALERT_OBSERVATION_WINDOW_HOURS")
    tv_alert_consecutive_failure_warn: int = Field(default=2, alias="TV_ALERT_CONSECUTIVE_FAILURE_WARN")
    tv_alert_consecutive_failure_error: int = Field(default=3, alias="TV_ALERT_CONSECUTIVE_FAILURE_ERROR")
    tv_alert_expected_symbols: str = Field(
        default="BTCUSDT,ETHUSDT,SOLUSDT", alias="TV_ALERT_EXPECTED_SYMBOLS"
    )

    # ===== V6.2 TradingView Cloud Alert Audit =====
    tv_cloud_audit_enabled: bool = Field(default=True, alias="TV_CLOUD_AUDIT_ENABLED")
    tv_cloud_audit_window_hours: int = Field(default=24, alias="TV_CLOUD_AUDIT_WINDOW_HOURS")
    tv_cloud_duplicate_signal_warn: int = Field(default=1, alias="TV_CLOUD_DUPLICATE_SIGNAL_WARN")
    tv_cloud_unauthorized_warn: int = Field(default=3, alias="TV_CLOUD_UNAUTHORIZED_WARN")
    tv_cloud_payload_invalid_warn: int = Field(default=3, alias="TV_CLOUD_PAYLOAD_INVALID_WARN")
    tv_cloud_runtime_lock_warn: int = Field(default=3, alias="TV_CLOUD_RUNTIME_LOCK_WARN")

    @property
    def tv_alert_expected_symbol_set(self) -> set[str]:
        return {s.strip().upper() for s in self.tv_alert_expected_symbols.split(",") if s.strip()}

    @property
    def tv_signal_allowed_source_set(self) -> set[str]:
        return {s.strip().lower() for s in self.tv_signal_allowed_sources.split(",") if s.strip()}

    @property
    def tv_signal_allowed_entry_type_set(self) -> set[str]:
        return {s.strip().lower() for s in self.tv_signal_allowed_entry_types.split(",") if s.strip()}

    @property
    def allowed_symbol_set(self) -> set[str]:
        return {s.strip().upper() for s in self.allowed_symbols.split(",") if s.strip()}

    def validate_runtime(self) -> None:
        missing = []
        if not self.webhook_secret:
            missing.append("WEBHOOK_SECRET")
        if self.enable_trading:
            if not self.binance_api_key:
                missing.append("BINANCE_API_KEY")
            if not self.binance_api_secret:
                missing.append("BINANCE_API_SECRET")
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        if self.default_entry_type not in {"market", "limit"}:
            raise RuntimeError("DEFAULT_ENTRY_TYPE must be market or limit")
        if self.default_limit_timeout_sec < 0 or self.default_limit_timeout_sec > 3600:
            raise RuntimeError("DEFAULT_LIMIT_TIMEOUT_SEC must be between 0 and 3600")
        if self.default_max_slippage_pct < 0 or self.default_max_slippage_pct > 10:
            raise RuntimeError("DEFAULT_MAX_SLIPPAGE_PCT must be between 0 and 10")
        if self.limit_poll_interval_sec <= 0 or self.limit_poll_interval_sec > 30:
            raise RuntimeError("LIMIT_POLL_INTERVAL_SEC must be > 0 and <= 30")
        if self.position_mode.upper() != "ONE_WAY":
            raise RuntimeError("This starter project supports ONE_WAY mode only. Set POSITION_MODE=ONE_WAY.")
        if self.risk_balance_field not in {"availableBalance", "balance"}:
            raise RuntimeError("RISK_BALANCE_FIELD must be availableBalance or balance")
        if not (0 < self.auto_margin_balance_pct <= 1):
            raise RuntimeError("AUTO_MARGIN_BALANCE_PCT must be between 0 and 1")
        if not (1 <= self.max_auto_leverage <= 125):
            raise RuntimeError("MAX_AUTO_LEVERAGE must be between 1 and 125")
        if self.default_position_policy not in {"replace", "reverse_only", "ignore_same_side", "add"}:
            raise RuntimeError("DEFAULT_POSITION_POLICY must be replace, reverse_only, ignore_same_side, or add")
        if not (0 <= self.default_taker_fee_rate <= 0.01):
            raise RuntimeError("DEFAULT_TAKER_FEE_RATE must be between 0 and 0.01")
        if self.fee_safety_multiplier < 1:
            raise RuntimeError("FEE_SAFETY_MULTIPLIER must be >= 1")
        if self.daily_max_loss_usdt < 0:
            raise RuntimeError("DAILY_MAX_LOSS_USDT must be >= 0")
        if self.daily_max_trades < 0:
            raise RuntimeError("DAILY_MAX_TRADES must be >= 0")
        if self.max_open_positions < 0:
            raise RuntimeError("MAX_OPEN_POSITIONS must be >= 0")
        if self.symbol_cooldown_sec < 0:
            raise RuntimeError("SYMBOL_COOLDOWN_SEC must be >= 0")
        if self.max_total_risk_usdt < 0:
            raise RuntimeError("MAX_TOTAL_RISK_USDT must be >= 0")
        if self.account_risk_no_sl_penalty_pct < 0:
            raise RuntimeError("ACCOUNT_RISK_NO_SL_PENALTY_PCT must be >= 0")
        if self.dashboard_auto_refresh_sec < 0 or self.dashboard_auto_refresh_sec > 3600:
            raise RuntimeError("DASHBOARD_AUTO_REFRESH_SEC must be between 0 and 3600")
        if (
            self.dashboard_enabled
            and self.dashboard_require_token
            and not self.dashboard_token.strip()
        ):
            raise RuntimeError(
                "DASHBOARD_TOKEN is required when DASHBOARD_ENABLED=true and DASHBOARD_REQUIRE_TOKEN=true"
            )
        if (self.protect_journal_api or self.protect_stats_api) and not self.dashboard_token.strip():
            raise RuntimeError(
                "DASHBOARD_TOKEN is required when PROTECT_JOURNAL_API or PROTECT_STATS_API is true"
            )
        if (
            self.runtime_control_enabled
            and self.runtime_control_require_token
            and not self.runtime_control_token.strip()
        ):
            raise RuntimeError(
                "RUNTIME_CONTROL_TOKEN is required when RUNTIME_CONTROL_ENABLED=true "
                "and RUNTIME_CONTROL_REQUIRE_TOKEN=true"
            )
        if self.tv_signal_max_risk_usdt < 0:
            raise RuntimeError("TV_SIGNAL_MAX_RISK_USDT must be >= 0")
        if self.tv_signal_max_margin_usdt < 0:
            raise RuntimeError("TV_SIGNAL_MAX_MARGIN_USDT must be >= 0")
        if not self.tv_signal_allowed_source_set:
            raise RuntimeError("TV_SIGNAL_ALLOWED_SOURCES must not be empty")
        if not self.tv_signal_allowed_entry_type_set:
            raise RuntimeError("TV_SIGNAL_ALLOWED_ENTRY_TYPES must not be empty")
        if self.tv_signal_sandbox_enabled and not self.tv_signal_id_prefix.strip():
            raise RuntimeError("TV_SIGNAL_ID_PREFIX must not be empty when TV_SIGNAL_SANDBOX_ENABLED=true")
        if self.tv_alert_stale_minutes < 1:
            raise RuntimeError("TV_ALERT_STALE_MINUTES must be >= 1")
        if self.tv_alert_observation_window_hours < 1 or self.tv_alert_observation_window_hours > 168:
            raise RuntimeError("TV_ALERT_OBSERVATION_WINDOW_HOURS must be between 1 and 168")
        if self.tv_alert_consecutive_failure_warn < 1:
            raise RuntimeError("TV_ALERT_CONSECUTIVE_FAILURE_WARN must be >= 1")
        if self.tv_alert_consecutive_failure_error < self.tv_alert_consecutive_failure_warn:
            raise RuntimeError(
                "TV_ALERT_CONSECUTIVE_FAILURE_ERROR must be >= TV_ALERT_CONSECUTIVE_FAILURE_WARN"
            )
        if self.tv_cloud_audit_window_hours < 1 or self.tv_cloud_audit_window_hours > 168:
            raise RuntimeError("TV_CLOUD_AUDIT_WINDOW_HOURS must be between 1 and 168")
        if self.tv_cloud_duplicate_signal_warn < 1:
            raise RuntimeError("TV_CLOUD_DUPLICATE_SIGNAL_WARN must be >= 1")
        if self.tv_cloud_unauthorized_warn < 1:
            raise RuntimeError("TV_CLOUD_UNAUTHORIZED_WARN must be >= 1")
        if self.tv_cloud_payload_invalid_warn < 1:
            raise RuntimeError("TV_CLOUD_PAYLOAD_INVALID_WARN must be >= 1")
        if self.tv_cloud_runtime_lock_warn < 1:
            raise RuntimeError("TV_CLOUD_RUNTIME_LOCK_WARN must be >= 1")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_runtime()
    return settings
