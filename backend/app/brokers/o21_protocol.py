"""Binary websocket protocol of the 021 developer API (API Guide, section Websockets).

Both sockets: binary frames, big-endian integers, prices in paise, every packet starts with a 2-byte
transaction code (TC). TC 10 [0x00, 0x0A] is a heartbeat. Exchange times are seconds since 1980-01-01 00:00 IST;
+315513000 gives Unix seconds.

Market socket (one frame = many packets back to back):
  TC 1  LTP, 12 bytes: TC(2) exch(2) token(4) ltp(4); in SNAPSHOT frames an ltpo/ltpc packet carries 5 extra bytes
        (b'o' or b'c' + 4-byte open / previous close) - detected by peeking: a new packet always starts with 0x00
  TC 3  Full, length by exchange: NSECM 220, NSEFO 234, NSEIDX 36, BSE (4/5/6) 262
  TC 9  Option chain: TC(2) token(4) exch(2) n(1) then n x (1-byte id + value; size by id)
Orders socket (one packet per frame):
  TC 4  NSE order event, 46 bytes + optional reason text
  TC 8  BSE order event, 32 bytes + optional reason text
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone

EPOCH_1980_IST = 315513000
HEARTBEAT = 10

# request exchange name -> (instruments/response name, websocket code)
EXCHANGES = {"NSE": ("NSECM", 1), "NSEFO": ("NSEFO", 2), "BSE": ("BSEEQ", 4), "BSEFO": ("BSEEQD", 5)}
WS_CODE = {"NSECM": 1, "NSEFO": 2, "NSEIDX": 3, "BSEEQ": 4, "BSEEQD": 5, "BSEIDX": 6}
FULL_LEN = {1: 220, 2: 234, 3: 36, 4: 262, 5: 262, 6: 262}
OC_FIELD_LEN = {1: 4, 2: 8, 3: 8, 4: 4, 7: 32, 8: 20, 9: 4}

ORDER_EVENTS = {1: "TRADE", 2: "ACCEPTED", 3: "REJECTED", 4: "SL_TRIGGERED", 5: "MODIFY_ACCEPTED",
                6: "MODIFY_REJECTED", 7: "CANCELLED", 8: "CANCEL_REJECTED", 9: "PRICE_CONFIRMATION"}


def exchange_time(t: int) -> datetime | None:
    return datetime.fromtimestamp(t + EPOCH_1980_IST, tz=timezone.utc) if t > 0 else None


class ProtocolError(ValueError):
    pass


@dataclass
class MarketPacket:
    tc: int
    exchange: int
    token: int
    ltp: int | None = None          # paise
    open: int | None = None
    high: int | None = None
    low: int | None = None
    close: int | None = None        # previous close
    volume: int | None = None       # cumulative day volume
    last_trade_qty: int | None = None
    last_trade_time: datetime | None = None
    exchange_time: datetime | None = None


def parse_market_frame(buf: bytes) -> list[MarketPacket]:
    """All packets of one market-socket frame. Unknown TCs abort the frame (their length is unknown)."""
    out: list[MarketPacket] = []
    i, n = 0, len(buf)
    while i + 2 <= n:
        tc = struct.unpack_from(">H", buf, i)[0]
        if tc == HEARTBEAT:
            i += 2
            continue
        if tc == 1:
            if i + 12 > n:
                raise ProtocolError("truncated LTP packet")
            _, ex, token, ltp = struct.unpack_from(">HHIi", buf, i)
            p = MarketPacket(1, ex, token, ltp=ltp)
            i += 12
            # snapshot-only ltpo/ltpc extension: 'o' or 'c' then a 4-byte price (a new packet starts with 0x00)
            if i + 5 <= n and buf[i] in (0x6F, 0x63):
                kind, price = buf[i], struct.unpack_from(">i", buf, i + 1)[0]
                if kind == 0x6F:
                    p.open = price
                else:
                    p.close = price
                i += 5
            out.append(p)
        elif tc == 3:
            if i + 4 > n:
                raise ProtocolError("truncated full packet header")
            ex = struct.unpack_from(">H", buf, i + 2)[0]
            size = FULL_LEN.get(ex)
            if size is None or i + size > n:
                raise ProtocolError(f"bad full packet (exchange {ex})")
            out.append(_parse_full(buf, i, ex))
            i += size
        elif tc == 9:
            if i + 9 > n:
                raise ProtocolError("truncated option-chain packet")
            j, cnt = i + 9, buf[i + 8]
            for _ in range(cnt):
                if j >= n:
                    raise ProtocolError("truncated option-chain fields")
                j += 1 + OC_FIELD_LEN.get(buf[j], 0)
            i = j  # option-chain data is not used by the platform
        else:
            raise ProtocolError(f"unknown market TC {tc}")
    return out


def _parse_full(b: bytes, i: int, ex: int) -> MarketPacket:
    u32 = lambda off: struct.unpack_from(">I", b, i + off)[0]  # noqa: E731
    s32 = lambda off: struct.unpack_from(">i", b, i + off)[0]  # noqa: E731
    u64 = lambda off: struct.unpack_from(">Q", b, i + off)[0]  # noqa: E731
    if ex == 1:  # NSECM
        return MarketPacket(3, ex, u32(4), ltp=s32(8), last_trade_qty=u32(12), volume=u64(20), close=s32(44),
                            open=s32(48), high=s32(52), low=s32(56), last_trade_time=exchange_time(u32(60)),
                            exchange_time=exchange_time(u32(216)))
    if ex == 2:  # NSEFO: packet id at 4, token at 6
        return MarketPacket(3, ex, u32(6), ltp=s32(10), last_trade_qty=u32(14), volume=u64(22), close=s32(46),
                            open=s32(50), high=s32(54), low=s32(58), last_trade_time=exchange_time(u32(62)),
                            exchange_time=exchange_time(u32(230)))
    if ex == 3:  # NSEIDX
        return MarketPacket(3, ex, u32(4), ltp=s32(8), close=s32(12), open=s32(16), high=s32(20), low=s32(24),
                            exchange_time=exchange_time(u32(28)))
    # BSE 4/5/6
    return MarketPacket(3, ex, u32(4), ltp=s32(8), last_trade_qty=int(u64(12)), volume=u64(24), close=s32(48),
                        open=s32(52), high=s32(56), low=s32(60), last_trade_time=exchange_time(u32(64)),
                        exchange_time=exchange_time(u32(244)))


@dataclass
class OrderEvent:
    tc: int
    status: int
    event: str
    ucc: str
    order_id: str
    quantity: int     # signed: negative = sell (fill quantity for trades)
    price: int        # paise (fill price for trades)
    symbol: str = ""
    exchange: int | None = None
    token: int | None = None
    reason: str = ""


def parse_order_frame(buf: bytes) -> OrderEvent | None:
    """One orders-socket frame. Returns None for heartbeats."""
    if len(buf) < 2:
        raise ProtocolError("short order frame")
    tc = struct.unpack_from(">H", buf, 0)[0]
    if tc == HEARTBEAT:
        return None
    text = lambda a, z: buf[a:z].split(b"\x00", 1)[0].decode("ascii", "replace").strip()  # noqa: E731
    if tc == 4:
        if len(buf) < 46:
            raise ProtocolError("short NSE order event")
        status = struct.unpack_from(">H", buf, 2)[0]
        oid, qty, price = struct.unpack_from(">Iii", buf, 34)
        return OrderEvent(4, status, ORDER_EVENTS.get(status, f"STATUS_{status}"), text(4, 14), str(oid), qty, price,
                          symbol=text(14, 24), reason=text(46, len(buf)))
    if tc == 8:
        if len(buf) < 32:
            raise ProtocolError("short BSE order event")
        status = struct.unpack_from(">H", buf, 2)[0]
        ex, token, oid, qty, price = struct.unpack_from(">HIIii", buf, 14)
        return OrderEvent(8, status, ORDER_EVENTS.get(status, f"STATUS_{status}"), text(4, 14), str(oid), qty, price,
                          exchange=ex, token=token, reason=text(32, len(buf)))
    raise ProtocolError(f"unknown order TC {tc}")
