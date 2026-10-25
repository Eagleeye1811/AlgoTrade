"""Market data mode "angelone": Angel One SmartAPI quote packets, TOTP login and the replay fallback."""
from __future__ import annotations

import asyncio
import struct
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.market.angelone import AngelAuthError, AngelOneProvider, parse_quote, totp
from app.market.models import LIVE, REPLAY
from app.market.providers import YahooReplayProvider
from tests.test_yahoo_replay import SESSIONS

D = Decimal


def packet(token: str, ts: datetime, ltp: int, volume: int, o=0, h=0, lo=0, c=0, mode=2, exch=1) -> bytes:
    head = struct.pack("<BB25sqqq", mode, exch, token.encode(), 1, int(ts.timestamp() * 1000), ltp)
    body = struct.pack("<qqqddqqqq", 1, ltp, volume, 0.0, 0.0, o, h, lo, c)
    return head + body


def provider(tmp_path, **kw) -> AngelOneProvider:
    async def fetch(sym):
        return SESSIONS

    p = AngelOneProvider(["RELIANCE"], "key", "A1", "1234", "GEZDGNBVGY3TQOJQ", str(tmp_path / "tokens.json"),
                         YahooReplayProvider(["RELIANCE"], str(tmp_path), step_s=0.05, fetch=fetch), **kw)
    p.tokens = {"2885": "RELIANCE"}
    return p


def test_totp_matches_rfc6238():
    # RFC 6238 appendix B, SHA-1 secret "12345678901234567890" at T=59 s -> 94287082 (8 digits)
    assert totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", at=59, digits=8) == "94287082"
    assert totp("gezd gnbv gy3t qojq gezd gnbv gy3t qojq", at=59) == "287082"


def test_parse_quote_packet():
    ts = datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc)
    q = parse_quote(packet("2885", ts, 119000, 1500, 118400, 120190, 116310, 118450))
    assert q.token == "2885" and q.exchange == 1 and q.exchange_time == ts
    assert (q.ltp, q.volume, q.open, q.high, q.low, q.close) == (119000, 1500, 118400, 120190, 116310, 118450)
    assert parse_quote(packet("2885", ts, 1, 1)[:51]) is None  # LTP-only packet


async def test_live_quotes_become_ticks_and_earlier_sessions_are_ignored(tmp_path):
    p = provider(tmp_path)
    ticks = []

    async def on_tick(t):
        ticks.append(t)

    p.on_tick = on_tick
    now = datetime.now(timezone.utc)
    await p.on_quote(parse_quote(packet("2885", now - timedelta(days=3), 118000, 900)))  # last session's snapshot
    assert ticks == [] and p.earlier_session_packets == 1
    await p.on_quote(parse_quote(packet("2885", now, 119000, 1000, 118400, 120190, 116310, 118450)))
    await p.on_quote(parse_quote(packet("2885", now, 119000, 1000)))  # nothing changed
    await p.on_quote(parse_quote(packet("2885", now - timedelta(seconds=5), 119050, 1250)))  # older stamp
    await p.on_quote(parse_quote(packet("9999", now, 5000, 10)))  # not subscribed
    assert [(t.price, t.volume, t.source) for t in ticks] == [(D("1190.00"), D(0), "angelone"),
                                                              (D("1190.50"), D(250), "angelone")]
    assert ticks[1].ts >= ticks[0].ts  # never goes back in time
    d = p.day["RELIANCE"]
    assert (d.open, d.high, d.low, d.prev_close) == (D("1184.00"), D("1201.90"), D("1163.10"), D("1184.50"))
    assert p.mode == LIVE


async def test_market_closed_replays_the_recorded_session(tmp_path):
    p = provider(tmp_path, market_open=lambda: False)
    ticks = []

    async def on_tick(t):
        ticks.append(t)

    p.on_tick = on_tick
    await p.start()
    try:
        for _ in range(100):
            if len(ticks) >= 2:
                break
            await asyncio.sleep(0.05)
        assert p.mode == REPLAY and p.status == REPLAY and "market closed" in p.source_name
        assert ticks and all(t.source == "yahoo-replay" for t in ticks)
        assert p.day["RELIANCE"].open == D("101.00")  # the replayed session's values
    finally:
        await p.stop()
    assert not p.replay.running


async def test_rejected_login_falls_back_to_replay(tmp_path):
    async def login():
        raise AngelAuthError("Invalid totp (AB1050)")

    p = provider(tmp_path, market_open=lambda: True, login=login)
    await p.start()
    try:
        for _ in range(100):
            if p.status == REPLAY:
                break
            await asyncio.sleep(0.05)
        assert p.mode == REPLAY and "Angel One login failed: Invalid totp (AB1050)" in p.source_name
    finally:
        await p.stop()
