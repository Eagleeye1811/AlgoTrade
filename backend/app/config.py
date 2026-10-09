"""Runtime configuration. All secrets/endpoints come from environment variables (.env)."""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ALGOTRADE_", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://localhost:5432/algotrade"
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    jwt_secret: str = "change-me-in-.env"
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 12 * 60
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Venue and market data
    #   broker_mode      "o21"  -> orders through the 021 developer API (POST /orders) - the runtime venue
    #                    "paper" -> local simulator, used only by the automated test suite (forces faults on demand)
    #   market_data_mode "o21"  -> 021 market-data websocket; "manual" -> test feed
    broker_mode: str = "o21"
    market_data_mode: str = "o21"
    symbols: str = "RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK,SBIN,BHARTIARTL,ITC,LT,KOTAKBANK,AXISBANK,HINDUNILVR,BAJFINANCE,MARUTI,SUNPHARMA"
    instruments_dir: str = "data/instruments"  # daily cache of GET /instruments
    # subscriptions created for the first ADMIN at startup (presets in engine/strategy_engine.py)
    default_strategies: str = "time_entry,open_breakout,ema_trend"
    strategy_clock: bool = True  # 1-second scheduler: clock-driven entries and the 15:15 IST square-off
    charges_table_path: str = "data/charges.json"  # published charges table (L2 net P&L)

    # 021 developer sandbox (credentials only from .env: ALGOTRADE_O21_USERNAME / ALGOTRADE_O21_PASSWORD)
    o21_base_url: str = "https://devapi.021.trade/api/developer-api/v1"
    o21_ws_market_url: str = "wss://devapi.021.trade/api/developer/websocket/market"
    o21_ws_orders_url: str = "wss://devapi.021.trade/api/developer/websocket/orders"
    o21_username: str = ""
    o21_password: str = ""
    o21_timeout_s: float = 5.0
    o21_rate_per_s: float = 5.0
    stale_after_s_live: float = 180.0
    # Sandbox stream: packets whose last-trade time is not newer than the newest accepted are normally rejected (the
    # sandbox interleaves stale packets). If no exchange-timed packet arrived for a symbol for this long (e.g. after
    # hours, when the sandbox keeps streaming a time-frozen snapshot), those packets are used, stamped with their
    # receive time. 0 disables the fallback.
    o21_sandbox_stream_after_s: float = 20.0
    # After hours (outside Mon-Fri 09:15-15:30 IST) replay the last recorded 021 session in real time instead of the
    # sandbox stream: each 1m candle as 4 ticks `replay_step_s` apart (15 s = one candle per minute).
    after_hours_replay: bool = True
    replay_step_s: float = 15.0
    stale_after_s_manual: float = 6.0

    # Account: 021 gives ₹10,00,000 virtual funds, topped up every trading day
    starting_capital: Decimal = Decimal("1000000")
    margin_rate: Decimal = Decimal("1.0")  # fraction of notional blocked as virtual margin
    # NSE session rules: no new entries after 15:10 IST, square-off at 15:15 IST, Strategy 1 entries only 09:15-15:15
    # on weekdays. False = trade at any time against the 021 sandbox (it runs after hours).
    enforce_market_hours: bool = False

    # Paper broker simulator (broker_mode "paper", tests only)
    broker_seed: int = 7
    broker_base_latency_ms: int = 40
    broker_jitter_ms: int = 40
    broker_max_participation: Decimal = Decimal("0.05")  # max share of tick volume fillable per tick
    broker_slippage_bps: Decimal = Decimal("2")
    broker_timeout_s: float = 5.0  # client-side timeout for broker API calls

    # Background jobs
    reconcile_interval_s: float = 30.0
    limit_order_ttl_s: float = 60.0
    pnl_snapshot_interval_s: float = 10.0

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]

    @property
    def default_strategy_list(self) -> list[str]:
        return [s.strip() for s in self.default_strategies.split(",") if s.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
