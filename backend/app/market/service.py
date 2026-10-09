"""MarketDataService: runs the provider, feeds the Candle Engine, tracks freshness, fans out ticks.

Modes: "o21" (021 market-data websocket, the only runtime source) and "manual" (automated tests).
021 has no candle-history endpoint, so every candle the platform builds is persisted (market_bars, source
"ticks") and the last days are reloaded at startup to warm indicators.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.events import Event
from app.core.health import FAILED, HEALTHY, WARNING
from app.database.models import MarketBar
from app.market.candle_engine import CandleEngine
from app.market.models import DELAYED, DISCONNECTED, LIVE, REPLAY, STALE, STOPPED, Bar, Tick
from app.market.providers import DayStats, ManualProvider, MarketDataProvider, O21MarketProvider, nse_market_open

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

log = logging.getLogger("algotrade.market.service")
TICK_PUSH_GAP_S = 0.25  # max 4 market.tick pushes per second per symbol
HISTORY_DAYS = 3
CLOSE_GRACE_S = 3.0  # a candle is closed by the clock this long after its period ends (late ticks still count)


class MarketDataService:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self.settings = system.settings
        self.candles = CandleEngine()
        self.provider: MarketDataProvider | None = None
        self.last: dict[str, tuple[Tick, float]] = {}
        self.tick_listeners: list[Callable[[Tick], Awaitable[None]]] = []
        self._last_tick_push: dict[str, float] = {}
        self._tick_trailing: dict[str, asyncio.Task] = {}
        self._last_status: str | None = None
        self._watchdog: asyncio.Task | None = None
        self.candles.on_update = self._on_candle_update
        self.candles.on_closed.append(self._persist)
        self.is_market_open: Callable[[], bool] = nse_market_open

    # ------------------------------------------------------------------ lifecycle
    def _make_provider(self) -> MarketDataProvider:
        syms = self.settings.symbol_list
        if self.settings.market_data_mode.lower() == "manual":
            return ManualProvider(syms)
        return O21MarketProvider(syms, self.sys.o21, self.sys.instruments, self.settings.o21_ws_market_url,
                                 self.settings.o21_sandbox_stream_after_s)

    async def start(self) -> None:
        prov = self._make_provider()
        prov.on_tick = self._on_tick
        prov.on_bar_close = self._on_bar_close
        prov.on_status = self._on_status
        self.provider = prov
        self.last.clear()
        self.candles.reset()
        history = await self._history(prov.symbols)
        for sym in prov.symbols:
            self.candles.seed(sym, history.get(sym, []))
        if isinstance(prov, O21MarketProvider):
            prov.seed_newest({sym: bars[-1].ts for sym, bars in history.items() if bars})
        await prov.start()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watch(), name="market-watchdog")
        await self.sys.audit.record("MARKET_DATA_STARTED", f"market data started ({prov.mode}): {prov.source_name}",
                                    source="MarketDataProvider", payload={"mode": prov.mode, "symbols": prov.symbols})

    async def stop(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None
        if self.provider:
            await self.provider.stop()
        self.candles.cancel_pending()
        for t in list(self._tick_trailing.values()):
            t.cancel()
        self._tick_trailing.clear()

    async def close(self) -> None:
        await self.stop()

    # ------------------------------------------------------------------ candle persistence (our own history)
    async def _history(self, symbols: list[str]) -> dict[str, list[Bar]]:
        since = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
        async with self.sys.db.session() as s:
            rows = (await s.execute(select(MarketBar).where(MarketBar.symbol.in_(symbols), MarketBar.interval == "1m",
                                                            MarketBar.ts >= since).order_by(MarketBar.ts))).scalars().all()
        out: dict[str, list[Bar]] = {}
        for r in rows:
            out.setdefault(r.symbol, []).append(Bar(r.symbol, r.ts, r.open, r.high, r.low, r.close, r.volume))
        return out

    async def _persist(self, symbol: str, tf: str, bar: Bar) -> None:
        if tf != "1m" or isinstance(self.provider, ManualProvider):
            return
        try:
            async with self.sys.db.transaction() as s:
                await s.execute(pg_insert(MarketBar).values(symbol=symbol, interval="1m", ts=bar.ts, open=bar.open,
                                                            high=bar.high, low=bar.low, close=bar.close,
                                                            volume=bar.volume, source="ticks").on_conflict_do_nothing())
        except Exception as e:  # never let persistence break the data path
            log.warning("could not persist candle %s %s: %s", symbol, bar.ts, e)

    # ------------------------------------------------------------------ data path
    async def _on_tick(self, tick: Tick) -> None:
        self.last[tick.symbol] = (tick, time.time())
        await self.candles.on_tick(tick)
        for listener in self.tick_listeners:
            try:
                await listener(tick)
            except Exception:
                log.exception("tick listener failed")
        wait = TICK_PUSH_GAP_S - (time.monotonic() - self._last_tick_push.get(tick.symbol, 0))
        if wait <= 0:
            await self._push_tick(tick.symbol)
        elif tick.symbol not in self._tick_trailing:
            # throttle UI pushes, but always publish the latest price of a burst
            self._tick_trailing[tick.symbol] = asyncio.create_task(self._push_tick_later(tick.symbol, wait))

    async def _push_tick(self, symbol: str) -> None:
        entry = self.last.get(symbol)
        if entry is None:
            return
        tick = entry[0]
        self._last_tick_push[symbol] = time.monotonic()
        await self.sys.redis.set_json(f"ltp:{symbol}", {"price": tick.price, "ts": tick.ts}, ttl_s=300)
        await self.sys.bus.publish(Event("market.tick", "MarketDataProvider", symbol=symbol, payload={
            "price": float(tick.price), "market_ts": tick.ts.isoformat(), "source": tick.source}))

    async def _push_tick_later(self, symbol: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            self._tick_trailing.pop(symbol, None)
        await self._push_tick(symbol)

    async def _on_bar_close(self, symbol: str, ts) -> None:
        await self.candles.close_minute(symbol, ts)

    async def close_due_candles(self, now: datetime | None = None) -> None:
        """Live feed: close candles whose period is over even if no tick of the next period arrived (a quiet
        stock would otherwise hold its candle - and the strategies - open)."""
        now = now or datetime.now(timezone.utc)
        for (sym, tf), w in list(self.candles.working.items()):
            span = timedelta(minutes=1 if tf == "1m" else 5)
            if now >= w.ts + span + timedelta(seconds=CLOSE_GRACE_S):
                # close_minute closes the 1m candle, and the 5m candle when called with its last minute
                await self.candles.close_minute(sym, w.ts if tf == "1m" else w.ts + timedelta(minutes=4))

    async def _on_candle_update(self, symbol: str, tf: str, bar: Bar) -> None:
        await self.sys.bus.publish(Event("candle.update", "CandleEngine", symbol=symbol,
                                         payload={"timeframe": tf, "candle": bar.to_dict()}))

    async def _on_status(self, status: str, detail: str) -> None:
        sev = "ERROR" if status == DISCONNECTED else "INFO"
        await self.sys.audit.record("MARKET_DATA_STATUS", f"market data {status}: {detail}", source="MarketDataProvider",
                                    severity=sev, payload={"status": status, "detail": detail})
        if status == DISCONNECTED:
            await self.sys.on_component_failure("market_data", detail)

    # ------------------------------------------------------------------ session values / freshness / status
    def now(self) -> datetime:
        """Market time: the test feed's clock in manual mode, the wall clock otherwise."""
        if isinstance(self.provider, ManualProvider):
            return self.provider.clock
        return datetime.now(timezone.utc)

    def day(self, symbol: str) -> DayStats:
        return self.provider.day.get(symbol, DayStats()) if self.provider else DayStats()

    def session_open(self, symbol: str) -> Decimal | None:
        """Today's open from the exchange (021 full packet)."""
        return self.day(symbol).open

    @property
    def stale_after(self) -> float:
        if isinstance(self.provider, ManualProvider):
            return self.settings.stale_after_s_manual
        return self.settings.stale_after_s_live

    def ltp(self, symbol: str) -> Decimal | None:
        t = self.last.get(symbol)
        return t[0].price if t else self.candles.last_close(symbol)

    def freshness(self, symbol: str) -> tuple[bool, float | None, str]:
        st = self.display_status()
        if st in (DISCONNECTED, STOPPED):
            return False, None, f"market data {st}"
        t = self.last.get(symbol)
        if t is None:
            return False, None, f"no tick received yet for {symbol}"
        age = time.time() - t[1]
        if age > self.stale_after:
            return False, age, f"last {symbol} tick {age:.1f}s ago (limit {self.stale_after:.0f}s)"
        return True, age, "fresh"

    def display_status(self) -> str:
        p = self.provider
        if p is None or not p.running:
            return STOPPED if (p is None or p.status != DISCONNECTED) else DISCONNECTED
        if p.status == DISCONNECTED:
            return DISCONNECTED
        if p.paused:
            return STALE
        if self.last:
            newest = max(w for _, w in self.last.values())
            if time.time() - newest > self.stale_after:
                return STALE
        return p.status

    def info(self) -> dict:
        p = self.provider
        d = p.info() if p else {"mode": None, "source": None}
        d["display_status"] = self.display_status()
        d["symbols"] = p.symbols if p else []
        d["market_open"] = self.is_market_open()
        d["stale_after_s"] = round(self.stale_after, 1)
        d["candle_gaps"] = self.candles.gaps
        d["ltp"] = {s: float(t.price) for s, (t, _) in self.last.items()}
        return d

    async def _watch(self) -> None:
        while True:
            try:
                if isinstance(self.provider, O21MarketProvider):
                    await self.close_due_candles()
                    await self.sys.redis.set_json("market:last_packets", self.provider.last_packet, ttl_s=60)
                st = self.display_status()
                if st != self._last_status:
                    self._last_status = st
                    await self.sys.bus.publish(Event("market.status", "MarketDataProvider", payload=self.info()))
                health = HEALTHY if st in (LIVE, REPLAY) else WARNING if st in (DELAYED, STALE) else FAILED
                if health == FAILED:
                    self.sys.health.error("market_data", f"{st}: {self.provider.status_detail if self.provider else ''}")
                else:
                    self.sys.health.beat("market_data", health, f"{st} - {self.provider.source_name}",
                                         self.provider.last_latency_ms if self.provider else None, success=health == HEALTHY)
            except Exception:
                log.exception("market watchdog failed")
            await asyncio.sleep(1.0)
