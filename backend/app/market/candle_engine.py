"""Builds 1m / 5m OHLCV candles from ticks. Candles are never fabricated: gaps stay gaps."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Awaitable, Callable

from app.market.models import Bar, Tick

TIMEFRAMES = {"1m": 60, "5m": 300}


def bucket(ts: datetime, tf_s: int) -> datetime:
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % tf_s, tz=timezone.utc)


@dataclass
class _Working:
    bar: Bar


CandleCallback = Callable[[str, str, Bar], Awaitable[None]]


UPDATE_MIN_GAP_S = 0.2  # max 5 candle.update pushes per second per (symbol, timeframe)


class CandleEngine:
    def __init__(self, max_candles: int = 2000):
        self.closed: dict[tuple[str, str], deque[Bar]] = {}
        self.working: dict[tuple[str, str], Bar] = {}
        self.max_candles = max_candles
        self.on_closed: list[CandleCallback] = []
        self.on_update: CandleCallback | None = None
        self.gaps: dict[str, int] = {}
        self._last_update_push: dict[tuple[str, str], float] = {}
        self._trailing: dict[tuple[str, str], asyncio.Task] = {}

    def _series(self, symbol: str, tf: str) -> deque[Bar]:
        return self.closed.setdefault((symbol, tf), deque(maxlen=self.max_candles))

    def cancel_pending(self) -> None:
        for t in list(self._trailing.values()):
            t.cancel()
        self._trailing.clear()

    def reset(self) -> None:
        self.cancel_pending()
        self.closed.clear()
        self.working.clear()
        self.gaps.clear()

    def seed(self, symbol: str, bars: list[Bar]) -> None:
        """Load closed 1m history and derive 5m candles from it."""
        s1 = self._series(symbol, "1m")
        s5 = self._series(symbol, "5m")
        s1.clear()
        s5.clear()
        cur5: Bar | None = None
        for b in bars:
            s1.append(b)
            b5 = bucket(b.ts, 300)
            if cur5 is None or cur5.ts != b5:
                if cur5 is not None:
                    s5.append(cur5)
                cur5 = Bar(symbol, b5, b.open, b.high, b.low, b.close, b.volume)
            else:
                cur5 = replace(cur5, high=max(cur5.high, b.high), low=min(cur5.low, b.low), close=b.close,
                               volume=cur5.volume + b.volume)
        if cur5 is not None:
            last1 = bars[-1].ts
            if bucket(last1 + timedelta(minutes=1), 300) != cur5.ts:
                s5.append(cur5)  # 5m bucket complete
            else:
                self.working[(symbol, "5m")] = cur5  # partially formed bucket continues live

    async def on_tick(self, tick: Tick) -> None:
        for tf, secs in TIMEFRAMES.items():
            key = (tick.symbol, tf)
            b = bucket(tick.ts, secs)
            w = self.working.get(key)
            if w is not None and b > w.ts:
                await self._close(key, w)
                expected = w.ts + timedelta(seconds=secs)
                if b > expected:  # missing candles - recorded, never invented
                    self.gaps[tick.symbol] = self.gaps.get(tick.symbol, 0) + int((b - expected).total_seconds() // secs)
                w = None
            elif w is not None and b < w.ts:
                continue  # out-of-order tick for an already-advanced bucket: ignore
            # the source bar's real traded range (live: Yahoo's bar high/low; replay: range seen so far)
            hi = max(tick.price, tick.range_high) if tick.range_high is not None else tick.price
            lo = min(tick.price, tick.range_low) if tick.range_low is not None else tick.price
            if w is None:
                last = self._series(*key)
                if last and last[-1].ts >= b:
                    continue  # tick belongs to an already closed candle
                op = tick.bar_open if tick.bar_open is not None else tick.price
                w = Bar(tick.symbol, b, op, max(hi, op), min(lo, op), tick.price, tick.volume)
            else:
                w = replace(w, high=max(w.high, hi), low=min(w.low, lo), close=tick.price, volume=w.volume + tick.volume)
                if tick.bar_open is not None and bucket(tick.ts, 60) == w.ts:  # source bar opens this candle
                    w = replace(w, open=tick.bar_open, high=max(w.high, tick.bar_open), low=min(w.low, tick.bar_open))
            self.working[key] = w
            if self.on_update:
                now = time.monotonic()
                wait = UPDATE_MIN_GAP_S - (now - self._last_update_push.get(key, 0))
                if wait <= 0:
                    self._last_update_push[key] = now
                    await self.on_update(tick.symbol, tf, w)
                elif key not in self._trailing:
                    # throttled: still deliver the latest state of this burst once the gap has passed
                    self._trailing[key] = asyncio.create_task(self._push_later(key, wait))

    async def _push_later(self, key: tuple[str, str], delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            self._trailing.pop(key, None)
        w = self.working.get(key)  # None if the candle closed meanwhile (close_minute already pushed it)
        if w is not None and self.on_update:
            self._last_update_push[key] = time.monotonic()
            await self.on_update(key[0], key[1], w)

    async def close_minute(self, symbol: str, minute_ts: datetime) -> None:
        """Explicit bar-complete signal from the provider (no need to wait for the next tick)."""
        k1 = (symbol, "1m")
        w = self.working.get(k1)
        if w is not None and w.ts == bucket(minute_ts, 60):
            await self._close(k1, w)
            if self.on_update:
                await self.on_update(symbol, "1m", w)
        if (bucket(minute_ts, 60) + timedelta(minutes=1)) == bucket(minute_ts, 300) + timedelta(minutes=5):
            k5 = (symbol, "5m")
            w5 = self.working.get(k5)
            if w5 is not None and w5.ts == bucket(minute_ts, 300):
                await self._close(k5, w5)
                if self.on_update:
                    await self.on_update(symbol, "5m", w5)

    async def _close(self, key: tuple[str, str], bar: Bar) -> None:
        self.working.pop(key, None)
        series = self._series(*key)
        if series and series[-1].ts >= bar.ts:
            return
        series.append(bar)
        for cb in self.on_closed:
            await cb(key[0], key[1], bar)

    def candles(self, symbol: str, tf: str, limit: int = 500, include_working: bool = True) -> list[Bar]:
        out = list(self._series(symbol, tf))[-limit:]
        w = self.working.get((symbol, tf))
        if include_working and w is not None:
            out.append(w)
        return out

    def last_close(self, symbol: str) -> Decimal | None:
        w = self.working.get((symbol, "1m"))
        if w:
            return w.close
        s = self._series(symbol, "1m")
        return s[-1].close if s else None
