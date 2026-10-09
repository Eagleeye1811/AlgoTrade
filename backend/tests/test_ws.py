"""WebSocket fan-out filtering: ticks reach every client, candle updates only subscribed symbols."""
from __future__ import annotations

from app.core.ws import WSManager, _Client


class _NoRedis:
    available = False


async def test_ticks_reach_all_clients_candles_are_filtered():
    m = WSManager(_NoRedis())
    chart_on_tcs = _Client(None, "a")
    chart_on_tcs.symbols = {"TCS"}
    unfiltered = _Client(None, "b")
    m.clients = {chart_on_tcs, unfiltered}
    await m.broadcast("tick-infy", "market.tick", "INFY", "e1")
    await m.broadcast("candle-infy", "candle.update", "INFY", "e2")
    await m.broadcast("candle-tcs", "candle.update", "TCS", "e3")
    got = lambda c: [c.queue.get_nowait() for _ in range(c.queue.qsize())]
    assert got(chart_on_tcs) == ["tick-infy", "candle-tcs"]
    assert got(unfiltered) == ["tick-infy", "candle-infy", "candle-tcs"]
