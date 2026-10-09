"""MarketDataService: selects the provider, feeds the Candle Engine, tracks freshness, fans out ticks."""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Awaitable, Callable

from app.core.events import Event
from app.core.health import FAILED, HEALTHY, WARNING
from app.market.candle_engine import CandleEngine
from app.market.models import DELAYED, DISCONNECTED, LIVE, REPLAY, STALE, STOPPED, Bar, Tick
from app.market.providers import LiveYahooProvider, ManualProvider, MarketDataProvider, ReplayProvider, nse_market_open
from app.market.yahoo import MarketDataError, YahooChartClient

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

log = logging.getLogger("algotrade.market.service")


class MarketDataService:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        s = system.settings
        self.settings = s
        self.client = YahooChartClient(s.yahoo_base_url, s.yahoo_timeout_s)
        self.candles = CandleEngine()
        self.provider: MarketDataProvider | None = None
        self.last: dict[str, tuple[Tick, float]] = {}
        self.tick_listeners: list[Callable[[Tick], Awaitable[None]]] = []
        self._last_tick_push: dict[str, float] = {}
        self._last_status: str | None = None
        self._watchdog: asyncio.Task | None = None
        self.candles.on_update = self._on_candle_update
        # automatic NSE session switching (only when market_data_mode == "auto")
        self.is_market_open: Callable[[], bool] = nse_market_open  # injectable clock for tests
        self.session_check_interval_s = 30.0
        self.override: str | None = None  # operator-forced mode, honoured until the next session boundary
        self._last_desired: str | None = None
        self._switch_lock = asyncio.Lock()
        self._scheduler: asyncio.Task | None = None
        self.last_switch: dict | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def auto(self) -> bool:
        return self.settings.market_data_mode.lower() == "auto"

    def _desired_mode(self) -> str:
        """Mode the clock asks for: live during NSE hours, otherwise replay of real past sessions."""
        return "live" if self.is_market_open() else "replay"

    def _choose_mode(self) -> str:
        if self.auto:
            return self.override or self._desired_mode()
        return self.settings.market_data_mode.lower()

    async def _make_provider(self, mode: str) -> MarketDataProvider:
        syms = self.settings.symbol_list
        if mode == "live":
            prov = LiveYahooProvider(syms, self.client, self.settings.live_poll_interval_s,
                                     self.settings.live_focus_poll_interval_s)
            prov.focus = self.sys.ws.focus_symbols
            return prov
        if mode == "manual":
            return ManualProvider(syms)
        prov = ReplayProvider(syms, self.sys.db, self.client, self.settings.replay_data_dir,
                              self.settings.replay_speed, self.settings.replay_warmup_bars)
        await prov.load()
        cursor = await self.sys.state.get("replay_cursor")
        prov.set_cursor(int(cursor) if cursor is not None else prov.warmup_bars)
        prov.on_cursor = self._on_cursor
        return prov

    async def start(self, mode: str | None = None) -> None:
        mode = mode or self._choose_mode()
        prov = await self._make_provider(mode)
        prov.on_tick = self._on_tick
        prov.on_bar_close = self._on_bar_close
        prov.on_status = self._on_status
        self.provider = prov
        self.last.clear()
        self.candles.reset()
        for sym in prov.symbols:
            try:
                self.candles.seed(sym, await prov.history(sym))
            except MarketDataError as e:
                self.sys.health.error("market_data", f"history for {sym} failed: {e}", WARNING)
        await prov.start()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watch(), name="market-watchdog")
        if self.auto:
            if self._last_desired is None:
                self._last_desired = self._desired_mode()
            if self._scheduler is None or self._scheduler.done():
                self._scheduler = asyncio.create_task(self._session_loop(), name="market-session-scheduler")
        await self.sys.audit.record("MARKET_DATA_STARTED", f"market data started in {prov.mode} mode: {prov.source_name}",
                                    source="MarketDataProvider", payload={"mode": prov.mode, "symbols": prov.symbols})

    # ------------------------------------------------------------------ automatic session switching
    async def _session_loop(self) -> None:
        while True:
            await asyncio.sleep(self.session_check_interval_s)
            try:
                await self.check_session()
            except Exception as e:
                log.exception("session check failed")
                self.sys.health.error("market_data", f"session switch failed: {e}")

    async def check_session(self) -> str | None:
        """One scheduler tick. Switches LIVE <-> REPLAY when the NSE session opens/closes. Returns new mode or None."""
        if not self.auto or self.provider is None:
            return None
        desired = self._desired_mode()
        if desired != self._last_desired:
            boundary = "NSE session opened" if desired == "live" else "NSE session closed"
            if self.override:
                await self.sys.audit.record("MARKET_OVERRIDE_CLEARED", f"{boundary}: operator override "
                                            f"'{self.override}' cleared", source="MarketDataProvider")
            self.override = None
            self._last_desired = desired
        target = self.override or desired
        if self.provider.mode.lower() == target:
            return None
        await self.switch_to(target, "NSE session opened" if target == "live" else "NSE session closed")
        return target

    async def switch_to(self, mode: str, reason: str) -> None:
        """Safe feed switch: block orders, cancel orders priced on the old feed, swap provider,
        re-warm strategies, reconcile, unblock. The provider swap never happens with orders allowed."""
        sys = self.sys
        async with self._switch_lock:
            old = self.provider.mode if self.provider else None
            await sys.controller.block("market_switch", f"switching market data {old} -> {mode.upper()} ({reason})")
            try:
                cancelled = []
                for o in list(sys.orders.open_orders.values()):
                    if o.origin == "KILL_SWITCH":
                        continue
                    try:
                        await sys.orders.cancel(o.order_id, f"cancelled: market data switching {old} -> {mode.upper()}")
                        cancelled.append(o.order_id[:8])
                    except Exception as e:
                        log.warning("could not cancel %s before feed switch: %s", o.order_id, e)
                if self.provider:
                    await self.provider.stop()
                await self.start(mode)
                await sys.strategies.reinitialize_all()
                open_pos = [f"{p.strategy_id} {p.symbol} {p.quantity:+}" for p in sys.positions.all() if p.quantity]
                self.last_switch = {"from": old, "to": self.provider.mode, "reason": reason,
                                    "cancelled_orders": cancelled, "carried_positions": open_pos}
                await sys.audit.record(
                    "MARKET_DATA_SWITCHED",
                    f"market data {old} -> {self.provider.mode} ({reason}); {len(cancelled)} open order(s) cancelled"
                    + (f"; positions carried over and now marked at {self.provider.mode} prices: {', '.join(open_pos)}"
                       if open_pos else ""),
                    source="MarketDataProvider", severity="WARN", payload=self.last_switch)
                if sys.account is not None:
                    await sys.reconciler.run("MARKET_SESSION_SWITCH")
            finally:
                await sys.controller.unblock("market_switch", f"- now {self.provider.mode if self.provider else '?'}")
            await sys.bus.publish(Event("market.status", "MarketDataProvider", payload=self.info()))

    async def stop(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None
        if self._scheduler and self._scheduler is not asyncio.current_task():
            self._scheduler.cancel()
            self._scheduler = None
        if self.provider:
            await self.provider.stop()
        self.candles.cancel_pending()

    async def close(self) -> None:
        await self.stop()
        await self.client.close()

    async def restart_replay(self, from_start: bool = True) -> None:
        if self.auto and self._desired_mode() == "live":
            # operator explicitly wants replay during market hours: honour it until the session closes
            self.override = "replay"
        if self.provider:
            await self.provider.stop()
        if from_start:
            await self.sys.state.set("replay_cursor", None)
        self.last.clear()
        await self.start("replay")
        await self.sys.strategies.reinitialize_all()

    def set_speed(self, speed: float) -> None:
        if isinstance(self.provider, ReplayProvider):
            self.provider.speed = max(1.0, min(speed, 600.0))

    async def _on_cursor(self, cursor: int) -> None:
        if cursor % 5 == 0:
            await self.sys.state.set("replay_cursor", cursor)

    # ------------------------------------------------------------------ data path
    async def _on_tick(self, tick: Tick) -> None:
        self.last[tick.symbol] = (tick, time.time())
        await self.candles.on_tick(tick)
        for listener in self.tick_listeners:
            try:
                await listener(tick)
            except Exception:
                log.exception("tick listener failed")
        now = time.monotonic()
        if now - self._last_tick_push.get(tick.symbol, 0) >= 0.25:
            self._last_tick_push[tick.symbol] = now
            await self.sys.redis.set_json(f"ltp:{tick.symbol}", {"price": tick.price, "ts": tick.ts}, ttl_s=300)
            await self.sys.bus.publish(Event("market.tick", "MarketDataProvider", symbol=tick.symbol, payload={
                "price": float(tick.price), "market_ts": tick.ts.isoformat(), "source": tick.source}))

    async def _on_bar_close(self, symbol: str, ts) -> None:
        await self.candles.close_minute(symbol, ts)

    async def _on_candle_update(self, symbol: str, tf: str, bar: Bar) -> None:
        await self.sys.bus.publish(Event("candle.update", "CandleEngine", symbol=symbol,
                                         payload={"timeframe": tf, "candle": bar.to_dict()}))

    async def _on_status(self, status: str, detail: str) -> None:
        sev = "ERROR" if status == DISCONNECTED else "INFO"
        await self.sys.audit.record("MARKET_DATA_STATUS", f"market data {status}: {detail}", source="MarketDataProvider",
                                    severity=sev, payload={"status": status, "detail": detail})
        if status == DISCONNECTED:
            await self.sys.on_component_failure("market_data", detail)

    # ------------------------------------------------------------------ freshness / status
    @property
    def stale_after(self) -> float:
        if isinstance(self.provider, ReplayProvider):
            # several missed ticks in a row = stale
            return max(self.settings.stale_after_s_replay, 5 * self.provider.step_s)
        if isinstance(self.provider, ManualProvider):
            return self.settings.stale_after_s_replay
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
        d["auto_switching"] = self.auto
        d["override"] = self.override
        d["last_switch"] = self.last_switch
        d["stale_after_s"] = round(self.stale_after, 1)
        d["candle_gaps"] = self.candles.gaps
        if isinstance(p, ReplayProvider):
            mt = p.market_time
            d.update({"speed": p.speed, "ticks_per_bar": p.ticks_per_bar, "cursor": p.cursor, "total_minutes": len(p.timeline),
                      "market_time": mt.isoformat() if mt else None, "ended": p.ended})
        if isinstance(p, LiveYahooProvider):
            d.update({"poll_interval_s": p.poll_interval_s, "focus_poll_interval_s": p.focus_interval_s,
                      "focus_symbols": p.current_focus()})
        d["ltp"] = {s: float(t.price) for s, (t, _) in self.last.items()}
        return d

    async def _watch(self) -> None:
        while True:
            try:
                st = self.display_status()
                if st != self._last_status:
                    self._last_status = st
                    await self.sys.bus.publish(Event("market.status", "MarketDataProvider", payload=self.info()))
                health = HEALTHY if st in (LIVE, REPLAY) else WARNING if st in (DELAYED, STALE) else FAILED
                lat = self.provider.last_latency_ms if self.provider else None
                if health == FAILED:
                    self.sys.health.error("market_data", f"{st}: {self.provider.status_detail if self.provider else ''}")
                else:
                    self.sys.health.beat("market_data", health, f"{st} - {self.provider.source_name}", lat,
                                         success=health == HEALTHY)
            except Exception:
                log.exception("market watchdog failed")
            await asyncio.sleep(1.0)
