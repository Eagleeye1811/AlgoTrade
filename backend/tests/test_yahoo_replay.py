"""Market data mode "replay": recorded NSE sessions from the Yahoo Finance chart endpoint, replayed tick by tick."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.market.providers import YahooReplayProvider, parse_yahoo_chart

D = Decimal


def chart(minutes: list[tuple[datetime, float | None, float | None, float | None, float | None, int]]) -> dict:
    ts, o, h, l, c, v = zip(*minutes)
    return {"chart": {"result": [{"timestamp": [int(t.timestamp()) for t in ts],
                                  "indicators": {"quote": [{"open": list(o), "high": list(h), "low": list(l),
                                                            "close": list(c), "volume": list(v)}]}}]}}


# two sessions: 22 Oct 2026 (one candle) and 23 Oct 2026 (two candles + a minute without trades)
SESSIONS = chart([
    (datetime(2026, 10, 22, 9, 59, tzinfo=timezone.utc), 100, 101, 99, 100.5, 500),
    (datetime(2026, 10, 23, 3, 45, tzinfo=timezone.utc), 101, 103, 100, 102, 1000),
    (datetime(2026, 10, 23, 3, 46, tzinfo=timezone.utc), None, None, None, None, 0),
    (datetime(2026, 10, 23, 3, 47, tzinfo=timezone.utc), 102, 102.5, 98, 99, 2000),
])


def test_parse_yahoo_chart_skips_minutes_without_trades():
    bars = parse_yahoo_chart("RELIANCE", SESSIONS)
    assert [b.close for b in bars] == [D("100.50"), D("102.00"), D("99.00")]
    assert bars[1].ts == datetime(2026, 10, 23, 3, 45, tzinfo=timezone.utc) and bars[1].volume == D(1000)


async def test_replay_uses_latest_session_and_emits_ticks_inside_each_candle(tmp_path):
    async def fetch(sym):
        return SESSIONS

    p = YahooReplayProvider(["RELIANCE"], str(tmp_path), fetch=fetch)
    ticks = []

    async def on_tick(t):
        ticks.append(t)

    p.on_tick = on_tick
    await p.load()
    assert str(p.replay_day) == "2026-10-23"
    assert p.day["RELIANCE"].open == D("101.00") and p.day["RELIANCE"].prev_close == D("100.50")
    assert "23 Oct 2026" in p.source_name
    assert (tmp_path / "RELIANCE_1m.json").exists()  # cached for offline starts
    for _ in range(9):
        await p.step()
    # candle 1 (up): open, low, high, close; candle 2 (down): open, high, low, close; then the session loops
    assert [t.price for t in ticks] == [D(101), D(100), D(103), D(102), D(102), D("102.5"), D(98), D(99), D(101)]
    assert all(t.range_low <= t.price <= t.range_high and t.source == "yahoo-replay" for t in ticks)
    assert ticks[0].volume == D(250)


async def test_replay_falls_back_to_cache_when_yahoo_is_unreachable(tmp_path):
    (tmp_path / "RELIANCE_1m.json").write_text(json.dumps(SESSIONS))

    async def fetch(sym):
        raise OSError("network down")

    p = YahooReplayProvider(["RELIANCE"], str(tmp_path), fetch=fetch)
    await p.load()
    assert str(p.replay_day) == "2026-10-23" and len(p.bars["RELIANCE"]) == 2
    assert "network down" in p.last_error


async def test_replay_without_data_fails(tmp_path):
    async def fetch(sym):
        raise OSError("network down")

    with pytest.raises(RuntimeError, match="no recorded NSE session"):
        await YahooReplayProvider(["RELIANCE"], str(tmp_path), fetch=fetch).load()
