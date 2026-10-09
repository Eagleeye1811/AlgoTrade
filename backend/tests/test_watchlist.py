"""Dashboard watchlist: per-symbol day summary built from the 1m candles."""
from __future__ import annotations

from decimal import Decimal

from app.api.market import watchlist
from tests.test_trading import fill_order

D = Decimal


async def test_watchlist_rows(trading):
    sys_ = trading
    await sys_.market.provider.push("RELIANCE", "1000")
    await sys_.market.provider.push("RELIANCE", "1010")
    o = (await sys_.strategies.handle_signal("macd", "BUY", "entry", origin="STRATEGY"))["order"]
    assert await fill_order(sys_, "RELIANCE", "1010", o["id"])
    rows = {r["symbol"]: r for r in await watchlist(None, sys_)}
    assert set(rows) == {"RELIANCE", "INFY"}
    r = rows["RELIANCE"]
    assert r["ltp"] == D("1010") and r["position"] == D(50)
    assert r["high"] >= D("1010") and r["low"] <= D("1000")
    assert r["change"] == r["ltp"] - r["ref_price"] and r["spark"][-1] == 1010.0
    assert rows["INFY"]["position"] == 0
