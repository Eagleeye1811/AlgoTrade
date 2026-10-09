"""TradingSystem: composition root of the modular monolith + startup recovery sequence.

Recovery (startup, simulated restart, demo reset):
  1. load PostgreSQL state   2. start market data   3. restore strategy state (warm indicators)
  4. connect to broker       5. fetch broker orders/fills/positions + reconcile
  6. restore safe operating state (PAUSED or persisted SAFE MODE; BLOCK orders if reconciliation fails)
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from app.brokers.paper_broker import PaperBrokerSimulator
from app.config import Settings
from app.core.audit import Audit, StateStore
from app.core.events import Event, EventBus
from app.core.health import HEALTHY, WARNING, HealthRegistry
from app.core.redis_client import RedisClient
from app.core.ws import WSManager
from app.database.models import Order, StrategyAllocation, TradingAccount
from app.database.session import Database
from app.engine.broker_gateway import BrokerGateway
from app.engine.controller import PAUSED, RUNNING, SAFE_MODE, TradingController
from app.engine.order_engine import OrderEngine, order_to_dict
from app.engine.pnl_engine import PnLEngine
from app.engine.position_manager import PositionManager
from app.engine.risk_firewall import RiskFirewall
from app.engine.risk_limits import RiskLimitStore
from app.engine.strategy_engine import DEFAULT_STRATEGIES, StrategyEngine
from app.market.service import MarketDataService
from app.resilience.demo import DemoMode
from app.resilience.fault_simulator import FaultSimulator
from app.resilience.kill_switch import KillSwitch
from app.resilience.reconciliation import Reconciler

log = logging.getLogger("algotrade.system")
Report = Callable[..., Awaitable[None]]


async def _noop(*a, **k) -> None:
    return None


class TradingSystem:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_url)
        self.redis = RedisClient(settings.redis_url)
        self.bus = EventBus()
        self.audit = Audit(self.db, self.bus)
        self.state = StateStore(self.db)
        self.health = HealthRegistry()
        self.ws = WSManager(self.redis)
        self.bus.set_fanout(self.ws.fanout)

        self.account: TradingAccount | None = None
        self.controller = TradingController(self)
        self.limits = RiskLimitStore(self)
        self.market = MarketDataService(self)
        self.positions = PositionManager(self)
        self.orders = OrderEngine(self)
        self.risk = RiskFirewall(self)
        self.strategies = StrategyEngine(self)
        self.pnl = PnLEngine(self)
        adapter = PaperBrokerSimulator(
            self.db, seed=settings.broker_seed, base_latency_ms=settings.broker_base_latency_ms,
            jitter_ms=settings.broker_jitter_ms, max_participation=settings.broker_max_participation,
            slippage_bps=settings.broker_slippage_bps, timeout_s=settings.broker_timeout_s,
            margin_rate=settings.margin_rate)
        adapter.on_execution = self.orders.on_execution
        adapter.on_order_update = self.orders.on_order_update
        self.broker = BrokerGateway(self, adapter)
        self.reconciler = Reconciler(self)
        self.kill_switch = KillSwitch(self)
        self.faults = FaultSimulator(self)
        self.demo = DemoMode(self)

        # wiring: venue observes the market; strategies consume closed candles
        self.market.tick_listeners.append(adapter.on_market_tick)
        self.market.candles.on_closed.append(self.strategies.on_candle_closed)
        self.market.tick_listeners.append(self.strategies.on_tick)

        self._engine_tasks: list[asyncio.Task] = []
        self._global_tasks: list[asyncio.Task] = []
        self.started_at = time.time()
        self.last_recovery: dict[str, Any] | None = None

    # ------------------------------------------------------------------ process lifecycle
    async def startup(self) -> None:
        await self.redis.connect()
        await self.ws.start()
        await self.start_engine("STARTUP")
        self._global_tasks.append(asyncio.create_task(self._health_loop(), name="health"))

    async def shutdown(self) -> None:
        for t in self._global_tasks:
            t.cancel()
        await self.stop_engine()
        await self.market.close()
        await self.ws.stop()
        await self.redis.close()
        await self.db.dispose()

    # ------------------------------------------------------------------ engine lifecycle
    async def start_engine(self, trigger: str, report: Report = _noop) -> dict[str, Any]:
        t0 = time.perf_counter()
        steps: list[dict] = []

        async def rep(title: str, detail: str, status: str = "ok", data: dict | None = None):
            steps.append({"title": title, "detail": detail, "status": status})
            await report(title, detail, status, data)

        await self.controller.block("recovery", f"{trigger.lower()} recovery in progress")
        # 1. PostgreSQL state
        await self.db.ping()
        async with self.db.session() as s:
            self.account = (await s.execute(select(TradingAccount).order_by(TradingAccount.id))).scalars().first()
        await self.controller.load()
        await self.kill_switch.load()
        capital = self.account.starting_capital if self.account else self.settings.starting_capital
        await self.strategies.ensure_defaults(capital)
        async with self.db.session() as s:
            allocs = {a.strategy_id: a.allocated_capital for a in (await s.execute(select(StrategyAllocation))).scalars()}
        await self.limits.ensure_defaults(allocs)
        await self.positions.load()
        await self.orders.load()
        await rep("1. Loaded PostgreSQL state",
                  f"account {'configured' if self.account else 'NOT configured'}; {sum(1 for p in self.positions.all() if p.quantity)} "
                  f"open position(s); {len(self.orders.open_orders)} open/uncertain order(s); trading status {self.controller.status}")
        # 2. market data
        try:
            await self.market.start()
            await rep("2. Market data started", f"{self.market.provider.mode}: {self.market.provider.source_name}")
        except Exception as e:
            self.health.error("market_data", f"market data failed to start: {e}")
            await rep("2. Market data FAILED", str(e), "error")
        # 3. strategies
        await self.strategies.load()
        await rep("3. Strategy state restored", ", ".join(f"{r.name}: {r.status} (config v{r.config_version}, "
                                                            f"{len(r.instance.bars)} bars warm)" for r in self.strategies.runtimes.values()))
        # 4. broker
        rec_status = "SKIPPED"
        if self.account is None:
            await self.controller.block("account", "trading account not configured - complete Account Setup")
            await rep("4. Broker not connected", "no trading account configured yet", "warn")
        else:
            await self.controller.unblock("account")
            try:
                await self.broker.call("connect", lambda: self.broker.adapter.connect(self.account.broker_account_id))
                self.broker.start()
                await rep("4. Connected to broker", f"{self.broker.adapter.name} account {self.account.broker_account_id} "
                                                    f"(PAPER TRADING - SIMULATED EXECUTION)")
            except Exception as e:
                await rep("4. Broker connection FAILED", str(e), "error")
            # 5. reconcile
            rec = await self.reconciler.run(trigger)
            rec_status = rec.get("status", "FAILED")
            await rep(f"5. Fetched broker orders/positions & reconciled: {rec_status}", rec.get("summary", ""),
                      "ok" if rec_status in ("MATCHED", "MISMATCH_RESOLVED") else "error",
                      {"mismatches": rec.get("mismatches"), "actions": rec.get("actions")})
        # 6. safe operating state
        self.strategies.start()
        self._engine_tasks = [
            asyncio.create_task(self.pnl.run(), name="pnl"),
            asyncio.create_task(self.reconciler.periodic(), name="recon-periodic"),
            asyncio.create_task(self.orders.sweeper(), name="order-sweeper"),
        ]
        await self.controller.unblock("recovery", f"- {trigger.lower()} recovery complete")
        ok = rec_status in ("MATCHED", "MISMATCH_RESOLVED", "SKIPPED")
        await rep("6. Safe operating state restored",
                  f"trading {self.controller.status} ({self.controller.status_reason}); "
                  + ("new orders BLOCKED: " + "; ".join(self.controller.blocks.values()) if self.controller.blocks else "no order blocks"),
                  "ok" if ok else "error")
        self.last_recovery = {"trigger": trigger, "ok": ok, "reconciliation": rec_status, "steps": steps,
                              "duration_ms": int((time.perf_counter() - t0) * 1000), "at": time.time()}
        await self.audit.record("RECOVERY_COMPLETED", f"{trigger} recovery finished: reconciliation {rec_status}, trading "
                                f"{self.controller.status}", source="Recovery", severity="INFO" if ok else "ERROR",
                                payload=self.last_recovery)
        await self.controller.publish()
        return self.last_recovery

    async def stop_engine(self) -> None:
        for t in self._engine_tasks:
            t.cancel()
        for t in self._engine_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._engine_tasks = []
        self.reconciler.cancel_pending()
        await self.strategies.stop()
        await self.broker.stop()
        await self.market.stop()
        await self.broker.adapter.close()

    async def restart_engine(self, report: Report = _noop) -> dict[str, Any]:
        """Simulated process restart: stop everything and discard ALL in-memory trading state."""
        await self.audit.record("ENGINE_RESTART", "engine restart: discarding in-memory state", source="Recovery", severity="WARN")
        await self.stop_engine()
        self.positions.cache.clear()
        self.orders.open_orders.clear()
        self.orders.slippage.clear()
        self.strategies.runtimes.clear()
        self.controller.blocks.clear()
        self.risk.recent_identical.clear()
        self.market.candles.reset()
        self.market.last.clear()
        self.account = None
        await report("In-memory state discarded", "positions cache, open orders, strategies, candles, order blocks: empty",
                     "warn")
        return await self.start_engine("RESTART", report)

    # ------------------------------------------------------------------ operator actions
    async def setup_account(self, name: str, capital: Decimal, allocations: dict[str, Decimal] | None,
                            actor: str, user_id: int | None) -> TradingAccount:
        if self.account is not None:
            raise ValueError("trading account already configured")
        if capital < Decimal(100000):
            raise ValueError("starting capital must be at least ₹1,00,000")
        allocations = allocations or {sid: (capital * frac).quantize(Decimal(1)) for sid, _, _, _, frac in DEFAULT_STRATEGIES}
        if sum(allocations.values()) > capital:
            raise ValueError("strategy allocations exceed starting capital")
        broker_acct = "PAPER-" + uuid.uuid4().hex[:8].upper()
        await self.broker.adapter.open_account(broker_acct, capital)
        async with self.db.transaction() as s:
            acct = TradingAccount(name=name, account_type="PAPER", broker=self.broker.adapter.name,
                                  broker_account_id=broker_acct, starting_capital=capital, created_by=user_id)
            s.add(acct)
            for sid, amt in allocations.items():
                a = await s.get(StrategyAllocation, sid)
                if a is None:
                    raise ValueError(f"unknown strategy {sid}")
                a.allocated_capital = amt
        self.account = acct
        for sid, amt in allocations.items():
            await self.limits.set("STRATEGY", sid, "max_exposure", amt, user_id)
            if sid in self.strategies.runtimes:
                self.strategies.runtimes[sid].allocation = amt
        await self.broker.call("connect", lambda: self.broker.adapter.connect(broker_acct))
        self.broker.start()
        await self.controller.unblock("account", "- account configured")
        await self.audit.record("ACCOUNT_CREATED", f"paper trading account {broker_acct} created with capital ₹{capital:,.0f}",
                                source="Account", actor=actor, payload={"allocations": allocations})
        await self.reconciler.run("ACCOUNT_SETUP")
        return acct

    async def start_trading(self, actor: str) -> None:
        if self.account is None:
            raise ValueError("configure a trading account first")
        if self.controller.status == SAFE_MODE:
            raise ValueError("system is in SAFE MODE - reset the kill switch first (ADMIN)")
        if self.controller.blocks:
            raise ValueError("cannot start: " + "; ".join(self.controller.blocks.values()))
        await self.controller.set_status(RUNNING, f"started by {actor}", actor=actor)

    async def pause_trading(self, actor: str) -> None:
        if self.controller.status == SAFE_MODE:
            raise ValueError("system is in SAFE MODE")
        await self.controller.set_status(PAUSED, f"paused by {actor}", actor=actor)

    async def on_component_failure(self, component: str, detail: str) -> None:
        await self.bus.publish(Event("system.alert", component, payload={"component": component, "detail": detail}))

    async def recent_orders(self, n: int = 50) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(Order).order_by(Order.created_at.desc()).limit(n))).scalars().all()
        return [order_to_dict(o) for o in rows]

    # ------------------------------------------------------------------ health
    async def _health_loop(self) -> None:
        while True:
            try:
                t = time.perf_counter()
                await self.db.ping()
                self.health.beat("postgres", HEALTHY, "connected", (time.perf_counter() - t) * 1000)
            except Exception as e:
                self.health.error("postgres", f"unreachable: {e}")
                await self.controller.block("database", "PostgreSQL unreachable - financial state cannot be persisted")
            else:
                await self.controller.unblock("database", "- PostgreSQL reachable")
            try:
                lat = await self.redis.ping()
                self.health.beat("redis", HEALTHY, "connected (cache, pub/sub, rate limits, locks)", lat)
            except Exception as e:
                self.health.error("redis", f"unreachable - process-local fallback active: {e}", WARNING)
            self.health.beat("websocket", HEALTHY, f"{len(self.ws.clients)} client(s), {self.ws.sent} messages sent, "
                                                   f"{'via Redis pub/sub' if self.ws.via_redis else 'direct'}")
            dead = [t.get_name() for t in self._engine_tasks if t.done()]
            sw = self.strategies._worker
            if sw is not None and sw.done():
                dead.append("strategy-engine")
            if dead:
                self.health.error("workers", "stopped: " + ", ".join(dead))
            else:
                self.health.beat("workers", HEALTHY, f"{len(self._engine_tasks) + 1} engine workers alive")
            if self.reconciler.status in ("MATCHED", "MISMATCH_RESOLVED"):
                self.health.beat("reconciliation", HEALTHY, f"last: {self.reconciler.status}", success=True)
            if not self.broker.connected and self.account is None:
                self.health.beat("broker", WARNING, "no account configured", success=False)
            self.health.beat("risk_firewall", HEALTHY, f"{self.risk.stats['approved']} approved / "
                                                       f"{self.risk.stats['rejected']} rejected")
            await self.bus.publish(Event("health.update", "HealthMonitor", payload={
                "components": self.health.snapshot(), "overall": self.health.overall()}))
            await asyncio.sleep(3)

