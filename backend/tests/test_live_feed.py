"""Live Yahoo feed -> candles. Reproduces Yahoo's real response shape during market hours: the forming minute
is null and a zero-volume "current price" point is appended mid-minute (stamped meta.regularMarketTime)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.market.candle_engine import CandleEngine
from app.market.providers import LiveYahooProvider
from app.market.yahoo import parse_chart

D = Decimal


def ts(h, m, s=0):  # IST wall-clock time on 9 Oct 2026 -> epoch seconds
    return int(datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()) + h * 3600 + m * 60 + s - 19800


def response(rows):
    """rows: (epoch, o, h, l, c, v) with None for Yahoo's null forming bar."""
    q = {k: [] for k in ("open", "high", "low", "close", "volume")}
    for _, o, h, l, c, v in rows:
        for k, x in zip(q, (o, h, l, c, v)):
            q[k].append(x)
    return {"chart": {"result": [{"meta": {}, "timestamp": [r[0] for r in rows], "indicators": {"quote": [q]}}]}}


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.last_latency_ms = 1.0

    async def fetch_bars(self, symbol, interval="1m", range_="1d"):
        return parse_chart(symbol, self.responses.pop(0))


def test_pseudo_point_is_merged_into_its_minute():
    bars, _, dropped = parse_chart("X", response([
        (ts(11, 22), 1174.5, 1175.4, 1174.2, 1174.3, 52968),
        (ts(11, 23), None, None, None, None, None),
        (ts(11, 23, 35), 1175.2, 1175.2, 1175.2, 1175.2, 0),
    ]))
    assert dropped == 1
    assert [b.ts.second for b in bars] == [0, 0]  # every bar keyed by its minute
    assert bars[-1].close == D("1175.20") and bars[-1].volume == 0
    # a mid-minute point after a real forming bar updates that bar's close and range
    bars, _, _ = parse_chart("X", response([
        (ts(11, 24), 1175.0, 1175.3, 1174.9, 1175.1, 5000),
        (ts(11, 24, 40), 1175.6, 1175.6, 1175.6, 1175.6, 0),
    ]))
    assert len(bars) == 1 and bars[0].open == D("1175.00") and bars[0].high == D("1175.60") and bars[0].close == D("1175.60")
    assert bars[0].volume == 5000


async def test_live_candles_match_yahoo_final_bars():
    poll1 = response([  # polled at 11:23:35
        (ts(11, 22), 1174.5, 1175.4, 1174.2, 1174.3, 52968),
        (ts(11, 23), None, None, None, None, None),
        (ts(11, 23, 35), 1175.2, 1175.2, 1175.2, 1175.2, 0),
    ])
    poll2 = response([  # polled ~65s later: 11:23 is final now
        (ts(11, 22), 1174.5, 1175.4, 1174.2, 1174.3, 52968),
        (ts(11, 23), 1174.3, 1175.6, 1174.0, 1175.0, 40000),
        (ts(11, 24), 1175.0, 1175.3, 1174.9, 1175.1, 5000),
        (ts(11, 24, 40), 1175.1, 1175.1, 1175.1, 1175.1, 0),
    ])
    ce = CandleEngine()
    p = LiveYahooProvider(["X"], FakeClient([poll1, poll2]))
    p.on_tick = ce.on_tick
    p.on_bar_close = ce.close_minute
    await p._poll_symbol("X")
    await p._poll_symbol("X")
    closed = {b.ts.astimezone(timezone.utc): b for b in ce.candles("X", "1m", include_working=False)}
    c23 = closed[datetime.fromtimestamp(ts(11, 23), timezone.utc)]
    # exactly Yahoo's final 11:23 bar - not a flat zero-volume dash at the mid-minute price
    assert (c23.open, c23.high, c23.low, c23.close, c23.volume) == (D("1174.30"), D("1175.60"), D("1174.00"), D("1175.00"), D(40000))
    c22 = closed[datetime.fromtimestamp(ts(11, 22), timezone.utc)]
    assert (c22.open, c22.high, c22.low, c22.close, c22.volume) == (D("1174.50"), D("1175.40"), D("1174.20"), D("1174.30"), D(52968))
    w = ce.candles("X", "1m")[-1]  # forming 11:24 candle keeps the real wick range
    assert w.open == D("1175.00") and w.high == D("1175.30") and w.low == D("1174.90")


def test_focus_lane_selection():
    p = LiveYahooProvider(["A", "B", "C", "D", "E"], FakeClient([]))
    assert p.current_focus() == []
    p.focus = lambda: ["ZZZ", "C", "A", "B", "D"]  # unknown symbols ignored, capped
    assert p.current_focus() == ["C", "A", "B"]
    p.focus = lambda: 1 / 0  # a broken focus source never stops the feed
    assert p.current_focus() == []


def test_ws_focus_symbols_are_open_charts():
    from app.core.ws import WSManager, _Client

    class NoRedis:
        available = False

    m = WSManager(NoRedis())
    a, b, c = _Client(None, "a"), _Client(None, "b"), _Client(None, "c")
    a.symbols, b.symbols = {"TCS"}, {"INFY", "TCS"}
    m.clients = {a, b, c}
    assert m.focus_symbols() == ["INFY", "TCS"]


async def test_fast_lane_builds_forming_candle_within_the_minute():
    # three polls inside 11:24 as the price moves: the forming candle tracks every one of them
    polls = [response([(ts(11, 24), 1175.0, 1175.0, 1175.0, 1175.0, 1000), (ts(11, 24, 5), 1175.0, 1175.0, 1175.0, 1175.0, 0)]),
             response([(ts(11, 24), 1175.0, 1175.4, 1175.0, 1175.4, 3000), (ts(11, 24, 8), 1175.4, 1175.4, 1175.4, 1175.4, 0)]),
             response([(ts(11, 24), 1175.0, 1175.4, 1174.6, 1174.7, 5000), (ts(11, 24, 11), 1174.6, 1174.6, 1174.6, 1174.6, 0)])]
    ce = CandleEngine()
    updates = []

    async def on_update(sym, tf, b):
        updates.append((tf, b.close))
    ce.on_update = on_update
    p = LiveYahooProvider(["X"], FakeClient(polls))
    p.on_tick = ce.on_tick
    seen = []
    for _ in polls:
        await p._poll_symbol("X")
        w = ce.candles("X", "1m")[-1]
        seen.append((w.open, w.high, w.low, w.close, w.volume))
    assert seen == [(D("1175.00"), D("1175.00"), D("1175.00"), D("1175.00"), D(1000)),
                    (D("1175.00"), D("1175.40"), D("1175.00"), D("1175.40"), D(3000)),
                    (D("1175.00"), D("1175.40"), D("1174.60"), D("1174.60"), D(5000))]


async def test_burst_of_ticks_still_pushes_latest_candle():
    import asyncio
    from app.market.models import Tick
    ce = CandleEngine()
    pushed = []

    async def on_update(sym, tf, b):
        if tf == "1m":
            pushed.append(b.close)
    ce.on_update = on_update
    t = datetime.fromtimestamp(ts(11, 24), timezone.utc)
    for price in ("1175.0", "1175.2", "1175.6"):  # one poll emits several ticks within microseconds
        await ce.on_tick(Tick("X", t, D(price), D(1), "yahoo"))
    assert pushed == [D("1175.0")]  # throttled...
    await asyncio.sleep(0.3)
    assert pushed == [D("1175.0"), D("1175.6")]  # ...but the latest state is delivered, not dropped
