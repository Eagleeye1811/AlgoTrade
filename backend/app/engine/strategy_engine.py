"""Strategy Engine: runs strategies on closed candles, turns signals into order intents.

Strategies never touch the broker. Intents go to OrderEngine.submit(), which always runs the Risk Firewall.
Each strategy has an isolated id, config, allocation, positions, orders and P&L. A strategy trades every symbol in
the market universe: it keeps one indicator instance per symbol, and its allocation is shared across symbols.
`symbol` on the strategy row is its primary symbol (default chart, fault-lab target).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update

from app.core.events import Event, new_id
from app.database.models import Signal, Strategy as StrategyRow, StrategyAllocation, StrategyConfig
from app.engine.controller import RUNNING
from app.engine.types import OrderIntent
from app.market.models import Bar, Tick
from app.strategies.base import BUY, HOLD, SELL, Strategy, StrategySignal
from app.strategies.registry import REGISTRY

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

log = logging.getLogger("algotrade.strategy_engine")

# id, kind, symbol, quantity, allocation fraction of starting capital
DEFAULT_STRATEGIES = [
    ("rsi_ema", "RSI_EMA", "RELIANCE", 100, Decimal("0.4")),
    ("macd", "MACD", "RELIANCE", 50, Decimal("0.3")),
    ("breakout", "BREAKOUT", "INFY", 100, Decimal("0.3")),
]
TRADING_ORIGINS = ("STRATEGY", "RUNAWAY")
EXIT_ORIGINS = ("STOP_LOSS", "TAKE_PROFIT")
EXIT_RETRY_S = 5.0  # min gap between exit attempts per strategy (e.g. after a risk rejection)


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
    instances: dict[str, Strategy]  # symbol -> indicator state; primary symbol first
    last_actionable: dict[str, Any] | None = None
    signal_count: int = 0
    runaway_task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def instance(self) -> Strategy:
        """Primary-symbol instance."""
        return self.instances[self.symbol]

    @property
    def symbols(self) -> list[str]:
        return list(self.instances)


class StrategyEngine:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self.runtimes: dict[str, StrategyRuntime] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._order_tasks: set[asyncio.Task] = set()
        self._exit_attempt: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------ persistence
    async def ensure_defaults(self, starting_capital: Decimal) -> None:
        async with self.sys.db.transaction() as s:
            for sid, kind, sym, qty, frac in DEFAULT_STRATEGIES:
                if await s.get(StrategyRow, sid) is None:
                    cls = REGISTRY[kind]
                    s.add(StrategyRow(id=sid, name=cls.display_name, kind=kind, symbol=sym, timeframe="1m",
                                      quantity=Decimal(qty), status="RUNNING"))
                    await s.flush()
                    s.add(StrategyConfig(strategy_id=sid, version=1, params=cls.defaults(), is_active=True))
                    s.add(StrategyAllocation(strategy_id=sid, allocated_capital=(starting_capital * frac).quantize(Decimal(1))))

    async def load(self) -> None:
        async with self.sys.db.session() as s:
            rows = (await s.execute(select(StrategyRow).order_by(StrategyRow.created_at))).scalars().all()
            cfgs = {c.strategy_id: c for c in (await s.execute(
                select(StrategyConfig).where(StrategyConfig.is_active.is_(True)))).scalars().all()}
            allocs = {a.strategy_id: a.allocated_capital for a in (await s.execute(select(StrategyAllocation))).scalars().all()}
        self.runtimes = {}
        for r in rows:
            cfg = cfgs.get(r.id)
            insts = self._instances(r.id, r.kind, r.symbol, r.timeframe, cfg.params if cfg else {})
            self.runtimes[r.id] = StrategyRuntime(r.id, r.name, r.kind, r.symbol, r.timeframe, r.quantity, r.status,
                                                  r.status_reason, insts[r.symbol].get_parameters(),
                                                  cfg.version if cfg else 1, allocs.get(r.id, Decimal(0)), insts)
        self.sys.health.beat("strategy_engine", detail=f"{len(self.runtimes)} strategies loaded")

    def universe(self, primary: str) -> list[str]:
        prov = self.sys.market.provider
        syms = list(prov.symbols) if prov is not None and prov.symbols else self.sys.settings.symbol_list
        return [primary] + [x for x in syms if x != primary]

    def _instances(self, sid: str, kind: str, primary: str, tf: str, params: dict) -> dict[str, Strategy]:
        out: dict[str, Strategy] = {}
        for sym in self.universe(primary):
            inst = REGISTRY[kind](sid, sym, params)
            inst.initialize(self.sys.market.candles.candles(sym, tf, limit=600, include_working=False))
            out[sym] = inst
        return out

    async def reinitialize_all(self) -> None:
        for rt in self.runtimes.values():
            for sym, inst in rt.instances.items():
                inst.initialize(self.sys.market.candles.candles(sym, rt.timeframe, limit=600, include_working=False))

    # ------------------------------------------------------------------ candle intake
    async def on_candle_closed(self, symbol: str, tf: str, bar: Bar) -> None:
        for rt in self.runtimes.values():
            if symbol in rt.instances and rt.timeframe == tf:
                await self.queue.put((rt.id, bar))

    # ------------------------------------------------------------------ stop-loss / take-profit
    def exit_levels(self, rt: StrategyRuntime, symbol: str | None = None) -> dict[str, Decimal | None]:
        """Stop-loss / take-profit prices for the strategy's open position (None when flat or disabled)."""
        pos = self.sys.positions.get(rt.id, symbol or rt.symbol)
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
        if self.sys.controller.status != RUNNING:
            return
        for rt in self.runtimes.values():
            sym = tick.symbol
            if sym not in rt.instances or rt.status != "RUNNING":
                continue
            lv = self.exit_levels(rt, sym)
            if lv["stop_loss"] is None and lv["take_profit"] is None:
                continue
            pos = self.sys.positions.get(rt.id, sym)
            long, px = pos.quantity > 0, tick.price
            if lv["stop_loss"] is not None and (px <= lv["stop_loss"] if long else px >= lv["stop_loss"]):
                origin, level = "STOP_LOSS", lv["stop_loss"]
            elif lv["take_profit"] is not None and (px >= lv["take_profit"] if long else px <= lv["take_profit"]):
                origin, level = "TAKE_PROFIT", lv["take_profit"]
            else:
                continue
            if any(o.strategy_id == rt.id and o.symbol == sym for o in self.sys.orders.open_orders.values()):
                continue  # an order (possibly this exit) is already working
            now = time.monotonic()
            if now - self._exit_attempt.get((rt.id, sym), 0) < EXIT_RETRY_S:
                continue
            self._exit_attempt[(rt.id, sym)] = now
            pct = rt.params["stop_loss_pct" if origin == "STOP_LOSS" else "take_profit_pct"]
            side = SELL if long else BUY
            reason = (f"{origin.replace('_', '-').lower()} hit: price {px:.2f} {'<=' if (origin == 'STOP_LOSS') == long else '>='} "
                      f"{level:.2f} ({pct:g}% from avg {pos.avg_price:.2f}) - closing {'long' if long else 'short'}")
            ind = {"price": float(px), "avg_price": float(pos.avg_price), "level": float(level), "pct": pct,
                   "position": float(pos.quantity)}
            self._spawn(self.handle_signal(rt.id, side, reason, ind, origin=origin, symbol=sym))

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(), name="strategy-engine")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            self._worker = None
        for rt in self.runtimes.values():
            if rt.runaway_task:
                rt.runaway_task.cancel()

    async def _work(self) -> None:
        while True:
            sid, bar = await self.queue.get()
            rt = self.runtimes.get(sid)
            inst = rt.instances.get(bar.symbol) if rt else None
            if inst is None:
                continue
            try:
                sig = inst.on_candle(bar)
                await self.sys.bus.publish(Event("strategy.update", "StrategyEngine", strategy_id=sid, symbol=bar.symbol,
                                                 payload={"signal": sig.signal, "reason": sig.reason,
                                                          "indicators": sig.indicators, "candle": bar.to_dict()}))
                if sig.signal in (BUY, SELL):
                    self._spawn(self.handle_signal(sid, sig.signal, sig.reason, sig.indicators, origin="STRATEGY", bar=bar,
                                                   symbol=bar.symbol))
                self.sys.health.beat("strategy_engine", detail=f"{len(self.runtimes)} strategies, last candle {bar.symbol} "
                                                              f"{bar.ts:%H:%M}")
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
                            symbol: str | None = None) -> dict[str, Any]:
        rt = self.runtimes[strategy_id]
        sym = symbol or rt.symbol
        corr = correlation_id or new_id()
        pos = self.sys.positions.get(strategy_id, sym).quantity
        price = bar.close if bar is not None else self.sys.market.ltp(sym)
        qty, reduce_only, action = Decimal(0), False, "ORDER"
        ctl = self.sys.controller
        if origin in TRADING_ORIGINS and ctl.status != RUNNING:
            action = f"NOT_TRADED_{ctl.status}"
        elif origin in TRADING_ORIGINS and rt.status != "RUNNING":
            action = f"NOT_TRADED_STRATEGY_{rt.status}"
        elif quantity is not None:  # explicit test/demo order: no position logic
            qty = Decimal(quantity)
        elif side == BUY:
            if pos > 0:
                action = "IGNORED_ALREADY_LONG"
            elif pos < 0:
                qty, reduce_only = -pos, True
            else:
                qty = self.entry_quantity(rt, price)
        elif side == SELL:
            if pos < 0:
                action = "IGNORED_ALREADY_SHORT"
            elif pos > 0:
                qty, reduce_only = pos, True
            else:
                qty = self.entry_quantity(rt, price)
        if action == "ORDER" and qty <= 0:
            action = "NOT_TRADED_BUDGET"
        sig_id = uuid.uuid4()
        async with self.sys.db.transaction() as s:
            s.add(Signal(id=sig_id, strategy_id=strategy_id, symbol=sym, signal=side, price=price,
                         candle_time=bar.ts if bar is not None else None, indicators=indicators or {}, reason=reason,
                         origin=origin, action=action if action != "ORDER" else
                         ("ORDER_CLOSE" if reduce_only else "ORDER_OPEN"), correlation_id=corr))
        rt.signal_count += 1
        rt.last_actionable = {"symbol": sym, "signal": side, "reason": reason, "action": action, "price": price,
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

    def entry_quantity(self, rt: StrategyRuntime, price: Decimal | None) -> Decimal:
        """Shares for a new position: the configured quantity, capped so one position fits in the strategy's
        allocation split evenly across its symbols (prices range from ~₹300 to ~₹13,000)."""
        if price is None or price <= 0 or not rt.instances:
            return rt.quantity
        budget = rt.allocation / len(rt.instances)
        return min(rt.quantity, (budget / Decimal(price)).to_integral_value(rounding=ROUND_FLOOR))

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
            total_other = sum((r.allocation for r in self.runtimes.values() if r.id != strategy_id), Decimal(0))
            cap = self.sys.account.starting_capital if self.sys.account else Decimal(0)
            if allocation < 0 or total_other + allocation > cap:
                raise ValueError(f"allocations would exceed total capital (other strategies hold {total_other:,.0f} of {cap:,.0f})")
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
            rt.instances = self._instances(rt.id, rt.kind, rt.symbol, rt.timeframe, new_params)
        if changes:
            await self.sys.audit.record("STRATEGY_CONFIG_CHANGED", f"{rt.name} configuration changed: {', '.join(changes)}",
                                        source="StrategyEngine", actor=actor, strategy_id=strategy_id, payload=changes)
            await self.sys.bus.publish(Event("strategy.config", "StrategyEngine", strategy_id=strategy_id, payload=changes))
        return rt

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
        books = []
        for sym, inst in rt.instances.items():
            p = self.sys.positions.get(rt.id, sym)
            books.append({"symbol": sym, "signal": inst.last_signal.signal, "reason": inst.last_signal.reason,
                          "position": {"quantity": p.quantity, "side": p.side, "avg_price": p.avg_price,
                                       "realized_pnl": p.realized_pnl},
                          "ltp": self.sys.market.ltp(sym), "exits": self.exit_levels(rt, sym)})
        return {
            "id": rt.id, "name": rt.name, "kind": rt.kind, "description": cls.description, "symbol": rt.symbol,
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
            "symbols": rt.symbols,
            "books": books,
            "open_positions": sum(1 for b in books if b["position"]["quantity"] != 0),
        }
