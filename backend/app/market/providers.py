"""Market data providers behind one interface.

    MarketDataProvider
    ├── O21MarketProvider  - 021 market-data websocket (binary "full" packets: LTP, OHLC, volume, trade time)
    └── ManualProvider     - test-only feed driven explicitly by automated tests (mode "manual")

Both emit the same Tick callbacks into the same Candle Engine -> Strategy pipeline: 1m and 5m candles are built
by the platform from these ticks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import websockets

from app.brokers.o21_protocol import ProtocolError, parse_market_frame
from app.market.models import DELAYED, DISCONNECTED, LIVE, REPLAY, STOPPED, Bar, Tick

log = logging.getLogger("algotrade.market")
IST = ZoneInfo("Asia/Kolkata")
P100 = Decimal(100)
MAX_PER_CONNECTION = 100  # 021: 100 instruments per market connection

TickHandler = Callable[[Tick], Awaitable[None]]
BarCloseHandler = Callable[[str, datetime], Awaitable[None]]


def nse_market_open(now: datetime | None = None) -> bool:
    """Regular NSE session Mon-Fri 09:15-15:30 IST (exchange holidays are not modelled)."""
    n = (now or datetime.now(timezone.utc)).astimezone(IST)
    return n.weekday() < 5 and dtime(9, 15) <= n.time() <= dtime(15, 30)


@dataclass
class DayStats:
    """Session values from the exchange (full packet), in rupees."""
    open: Decimal | None = None
    prev_close: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: int = 0
    trade_day: object = None  # IST date the cumulative volume belongs to


class MarketDataProvider(ABC):
    mode: str = ""
    source_name: str = ""

    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self.status = STOPPED
        self.status_detail = ""
        self.on_tick: TickHandler | None = None
        self.on_bar_close: BarCloseHandler | None = None
        self.on_status: Callable[[str, str], Awaitable[None]] | None = None
        self.day: dict[str, DayStats] = {}
        self._task: asyncio.Task | None = None
        self._paused = asyncio.Event()
        self._paused.set()  # set == running
        self.outage_until = 0.0
        self.last_latency_ms: float | None = None
        self.last_error: str | None = None
        self.dropped_bars = 0

    async def _set_status(self, status: str, detail: str = "") -> None:
        changed = status != self.status or detail != self.status_detail
        self.status, self.status_detail = status, detail
        if changed and self.on_status:
            await self.on_status(status, detail)

    @abstractmethod
    async def _run(self) -> None: ...

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name=f"market-{self.mode}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        await self._set_status(STOPPED, "stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    @property
    def paused(self) -> bool:
        return not self._paused.is_set()

    def inject_outage(self, seconds: float) -> None:
        """Fault simulation: the feed stops delivering for `seconds`."""
        self.outage_until = time.time() + seconds

    def info(self) -> dict:
        return {"mode": self.mode, "source": self.source_name, "status": self.status, "detail": self.status_detail,
                "paused": self.paused, "latency_ms": self.last_latency_ms, "last_error": self.last_error,
                "dropped_malformed_bars": self.dropped_bars}


# --------------------------------------------------------------------------- 021 market websocket
class O21MarketProvider(MarketDataProvider):
    """Subscribes every symbol in `full` mode (NSECM, exchange code 1) and turns each update into a Tick.

    Updates arrive at most every 300 ms per changed instrument and carry the latest snapshot, not every trade: a
    tick is emitted when the LTP or the cumulative day volume changes; the tick's volume is the volume increase.

    Data integrity: the sandbox interleaves packets that cannot be current for the same token (last-trade time
    days old and frozen, cumulative volume going up and down, another session's open/close). A packet is
    accepted only if its last-trade time is not older than the newest accepted for that symbol and, within the same
    trade day, its cumulative volume does not decrease. Rejections are counted per reason (`rejected`). Ticks are
    stamped with the packet's last-trade time (the exchange-time field does not decode to a plausible date).

    Sandbox stream fallback: when no packet passed that filter for `sandbox_after_s` seconds (after hours the sandbox
    keeps streaming prices under a frozen, days-old trade time), the rejected packets are used anyway, stamped with
    their receive time and with source "021-sandbox" (counted in `sandbox_ticks`). Exchange-timed packets take over
    again as soon as one passes the filter. The session open/previous close are not taken from those packets.
    The socket is re-opened with a fresh ephemeral key after any drop (021 restarts all sockets at 08:00 IST and
    does not remember subscriptions), with exponential backoff.
    """
    mode = "LIVE"
    source_name = "021 market-data websocket (full packets)"

    def __init__(self, symbols: list[str], client, instruments, ws_url: str, sandbox_after_s: float = 0.0):
        super().__init__(symbols)
        self.client, self.instruments, self.ws_url = client, instruments, ws_url
        self.sandbox_after_s = sandbox_after_s
        self.sandbox_ticks = 0
        self._last_exchange: dict[str, float] = {}  # symbol -> monotonic time of the last packet that passed the filter
        self._sandbox_vol: dict[str, int] = {}
        self._connected_mono = time.monotonic()
        self.frames = 0
        self.connected_at: float | None = None
        self._last_price: dict[str, Decimal] = {}
        self.last_packet: dict[str, list[dict]] = {}  # diagnostics: latest raw packets per symbol
        self._newest: dict[str, tuple[datetime, int]] = {}  # symbol -> (last-trade time, volume) last accepted
        self.rejected: dict[str, int] = {"older_than_accepted": 0, "volume_decreased": 0, "no_trade_time": 0}

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                if self.status != DISCONNECTED:
                    await self._set_status(DELAYED, "connecting to the 021 market socket")
                await self.instruments.ensure(self.client)
                tokens = {}
                for s in self.symbols:
                    try:
                        tokens[self.instruments.nse(s).token] = s
                    except KeyError as e:
                        log.warning("%s", e)
                key = (await self.client.get("/websocket/ephemeral-key") or {}).get("token")
                async with websockets.connect(f"{self.ws_url}?token={key}", ping_interval=None, max_size=2 ** 22) as ws:
                    ids = [[1, t] for t in tokens]
                    for i in range(0, len(ids), MAX_PER_CONNECTION):
                        await ws.send(json.dumps({"Task": "subscribe", "Mode": "full",
                                                  "Instruments": ids[i:i + MAX_PER_CONNECTION]}, separators=(",", ":")))
                    self.connected_at, backoff = time.time(), 1.0
                    self._connected_mono = time.monotonic()
                    await self._set_status(LIVE, f"subscribed {len(tokens)} NSE instruments")
                    async for msg in ws:
                        if not isinstance(msg, (bytes, bytearray)) or not self._paused.is_set() or time.time() < self.outage_until:
                            continue
                        self.frames += 1
                        try:
                            packets = parse_market_frame(bytes(msg))
                        except ProtocolError as e:
                            self.dropped_bars += 1
                            self.last_error = str(e)
                            continue
                        for p in packets:
                            sym = tokens.get(p.token)
                            if sym and p.exchange == 1:
                                await self._on_packet(sym, p)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)
                await self._set_status(DISCONNECTED, f"021 market socket: {e}")
                log.warning("021 market socket: %s - reconnecting in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    async def _on_packet(self, sym: str, p) -> None:
        self.last_packet.setdefault(sym, [])
        if len(self.last_packet[sym]) >= 8:
            self.last_packet[sym].pop(0)
        self.last_packet[sym].append({"tc": p.tc, "ltp": p.ltp, "open": p.open, "close": p.close, "volume": p.volume,
                                 "ltq": p.last_trade_qty,
                                 "ltt": p.last_trade_time.isoformat() if p.last_trade_time else None,
                                 "exch_time": p.exchange_time.isoformat() if p.exchange_time else None,
                                 "rx": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        if not self._accept(sym, p):
            if self.sandbox_after_s and p.ltp and (time.monotonic() - self._last_exchange.get(sym, self._connected_mono)
                                                   >= self.sandbox_after_s):
                await self._on_sandbox_packet(sym, p)
            return
        self._last_exchange[sym] = time.monotonic()
        d = self.day.setdefault(sym, DayStats())
        rp = lambda v: (Decimal(v) / P100).quantize(Decimal("0.01")) if v else None  # noqa: E731
        if p.open:
            d.open = rp(p.open)
        if p.close:
            d.prev_close = rp(p.close)
        if p.high:
            d.high = rp(p.high)
        if p.low:
            d.low = rp(p.low)
        if not p.ltp:
            return
        price = rp(p.ltp)
        vol = p.volume if p.volume is not None else d.volume
        day = p.last_trade_time.astimezone(IST).date()
        if d.trade_day != day:  # a new trade day restarts the cumulative volume baseline
            d.trade_day, d.volume = day, 0
        dv = max(0, vol - d.volume) if d.volume else 0
        if price == self._last_price.get(sym) and dv == 0:
            return
        self._last_price[sym] = price
        d.volume = vol
        ts = p.last_trade_time
        if self.on_tick:
            await self.on_tick(Tick(sym, ts, price, Decimal(dv), "021"))

    def seed_newest(self, newest: dict[str, datetime]) -> None:
        """Start the integrity filter from the newest candle already built (after a restart a stale packet must not
        be the first one accepted)."""
        for sym, ts in newest.items():
            if sym not in self._newest or self._newest[sym][0] < ts:
                self._newest[sym] = (ts, 0)

    async def _on_sandbox_packet(self, sym: str, p) -> None:
        price = (Decimal(p.ltp) / P100).quantize(Decimal("0.01"))
        prev = self._sandbox_vol.get(sym)
        dv = max(0, p.volume - prev) if (p.volume is not None and prev is not None) else 0
        if p.volume is not None:
            self._sandbox_vol[sym] = p.volume
        if price == self._last_price.get(sym) and dv == 0:
            return
        self._last_price[sym] = price
        self.sandbox_ticks += 1
        if self.on_tick:
            await self.on_tick(Tick(sym, datetime.now(timezone.utc), price, Decimal(dv), "021-sandbox"))

    def _accept(self, sym: str, p) -> bool:
        ltt = p.last_trade_time
        if ltt is None:
            self.rejected["no_trade_time"] += 1
            return False
        prev = self._newest.get(sym)
        if prev is not None:
            if ltt < prev[0]:
                self.rejected["older_than_accepted"] += 1
                return False
            same_day = ltt.astimezone(IST).date() == prev[0].astimezone(IST).date()
            if same_day and p.volume is not None and p.volume < prev[1]:
                self.rejected["volume_decreased"] += 1
                return False
        self._newest[sym] = (ltt, p.volume if p.volume is not None else (prev[1] if prev else 0))
        return True

    def info(self) -> dict:
        d = super().info()
        d.update({"frames": self.frames, "connected_since": self.connected_at, "rejected_packets": dict(self.rejected),
                  "sandbox_ticks": self.sandbox_ticks, "sandbox_stream_after_s": self.sandbox_after_s,
                  "sandbox_stream_symbols": sorted(s for s in self.symbols if self.sandbox_after_s and time.monotonic()
                                                   - self._last_exchange.get(s, self._connected_mono) >= self.sandbox_after_s)})
        return d


# --------------------------------------------------------------------------- tests
class ManualProvider(MarketDataProvider):
    """Deterministic feed for automated tests only. Never selected unless market_data_mode == "manual"."""

    mode = "MANUAL"

    def __init__(self, symbols: list[str], start: datetime | None = None):
        super().__init__(symbols)
        self.source_name = "manual test feed"
        self.clock = start or datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc)

    async def _run(self) -> None:
        await self._set_status(REPLAY, self.source_name)
        await asyncio.Event().wait()

    async def push(self, symbol: str, price: Decimal | float | str, volume: int = 20000, advance_s: int = 15) -> Tick:
        p = Decimal(str(price))
        d = self.day.setdefault(symbol, DayStats())
        if d.open is None:
            d.open = p
        tick = Tick(symbol, self.clock, p, Decimal(volume), "manual", p, p)
        self.clock += timedelta(seconds=advance_s)
        if self.on_tick:
            await self.on_tick(tick)
        return tick

    async def bar(self, b: Bar) -> None:
        """Push a whole bar: open, high, low, close ticks inside its minute, then close it."""
        for k, px in enumerate((b.open, b.high, b.low, b.close)):
            if self.on_tick:
                await self.on_tick(Tick(b.symbol, b.ts + timedelta(seconds=10 * k), px, b.volume / 4, "manual", b.high, b.low))
        if self.on_bar_close:
            await self.on_bar_close(b.symbol, b.ts)
