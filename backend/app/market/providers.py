"""Market data providers behind one interface.

    MarketDataProvider
    ├── LiveYahooProvider   - polls the real Yahoo chart endpoint during NSE hours
    ├── ReplayProvider      - replays REAL historical 1m NSE bars (never random prices)
    └── ManualProvider      - test-only feed driven explicitly by automated tests (mode "manual")

Both emit the same Tick / bar-close callbacks into the same Candle Engine -> Strategy pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database.models import MarketBar
from app.database.session import Database
from app.market.models import DELAYED, DISCONNECTED, LIVE, REPLAY, STOPPED, Bar, Tick
from app.market.yahoo import MarketDataError, YahooChartClient

log = logging.getLogger("algotrade.market")
IST = ZoneInfo("Asia/Kolkata")

TickHandler = Callable[[Tick], Awaitable[None]]
BarCloseHandler = Callable[[str, datetime], Awaitable[None]]


def nse_market_open(now: datetime | None = None) -> bool:
    """Regular NSE session Mon-Fri 09:15-15:30 IST (exchange holidays are not modelled)."""
    n = (now or datetime.now(timezone.utc)).astimezone(IST)
    return n.weekday() < 5 and dtime(9, 15) <= n.time() <= dtime(15, 30)


def ohlc_path(bar: Bar) -> list[Decimal]:
    """Deterministic intra-bar path through the REAL OHLC values (standard O-L-H-C / O-H-L-C convention)."""
    if bar.close >= bar.open:
        return [bar.open, bar.low, bar.high, bar.close]
    return [bar.open, bar.high, bar.low, bar.close]


def interpolated_path(bar: Bar, n: int) -> list[tuple[Decimal, bool]]:
    """n intra-bar prices walking the real O -> L/H -> H/L -> C waypoints.

    Waypoints are hit exactly (flag True); points between them are linear interpolations rounded to the
    0.05 tick and clamped to the bar's real [low, high] (flag False). Time spent on each leg is proportional
    to the price distance travelled, like a steadily moving market. Only 1m OHLC is available from the data
    source, so the true tick-by-tick path is unknown - completed candles still equal the real bar exactly.
    """
    way = ohlc_path(bar)
    n = max(n, 4)
    dist = [abs(b - a) for a, b in zip(way, way[1:])]
    total = sum(dist)
    steps = n - 1
    if total == 0:
        counts = [steps // 3 + (1 if i < steps % 3 else 0) for i in range(3)]
    else:
        counts = [max(1, int(steps * d / total)) for d in dist]
        while sum(counts) > steps:
            counts[counts.index(max(counts))] -= 1
        while sum(counts) < steps:
            counts[dist.index(max(dist))] += 1
    tick = Decimal("0.05")
    out: list[tuple[Decimal, bool]] = [(way[0], True)]
    for (a, b), c in zip(zip(way, way[1:]), counts):
        for k in range(1, c + 1):
            if k == c:
                out.append((b, True))
            else:
                v = a + (b - a) * Decimal(k) / Decimal(c)
                v = (v / tick).quantize(Decimal(1)) * tick
                out.append((min(max(v, bar.low), bar.high).quantize(Decimal("0.01")), False))
    return out


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
    async def history(self, symbol: str) -> list[Bar]: ...

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
        """Fault simulation: the upstream API stops answering for `seconds`."""
        self.outage_until = time.time() + seconds

    def info(self) -> dict:
        return {
            "mode": self.mode,
            "source": self.source_name,
            "status": self.status,
            "detail": self.status_detail,
            "paused": self.paused,
            "latency_ms": self.last_latency_ms,
            "last_error": self.last_error,
            "dropped_malformed_bars": self.dropped_bars,
        }


# --------------------------------------------------------------------------- replay
class ReplayProvider(MarketDataProvider):
    mode = "REPLAY"

    def __init__(self, symbols: list[str], db: Database, client: YahooChartClient | None, data_dir: str,
                 speed: float = 30.0, warmup_bars: int = 60):
        super().__init__(symbols)
        self.db = db
        self.client = client
        self.data_dir = Path(data_dir)
        self.speed = speed
        self.warmup_bars = warmup_bars
        self.bars: dict[str, dict[datetime, Bar]] = {}
        self.timeline: list[datetime] = []
        self.cursor = 0
        self.source_name = "REPLAY of real NSE 1m bars"
        self.on_cursor: Callable[[int], Awaitable[None]] | None = None
        self.ended = False

    # ---- data loading: PostgreSQL cache -> bundled real snapshot -> live API fetch
    async def load(self) -> None:
        loaded: dict[str, list[Bar]] = {}
        async with self.db.session() as s:
            for sym in self.symbols:
                rows = (await s.execute(
                    select(MarketBar).where(MarketBar.symbol == sym, MarketBar.interval == "1m").order_by(MarketBar.ts)
                )).scalars().all()
                if rows:
                    loaded[sym] = [Bar(sym, r.ts, r.open, r.high, r.low, r.close, r.volume) for r in rows]
        missing = [s for s in self.symbols if s not in loaded]
        for sym in missing:
            bars, src = await self._load_external(sym)
            if bars:
                loaded[sym] = bars
                await self._store(bars, src)
        if not loaded:
            raise MarketDataError("no replay data available (DB empty, no snapshot, API unreachable)")
        self.symbols = [s for s in self.symbols if s in loaded]
        self.bars = {s: {b.ts: b for b in bs} for s, bs in loaded.items()}
        self.timeline = sorted({ts for bs in self.bars.values() for ts in bs})
        self.source_name = f"REPLAY of real NSE 1m bars ({self.timeline[0].astimezone(IST):%d %b} - {self.timeline[-1].astimezone(IST):%d %b %Y})"
        log.info("replay loaded %d symbols, %d minutes", len(self.bars), len(self.timeline))

    async def _load_external(self, sym: str) -> tuple[list[Bar], str]:
        f = self.data_dir / f"{sym}_1m.json"
        if f.exists():
            from app.market.yahoo import parse_chart

            bars, _, dropped = parse_chart(sym, json.loads(f.read_text()))
            self.dropped_bars += dropped
            return bars, "yahoo_snapshot"
        if self.client is not None:
            try:
                bars, _, dropped = await self.client.fetch_bars(sym, "1m", "5d")
                self.dropped_bars += dropped
                return bars, "yahoo_api"
            except MarketDataError as e:
                log.warning("replay fetch failed for %s: %s", sym, e)
        return [], ""

    async def refresh_from_api(self) -> dict:
        """Fetch the latest 5 sessions of real 1m bars from the API into the cache (admin action)."""
        if self.client is None:
            raise MarketDataError("no API client configured")
        out = {}
        for sym in self.symbols:
            bars, _, dropped = await self.client.fetch_bars(sym, "1m", "5d")
            await self._store(bars, "yahoo_api")
            out[sym] = {"bars": len(bars), "dropped_malformed": dropped}
        return out

    async def _store(self, bars: list[Bar], source: str) -> None:
        if not bars:
            return
        async with self.db.transaction() as s:
            for i in range(0, len(bars), 500):
                chunk = bars[i:i + 500]
                stmt = pg_insert(MarketBar).values([
                    dict(symbol=b.symbol, interval="1m", ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close,
                         volume=b.volume, source=source) for b in chunk
                ])
                stmt = stmt.on_conflict_do_update(
                    index_elements=[MarketBar.symbol, MarketBar.interval, MarketBar.ts],
                    set_={c: stmt.excluded[c] for c in ("open", "high", "low", "close", "volume", "source")},
                )
                await s.execute(stmt)

    def set_cursor(self, cursor: int) -> None:
        self.cursor = max(self.warmup_bars, min(cursor, len(self.timeline)))
        self.ended = self.cursor >= len(self.timeline)

    async def history(self, symbol: str) -> list[Bar]:
        bs = self.bars.get(symbol, {})
        return [bs[t] for t in self.timeline[: self.cursor] if t in bs]

    @property
    def market_time(self) -> datetime | None:
        if not self.timeline:
            return None
        return self.timeline[min(self.cursor, len(self.timeline) - 1)]

    MAX_TICKS_PER_BAR = 60   # 1 tick per second at real-time speed
    MIN_STEP_S = 0.1         # never faster than 10 steps per second

    @property
    def ticks_per_bar(self) -> int:
        bar_s = 60.0 / max(self.speed, 0.1)
        return max(4, min(self.MAX_TICKS_PER_BAR, int(bar_s / self.MIN_STEP_S)))

    @property
    def step_s(self) -> float:
        return 60.0 / max(self.speed, 0.1) / self.ticks_per_bar

    async def _run(self) -> None:
        if self.cursor < self.warmup_bars:
            self.cursor = self.warmup_bars
        await self._set_status(REPLAY, self.source_name)
        loop = asyncio.get_running_loop()
        while self.cursor < len(self.timeline):
            await self._paused.wait()
            if time.time() < self.outage_until:
                await self._set_status(DISCONNECTED, "market-data API unreachable (injected outage)")
                while time.time() < self.outage_until:
                    await asyncio.sleep(0.2)
                await self._set_status(REPLAY, "reconnected after outage")
            ts = self.timeline[self.cursor]
            n = self.ticks_per_bar
            minute_bars = [self.bars[s][ts] for s in self.symbols if ts in self.bars[s]]
            paths = {b.symbol: interpolated_path(b, n) for b in minute_bars}
            seen_hi = {b.symbol: b.open for b in minute_bars}
            seen_lo = {b.symbol: b.open for b in minute_bars}
            next_at = loop.time()
            for j in range(n):
                if not self._paused.is_set():  # pause immediately (mid-bar); the bar resumes intact
                    await self._paused.wait()
                    next_at = loop.time()
                for b in minute_bars:
                    price, real = paths[b.symbol][j]
                    seen_hi[b.symbol] = max(seen_hi[b.symbol], price)
                    seen_lo[b.symbol] = min(seen_lo[b.symbol], price)
                    vol = (b.volume // n) if j < n - 1 else b.volume - (n - 1) * (b.volume // n)
                    tick = Tick(b.symbol, ts + timedelta(seconds=60 * j / n), price, vol,
                                "replay" if real else "replay-interpolated",
                                range_high=seen_hi[b.symbol], range_low=seen_lo[b.symbol])
                    if self.on_tick:
                        await self.on_tick(tick)
                step = 60.0 / max(self.speed, 0.1) / n  # re-read: speed may change mid-bar
                next_at += step
                await asyncio.sleep(max(0.0, next_at - loop.time()))
            for b in minute_bars:
                if self.on_bar_close:
                    await self.on_bar_close(b.symbol, ts)
            self.cursor += 1
            if self.on_cursor:
                await self.on_cursor(self.cursor)
        self.ended = True
        await self._set_status(STOPPED, "replay dataset finished - restart replay to continue")


# --------------------------------------------------------------------------- live
class LiveYahooProvider(MarketDataProvider):
    mode = "LIVE"

    def __init__(self, symbols: list[str], client: YahooChartClient, poll_interval_s: float = 4.0):
        super().__init__(symbols)
        self.client = client
        self.poll_interval_s = poll_interval_s
        self.source_name = "Yahoo Finance chart API (public endpoint, best-effort, may be delayed)"
        self.last_bar: dict[str, Bar] = {}
        self.failures = 0
        self._dropped: dict[str, int] = {}  # malformed bars in the latest response per symbol (not cumulative)

    async def history(self, symbol: str) -> list[Bar]:
        bars, _, dropped = await self.client.fetch_bars(symbol, "1m", "5d")
        self._dropped[symbol] = dropped
        if not bars:
            return []
        self.last_bar[symbol] = bars[-1]
        if (datetime.now(timezone.utc) - bars[-1].ts).total_seconds() < 120:
            return bars[:-1]  # newest bar is still forming; it is emitted live, not used as closed history
        return bars  # market closed / feed lagging: the newest bar is final

    @property
    def dropped_bars(self) -> int:  # type: ignore[override]
        return sum(self._dropped.values())

    @dropped_bars.setter
    def dropped_bars(self, v: int) -> None:
        pass

    async def _poll_symbol(self, sym: str) -> datetime | None:
        bars, _, dropped = await self.client.fetch_bars(sym, "1m", "1d")
        self._dropped[sym] = dropped
        self.last_latency_ms = self.client.last_latency_ms
        if not bars:
            return None
        prev = self.last_bar.get(sym)
        new = [b for b in bars if prev is None or b.ts >= prev.ts]
        for b in new:
            if prev is not None and b.ts == prev.ts:
                dv = max(b.volume - prev.volume, Decimal(0))
                if b.close != prev.close or dv > 0:
                    await self._emit(Tick(sym, b.ts, b.close, dv, "yahoo", b.high, b.low))
            else:
                if prev is not None and self.on_bar_close:
                    await self.on_bar_close(sym, prev.ts)
                await self._emit(Tick(sym, b.ts, b.open, Decimal(0), "yahoo", b.open, b.open))
                await self._emit(Tick(sym, b.ts, b.close, b.volume, "yahoo", b.high, b.low))
            prev = b
        self.last_bar[sym] = bars[-1]
        return bars[-1].ts

    async def _emit(self, tick: Tick) -> None:
        if self.on_tick:
            await self.on_tick(tick)

    async def _run(self) -> None:
        i = 0
        await self._set_status(DELAYED, "connecting")
        while True:
            await self._paused.wait()
            sym = self.symbols[i % len(self.symbols)]
            i += 1
            try:
                if time.time() < self.outage_until:
                    raise MarketDataError("market-data API unreachable (injected outage)")
                newest = await self._poll_symbol(sym)
                self.failures = 0
                self.last_error = None
                if newest is not None:
                    lag = (datetime.now(timezone.utc) - newest).total_seconds()
                    if lag <= 120:
                        await self._set_status(LIVE, f"newest bar {lag:.0f}s old")
                    else:
                        await self._set_status(DELAYED, f"newest bar {lag / 60:.1f} min old")
            except MarketDataError as e:
                self.failures += 1
                self.last_error = str(e)
                if self.failures >= 3:
                    await self._set_status(DISCONNECTED, str(e))
                await asyncio.sleep(min(30, self.poll_interval_s * self.failures))
                continue
            await asyncio.sleep(self.poll_interval_s)


# --------------------------------------------------------------------------- tests
class ManualProvider(MarketDataProvider):
    """Deterministic feed for automated tests only. Never selected unless market_data_mode == "manual"."""

    mode = "MANUAL"

    def __init__(self, symbols: list[str], start: datetime | None = None):
        super().__init__(symbols)
        self.source_name = "manual test feed"
        self.clock = start or datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc)

    async def history(self, symbol: str) -> list[Bar]:
        return []

    async def _run(self) -> None:
        await self._set_status(REPLAY, self.source_name)
        await asyncio.Event().wait()

    async def push(self, symbol: str, price: Decimal | float | str, volume: int = 20000, advance_s: int = 15) -> Tick:
        p = Decimal(str(price))
        tick = Tick(symbol, self.clock, p, Decimal(volume), "manual", p, p)
        self.clock += timedelta(seconds=advance_s)
        if self.on_tick:
            await self.on_tick(tick)
        return tick
