"""Test harness: real PostgreSQL (algotrade_test, migrated with Alembic), real Redis (db 15),
real engine components, paper broker, and the test-only ManualProvider market feed."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text

from app.config import Settings
from app.engine.system import TradingSystem

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = os.environ.get("ALGOTRADE_TEST_DATABASE_URL", "postgresql+asyncpg://localhost:5432/algotrade_test")
TEST_REDIS = os.environ.get("ALGOTRADE_TEST_REDIS_URL", "redis://localhost:6379/15")
SYNC_DB = TEST_DB.replace("+asyncpg", "+psycopg2")


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    eng = create_engine(SYNC_DB)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS paper_broker CASCADE"))
        c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    eng.dispose()
    env = {**os.environ, "ALEMBIC_DATABASE_URL": TEST_DB}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True,
                   capture_output=True)
    yield


def _truncate():
    eng = create_engine(SYNC_DB)
    with eng.begin() as c:
        tables = [r[0] for r in c.execute(text(
            "select schemaname||'.'||tablename from pg_tables where schemaname in ('public','paper_broker') "
            "and tablename <> 'alembic_version'"))]
        c.execute(text("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE"))
    eng.dispose()


def make_settings(**over) -> Settings:
    base = dict(database_url=TEST_DB, redis_url=TEST_REDIS, market_data_mode="manual", broker_mode="paper", symbols="RELIANCE,INFY,TCS",
                default_strategies="ema_trend,rsi_ema,macd,breakout",
                jwt_secret="test-secret", redis_namespace="algotrade_test", broker_base_latency_ms=1, broker_jitter_ms=1, broker_timeout_s=1.0,
                stale_after_s_manual=3.0, reconcile_interval_s=3600, enforce_market_hours=True, broker_max_participation=Decimal("1"))
    base.update(over)
    return Settings(**base)


@pytest_asyncio.fixture
async def system():
    _truncate()
    sys_ = TradingSystem(make_settings())
    await sys_.redis.connect()
    if sys_.redis.r is not None and sys_.redis.available:
        await sys_.redis.r.flushdb()
    await sys_.start_engine("STARTUP")
    yield sys_
    await sys_.stop_engine()
    await sys_.market.close()
    await sys_.redis.close()
    await sys_.db.dispose()


async def wait_for(cond, timeout: float = 5.0, interval: float = 0.05) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        r = cond()
        if asyncio.iscoroutine(r):
            r = await r
        if r:
            return True
        await asyncio.sleep(interval)
    return False


@pytest_asyncio.fixture
async def trading(system):
    """Account configured, prices flowing, trading RUNNING."""
    # nothing is allocated automatically; the tests allocate like a user would
    alloc = {sid: Decimal(200000) for sid in ("ema_trend", "rsi_ema", "macd", "breakout")}
    await system.setup_account("Test", Decimal(1000000), alloc, "test", None)
    feed = system.market.provider
    await feed.push("RELIANCE", "1000.00")
    await feed.push("INFY", "1500.00")
    await system.start_trading("test")
    return system
