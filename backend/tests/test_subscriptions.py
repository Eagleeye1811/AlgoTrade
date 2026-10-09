"""L1 subscriptions + reference strategy, L3 per-strategy daily loss, intraday square-off / entry cut-off."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.main import create_app
from app.market.models import Bar
from app.strategies.base import BUY, HOLD, SELL
from app.strategies.ema_trend import EmaTrendStrategy
from tests.conftest import wait_for
from tests.test_trading import fill_order

D = Decimal
T0 = datetime(2026, 1, 5, 3, 45, tzinfo=timezone.utc)  # 09:15 IST


def bars1(closes, start=T0):
    return [Bar("X", start + timedelta(minutes=i), D(str(c)), D(str(c)), D(str(c)), D(str(c)), D(100)) for i, c in enumerate(closes)]


def bars5(closes, start=T0):
    return [Bar("X", start + timedelta(minutes=5 * i), D(str(c)), D(str(c)), D(str(c)), D(str(c)), D(500)) for i, c in enumerate(closes)]


# ---------------------------------------------------------------- reference strategy rules
def make(trend5):
    s = EmaTrendStrategy("t", "X", {"fast_period": 2, "slow_period": 3, "trend_period": 2})
    s.initialize(bars1([10, 10, 10, 10, 9]), bars5(trend5))
    return s


def test_ema_trend_entry_only_with_5m_trend():
    up = make([9, 10, 11])  # last 5m close 11 > 5m EMA2
    sig = up.on_candle(bars1([12], T0 + timedelta(minutes=5))[0])
    assert sig.signal == BUY and not sig.close_only and "entry allowed" in sig.reason
    against = make([12, 11, 10])  # 5m close below its EMA: bullish cross may only close a short
    sig = against.on_candle(bars1([12], T0 + timedelta(minutes=5))[0])
    assert sig.signal == BUY and sig.close_only and "against trend" in sig.reason


def test_ema_trend_short_entry_and_no_cross_hold():
    s = EmaTrendStrategy("t", "X", {"fast_period": 2, "slow_period": 3, "trend_period": 2})
    s.initialize(bars1([10, 10, 10, 10, 11]), bars5([12, 11, 10]))
    sig = s.on_candle(bars1([8], T0 + timedelta(minutes=5))[0])
    assert sig.signal == SELL and not sig.close_only
    assert s.on_candle(bars1([7], T0 + timedelta(minutes=6))[0]).signal == HOLD


def test_ema_trend_without_5m_history_is_exit_only():
    s = EmaTrendStrategy("t", "X", {"fast_period": 2, "slow_period": 3, "trend_period": 2})
    s.initialize(bars1([10, 10, 10, 10, 9]), [])
    sig = s.on_candle(bars1([12], T0 + timedelta(minutes=5))[0])
    assert sig.signal == BUY and sig.close_only


async def test_engine_feeds_5m_candles_to_strategies(trading):
    sys_ = trading
    rt = sys_.strategies.runtimes["ema_trend"]
    n = len(rt.instance.bars5)
    await sys_.strategies.on_candle_closed("RELIANCE", "5m", bars5([1000], datetime(2026, 1, 6, 4, 0, tzinfo=timezone.utc))[0])
    assert len(rt.instance.bars5) == n + 1


# ---------------------------------------------------------------- close-only signals, cut-off, square-off
async def test_close_only_signal_never_opens(trading):
    sys_ = trading
    r = await sys_.strategies.handle_signal("ema_trend", "BUY", "against trend", origin="STRATEGY", close_only=True)
    assert r["action"] == "IGNORED_EXIT_ONLY" and not sys_.orders.open_orders
    o = (await sys_.strategies.handle_signal("ema_trend", "SELL", "entry", origin="STRATEGY"))["order"]
    assert await fill_order(sys_, "RELIANCE", "1000", o["id"])
    r = await sys_.strategies.handle_signal("ema_trend", "BUY", "against trend", origin="STRATEGY", close_only=True)
    assert r["action"] == "ORDER" and r["order"]["reduce_only"]  # but it does close the short


async def test_no_new_entries_after_cutoff(trading):
    sys_ = trading
    late = Bar("RELIANCE", datetime(2026, 1, 5, 9, 41, tzinfo=timezone.utc), D(1000), D(1000), D(1000), D(1000), D(1))  # 15:11 IST
    r = await sys_.strategies.handle_signal("ema_trend", "BUY", "late", origin="STRATEGY", bar=late)
    assert r["action"] == "IGNORED_AFTER_ENTRY_CUTOFF"


async def test_square_off_closes_positions(trading):
    sys_ = trading
    o = (await sys_.strategies.handle_signal("macd", "BUY", "entry", origin="STRATEGY"))["order"]
    assert await fill_order(sys_, "RELIANCE", "1000", o["id"])
    feed = sys_.market.provider
    feed.clock = datetime(2026, 1, 5, 9, 46, tzinfo=timezone.utc)  # 15:16 IST
    ok = False
    for _ in range(30):
        await feed.push("RELIANCE", "1000")
        if sys_.positions.get("macd", "RELIANCE").quantity == 0:
            ok = True
            break
        await __import__("asyncio").sleep(0.1)
    assert ok


# ---------------------------------------------------------------- per-strategy daily loss (L3)
async def test_strategy_daily_loss_blocks_new_risk_only(trading):
    sys_ = trading
    await sys_.strategies.update_config("macd", {"stop_loss_pct": 0}, None, None, None, "test")
    await sys_.limits.set("STRATEGY", "macd", "max_daily_loss", D(200), None)
    o = (await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(50)))["order"]
    assert await fill_order(sys_, "RELIANCE", "1000", o["id"])
    await sys_.market.provider.push("RELIANCE", "995")  # unrealized -250 > 200
    r = await sys_.strategies.handle_signal("macd", "BUY", "more", origin="FAULT_LAB", quantity=D(10))
    assert r["order"]["status"] == "REJECTED" and r["order"]["reject_rule"] == "STRATEGY_DAILY_LOSS"
    other = await sys_.strategies.handle_signal("rsi_ema", "BUY", "x", origin="STRATEGY")  # other strategies unaffected
    assert other["order"]["status"] == "ACKNOWLEDGED"
    close = await sys_.strategies.handle_signal("macd", "SELL", "close", origin="STRATEGY")
    assert close["order"]["reduce_only"] and close["order"]["status"] != "REJECTED"


# ---------------------------------------------------------------- subscriptions through the HTTP API (L1)
@pytest.fixture
async def api(trading):
    app = create_app()
    app.state.system = trading
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c, trading


async def login(c, email):
    r = await c.post("/api/auth/register", json={"email": email, "name": email.split("@")[0], "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_user_subscribes_and_sees_only_own_strategies(api):
    c, sys_ = api
    admin = await login(c, "admin@x.io")
    trader = await login(c, "trader@x.io")
    cat = (await c.get("/api/strategies/catalog", headers=trader)).json()
    assert "EMA_TREND" in {x["kind"] for x in cat["strategies"]} and "INFY" in cat["symbols"]
    r = await c.post("/api/strategies", headers=trader, json={"kind": "MACD", "symbol": "INFY", "quantity": 5,
                                                              "allocation": 50000, "max_daily_loss": 1500})
    assert r.status_code == 200, r.text
    sub = r.json()
    assert sub["symbol"] == "INFY" and sub["status"] == "RUNNING" and sub["limits"]["max_daily_loss"] == 1500
    mine = (await c.get("/api/strategies?mine=true", headers=trader)).json()
    assert [s["id"] for s in mine] == [sub["id"]]
    assert len((await c.get("/api/strategies", headers=trader)).json()) == 5  # 4 demo + this one share the account
    # other users cannot manage it; the owner can unsubscribe while flat
    other = await login(c, "other@x.io")
    assert (await c.post(f"/api/strategies/{sub['id']}/pause", headers=other)).status_code == 403
    assert (await c.post(f"/api/strategies/{sub['id']}/pause", headers=admin)).status_code == 200
    assert (await c.delete(f"/api/strategies/{sub['id']}", headers=trader)).status_code == 200
    assert sub["id"] not in sys_.strategies.runtimes


async def test_subscription_validation(api):
    c, sys_ = api
    h = await login(c, "a@x.io")
    bad = [{"kind": "NOPE", "symbol": "INFY", "quantity": 1, "allocation": 1000},
           {"kind": "MACD", "symbol": "ZZZ", "quantity": 1, "allocation": 1000},
           {"kind": "MACD", "symbol": "INFY", "quantity": 1, "allocation": 10_000_000}]  # more than unallocated
    for b in bad:
        assert (await c.post("/api/strategies", headers=h, json=b)).status_code == 400


async def test_cannot_unsubscribe_with_open_position(api):
    c, sys_ = api
    h = await login(c, "a@x.io")
    sub = (await c.post("/api/strategies", headers=h, json={"kind": "MACD", "symbol": "INFY", "quantity": 5,
                                                            "allocation": 50000})).json()
    o = (await sys_.strategies.handle_signal(sub["id"], "BUY", "x", origin="STRATEGY"))["order"]
    assert await fill_order(sys_, "INFY", "1500", o["id"])
    r = await c.delete(f"/api/strategies/{sub['id']}", headers=h)
    assert r.status_code == 400 and "position" in r.json()["detail"]


async def test_entry_cutoff_boundary_is_candle_close_time(trading):
    sys_ = trading

    def candle(h, m):  # IST start time
        t = datetime(2026, 1, 5, h, m, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        return Bar("RELIANCE", t, D(1000), D(1000), D(1000), D(1000), D(1))
    r = await sys_.strategies.handle_signal("ema_trend", "BUY", "closes 15:10", origin="STRATEGY", bar=candle(15, 9))
    assert r["action"] == "IGNORED_AFTER_ENTRY_CUTOFF"
    r = await sys_.strategies.handle_signal("ema_trend", "BUY", "closes 15:09", origin="STRATEGY", bar=candle(15, 8))
    assert r["action"] == "ORDER"


async def test_no_automatic_allocation(trading):
    sys_ = trading
    # a subscription without allocation exists but cannot open positions until the user allocates capital
    rt = await sys_.strategies.subscribe(None, "MACD", "INFY", D(5), D(0), None, "test")
    assert rt.allocation == 0 and sys_.limits.daily_loss_limit(rt.id, rt.allocation) == 0
    r = await sys_.strategies.handle_signal(rt.id, "BUY", "x", origin="STRATEGY")
    assert r["order"]["reject_rule"] == "STRATEGY_MARGIN" and "no capital allocated" in r["order"]["reject_reason"]
    await sys_.strategies.update_config(rt.id, None, None, D(100000), None, "test")
    assert sys_.limits.daily_loss_limit(rt.id, rt.allocation) == D(5000)  # default follows the new allocation
    r = await sys_.strategies.handle_signal(rt.id, "BUY", "x", origin="STRATEGY")
    assert r["order"]["status"] == "ACKNOWLEDGED"


async def test_default_subscriptions_start_unallocated(system):
    await system.setup_account("T", D(1000000), None, "test", None)
    assert all(rt.allocation == 0 for rt in system.strategies.runtimes.values())
    assert system.strategies.unallocated() == D(1000000)
