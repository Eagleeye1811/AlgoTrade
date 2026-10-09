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

    # Market data
    # auto   -> LIVE (polled, Yahoo chart endpoint) during NSE hours, otherwise REPLAY of real bars
    # live   -> always poll Yahoo (status DELAYED/LIVE/DISCONNECTED)
    # replay -> always replay real historical 1m bars
    market_data_mode: str = "auto"
    symbols: str = "RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK,SBIN,BHARTIARTL,ITC,LT,KOTAKBANK,AXISBANK,HINDUNILVR,BAJFINANCE,MARUTI,SUNPHARMA"
    yahoo_base_url: str = "https://query1.finance.yahoo.com"
    yahoo_timeout_s: float = 8.0
    live_poll_interval_s: float = 4.0  # one symbol per poll (round-robin)
    live_focus_poll_interval_s: float = 2.5  # symbols open in a chart are also polled this often (forming candle)
    replay_speed: float = 1.0  # market minutes per wall minute (1 = real time, like a live platform)
    replay_warmup_bars: int = 60
    replay_data_dir: str = "data/replay"
    stale_after_s_live: float = 180.0
    stale_after_s_replay: float = 6.0

    # Account (paper)
    starting_capital: Decimal = Decimal("1000000")
    margin_rate: Decimal = Decimal("1.0")  # fraction of notional blocked as virtual margin

    # Paper broker simulation
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
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
