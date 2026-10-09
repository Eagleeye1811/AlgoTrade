"""021 websocket binary protocol, built byte-for-byte from the API Guide layouts."""
from __future__ import annotations

import struct
from datetime import datetime, timezone

import pytest

from app.brokers.o21_protocol import EPOCH_1980_IST, ProtocolError, parse_market_frame, parse_order_frame


def t1980(dt: datetime) -> int:
    return int(dt.timestamp()) - EPOCH_1980_IST


def ltp_packet(ex=1, token=2885, ltp=141400, ext: bytes = b"") -> bytes:
    return struct.pack(">HHIi", 1, ex, token, ltp) + ext


def full_nsecm(token=2885, ltp=141400, ltq=5, vol=123456, close=139200, open_=140000, high=142000, low=139900,
               ltt=None, ext=None) -> bytes:
    b = bytearray(220)
    struct.pack_into(">HHIiIi", b, 0, 3, 1, token, ltp, ltq, 141000)
    struct.pack_into(">Q", b, 20, vol)
    struct.pack_into(">iiii", b, 44, close, open_, high, low)
    struct.pack_into(">I", b, 60, ltt or 0)
    struct.pack_into(">I", b, 216, ext or 0)
    return bytes(b)


def test_ltp_and_full_packets_back_to_back_with_heartbeat():
    trade = datetime(2026, 10, 9, 4, 0, 5, tzinfo=timezone.utc)  # 09:30:05 IST
    frame = ltp_packet() + b"\x00\x0a" + full_nsecm(ltt=t1980(trade), ext=t1980(trade)) + ltp_packet(token=11536, ltp=215450)
    a, b, c = parse_market_frame(frame)
    assert (a.tc, a.exchange, a.token, a.ltp) == (1, 1, 2885, 141400)
    assert (b.tc, b.token, b.ltp, b.volume, b.close, b.open, b.high, b.low) == (3, 2885, 141400, 123456, 139200, 140000, 142000, 139900)
    assert b.last_trade_time == trade and b.exchange_time == trade
    assert (c.token, c.ltp) == (11536, 215450)


def test_snapshot_ltpo_ltpc_extensions():
    frame = ltp_packet(ext=b"o" + struct.pack(">i", 140000)) + ltp_packet(token=7, ext=b"c" + struct.pack(">i", 139200))
    a, b = parse_market_frame(frame)
    assert a.open == 140000 and a.close is None
    assert b.close == 139200 and b.token == 7


def test_full_packet_lengths_by_exchange_and_option_chain_skip():
    nsefo = bytearray(234)
    struct.pack_into(">HHHIi", nsefo, 0, 3, 2, 1, 43210, 5000)
    idx = bytearray(36)
    struct.pack_into(">HHIi", idx, 0, 3, 3, 26000, 2500000)
    oc = struct.pack(">HIHB", 9, 43210, 2, 2) + b"\x01" + struct.pack(">i", 5000) + b"\x02" + struct.pack(">Q", 99)
    pk = parse_market_frame(bytes(nsefo) + oc + bytes(idx))
    assert [(p.exchange, p.token, p.ltp) for p in pk] == [(2, 43210, 5000), (3, 26000, 2500000)]


def test_truncated_or_unknown_packets_are_rejected():
    with pytest.raises(ProtocolError):
        parse_market_frame(full_nsecm()[:100])
    with pytest.raises(ProtocolError):
        parse_market_frame(struct.pack(">H", 77) + b"\x00" * 10)


def test_nse_and_bse_order_events():
    nse = bytearray(46)
    struct.pack_into(">HH", nse, 0, 4, 1)
    nse[4:14] = b"HACK1234  "
    nse[14:22] = b"RELIANCE"
    struct.pack_into(">Iii", nse, 34, 1042, -1, 148470)
    ev = parse_order_frame(bytes(nse))
    assert (ev.event, ev.ucc, ev.symbol, ev.order_id, ev.quantity, ev.price) == ("TRADE", "HACK1234", "RELIANCE", "1042", -1, 148470)
    rej = bytes(nse[:2]) + struct.pack(">H", 3) + bytes(nse[4:]) + b"Insufficient funds"
    assert parse_order_frame(rej).event == "REJECTED" and parse_order_frame(rej).reason == "Insufficient funds"
    bse = bytearray(32)
    struct.pack_into(">HH", bse, 0, 8, 7)
    bse[4:14] = b"HACK1234  "
    struct.pack_into(">HIIii", bse, 14, 4, 500325, 77, 5, 245000)
    ev = parse_order_frame(bytes(bse) + b"cancelled by user")
    assert (ev.event, ev.exchange, ev.token, ev.order_id, ev.quantity, ev.reason) == ("CANCELLED", 4, 500325, "77", 5, "cancelled by user")
    assert parse_order_frame(b"\x00\x0a") is None


async def test_market_provider_turns_full_packets_into_ticks():
    from decimal import Decimal as D
    from app.brokers.o21_protocol import MarketPacket
    from app.market.providers import O21MarketProvider
    p = O21MarketProvider(["RELIANCE"], None, None, "wss://unused")
    ticks = []

    async def on_tick(t):
        ticks.append(t)
    p.on_tick = on_tick
    t0 = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)
    pk = lambda ltp, vol: MarketPacket(3, 1, 2885, ltp=ltp, volume=vol, open=140000, close=139200, high=142000,  # noqa: E731
                                       low=139900, last_trade_time=t0)
    await p._on_packet("RELIANCE", pk(141400, 1000))   # snapshot: price, no volume delta yet
    await p._on_packet("RELIANCE", pk(141400, 1000))   # unchanged snapshot -> no tick
    await p._on_packet("RELIANCE", pk(141450, 1250))   # trade: price + 250 shares
    assert [(t.price, t.volume) for t in ticks] == [(D("1414.00"), D(0)), (D("1414.50"), D(250))]
    d = p.day["RELIANCE"]
    assert (d.open, d.prev_close, d.high, d.low) == (D("1400.00"), D("1392.00"), D("1420.00"), D("1399.00"))
    assert ticks[0].ts == t0 and ticks[0].source == "021"


async def test_market_provider_rejects_stale_interleaved_packets():
    """Seen on the sandbox: a frozen two-day-old stream with jittering price and non-monotonic volume interleaved
    with the live stream on the same token. Only the live stream may become ticks / day stats."""
    from decimal import Decimal as D
    from app.brokers.o21_protocol import MarketPacket
    from app.market.providers import O21MarketProvider
    p = O21MarketProvider(["TCS"], None, None, "wss://unused")
    ticks = []

    async def on_tick(t):
        ticks.append(t)
    p.on_tick = on_tick
    old = datetime(2026, 10, 7, 10, 29, 55, tzinfo=timezone.utc)   # 7 Oct 15:59:55 IST, frozen
    live = datetime(2026, 10, 9, 8, 5, 30, tzinfo=timezone.utc)    # 9 Oct 13:35:30 IST
    stale = lambda ltp, vol: MarketPacket(3, 1, 11536, ltp=ltp, volume=vol, open=208520, close=210000, last_trade_time=old)  # noqa: E731
    cur = lambda ltp, vol, s=0: MarketPacket(3, 1, 11536, ltp=ltp, volume=vol, open=208600, close=207600,  # noqa: E731
                                             last_trade_time=live.replace(second=30 + s))
    for pk in (stale(207987, 1349745), cur(217140, 8994715), stale(207960, 1349688), stale(207907, 1349790),
               cur(217150, 8994900, 2), cur(217140, 8994800, 3)):   # last one: volume went backwards
        await p._on_packet("TCS", pk)
    assert [t.price for t in ticks] == [D("2079.87"), D("2171.40"), D("2171.50")]
    assert ticks[1].volume == 0  # first packet of a new trade day: no volume carried over from the stale stream
    assert ticks[-1].volume == D(185) and ticks[-1].ts == live.replace(second=32)
    assert p.day["TCS"].open == D("2086.00") and p.day["TCS"].prev_close == D("2076.00")  # today's session
    assert p.rejected == {"older_than_accepted": 2, "volume_decreased": 1, "no_trade_time": 0}


async def test_sandbox_stream_fallback_after_hours():
    """No exchange-timed packet for `sandbox_after_s`: the sandbox's frozen-time stream is used, stamped on receipt;
    a packet that passes the filter switches back to exchange time immediately."""
    import time as _t
    from decimal import Decimal as D
    from app.brokers.o21_protocol import MarketPacket
    from app.market.providers import O21MarketProvider
    p = O21MarketProvider(["RELIANCE"], None, None, "wss://unused", sandbox_after_s=20)
    ticks = []

    async def on_tick(t):
        ticks.append(t)
    p.on_tick = on_tick
    live = datetime(2026, 10, 9, 9, 59, 0, tzinfo=timezone.utc)
    old = datetime(2026, 10, 7, 10, 29, 44, tzinfo=timezone.utc)
    await p._on_packet("RELIANCE", MarketPacket(3, 1, 1, ltp=117030, volume=5000, open=117900, last_trade_time=live))
    await p._on_packet("RELIANCE", MarketPacket(3, 1, 1, ltp=120637, volume=11880895, last_trade_time=old))
    assert len(ticks) == 1  # within 20 s of a real packet: the stale stream is still rejected
    p._last_exchange["RELIANCE"] = _t.monotonic() - 21
    before = datetime.now(timezone.utc)
    await p._on_packet("RELIANCE", MarketPacket(3, 1, 1, ltp=120637, volume=11880895, last_trade_time=old))
    await p._on_packet("RELIANCE", MarketPacket(3, 1, 1, ltp=120748, volume=11880826, last_trade_time=old))
    await p._on_packet("RELIANCE", MarketPacket(3, 1, 1, ltp=120796, volume=11880900, last_trade_time=old))
    assert [t.price for t in ticks[1:]] == [D("1206.37"), D("1207.48"), D("1207.96")]
    assert all(t.source == "021-sandbox" and t.ts >= before for t in ticks[1:])
    assert [t.volume for t in ticks[1:]] == [0, 0, 74]  # volume falling never counts
    assert p.day["RELIANCE"].open == D("1179.00")  # session open not taken from the stale stream
    assert p.sandbox_ticks == 3
    await p._on_packet("RELIANCE", MarketPacket(3, 1, 1, ltp=117100, volume=5100, last_trade_time=live.replace(second=5)))
    assert ticks[-1].source == "021" and ticks[-1].ts == live.replace(second=5)


async def test_restart_seeds_filter_from_newest_candle():
    """After a restart the first packet must not be a stale one: the filter starts from the newest saved candle."""
    from app.brokers.o21_protocol import MarketPacket
    from app.market.providers import O21MarketProvider
    p = O21MarketProvider(["RELIANCE"], None, None, "wss://unused", sandbox_after_s=20)
    ticks = []

    async def on_tick(t):
        ticks.append(t)
    p.on_tick = on_tick
    p.seed_newest({"RELIANCE": datetime(2026, 10, 9, 9, 59, tzinfo=timezone.utc)})
    await p._on_packet("RELIANCE", MarketPacket(3, 1, 1, ltp=120637, volume=11880895,
                                                last_trade_time=datetime(2026, 10, 7, 10, 29, 44, tzinfo=timezone.utc)))
    assert ticks == [] and p.rejected["older_than_accepted"] == 1
