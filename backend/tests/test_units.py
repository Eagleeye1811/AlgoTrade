"""Pure unit tests: market data parsing, candle generation, indicators/strategies, position math."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.position_math import apply_fill_math, unrealized
from app.engine.risk_firewall import inr
from app.engine.types import OrderIntent
from app.market.candle_engine import CandleEngine
from app.market.models import Bar, Tick
from app.market.providers import ohlc_path
from app.market.yahoo import MarketDataError, parse_chart
from app.strategies.base import BUY, HOLD, SELL
from app.strategies.breakout import BreakoutStrategy
from app.strategies.indicators import ema, macd, rsi
from app.strategies.macd import MacdStrategy
from app.strategies.rsi_ema import RsiEmaStrategy

D = Decimal
T0 = datetime(2026, 1, 5, 3, 45, tzinfo=timezone.utc)


def bar(i: int, c: float, o: float | None = None, h: float | None = None, l: float | None = None, v: int = 1000, sym="X"):
    o = c if o is None else o
    return Bar(sym, T0 + timedelta(minutes=i), D(str(o)), D(str(h if h is not None else max(o, c))),
               D(str(l if l is not None else min(o, c))), D(str(c)), D(v))


# ---------------------------------------------------------------- market data parsing
def test_parse_chart_drops_malformed_bars():
    data = {"chart": {"result": [{"meta": {}, "timestamp": [1, 61, 121, 181],
                                  "indicators": {"quote": [{"open": [10, None, 10, 10], "high": [11, 11, 9, 11],
                                                            "low": [9, 9, 10, 9], "close": [10.5, 10, 10, 10.2],
                                                            "volume": [100, 100, 100, None]}]}}]}}
    bars, _, dropped = parse_chart("X", data)
    assert len(bars) == 1 and dropped == 3  # null open, high<low, null volume
    assert bars[0].close == D("10.50")


def test_parse_chart_rejects_garbage():
    with pytest.raises(MarketDataError):
        parse_chart("X", {"chart": {"result": None, "error": {"code": "Not Found"}}})


def test_real_snapshot_is_parseable():
    f = Path(__file__).resolve().parent.parent / "data" / "replay" / "RELIANCE_1m.json"
    bars, meta, dropped = parse_chart("RELIANCE", json.loads(f.read_text()))
    assert len(bars) > 1000 and meta.get("exchangeName") == "NSI"
    assert all(b.low <= b.open <= b.high and b.low <= b.close <= b.high for b in bars)


def test_ohlc_path_uses_only_real_values():
    b = bar(0, 101, o=100, h=103, l=99)
    assert ohlc_path(b) == [D(100), D(99), D(103), D(101)]
    b2 = bar(0, 99, o=100, h=103, l=98)
    assert ohlc_path(b2) == [D(100), D(103), D(98), D(99)]


# ---------------------------------------------------------------- candle engine
async def test_candle_engine_builds_1m_and_5m():
    ce = CandleEngine()
    closed = []

    async def on_closed(sym, tf, b):
        closed.append((tf, b))

    ce.on_closed.append(on_closed)
    prices = [100, 102, 99, 101]
    for m in range(6):
        for j, p in enumerate(prices):
            await ce.on_tick(Tick("X", T0 + timedelta(minutes=m, seconds=15 * j), D(p + m), D(10), "t"))
        await ce.close_minute("X", T0 + timedelta(minutes=m))
    one = [b for tf, b in closed if tf == "1m"]
    five = [b for tf, b in closed if tf == "5m"]
    assert len(one) == 6
    assert one[0].open == D(100) and one[0].high == D(102) and one[0].low == D(99) and one[0].close == D(101)
    assert one[0].volume == D(40)
    # 03:45 is a 5-minute boundary: minutes 0-4 form one 5m candle
    assert len(five) == 1 and five[0].open == D(100) and five[0].close == D(105) and five[0].volume == D(200)
    assert five[0].high == D(106) and five[0].low == D(99)


async def test_candle_engine_records_gaps_without_inventing_candles():
    ce = CandleEngine()
    await ce.on_tick(Tick("X", T0, D(100), D(1), "t"))
    await ce.on_tick(Tick("X", T0 + timedelta(minutes=4), D(101), D(1), "t"))
    assert len(ce.candles("X", "1m", include_working=False)) == 1
    assert ce.gaps["X"] == 3


async def test_candle_engine_ignores_out_of_order_ticks():
    ce = CandleEngine()
    await ce.on_tick(Tick("X", T0 + timedelta(minutes=1), D(100), D(1), "t"))
    await ce.on_tick(Tick("X", T0, D(50), D(1), "t"))
    assert ce.candles("X", "1m")[-1].low == D(100)


# ---------------------------------------------------------------- indicators
def test_ema_seeded_with_sma():
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None] and out[2] == 2.0 and out[3] == pytest.approx(3.0) and out[4] == pytest.approx(4.0)


def test_rsi_extremes():
    assert rsi(list(range(1, 30)), 14)[-1] == 100.0
    assert rsi(list(range(30, 1, -1)), 14)[-1] == pytest.approx(0.0)


def test_macd_histogram_is_line_minus_signal():
    vals = [100 + (i % 7) * 0.5 + i * 0.1 for i in range(80)]
    line, sig, hist = macd(vals, 12, 26, 9)
    assert hist[-1] == pytest.approx(line[-1] - sig[-1])


# ---------------------------------------------------------------- strategies
def test_macd_strategy_signals_on_crossovers():
    closes = [100.0] * 40 + [100 - i * 0.5 for i in range(1, 25)] + [88 + i * 0.8 for i in range(1, 30)]
    s = MacdStrategy("macd", "X")
    sigs = [s.on_candle(bar(i, c)).signal for i, c in enumerate(closes)]
    assert BUY in sigs
    first_buy = sigs.index(BUY)
    assert first_buy > 64  # only after the down-leg reverses
    assert sigs.count(BUY) == 1


def test_breakout_strategy_uses_real_levels():
    s = BreakoutStrategy("b", "X", {"lookback": 10, "confirmation": 1, "buffer_pct": 0})
    for i in range(12):
        s.on_candle(bar(i, 100 + (i % 2), h=101.5, l=99))
    sig = s.on_candle(bar(12, 103, o=101, h=103.2, l=101))
    assert sig.signal == BUY and sig.indicators["resistance"] == 101.5
    assert s.on_candle(bar(13, 104, o=103, h=104.5, l=103)).signal == HOLD  # fires once per breakout
    sig = s.on_candle(bar(14, 98, o=100, h=100, l=97.5))
    assert sig.signal == SELL or sig.signal == HOLD  # support is min low of window


def test_rsi_ema_reports_real_indicator_values():
    s = RsiEmaStrategy("r", "X")
    for i in range(40):
        sig = s.on_candle(bar(i, 100 + i * 0.3))
    assert sig.indicators["ema_fast"] > sig.indicators["ema_slow"]
    assert 0 <= sig.indicators["rsi"] <= 100


@pytest.mark.parametrize("cls,params", [
    (MacdStrategy, {"fast": 30, "slow": 26}),
    (RsiEmaStrategy, {"oversold": 70, "overbought": 60}),
    (RsiEmaStrategy, {"unknown": 1}),
    (BreakoutStrategy, {"lookback": 1}),
])
def test_parameter_validation(cls, params):
    with pytest.raises(ValueError):
        cls.validate_parameters(params)


# ---------------------------------------------------------------- position / P&L math
@pytest.mark.parametrize("qty,avg,delta,price,exp", [
    (0, 0, 100, 100, (100, 100, 0)),                      # open long
    (100, 100, 100, 110, (200, 105, 0)),                  # add -> weighted average
    (200, 105, -50, 115, (150, 105, 500)),                # partial close books realized
    (150, 105, -200, 100, (-50, 100, -750)),              # flip to short
    (-50, 100, 50, 90, (0, 0, 500)),                      # cover short at profit
    (0, 0, -60, 100, (-60, 100, 0)),                      # open short
])
def test_apply_fill_math(qty, avg, delta, price, exp):
    q, a, r = apply_fill_math(D(qty), D(avg), D(delta), D(price))
    assert (q, a, r) == (D(exp[0]), D(exp[1]), D(exp[2]))


def test_unrealized():
    assert unrealized(D(-50), D(100), D(90)) == D(500)
    assert unrealized(D(10), D(100), None) == 0


def test_inr_indian_grouping():
    assert inr(1000000) == "₹10,00,000" and inr(50000) == "₹50,000" and inr(-8200) == "-₹8,200"


def test_order_intent_rejects_fractional_quantity():
    with pytest.raises(ValueError):
        OrderIntent("macd", "X", "BUY", D("1.5"))
    assert OrderIntent("macd", "X", "BUY", D("600.0000")).quantity == D(600)


# ---------------------------------------------------------------- real-time replay path
@pytest.mark.parametrize("o,h,l,c", [(100, 103, 99, 101), (100, 103, 98, 99), (100, 100, 100, 100), (1177.4, 1178.0, 1176.85, 1177.9)])
def test_interpolated_path_respects_real_bar(o, h, l, c):
    from app.market.providers import interpolated_path

    b = bar(0, c, o=o, h=h, l=l)
    path = interpolated_path(b, 60)
    prices = [p for p, _ in path]
    assert len(path) == 60
    assert prices[0] == b.open and prices[-1] == b.close           # starts at real open, ends at real close
    assert max(prices) == b.high and min(prices) == b.low           # real extremes are reached exactly
    assert all(b.low <= p <= b.high for p in prices)                # never outside the real traded range
    real = [p for p, is_real in path if is_real]
    assert real == ohlc_path(b)                                     # waypoints are the real O/L/H/C in order


def test_replay_tick_density_scales_with_speed():
    from app.market.providers import ReplayProvider

    p = ReplayProvider(["X"], None, None, "data/replay", speed=1.0)
    assert p.ticks_per_bar == 60 and p.step_s == pytest.approx(1.0)  # real time: 1 tick/second
    p.speed = 30
    assert p.ticks_per_bar == 20 and p.step_s == pytest.approx(0.1)
    p.speed = 600
    assert p.ticks_per_bar == 4                                      # never fewer than the 4 real points
