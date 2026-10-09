"""Automatic NSE session switching (LIVE <-> REPLAY) driven by an injected clock; no network."""
from __future__ import annotations

from decimal import Decimal

from app.market.providers import ManualProvider
from tests.conftest import wait_for

D = Decimal


class FakeLive(ManualProvider):
    mode = "LIVE"


class FakeReplay(ManualProvider):
    mode = "REPLAY"


def enable_auto(sys_, market_open: dict):
    md = sys_.market
    sys_.settings.market_data_mode = "auto"
    md.is_market_open = lambda: market_open["v"]
    made = []

    async def make(mode):
        made.append(mode)
        return FakeLive(md.settings.symbol_list) if mode == "live" else FakeReplay(md.settings.symbol_list)

    md._make_provider = make
    md._last_desired = md._desired_mode()
    return made


async def test_switches_to_live_when_session_opens_and_back(trading):
    sys_ = trading
    clock = {"v": False}
    made = enable_auto(sys_, clock)
    assert await sys_.market.check_session() == "replay"  # manual feed -> replay at night
    assert sys_.market.provider.mode == "REPLAY"
    assert await sys_.market.check_session() is None  # stable
    clock["v"] = True  # 09:15 IST
    assert await sys_.market.check_session() == "live"
    assert sys_.market.provider.mode == "LIVE" and made[-1] == "live"
    assert sys_.market.last_switch["to"] == "LIVE"
    assert "market_switch" not in sys_.controller.blocks  # unblocked after reconciliation
    clock["v"] = False  # 15:30 IST
    assert await sys_.market.check_session() == "replay"
    assert sys_.market.provider.mode == "REPLAY"


async def test_switch_cancels_orders_priced_on_old_feed(trading):
    sys_ = trading
    clock = {"v": False}
    enable_auto(sys_, clock)
    await sys_.market.check_session()
    await sys_.market.provider.push("RELIANCE", "1000")
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(10),
                                            order_type="LIMIT", limit_price=D("900"))
    oid = r["order"]["id"]
    assert oid in sys_.orders.open_orders
    clock["v"] = True
    await sys_.market.check_session()
    assert await wait_for(lambda: oid not in sys_.orders.open_orders)
    assert sys_.market.last_switch["cancelled_orders"] == [oid[:8]]


async def test_orders_blocked_during_switch(trading):
    sys_ = trading
    clock = {"v": False}
    made = enable_auto(sys_, clock)
    seen = {}
    orig = sys_.market._make_provider

    async def spy(mode):
        seen["blocked"] = "market_switch" in sys_.controller.blocks
        return await orig(mode)

    sys_.market._make_provider = spy
    await sys_.market.check_session()
    assert seen["blocked"] is True and made


async def test_operator_replay_override_lasts_until_session_close(trading):
    sys_ = trading
    clock = {"v": True}
    enable_auto(sys_, clock)
    await sys_.market.check_session()
    assert sys_.market.provider.mode == "LIVE"
    sys_.market.override = "replay"  # what restart_replay() sets during market hours
    assert await sys_.market.check_session() == "replay"
    assert await sys_.market.check_session() is None  # override honoured while market open
    clock["v"] = False  # session closes -> override cleared, replay is desired anyway
    assert await sys_.market.check_session() is None and sys_.market.override is None
    clock["v"] = True  # next session opens -> live again
    assert await sys_.market.check_session() == "live"


async def test_no_switching_when_mode_is_pinned(trading):
    sys_ = trading
    sys_.settings.market_data_mode = "manual"
    sys_.market.is_market_open = lambda: True
    assert await sys_.market.check_session() is None
