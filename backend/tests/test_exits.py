"""Stop-loss / take-profit: tick-driven exits measured from the position's average price."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.models import Order, Signal
from tests.test_trading import fill_order

D = Decimal


async def open_position(sys_, sid: str, side: str, price: str = "1000"):
    o = (await sys_.strategies.handle_signal(sid, side, "entry", origin="STRATEGY"))["order"]
    assert await fill_order(sys_, "RELIANCE", price, o["id"])
    pos = sys_.positions.get(sid, "RELIANCE")
    assert pos.quantity != 0
    return pos


async def drive(sys_, price: str, until, timeout: float = 5.0) -> bool:
    """Keep ticking `price` (an exit needs one tick to trigger and later ticks to fill)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        await sys_.market.provider.push("RELIANCE", price)
        if until():
            return True
        await asyncio.sleep(0.1)
    return False


async def exit_signals(sys_, sid: str) -> list[Signal]:
    async with sys_.db.session() as s:
        return (await s.execute(select(Signal).where(Signal.strategy_id == sid, Signal.origin.in_(("STOP_LOSS", "TAKE_PROFIT")))
                                .order_by(Signal.created_at))).scalars().all()


async def test_long_stop_loss_closes_position(trading):
    sys_ = trading
    pos = await open_position(sys_, "macd", "BUY")
    lv = sys_.strategies.exit_levels(sys_.strategies.runtimes["macd"])
    assert lv["stop_loss"] == (pos.avg_price * D("0.99")).quantize(D("0.01"))
    assert lv["take_profit"] == (pos.avg_price * D("1.02")).quantize(D("0.01"))
    assert await drive(sys_, "989", lambda: sys_.positions.get("macd", "RELIANCE").quantity == 0)
    sigs = await exit_signals(sys_, "macd")
    assert len(sigs) == 1 and sigs[0].origin == "STOP_LOSS" and sigs[0].signal == "SELL" and sigs[0].action == "ORDER_CLOSE"
    async with sys_.db.session() as s:
        o = (await s.execute(select(Order).where(Order.origin == "STOP_LOSS"))).scalar_one()
        assert o.reduce_only and o.side == "SELL" and o.quantity == D(50)
    assert sys_.strategies.exit_levels(sys_.strategies.runtimes["macd"]) == {"stop_loss": None, "take_profit": None}


async def test_long_take_profit_closes_position(trading):
    sys_ = trading
    await open_position(sys_, "macd", "BUY")
    assert await drive(sys_, "1021", lambda: sys_.positions.get("macd", "RELIANCE").quantity == 0)
    sigs = await exit_signals(sys_, "macd")
    assert [x.origin for x in sigs] == ["TAKE_PROFIT"]
    assert sys_.positions.get("macd", "RELIANCE").realized_pnl > 0


async def test_short_stop_loss_buys_back(trading):
    sys_ = trading
    pos = await open_position(sys_, "macd", "SELL")
    assert pos.quantity < 0
    assert await drive(sys_, "1011", lambda: sys_.positions.get("macd", "RELIANCE").quantity == 0)
    sigs = await exit_signals(sys_, "macd")
    assert len(sigs) == 1 and sigs[0].origin == "STOP_LOSS" and sigs[0].signal == "BUY"


async def test_no_exit_inside_band(trading):
    sys_ = trading
    await open_position(sys_, "macd", "BUY")
    assert not await drive(sys_, "995", lambda: sys_.positions.get("macd", "RELIANCE").quantity == 0, timeout=1.0)
    assert await exit_signals(sys_, "macd") == []


async def test_exits_disabled_with_zero(trading):
    sys_ = trading
    await sys_.strategies.update_config("macd", {"stop_loss_pct": 0, "take_profit_pct": 0}, None, None, None, "test")
    await open_position(sys_, "macd", "BUY")
    assert sys_.strategies.exit_levels(sys_.strategies.runtimes["macd"]) == {"stop_loss": None, "take_profit": None}
    assert not await drive(sys_, "980", lambda: sys_.positions.get("macd", "RELIANCE").quantity == 0, timeout=1.0)
    assert await exit_signals(sys_, "macd") == []


async def test_exit_params_validated(trading):
    sys_ = trading
    with pytest.raises(ValueError):
        await sys_.strategies.update_config("macd", {"stop_loss_pct": 25}, None, None, None, "test")


# ---------------------------------------------------------------- multi-symbol strategies
async def test_strategy_trades_every_symbol(trading):
    sys_ = trading
    rt = sys_.strategies.runtimes["macd"]
    assert rt.symbols == ["RELIANCE", "INFY"]  # primary first, then the rest of the universe
    await sys_.market.provider.push("INFY", "1500")
    res = await sys_.strategies.handle_signal("macd", "BUY", "infy entry", origin="STRATEGY", symbol="INFY")
    assert res["order"]["symbol"] == "INFY"
    assert await fill_order(sys_, "INFY", "1500", res["order"]["id"])
    assert sys_.positions.get("macd", "INFY").quantity > 0
    assert sys_.positions.get("macd", "RELIANCE").quantity == 0
    snap = sys_.strategies.snapshot(rt)
    assert snap["open_positions"] == 1 and {b["symbol"] for b in snap["books"]} == {"RELIANCE", "INFY"}


async def test_entry_size_capped_by_per_symbol_budget(trading):
    sys_ = trading
    rt = sys_.strategies.runtimes["macd"]
    budget = rt.allocation / len(rt.symbols)
    pricey = (budget / 10).quantize(D(1))  # only ~10 shares fit
    assert sys_.strategies.entry_quantity(rt, pricey) == min(rt.quantity, (budget / pricey).to_integral_value(rounding="ROUND_FLOOR"))
    assert sys_.strategies.entry_quantity(rt, D(1)) == rt.quantity  # cheap stock: configured quantity
    assert sys_.strategies.entry_quantity(rt, budget * 2) == 0
