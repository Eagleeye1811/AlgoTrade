"""Deterministic Demo Mode.

Drives the REAL engine through the judge script. Nothing here writes UI values: it uses the live 021 market
feed (NSE hours), starts trading, waits for real strategy orders/fills, then runs real fault scenarios
and finally the real kill switch. Pause/resume take effect between steps.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from app.core.events import Event, new_id, utcnow
from app.engine.controller import PAUSED, RUNNING
from app.resilience.fault_simulator import wait_until

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

log = logging.getLogger("algotrade.demo")

WIPE_TABLES = ["order_events", "position_events", "fills", "orders", "signals", "positions", "pnl_snapshots",
               "risk_events", "reconciliation_events", "fault_simulations", "system_events"]

STEPS = [
    ("market", "Live market data", "021 market-data ticks stream through the candle engine (NSE hours)"),
    ("trading", "Start algorithmic trading", "Global trading RUNNING, all strategies enabled"),
    ("first_fill", "Strategy signal -> risk -> order -> fill", "Wait for a strategy to trade on real candles"),
    ("pnl", "Position & P&L update", "Position and P&L computed from the actual fill"),
    ("risk_rejection", "Risk rejection", "Oversized order blocked by the Risk Firewall"),
    ("timeout", "API timeout + duplicate prevention", "Lost broker response; engine verifies instead of re-sending"),
    ("duplicate", "Duplicate order attempt", "Idempotency key reused"),
    ("mismatch", "Reconciliation mismatch", "Execution report lost; reconciliation repairs from broker truth"),
    ("runaway", "Runaway strategy", "Malfunctioning strategy contained by the firewall circuit breaker"),
    ("kill", "Kill switch", "Emergency stop, cancel, flatten"),
    ("safe", "SAFE MODE", "System locked in SAFE MODE"),
]


class DemoMode:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self.task: asyncio.Task | None = None
        self.run_id: str | None = None
        self.steps: list[dict[str, Any]] = []
        self.status = "IDLE"
        self._go = asyncio.Event()
        self._go.set()

    def snapshot(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "status": self.status, "steps": self.steps,
                "plan": [{"key": k, "title": t, "description": d} for k, t, d in STEPS]}

    async def _publish(self) -> None:
        await self.sys.bus.publish(Event("demo.update", "DemoMode", payload=self.snapshot()))

    async def _mark(self, key: str, status: str, detail: str = "", data: dict | None = None) -> None:
        title = next(t for k, t, _ in STEPS if k == key)
        existing = next((s for s in self.steps if s["key"] == key), None)
        rec = {"key": key, "title": title, "status": status, "detail": detail, "at": utcnow().isoformat(), "data": data or {}}
        if existing:
            existing.update(rec)
        else:
            self.steps.append(rec)
        await self._publish()

    async def _gate(self) -> None:
        if not self._go.is_set():
            self.status = "PAUSED"
            await self._publish()
            await self._go.wait()
            self.status = "RUNNING"
            await self._publish()

    # ------------------------------------------------------------------ controls
    def start(self, actor: str) -> None:
        if self.task and not self.task.done():
            raise ValueError("demo already running")
        self.run_id, self.steps, self.status = new_id(), [], "RUNNING"
        self._go.set()
        self.task = asyncio.create_task(self._run(actor))

    async def pause(self) -> None:
        if self.status != "RUNNING":
            raise ValueError("demo is not running")
        self._go.clear()
        self.status = "PAUSING"
        if self.sys.market.provider:
            self.sys.market.provider.pause()
        await self._publish()

    async def resume(self) -> None:
        if self.status not in ("PAUSED", "PAUSING"):
            raise ValueError("demo is not paused")
        if self.sys.market.provider:
            self.sys.market.provider.resume()
        self._go.set()
        self.status = "RUNNING"
        await self._publish()

    async def reset(self, actor: str) -> dict[str, Any]:
        """Wipe the platform's trading history (and the simulator's, in paper mode). Destructive, ADMIN only."""
        if self.task and not self.task.done():
            self.task.cancel()
        self._go.set()
        sys = self.sys
        if sys.account is None:
            raise ValueError("trading account not configured")
        if not sys.is_paper:  # a real venue cannot be wiped: only reset while the 021 account is flat
            open_pos = [p for p in await sys.broker.adapter.get_positions() if p.quantity != 0]
            if open_pos or sys.orders.open_orders:
                raise ValueError("the 021 account must be flat with no working orders before a demo reset "
                                 "(use the kill switch first): " + ", ".join(f"{p.symbol} {p.quantity:+}" for p in open_pos))
        await sys.stop_engine()
        async with sys.db.transaction() as s:
            await s.execute(text("TRUNCATE " + ", ".join(WIPE_TABLES) + " RESTART IDENTITY CASCADE"))
            await s.execute(text("DELETE FROM system_state WHERE key = 'kill_switch'"))
            # unsubscribed strategies stay STOPPED
            await s.execute(text("UPDATE strategies SET status='RUNNING', status_reason=NULL WHERE status <> 'STOPPED'"))
        if sys.is_paper:
            await sys.broker.adapter.reset_account(sys.account.broker_account_id, sys.account.starting_capital)
        await sys.redis.reset_window("")
        sys.risk.recent_identical.clear()
        self.steps, self.status, self.run_id = [], "IDLE", None
        await sys.controller.set_status(PAUSED, "demo reset", actor=actor)
        await sys.start_engine(trigger="DEMO_RESET")
        if not sys.is_paper:
            # 021 keeps the day's trades but the platform's history was just wiped (account verified flat above):
            # acknowledge those trades as pre-reset history so reconciliation does not block trading
            try:
                await sys.reconciler.adopt_baseline(f"{actor} (demo reset)")
            except ValueError as e:  # a position appeared meanwhile: leave it to reconciliation (fails closed)
                await sys.audit.record("DEMO_RESET_BASELINE_SKIPPED", str(e), source="DemoMode", severity="WARN", actor=actor)
        await sys.audit.record("DEMO_RESET", f"demo reset by {actor}: trading history wiped",
                               source="DemoMode", severity="WARN", actor=actor)
        await self._publish()
        return self.snapshot()

    # ------------------------------------------------------------------ script
    async def _run(self, actor: str) -> None:
        sys = self.sys
        try:
            await sys.audit.record("DEMO_STARTED", f"demo mode started by {actor}", source="DemoMode", actor=actor)
            # 1. market data
            await self._gate()
            md = sys.market
            ok = await wait_until(lambda: md.display_status() in ("LIVE", "REPLAY"), 15)
            await self._mark("market", "done" if ok else "failed",
                             f"{md.display_status()} - {md.provider.source_name if md.provider else ''}",
                             {"ltp": {k: float(v[0].price) for k, v in md.last.items()}})
            # 2. trading
            await self._gate()
            for sid, rt in sys.strategies.runtimes.items():
                if rt.status != "RUNNING":
                    await sys.strategies.set_status(sid, "RUNNING", "enabled by demo", actor="DemoMode")
            await sys.start_trading(actor="DemoMode")
            await self._mark("trading", "done" if sys.controller.status == RUNNING else "failed",
                             f"trading {sys.controller.status}")
            # 3. first strategy fill
            await self._gate()
            await self._mark("first_fill", "running", "waiting for a strategy signal on real candles (up to 90s)")
            n0 = sys.orders.stats["filled"]
            got = await wait_until(lambda: sys.orders.stats["filled"] > n0, 90, 0.5)
            if not got:
                sid = "macd" if "macd" in sys.strategies.runtimes else next(iter(sys.strategies.runtimes))
                rt = sys.strategies.runtimes[sid]
                await self._mark("first_fill", "running", "no natural signal within 90s - injecting a clearly labelled "
                                 "DEMO signal through the same pipeline")
                await sys.strategies.handle_signal(sid, "BUY", "DEMO-injected signal (no natural crossover in window)",
                                                   {"demo": True}, origin="DEMO", quantity=rt.quantity)
                got = await wait_until(lambda: sys.orders.stats["filled"] > n0, 20, 0.5)
            orders = await sys.recent_orders(5)
            filled = next((o for o in orders if o["filled_quantity"] > 0), None)
            await self._mark("first_fill", "done" if got else "failed",
                             f"{filled['strategy_id']} {filled['side']} {filled['filled_quantity']} {filled['symbol']} @ "
                             f"{filled['avg_fill_price']} ({filled['status']})" if filled else "no fill",
                             {"order_id": filled["id"] if filled else None, "correlation_id": filled["correlation_id"] if filled else None})
            # 4. P&L
            await self._gate()
            await asyncio.sleep(3)
            snap = sys.pnl.compute()
            p = snap["portfolio"]
            await self._mark("pnl", "done", f"positions {p['active_positions']}, realized {p['realized']}, "
                             f"unrealized {p['unrealized']}, margin used {p['margin_used']}", {"portfolio": p})
            # 5-9 fault scenarios on the real engine
            for key, scenario, params in (("risk_rejection", "risk_rejection", {"strategy_id": "macd"}),
                                          ("timeout", "network_timeout", {"strategy_id": "rsi_ema"}),
                                          ("duplicate", "duplicate_order", {"strategy_id": "breakout"}),
                                          ("mismatch", "position_mismatch", {"strategy_id": "macd"}),
                                          ("runaway", "runaway_strategy", {"strategy_id": "breakout"})):
                await self._gate()
                await self._mark(key, "running")
                r = await sys.faults.run(scenario, f"demo:{actor}", params, run_group=self.run_id)
                await self._mark(key, "done" if r["status"] == "COMPLETED" else "failed", r["outcome"], {"run_id": r["run_id"]})
                await asyncio.sleep(2)
            # 10-11 kill switch -> SAFE MODE
            await self._gate()
            await self._mark("kill", "running")
            ks = await sys.kill_switch.activate(f"demo:{actor}", "demo: emergency stop", wait=True)
            await self._mark("kill", "done", f"{len(ks['cancelled'])} cancelled, {len(ks['close_orders'])} close orders, "
                             f"{len(ks['unresolved'])} unresolved ({ks['phase']})", {"kill_switch": ks})
            await self._mark("safe", "done" if sys.controller.status == "SAFE_MODE" else "failed",
                             f"trading {sys.controller.status}: {sys.controller.status_reason}")
            self.status = "COMPLETED"
            await sys.audit.record("DEMO_COMPLETED", "demo script completed", source="DemoMode", actor=actor)
        except asyncio.CancelledError:
            self.status = "CANCELLED"
        except Exception as e:
            log.exception("demo failed")
            self.status = "FAILED"
            await sys.audit.record("DEMO_FAILED", f"demo failed: {e}", source="DemoMode", severity="ERROR")
        finally:
            if sys.market.provider and sys.market.provider.paused:
                sys.market.provider.resume()
            await self._publish()
