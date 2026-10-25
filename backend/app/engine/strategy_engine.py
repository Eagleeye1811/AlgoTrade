"""Strategy Engine: runs strategies on closed candles, turns signals into order intents.

Strategies never touch the broker. Intents go to OrderEngine.submit(), which always runs the Risk Firewall.
A strategy row is a user's *subscription*: one strategy kind on one symbol with a fixed order quantity, its own
allocation, risk limits, position, orders and P&L. Many subscriptions share the one broker account.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update

from app.core.events import Event, new_id
from app.database.models import Order, Signal, Strategy as StrategyRow, StrategyAllocation, StrategyConfig, User
from app.engine.controller import RUNNING
from app.engine.types import OrderIntent
from app.market.models import Bar, Tick
from app.strategies.base import BUY, HOLD, SELL, Strategy, StrategySignal
from app.strategies.registry import REGISTRY

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

log = logging.getLogger("algotrade.strategy_engine")

# Subscriptions created for the first ADMIN at startup (settings.default_strategies picks which), with no capital
# allocated: the user sets each allocation. id -> (kind, symbol, quantity)
PRESETS: dict[str, tuple[str, str, int]] = {
    "time_entry": ("TIME_ENTRY", "TCS", 1),          # given strategy 1
    "open_breakout": ("OPEN_BREAKOUT", "RELIANCE", 1),  # given strategy 2
    "ema_trend": ("EMA_TREND", "RELIANCE", 10),       # our strategy (strategy 3)
    "rsi_ema": ("RSI_EMA", "RELIANCE", 100),
    "macd": ("MACD", "RELIANCE", 50),
    "breakout": ("BREAKOUT", "INFY", 100),
}


def default_strategies(settings) -> list[tuple[str, str, str, int]]:
    return [(sid, *PRESETS[sid]) for sid in settings.default_strategy_list if sid in PRESETS]


TRADING_ORIGINS = ("STRATEGY", "RUNAWAY")
EXIT_ORIGINS = ("STOP_LOSS", "TAKE_PROFIT", "SQUARE_OFF")
EXIT_RETRY_S = 5.0  # min gap between exit attempts per strategy (e.g. after a risk rejection)
ENTRY_RETRY_S = 5.0  # min gap between tick/clock entry attempts per strategy
EXIT_CHASE_S = 10.0  # an unfilled limit exit is cancelled after this and re-sent as a market order
FLATTEN_DEADLINE_S = 10.0  # circuit breaker: a halted strategy's position is closed within this
IST = timezone(timedelta(hours=5, minutes=30))
NO_NEW_ENTRIES_AFTER = dtime(15, 10)  # IST market time: entry signals after this only close positions
SQUARE_OFF_AT = dtime(15, 15)         # IST market time: every open intraday position is closed


@dataclass
class StrategyRuntime:
    id: str
    name: str
    kind: str
    symbol: str
    timeframe: str
    quantity: Decimal
    status: str
    status_reason: str | None
    params: dict[str, Any]
    config_version: int
    allocation: Decimal
    instance: Strategy
    user_id: int | None = None
    last_actionable: dict[str, Any] | None = None
    signal_count: int = 0
    runaway_task: asyncio.Task | None = field(default=None, repr=False)
    entries_today: int = 0          # filled entries on entries_date (IST); platform-enforced max_entries_per_day
    entries_date: Any = None
    in_position: bool = False       # last observed: position open (an entry is counted when it opens)
    exit_market: bool = False       # a limit exit went unfilled: the next exit is a market order
    flatten_task: asyncio.Task | None = field(default=None, repr=False)
    last_flatten: dict[str, Any] | None = None


class StrategyEngine:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self.runtimes: dict[str, StrategyRuntime] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._order_tasks: set[asyncio.Task] = set()
        self._exit_attempt: dict[str, float] = {}
        self._entry_attempt: dict[str, float] = {}
        self._clock: asyncio.Task | None = None

    # ------------------------------------------------------------------ persistence
    async def ensure_defaults(self) -> None:
        async with self.sys.db.transaction() as s:
            owner = (await s.execute(select(User.id).where(User.role == "ADMIN").order_by(User.id).limit(1))).scalar()
            for sid, kind, sym, qty in default_strategies(self.sys.settings):
                if await s.get(StrategyRow, sid) is None:
                    cls = REGISTRY[kind]
                    s.add(StrategyRow(id=sid, name=cls.display_name, kind=kind, symbol=sym, timeframe="1m",
                                      quantity=Decimal(qty), status="RUNNING", user_id=owner))
                    await s.flush()
                    s.add(StrategyConfig(strategy_id=sid, version=1, params=cls.defaults(), is_active=True))
                    # no capital is allocated automatically: the user sets it (Strategies -> Configure)
                    s.add(StrategyAllocation(strategy_id=sid, allocated_capital=Decimal(0)))

    async def load(self) -> None:
        async with self.sys.db.session() as s:
            rows = (await s.execute(select(StrategyRow).where(StrategyRow.status != "STOPPED")
                                    .order_by(StrategyRow.created_at))).scalars().all()
            cfgs = {c.strategy_id: c for c in (await s.execute(
                select(StrategyConfig).where(StrategyConfig.is_active.is_(True)))).scalars().all()}
            allocs = {a.strategy_id: a.allocated_capital for a in (await s.execute(select(StrategyAllocation))).scalars().all()}
            # today's filled entries survive a restart (one-entry-per-day strategies must not re-enter)
            day0 = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
            filled = dict((await s.execute(select(Order.strategy_id, func.count()).where(
                Order.created_at >= day0, Order.reduce_only.is_(False), Order.origin == "STRATEGY",
                Order.filled_quantity > 0).group_by(Order.strategy_id))).all())
        self.runtimes = {}
        for r in rows:
            cfg = cfgs.get(r.id)
            inst = self._instance(r.id, r.kind, r.symbol, r.timeframe, cfg.params if cfg else {})
            self.runtimes[r.id] = StrategyRuntime(r.id, r.name, r.kind, r.symbol, r.timeframe, r.quantity, r.status,
                                                  r.status_reason, inst.get_parameters(), cfg.version if cfg else 1,
                                                  allocs.get(r.id, Decimal(0)), inst, r.user_id)
            rt = self.runtimes[r.id]
            rt.entries_today, rt.entries_date = filled.get(r.id, 0), day0.date()
            rt.in_position = self.sys.positions.get(r.id, r.symbol).quantity != 0
        self.sys.health.beat("strategy_engine", detail=f"{len(self.runtimes)} strategies loaded")

    def _instance(self, sid: str, kind: str, symbol: str, tf: str, params: dict) -> Strategy:
        inst = REGISTRY[kind](sid, symbol, params)
        inst.enforce_hours = self.sys.settings.enforce_market_hours
        self._warm(inst, tf)
        return inst

    def _warm(self, inst: Strategy, tf: str) -> None:
        c = self.sys.market.candles
        inst.initialize(c.candles(inst.symbol, tf, limit=600, include_working=False),
                        c.candles(inst.symbol, "5m", limit=600, include_working=False))

    async def reinitialize_all(self) -> None:
        for rt in self.runtimes.values():
            self._warm(rt.instance, rt.timeframe)

    # ------------------------------------------------------------------ subscriptions
    def symbols(self) -> list[str]:
        prov = self.sys.market.provider
        return list(prov.symbols) if prov is not None and prov.symbols else self.sys.settings.symbol_list

    def unallocated(self, exclude: str | None = None) -> Decimal:
        cap = self.sys.account.starting_capital if self.sys.account else Decimal(0)
        return cap - sum((r.allocation for r in self.runtimes.values() if r.id != exclude), Decimal(0))

    async def subscribe(self, user_id: int, kind: str, symbol: str, quantity: Decimal, allocation: Decimal,
                        params: dict | None, actor: str | None) -> StrategyRuntime:
        """A user subscribes to a strategy on one symbol. It starts RUNNING (it trades whenever the platform does)."""
        cls = REGISTRY.get(kind)
        if cls is None:
            raise ValueError(f"unknown strategy {kind}")
        symbol = symbol.upper()
        if symbol not in self.symbols():
            raise ValueError(f"{symbol} is not a tradable symbol")
        if quantity <= 0 or quantity != quantity.to_integral_value():
            raise ValueError("quantity must be a positive whole number of shares")
        if self.sys.account is None:
            raise ValueError("configure the trading account first")
        if allocation < 0 or allocation > self.unallocated():
            raise ValueError(f"allocation must be between 0 and the unallocated capital ({self.unallocated():,.0f}); "
                             "0 = set it later with Configure")
        p = cls.validate_parameters(params or {})
        async with self.sys.db.transaction() as s:
            n = (await s.execute(select(func.count()).select_from(StrategyRow))).scalar_one() + 1
            sid = f"{kind.lower()[:12]}-{symbol.lower()[:10]}-{n}"
            s.add(StrategyRow(id=sid, name=cls.display_name, kind=kind, symbol=symbol, timeframe="1m",
                              quantity=quantity, status="RUNNING", user_id=user_id))
            await s.flush()
            s.add(StrategyConfig(strategy_id=sid, version=1, params=p, is_active=True, created_by=user_id))
            s.add(StrategyAllocation(strategy_id=sid, allocated_capital=allocation))
        await self.sys.limits.ensure_defaults({sid: allocation})
        inst = self._instance(sid, kind, symbol, "1m", p)
        rt = StrategyRuntime(sid, cls.display_name, kind, symbol, "1m", quantity, "RUNNING", None, inst.get_parameters(),
                             1, allocation, inst, user_id)
        self.runtimes[sid] = rt
        await self.sys.audit.record("STRATEGY_SUBSCRIBED", f"{actor or user_id} subscribed to {rt.name} on {symbol} "
                                    f"(qty {quantity}, allocation {allocation:,.0f})", source="StrategyEngine",
                                    actor=actor, strategy_id=sid, payload={"kind": kind, "params": p})
        await self.sys.bus.publish(Event("strategy.status", "StrategyEngine", strategy_id=sid,
                                         payload={"status": "RUNNING", "reason": "subscribed"}))
        return rt

    async def unsubscribe(self, strategy_id: str, actor: str | None) -> None:
        """Stop a subscription for good. Only when flat with no working orders: history (orders, fills, P&L) stays."""
        rt = self.runtimes[strategy_id]
        if self.sys.positions.get(rt.id, rt.symbol).quantity != 0:
            raise ValueError("close the open position first (pause the strategy and square off, or use the kill switch)")
        if any(o.strategy_id == rt.id for o in self.sys.orders.open_orders.values()):
            raise ValueError("strategy has working orders")
        await self.set_status(strategy_id, "STOPPED", f"unsubscribed by {actor}", actor=actor)
        if rt.runaway_task:
            rt.runaway_task.cancel()
        del self.runtimes[strategy_id]
        async with self.sys.db.transaction() as s:
            await s.execute(update(StrategyAllocation).where(StrategyAllocation.strategy_id == strategy_id)
                            .values(allocated_capital=0))

    # ------------------------------------------------------------------ candle intake
    async def on_candle_closed(self, symbol: str, tf: str, bar: Bar) -> None:
        for rt in self.runtimes.values():
            if rt.symbol != symbol:
                continue
            if tf == "5m":
                rt.instance.on_htf_candle(bar)  # before the 1m candle of the same minute is evaluated
            if rt.timeframe == tf and rt.instance.uses_candles:
                await self.queue.put((rt.id, bar))

    # ------------------------------------------------------------------ stop-loss / take-profit
    def exit_levels(self, rt: StrategyRuntime) -> dict[str, Decimal | None]:
        """Stop-loss / take-profit prices for the strategy's open position (None when flat or disabled)."""
        pos = self.sys.positions.get(rt.id, rt.symbol)
        out: dict[str, Decimal | None] = {"stop_loss": None, "take_profit": None}
        if pos.quantity == 0 or pos.avg_price <= 0:
            return out
        long = pos.quantity > 0
        sl, tp = Decimal(str(rt.params.get("stop_loss_pct", 0))), Decimal(str(rt.params.get("take_profit_pct", 0)))
        if sl > 0:
            out["stop_loss"] = (pos.avg_price * (1 - sl / 100 if long else 1 + sl / 100)).quantize(Decimal("0.01"))
        if tp > 0:
            out["take_profit"] = (pos.avg_price * (1 + tp / 100 if long else 1 - tp / 100)).quantize(Decimal("0.01"))
        return out

    async def on_tick(self, tick: Tick) -> None:
        """Every LTP update: stop-loss / take-profit exits of open positions, and entries of tick-driven strategies."""
        if self.sys.controller.status != RUNNING:
            return
        for rt in self.runtimes.values():
            if rt.symbol != tick.symbol or rt.status != "RUNNING":
                continue
            pos = self._observe(rt)
            if pos.quantity != 0:
                await self._check_exit(rt, pos, tick.price)
            elif rt.instance.uses_ticks and self._may_enter(rt, tick.ts):
                sig = rt.instance.on_tick(tick.price, tick.ts, self.sys.market.session_open(rt.symbol))
                if sig is not None:
                    self._entry_attempt[rt.id] = time.monotonic()
                    self._spawn(self.handle_signal(rt.id, sig.signal, sig.reason, sig.indicators, origin="STRATEGY",
                                                   order_type=sig.order_type, limit_price=sig.limit_price,
                                                   close_only=sig.close_only))

    def _observe(self, rt: StrategyRuntime):
        """Track the position: a flat -> open transition is one filled entry for today."""
        pos = self.sys.positions.get(rt.id, rt.symbol)
        today = self.sys.market.now().astimezone(IST).date()
        if rt.entries_date != today:
            rt.entries_date, rt.entries_today = today, 0
        if pos.quantity != 0 and not rt.in_position:
            rt.entries_today += 1
        if pos.quantity == 0:
            rt.exit_market = False
        rt.in_position = pos.quantity != 0
        return pos

    def _may_enter(self, rt: StrategyRuntime, now: datetime) -> bool:
        cls = rt.instance
        if cls.max_entries_per_day is not None and rt.entries_today >= cls.max_entries_per_day:
            return False
        if self.sys.settings.enforce_market_hours and now.astimezone(IST).time() >= NO_NEW_ENTRIES_AFTER \
                and not cls.uses_clock:
            return False
        if any(o.strategy_id == rt.id for o in self.sys.orders.open_orders.values()):
            return False
        return time.monotonic() - self._entry_attempt.get(rt.id, -1e9) >= ENTRY_RETRY_S

    async def _check_exit(self, rt: StrategyRuntime, pos, px: Decimal) -> None:
        long = pos.quantity > 0
        lv = self.exit_levels(rt)
        if lv["stop_loss"] is not None and (px <= lv["stop_loss"] if long else px >= lv["stop_loss"]):
            origin, level = "STOP_LOSS", lv["stop_loss"]
        elif lv["take_profit"] is not None and (px >= lv["take_profit"] if long else px <= lv["take_profit"]):
            origin, level = "TAKE_PROFIT", lv["take_profit"]
        else:
            return
        working = [o for o in self.sys.orders.open_orders.values() if o.strategy_id == rt.id]
        if working:
            # a limit exit that has not filled in EXIT_CHASE_S is cancelled; the next tick re-sends it at market
            for o in working:
                if o.origin in ("STOP_LOSS", "TAKE_PROFIT") and o.order_type == "LIMIT" \
                        and time.monotonic() - o.created_monotonic > EXIT_CHASE_S and not rt.exit_market:
                    rt.exit_market = True
                    self._spawn(self.sys.orders.cancel(o.order_id, f"exit limit unfilled after {EXIT_CHASE_S:.0f}s - "
                                                                   "re-sending at market"))
            return
        now = time.monotonic()
        if now - self._exit_attempt.get(rt.id, 0) < EXIT_RETRY_S:
            return
        self._exit_attempt[rt.id] = now
        side = SELL if long else BUY
        pct = rt.params["stop_loss_pct" if origin == "STOP_LOSS" else "take_profit_pct"]
        order_type, limit = "MARKET", None
        if rt.instance.exit_at_level and not rt.exit_market:
            # limit at the level, marketable if the LTP already went past it (sell: min, buy: max)
            order_type, limit = "LIMIT", (min(level, px) if side == SELL else max(level, px))
        reason = (f"{origin.replace('_', '-').lower()} hit: price {px:.2f} {'<=' if (origin == 'STOP_LOSS') == long else '>='} "
                  f"{level:.2f} ({pct:g}% from avg {pos.avg_price:.2f}) - closing {'long' if long else 'short'}"
                  + (f" with a limit at {limit:.2f}" if limit is not None else " at market"))
        ind = {"price": float(px), "avg_price": float(pos.avg_price), "position": float(pos.quantity),
               "level": float(level), "pct": pct}
        self._spawn(self.handle_signal(rt.id, side, reason, ind, origin=origin, order_type=order_type, limit_price=limit))

    # ------------------------------------------------------------------ clock: scheduled entries + square-off
    async def _clock_loop(self) -> None:
        while True:
            try:
                await self.on_clock(self.sys.market.now())
            except Exception:
                log.exception("strategy clock failed")
            await asyncio.sleep(1.0)

    async def on_clock(self, now: datetime) -> None:
        if self.sys.controller.status != RUNNING:
            return
        ist = now.astimezone(IST)
        for rt in list(self.runtimes.values()):
            if rt.status != "RUNNING":
                continue
            pos = self._observe(rt)
            if self.sys.settings.enforce_market_hours and ist.time() >= SQUARE_OFF_AT:
                await self._square_off(rt, pos, ist)
            elif rt.instance.uses_clock and pos.quantity == 0 and self._may_enter(rt, now):
                sig = rt.instance.on_clock(ist)
                if sig is not None:
                    self._entry_attempt[rt.id] = time.monotonic()
                    self._spawn(self.handle_signal(rt.id, sig.signal, sig.reason, sig.indicators, origin="STRATEGY",
                                                   order_type=sig.order_type, limit_price=sig.limit_price))

    async def _square_off(self, rt: StrategyRuntime, pos, ist: datetime) -> None:
        """15:15 IST: cancel the strategy's working orders, then close the full position with an opposite order
        sized from the broker's squareOffQuantity (never more than this strategy's own position)."""
        working = [o for o in self.sys.orders.open_orders.values() if o.strategy_id == rt.id]
        for o in working:
            if o.origin != "SQUARE_OFF" and o.client_order_id not in self.sys.orders._cancel_intent:
                self._spawn(self.sys.orders.cancel(o.order_id, f"square-off at {SQUARE_OFF_AT:%H:%M} IST: open order cancelled"))
        if working or pos.quantity == 0:
            return
        now = time.monotonic()
        if now - self._exit_attempt.get(rt.id, 0) < EXIT_RETRY_S:
            return
        self._exit_attempt[rt.id] = now
        qty, note = abs(pos.quantity), ""
        sq = await self.broker_square_off_quantity(rt.symbol)
        if sq is not None:
            note = f"; broker squareOffQuantity {sq:+}"
            if sq == 0 or (sq > 0) == (pos.quantity > 0):
                await self.sys.audit.record("SQUARE_OFF_SKIPPED", f"{rt.name} {rt.symbol}: own position {pos.quantity:+} but broker "
                                            f"squareOffQuantity {sq:+} - nothing to close at the broker; reconciliation requested",
                                            source="StrategyEngine", severity="WARN", strategy_id=rt.id, symbol=rt.symbol)
                await self.sys.reconciler.request("SQUARE_OFF")
                return
            qty = min(qty, Decimal(abs(sq)))
        side = SELL if pos.quantity > 0 else BUY
        reason = f"intraday square-off at {ist:%H:%M:%S} IST (scheduled {SQUARE_OFF_AT:%H:%M}): closing {pos.quantity:+}{note}"
        self._spawn(self.handle_signal(rt.id, side, reason, {"position": float(pos.quantity)}, origin="SQUARE_OFF",
                                       quantity=qty))

    async def broker_square_off_quantity(self, symbol: str) -> int | None:
        """squareOffQuantity of the account's position in `symbol` from GET /portfolio/positions (021 only)."""
        adapter = self.sys.broker.adapter
        if not hasattr(adapter, "square_off"):
            return None
        try:
            await self.sys.broker.call("get_positions", adapter.get_positions)
        except Exception as e:
            log.warning("square-off: positions read failed (%s) - using the platform's own position", e)
            return None
        return adapter.square_off.get(symbol, 0)

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(), name="strategy-engine")
        if self.sys.settings.strategy_clock and (self._clock is None or self._clock.done()):
            self._clock = asyncio.create_task(self._clock_loop(), name="strategy-clock")

    async def stop(self) -> None:
        tasks = [self._worker, self._clock, *self._order_tasks]
        tasks += [t for rt in self.runtimes.values() for t in (rt.flatten_task, rt.runaway_task)]
        tasks = [t for t in tasks if t is not None and not t.done()]
        self._worker = self._clock = None
        for t in tasks:
            t.cancel()
        # wait for them to unwind: a task dropped mid-transaction would keep its database connection (and locks) open
        if tasks:
            await asyncio.wait(tasks, timeout=5)

    async def _work(self) -> None:
        while True:
            sid, bar = await self.queue.get()
            rt = self.runtimes.get(sid)
            if rt is None:
                continue
            try:
                sig = rt.instance.on_candle(bar)
                await self.sys.bus.publish(Event("strategy.update", "StrategyEngine", strategy_id=sid, symbol=rt.symbol,
                                                 payload={"signal": sig.signal, "reason": sig.reason,
                                                          "indicators": sig.indicators, "candle": bar.to_dict()}))
                if sig.signal in (BUY, SELL):
                    self._spawn(self.handle_signal(sid, sig.signal, sig.reason, sig.indicators, origin="STRATEGY", bar=bar,
                                                   close_only=sig.close_only))
                self.sys.health.beat("strategy_engine", detail=f"{len(self.runtimes)} strategies, last candle {bar.symbol} "
                                                              f"{bar.ts.astimezone(IST):%H:%M} IST")
            except Exception as e:
                log.exception("strategy %s failed on candle", sid)
                self.sys.health.error("strategy_engine", f"{sid}: {e}", "WARNING")

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._order_tasks.add(t)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, t: asyncio.Task) -> None:
        self._order_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("signal handling failed", exc_info=t.exception())
            self.sys.health.error("strategy_engine", f"signal handling failed: {t.exception()}", "WARNING")

    # ------------------------------------------------------------------ signal -> intent
    async def handle_signal(self, strategy_id: str, side: str, reason: str, indicators: dict | None = None, *,
                            origin: str = "STRATEGY", bar: Bar | None = None, quantity: Decimal | None = None,
                            order_type: str = "MARKET", limit_price: Decimal | None = None,
                            idempotency_key: str | None = None, correlation_id: str | None = None,
                            close_only: bool = False) -> dict[str, Any]:
        rt = self.runtimes[strategy_id]
        sym = rt.symbol
        corr = correlation_id or new_id()
        pos = self.sys.positions.get(strategy_id, sym).quantity
        price = bar.close if bar is not None else self.sys.market.ltp(sym)
        qty, reduce_only, action = Decimal(0), False, "ORDER"
        ctl = self.sys.controller
        if origin in TRADING_ORIGINS and ctl.status != RUNNING:
            action = f"NOT_TRADED_{ctl.status}"
        elif origin in TRADING_ORIGINS and rt.status != "RUNNING":
            action = f"NOT_TRADED_STRATEGY_{rt.status}"
        elif quantity is not None:  # explicit quantity (square-off, test/demo order): no position logic
            qty, reduce_only = Decimal(quantity), origin in EXIT_ORIGINS
        elif (side == BUY and pos > 0) or (side == SELL and pos < 0):
            action = "IGNORED_ALREADY_LONG" if pos > 0 else "IGNORED_ALREADY_SHORT"
        elif pos != 0:  # opposite signal: close the whole position, never reverse in one order
            qty, reduce_only = abs(pos), True
        elif close_only:
            action = "IGNORED_EXIT_ONLY"  # entry conditions not met: this signal may only close a position
        elif (origin in TRADING_ORIGINS and bar is not None and self.sys.settings.enforce_market_hours
              and self._closes_at(bar, rt).astimezone(IST).time() >= NO_NEW_ENTRIES_AFTER):
            action = "IGNORED_AFTER_ENTRY_CUTOFF"
        elif rt.instance.max_entries_per_day is not None and rt.entries_today >= rt.instance.max_entries_per_day:
            action = "IGNORED_DAILY_ENTRY_LIMIT"
        else:
            qty = rt.quantity
        sig_id = uuid.uuid4()
        async with self.sys.db.transaction() as s:
            s.add(Signal(id=sig_id, strategy_id=strategy_id, symbol=sym, signal=side, price=price,
                         candle_time=bar.ts if bar is not None else None, indicators=indicators or {}, reason=reason,
                         origin=origin, action=action if action != "ORDER" else
                         ("ORDER_CLOSE" if reduce_only else "ORDER_OPEN"), correlation_id=corr))
        rt.signal_count += 1
        rt.last_actionable = {"signal": side, "reason": reason, "action": action, "price": price,
                              "candle_time": bar.ts.isoformat() if bar else None, "origin": origin,
                              "correlation_id": corr}
        await self.sys.audit.record("SIGNAL_GENERATED", f"{rt.name} {side} {sym}: {reason} -> {action}",
                                    source="StrategyEngine", correlation_id=corr, strategy_id=strategy_id,
                                    symbol=sym, payload={"indicators": indicators or {}, "origin": origin,
                                                               "action": action, "position_before": pos})
        await self.sys.bus.publish(Event("strategy.signal", "StrategyEngine", correlation_id=corr, strategy_id=strategy_id,
                                         symbol=sym, payload={"signal": side, "reason": reason, "action": action,
                                                                    "price": price, "origin": origin,
                                                                    "indicators": indicators or {},
                                                                    "candle_time": int(bar.ts.timestamp()) if bar else None}))
        if action != "ORDER" or qty <= 0:
            return {"action": action, "correlation_id": corr}
        intent = OrderIntent(strategy_id, sym, side, qty, order_type, limit_price, origin, reduce_only, corr,
                             sig_id, idempotency_key, reason)
        res = await self.sys.orders.submit(intent)
        res["action"] = action
        res["correlation_id"] = corr
        return res

    @staticmethod
    def _closes_at(bar: Bar, rt: StrategyRuntime):
        """A candle is stamped with its start; it closes one timeframe later."""
        return bar.ts + timedelta(minutes=5 if rt.timeframe == "5m" else 1)

    # ------------------------------------------------------------------ control
    async def set_status(self, strategy_id: str, status: str, reason: str | None, actor: str | None = None,
                         correlation_id: str | None = None) -> None:
        rt = self.runtimes[strategy_id]
        prev = rt.status
        rt.status, rt.status_reason = status, reason
        async with self.sys.db.transaction() as s:
            await s.execute(update(StrategyRow).where(StrategyRow.id == strategy_id).values(status=status, status_reason=reason))
        if status == "RUNNING":
            await self.sys.redis.reset_window(f"rejects:{strategy_id}")
        await self.sys.audit.record("STRATEGY_STATUS_CHANGED", f"{rt.name}: {prev} -> {status}" + (f" ({reason})" if reason else ""),
                                    source="StrategyEngine", actor=actor, strategy_id=strategy_id,
                                    correlation_id=correlation_id, severity="WARN" if status == "HALTED" else "INFO")
        await self.sys.bus.publish(Event("strategy.status", "StrategyEngine", strategy_id=strategy_id,
                                         payload={"status": status, "reason": reason}))

    async def update_config(self, strategy_id: str, params: dict | None, quantity: Decimal | None,
                            allocation: Decimal | None, user_id: int | None, actor: str | None) -> StrategyRuntime:
        rt = self.runtimes[strategy_id]
        cls = REGISTRY[rt.kind]
        new_params = cls.validate_parameters({**rt.params, **(params or {})})
        if quantity is not None and quantity <= 0:
            raise ValueError("quantity must be positive")
        if allocation is not None:
            free = self.unallocated(exclude=strategy_id)
            if allocation < 0 or allocation > free:
                raise ValueError(f"allocations would exceed total capital (at most {free:,.0f} available for this strategy)")
        changes = {}
        async with self.sys.db.transaction() as s:
            if new_params != rt.params:
                ver = (await s.execute(select(func.max(StrategyConfig.version)).where(
                    StrategyConfig.strategy_id == strategy_id))).scalar_one() or 0
                await s.execute(update(StrategyConfig).where(StrategyConfig.strategy_id == strategy_id).values(is_active=False))
                s.add(StrategyConfig(strategy_id=strategy_id, version=ver + 1, params=new_params, is_active=True,
                                     created_by=user_id))
                changes["params"] = {"from": rt.params, "to": new_params}
                rt.config_version = ver + 1
            if quantity is not None and quantity != rt.quantity:
                await s.execute(update(StrategyRow).where(StrategyRow.id == strategy_id).values(quantity=quantity))
                changes["quantity"] = {"from": str(rt.quantity), "to": str(quantity)}
                rt.quantity = quantity
            if allocation is not None and allocation != rt.allocation:
                await s.execute(update(StrategyAllocation).where(StrategyAllocation.strategy_id == strategy_id)
                                .values(allocated_capital=allocation))
                changes["allocation"] = {"from": str(rt.allocation), "to": str(allocation)}
                rt.allocation = allocation
                await self.sys.limits.set("STRATEGY", strategy_id, "max_exposure", allocation, user_id)
        if "params" in changes:
            rt.params = new_params
            rt.instance = self._instance(rt.id, rt.kind, rt.symbol, rt.timeframe, new_params)
        if changes:
            await self.sys.audit.record("STRATEGY_CONFIG_CHANGED", f"{rt.name} configuration changed: {', '.join(changes)}",
                                        source="StrategyEngine", actor=actor, strategy_id=strategy_id, payload=changes)
            await self.sys.bus.publish(Event("strategy.config", "StrategyEngine", strategy_id=strategy_id, payload=changes))
        return rt

    # ------------------------------------------------------------------ circuit breaker: halt and flatten
    def start_flatten(self, strategy_id: str, reason: str, correlation_id: str | None = None) -> asyncio.Task | None:
        rt = self.runtimes.get(strategy_id)
        if rt is None or (rt.flatten_task is not None and not rt.flatten_task.done()):
            return rt.flatten_task if rt else None
        rt.flatten_task = asyncio.create_task(self.flatten(strategy_id, reason, correlation_id), name=f"flatten-{strategy_id}")
        return rt.flatten_task

    async def flatten(self, strategy_id: str, reason: str, correlation_id: str | None = None,
                      deadline_s: float = FLATTEN_DEADLINE_S) -> dict[str, Any]:
        """A halted strategy must not keep risk on the account: cancel its working orders and close its position
        with a reduce-only market order, re-sent until flat or the deadline. Other strategies are untouched."""
        rt = self.runtimes[strategy_id]
        if rt.runaway_task:
            rt.runaway_task.cancel()
        t0 = time.monotonic()
        corr = correlation_id or new_id()
        out: dict[str, Any] = {"strategy_id": strategy_id, "cancelled": [], "close_orders": [], "flat": False}
        working = [o for o in self.sys.orders.open_orders.values() if o.strategy_id == strategy_id and o.origin != "CIRCUIT_BREAKER"]
        for o in working:
            try:
                await self.sys.orders.cancel(o.order_id, f"circuit breaker: {reason}")
                out["cancelled"].append(o.order_id)
            except Exception as e:
                log.warning("flatten %s: cancel %s failed: %s", strategy_id, o.order_id, e)
        last_close = -1e9
        while time.monotonic() - t0 < deadline_s:
            pos = self.sys.positions.get(strategy_id, rt.symbol)
            open_close = any(o.strategy_id == strategy_id and o.origin == "CIRCUIT_BREAKER" for o in self.sys.orders.open_orders.values())
            pending = any(o.strategy_id == strategy_id and o.origin != "CIRCUIT_BREAKER" for o in self.sys.orders.open_orders.values())
            if pos.quantity == 0 and not open_close and not pending:
                out["flat"] = True
                break
            if pos.quantity != 0 and not open_close and not pending and time.monotonic() - last_close >= 1.5:
                last_close = time.monotonic()
                side = SELL if pos.quantity > 0 else BUY
                res = await self.sys.orders.submit(OrderIntent(strategy_id, rt.symbol, side, abs(pos.quantity), "MARKET", None,
                                                               "CIRCUIT_BREAKER", True, corr, None, None,
                                                               f"halt and flatten: {reason}"))
                o = res.get("order") or {}
                out["close_orders"].append({"order_id": o.get("id"), "side": side, "quantity": abs(pos.quantity),
                                            "status": o.get("status"), "reason": o.get("reject_reason")})
            await asyncio.sleep(0.2)
        out["elapsed_s"] = round(time.monotonic() - t0, 2)
        out["position"] = self.sys.positions.get(strategy_id, rt.symbol).quantity
        await self.sys.audit.record(
            "STRATEGY_FLATTENED" if out["flat"] else "STRATEGY_FLATTEN_INCOMPLETE",
            f"{rt.name} halted and {'flattened' if out['flat'] else 'NOT fully flattened'} in {out['elapsed_s']}s: "
            f"{len(out['cancelled'])} order(s) cancelled, {len(out['close_orders'])} close order(s); position {out['position']:+}",
            source="StrategyEngine", severity="WARN" if out["flat"] else "CRITICAL", strategy_id=strategy_id,
            symbol=rt.symbol, correlation_id=corr, payload={k: v for k, v in out.items() if k != "position"})
        rt.last_flatten = out
        return out

    # ------------------------------------------------------------------ runaway simulation
    async def start_runaway(self, strategy_id: str, duration_s: float = 8.0, interval_s: float = 0.25,
                            correlation_id: str | None = None) -> list[dict]:
        """Fault simulation: strategy logic goes haywire and flips BUY/SELL signals continuously.
        The signals take the normal path; the Risk Firewall must contain it (rate limits, duplicate
        protection, circuit breaker auto-halt)."""
        rt = self.runtimes[strategy_id]
        results: list[dict] = []
        loop = asyncio.get_running_loop()
        end = loop.time() + duration_s
        side = BUY
        n = 0
        while loop.time() < end:
            n += 1
            res = await self.handle_signal(strategy_id, side, f"RUNAWAY signal #{n} (malfunctioning strategy loop)",
                                           {"runaway": True, "n": n}, origin="RUNAWAY", correlation_id=correlation_id)
            results.append(res)
            if rt.status != "RUNNING":
                break
            side = SELL if side == BUY else BUY
            await asyncio.sleep(interval_s)
        return results

    # ------------------------------------------------------------------ views
    def snapshot(self, rt: StrategyRuntime) -> dict[str, Any]:
        cls = REGISTRY[rt.kind]
        sig: StrategySignal = rt.instance.last_signal
        pos = self.sys.positions.get(rt.id, rt.symbol)
        pnl = (self.sys.pnl.last or self.sys.pnl.compute())["strategies"].get(rt.id, {})
        return {
            "id": rt.id, "name": rt.name, "kind": rt.kind, "description": cls.description, "symbol": rt.symbol,
            "user_id": rt.user_id,
            "timeframe": rt.timeframe, "quantity": rt.quantity, "status": rt.status, "status_reason": rt.status_reason,
            "params": rt.params, "param_specs": [p.to_dict() for p in cls.param_specs],
            "config_version": rt.config_version, "allocation": rt.allocation,
            "current_signal": {"signal": sig.signal, "reason": sig.reason, "indicators": sig.indicators,
                               "candle_time": sig.bar.ts.isoformat() if sig.bar else None},
            "last_signal": rt.last_actionable,
            "position": {"quantity": pos.quantity, "side": pos.side, "avg_price": pos.avg_price,
                         "realized_pnl": pos.realized_pnl},
            "ltp": self.sys.market.ltp(rt.symbol),
            "exits": self.exit_levels(rt),
            "pnl": pnl,
            "bars_loaded": len(rt.instance.bars),
            "bars_5m_loaded": len(rt.instance.bars5),
        }
