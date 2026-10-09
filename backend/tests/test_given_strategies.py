"""021-03 given strategies through the real engine (test venue + manual feed, IST market clock).

Strategy 1: enter at 09:15 IST, square off the full position and cancel open orders at 15:15 IST.
Strategy 2: 1% breakout from today's open (limit at the LTP), platform-managed 5% target / 5% stop-loss, one entry/day.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.strategies.open_breakout import OpenBreakoutStrategy
from app.strategies.time_entry import TimeEntryStrategy
from tests.conftest import wait_for

D = Decimal
IST = timezone(timedelta(hours=5, minutes=30))


def ist(h, m, s=0):
    return datetime(2026, 1, 5, h, m, s, tzinfo=IST)  # a Monday


async def tick_until(sys_, sym, price, cond, n=40):
    for _ in range(n):
        await sys_.market.provider.push(sym, price, advance_s=0)
        if cond():
            return True
        await asyncio.sleep(0.1)
    return False


# ---------------------------------------------------------------- rules in isolation
def test_time_entry_rules():
    s = TimeEntryStrategy("t", "X", {"direction": -1})
    assert s.on_clock(ist(9, 14, 59)) is None
    sig = s.on_clock(ist(9, 15))
    assert sig.signal == "SELL" and sig.order_type == "MARKET"
    assert s.on_clock(ist(15, 15)) is None
    assert s.on_clock(datetime(2026, 1, 10, 9, 30, tzinfo=IST)) is None  # Saturday
    s.enforce_hours = False  # settings.enforce_market_hours off: enters whenever it runs
    assert s.on_clock(ist(16, 30)).signal == "SELL"
    assert s.on_clock(datetime(2026, 1, 10, 9, 30, tzinfo=IST)).signal == "SELL"


def test_open_breakout_rules():
    s = OpenBreakoutStrategy("t", "X", {})
    o = D("1400.00")
    assert s.on_tick(D("1413.95"), ist(9, 20), o) is None
    buy = s.on_tick(D("1414.00"), ist(9, 20), o)  # the guide's example: open 1400, LTP 1414 (+1%)
    assert buy.signal == "BUY" and buy.order_type == "LIMIT" and buy.limit_price == D("1414.00")
    sell = s.on_tick(D("1386.00"), ist(9, 20), o)
    assert sell.signal == "SELL" and sell.limit_price == D("1386.00")
    assert s.on_tick(D("1500"), ist(9, 20), None) is None  # no open yet


# ---------------------------------------------------------------- strategy 1 end to end
async def test_strategy1_enters_once_at_0915_and_squares_off_at_1515(trading):
    sys_ = trading
    feed = sys_.market.provider
    feed.clock = ist(9, 15, 1).astimezone(timezone.utc)
    await feed.push("TCS", "2000", advance_s=0)
    rt = await sys_.strategies.subscribe(None, "TIME_ENTRY", "TCS", D(5), D(100000), {"direction": 1}, "test")
    for _ in range(3):  # the 1-second clock calls this repeatedly; only one entry may result
        await sys_.strategies.on_clock(feed.clock)
    assert await tick_until(sys_, "TCS", "2000", lambda: sys_.positions.get(rt.id, "TCS").quantity == 5)
    await sys_.strategies.on_clock(feed.clock)
    await asyncio.sleep(0.2)
    entries = [o for o in await sys_.recent_orders(50) if o["strategy_id"] == rt.id]
    assert len(entries) == 1 and entries[0]["side"] == "BUY" and not entries[0]["reduce_only"]
    # 15:15 IST: a working order of the strategy is cancelled and the full position is closed
    pending = (await sys_.strategies.handle_signal(rt.id, "BUY", "x", origin="FAULT_LAB", quantity=D(1),
                                                   order_type="LIMIT", limit_price=D("1900")))["order"]
    assert pending["status"] == "ACKNOWLEDGED", pending["reject_reason"]
    feed.clock = ist(15, 15, 0).astimezone(timezone.utc)
    await feed.push("TCS", "2010", advance_s=0)
    for _ in range(40):
        await sys_.strategies.on_clock(feed.clock)
        await feed.push("TCS", "2010", advance_s=0)
        if sys_.positions.get(rt.id, "TCS").quantity == 0 and not sys_.orders.open_orders:
            break
        await asyncio.sleep(0.2)
    assert sys_.positions.get(rt.id, "TCS").quantity == 0
    orders = {o["id"]: o for o in await sys_.recent_orders(50)}
    assert orders[pending["id"]]["status"] == "CANCELLED"
    sq = [o for o in orders.values() if o["strategy_id"] == rt.id and o["origin"] == "SQUARE_OFF"]
    assert len(sq) == 1 and sq[0]["side"] == "SELL" and sq[0]["quantity"] == D(5) and sq[0]["reduce_only"]


# ---------------------------------------------------------------- strategy 2 end to end
async def test_strategy2_breakout_target_and_one_entry_per_day(trading):
    sys_ = trading
    feed = sys_.market.provider
    feed.clock = ist(9, 30).astimezone(timezone.utc)
    rt = await sys_.strategies.subscribe(None, "OPEN_BREAKOUT", "INFY", D(2), D(100000), None, "test")
    assert sys_.market.session_open("INFY") == D("1500.00")  # first INFY price of the session (fixture)
    # +1% -> buy limit at the LTP
    assert await tick_until(sys_, "INFY", "1515", lambda: sys_.positions.get(rt.id, "INFY").quantity == 2)
    pos = sys_.positions.get(rt.id, "INFY")
    target = (pos.avg_price * D("1.05")).quantize(D("0.01"))
    assert sys_.strategies.exit_levels(rt) == {"stop_loss": (pos.avg_price * D("0.95")).quantize(D("0.01")), "take_profit": target}
    # target hit: platform sends an opposite limit order of the same quantity at the level
    px = str(target + 1)
    assert await tick_until(sys_, "INFY", px, lambda: sys_.positions.get(rt.id, "INFY").quantity == 0)
    tp = [o for o in await sys_.recent_orders(50) if o["strategy_id"] == rt.id and o["origin"] == "TAKE_PROFIT"]
    assert tp and tp[0]["side"] == "SELL" and tp[0]["quantity"] == D(2) and tp[0]["order_type"] == "LIMIT"
    assert D(str(tp[0]["limit_price"])) == target
    # still above +1% but the day's entry is used: no new position
    await tick_until(sys_, "INFY", "1520", lambda: False, n=8)
    assert sys_.positions.get(rt.id, "INFY").quantity == 0 and rt.entries_today == 1


async def test_strategy2_short_stop_loss(trading):
    sys_ = trading
    feed = sys_.market.provider
    feed.clock = ist(9, 30).astimezone(timezone.utc)
    await feed.push("TCS", "2000", advance_s=0)  # sets today's open
    rt = await sys_.strategies.subscribe(None, "OPEN_BREAKOUT", "TCS", D(3), D(100000), None, "test")
    assert await tick_until(sys_, "TCS", "1979", lambda: sys_.positions.get(rt.id, "TCS").quantity == -3)  # -1.05%
    stop = sys_.strategies.exit_levels(rt)["stop_loss"]
    assert stop == (sys_.positions.get(rt.id, "TCS").avg_price * D("1.05")).quantize(D("0.01"))
    assert await tick_until(sys_, "TCS", str(stop + 5), lambda: sys_.positions.get(rt.id, "TCS").quantity == 0)
    sl = [o for o in await sys_.recent_orders(50) if o["strategy_id"] == rt.id and o["origin"] == "STOP_LOSS"]
    assert sl and sl[0]["side"] == "BUY" and sl[0]["quantity"] == D(3)
