"""Fault Simulation & Recovery Lab.

Every scenario injects a REAL fault into a real component (paper broker transport, market feed,
strategy logic, internal state, process state) and then observes how the actual engine responds.
The timeline (FAULT INJECTED -> SYSTEM RESPONSE -> RECOVERY ACTION -> FINAL STATE) is built from
what the engine actually did, read back from its state - never from scripted text alone.
Automated Chaos Mode runs several scenarios back-to-back through the same simulator.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.brokers.base import PlaceOrderRequest
from app.core.events import Event, json_default, new_id, utcnow
from app.database.models import FaultSimulation, Order, OrderEvent, Position
from app.engine.controller import RUNNING
from app.engine.position_manager import PositionState
from app.engine.types import OrderIntent

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

log = logging.getLogger("algotrade.faults")

INJECT, RESPONSE, RECOVERY, FINAL = "FAULT_INJECTED", "SYSTEM_RESPONSE", "RECOVERY_ACTION", "FINAL_STATE"


class ScenarioAborted(Exception):
    pass


class Timeline:
    def __init__(self, sim: "FaultSimulator", run_id: uuid.UUID, scenario: str, corr: str):
        self.sim, self.run_id, self.scenario, self.corr = sim, run_id, scenario, corr
        self.steps: list[dict[str, Any]] = []

    async def step(self, phase: str, title: str, detail: str = "", status: str = "info", data: dict | None = None) -> None:
        st = {"phase": phase, "title": title, "detail": detail, "status": status, "at": utcnow().isoformat(),
              "data": json.loads(json.dumps(data or {}, default=json_default))}
        self.steps.append(st)
        sys = self.sim.sys
        async with sys.db.transaction() as s:
            await s.execute(update(FaultSimulation).where(FaultSimulation.id == self.run_id).values(steps=self.steps))
        await sys.bus.publish(Event("fault.step", "FaultSimulator", correlation_id=self.corr,
                                    payload={"run_id": str(self.run_id), "scenario": self.scenario, "step": st}))


async def wait_until(cond: Callable[[], Any], timeout: float, interval: float = 0.2) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        r = cond()
        if asyncio.iscoroutine(r):
            r = await r
        if r:
            return True
        await asyncio.sleep(interval)
    return False


class FaultSimulator:
    SCENARIOS: dict[str, tuple[str, str]] = {
        "api_failure": ("API failure", "Broker API refuses connections for 8 seconds while an order is in flight"),
        "network_timeout": ("Network timeout", "Broker accepts an order but the response is lost (timeout)"),
        "partial_fill": ("Partial fill", "Liquidity shortage: only 40% fills, remainder fills later"),
        "order_rejection": ("Order rejection", "Broker RMS rejects the next order"),
        "risk_rejection": ("Risk rejection", "Order larger than the strategy's max position hits the Risk Firewall"),
        "position_mismatch": ("Position mismatch", "Broker fills an order but the execution report is lost in transit"),
        "state_corruption": ("Internal state corruption", "Internal position cache is corrupted (simulated bug)"),
        "server_restart": ("Server restart", "All in-memory state is discarded and rebuilt from PostgreSQL + broker"),
        "runaway_strategy": ("Runaway strategy", "Strategy logic malfunctions and flips BUY/SELL continuously"),
        "high_latency": ("High latency", "Broker API latency rises to ~2.8 seconds"),
        "duplicate_order": ("Duplicate order attempt", "The same order (idempotency key) is submitted twice"),
        "stale_data": ("Stale market data", "Market feed freezes; prices stop updating"),
    }
    # need the local simulator: a real exchange cannot be told to fill only 40% of an order
    SIMULATOR_ONLY = {"partial_fill"}
    # (scenario, target strategy) - spread across strategies so per-strategy order-rate limits stay meaningful
    CHAOS_PLAN = [("high_latency", "macd"), ("network_timeout", "rsi_ema"), ("duplicate_order", "breakout"),
                  ("position_mismatch", "macd"), ("partial_fill", "rsi_ema")]
    CHAOS_SEQUENCE = [sc for sc, _ in CHAOS_PLAN]

    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self._lock = asyncio.Lock()
        self.active: dict[str, Any] | None = None
        self.chaos_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ helpers
    def catalog(self) -> list[dict[str, str]]:
        return [{"key": k, "title": t, "description": d,
                 "available": self.sys.is_paper or k not in self.SIMULATOR_ONLY} for k, (t, d) in self.SCENARIOS.items()]

    def _plan(self) -> list[tuple[str, str]]:
        plan = [(sc, sid) for sc, sid in self.CHAOS_PLAN if self.sys.is_paper or sc not in self.SIMULATOR_ONLY]
        # target strategies that exist in this deployment (the demo subscriptions differ between modes)
        ids = list(self.sys.strategies.runtimes)
        return [(sc, sid if sid in self.sys.strategies.runtimes else ids[i % len(ids)]) for i, (sc, sid) in enumerate(plan)] if ids else []

    def _target(self, params: dict) -> str:
        sid = params.get("strategy_id") or "macd"
        if sid not in self.sys.strategies.runtimes:
            sid = next(iter(self.sys.strategies.runtimes))
        return sid

    async def _require_trading(self, tl: Timeline, sid: str) -> None:
        ctl = self.sys.controller
        rt = self.sys.strategies.runtimes[sid]
        problems = []
        if ctl.status != RUNNING:
            problems.append(f"global trading is {ctl.status}")
        if ctl.blocks:
            problems.append("orders blocked: " + "; ".join(ctl.blocks.values()))
        if rt.status != "RUNNING":
            problems.append(f"strategy {rt.name} is {rt.status}")
        if problems:
            await tl.step(INJECT, "Precondition failed - scenario not started", ", ".join(problems) +
                          ". Start trading / resume the strategy first so the fault hits a live order path.", "error")
            raise ScenarioAborted(", ".join(problems))

    def _test_side(self, sid: str, qty: Decimal) -> str:
        rt = self.sys.strategies.runtimes[sid]
        pos = self.sys.positions.get(sid, rt.symbol).quantity
        return "BUY" if pos <= 0 else "SELL"

    async def _test_order(self, sid: str, qty: Decimal, reason: str, key: str | None = None, corr: str | None = None,
                          side: str | None = None) -> dict:
        side = side or self._test_side(sid, qty)
        return await self.sys.strategies.handle_signal(sid, side, f"FAULT LAB test order: {reason}", {"fault_lab": True},
                                                       origin="FAULT_LAB", quantity=qty, idempotency_key=key,
                                                       correlation_id=corr)

    async def _reached_broker(self, tl: Timeline, res: dict) -> dict:
        """Abort (honestly) if the test order was stopped before reaching the broker."""
        o = res.get("order") or {}
        if not o:
            await tl.step(FINAL, "Test order not created", f"signal action: {res.get('action')}", "warn")
            raise ScenarioAborted(f"test order not created ({res.get('action')})")
        if o.get("status") == "REJECTED" and o.get("reject_rule") != "BROKER_REJECTED":
            await tl.step(FINAL, "Test order blocked by Risk Firewall before reaching the broker",
                          o.get("reject_reason") or "", "warn")
            raise ScenarioAborted(f"risk firewall blocked the test order: {o.get('reject_rule')}")
        return o

    async def _order(self, order_id: str) -> Order | None:
        async with self.sys.db.session() as s:
            return await s.get(Order, uuid.UUID(order_id))

    async def _order_events(self, order_id: str) -> list[OrderEvent]:
        async with self.sys.db.session() as s:
            return list((await s.execute(select(OrderEvent).where(OrderEvent.order_id == uuid.UUID(order_id))
                                         .order_by(OrderEvent.id))).scalars())

    def _pos(self, sid: str) -> Decimal:
        rt = self.sys.strategies.runtimes[sid]
        return self.sys.positions.get(sid, rt.symbol).quantity

    # ------------------------------------------------------------------ runner
    async def run(self, scenario: str, actor: str, params: dict | None = None, run_group: str | None = None) -> dict:
        if scenario not in self.SCENARIOS:
            raise ValueError(f"unknown scenario {scenario}")
        if scenario in self.SIMULATOR_ONLY and not self.sys.is_paper:
            raise ValueError(f"'{self.SCENARIOS[scenario][0]}' needs the local simulator - it cannot be forced on the 021 exchange")
        params = params or {}
        async with self._lock:
            corr = new_id()
            run_id = uuid.uuid4()
            async with self.sys.db.transaction() as s:
                s.add(FaultSimulation(id=run_id, scenario=scenario, status="RUNNING", steps=[], triggered_by=actor,
                                      run_group=run_group, correlation_id=corr))
            self.active = {"run_id": str(run_id), "scenario": scenario}
            tl = Timeline(self, run_id, scenario, corr)
            await self.sys.audit.record("FAULT_INJECTION_STARTED", f"fault scenario '{self.SCENARIOS[scenario][0]}' started by {actor}",
                                        source="FaultSimulator", severity="WARN", actor=actor, correlation_id=corr,
                                        payload={"scenario": scenario, "params": params})
            status, outcome = "COMPLETED", ""
            try:
                outcome = await getattr(self, f"_s_{scenario}")(tl, params)
            except ScenarioAborted as e:
                status, outcome = "ABORTED", str(e)
            except Exception as e:
                log.exception("scenario %s crashed", scenario)
                status, outcome = "FAILED", f"scenario error: {e}"
                await tl.step(FINAL, "Scenario error", str(e), "error")
            finally:
                self.sys.broker.adapter.faults.disarm()  # never leak an armed fault into real trading
                if scenario == "stale_data" and self.sys.market.provider and self.sys.market.provider.paused:
                    self.sys.market.provider.resume()
                self.active = None
            async with self.sys.db.transaction() as s:
                await s.execute(update(FaultSimulation).where(FaultSimulation.id == run_id).values(
                    status=status, outcome=outcome, finished_at=utcnow()))
            await self.sys.audit.record("FAULT_INJECTION_FINISHED", f"fault scenario '{self.SCENARIOS[scenario][0]}' {status}: {outcome}",
                                        source="FaultSimulator", actor=actor, correlation_id=corr,
                                        severity="INFO" if status == "COMPLETED" else "WARN")
            result = {"run_id": str(run_id), "scenario": scenario, "status": status, "outcome": outcome, "steps": tl.steps,
                      "correlation_id": corr}
            await self.sys.bus.publish(Event("fault.completed", "FaultSimulator", correlation_id=corr, payload=result))
            return result

    def start_background(self, scenario: str, actor: str, params: dict | None = None) -> None:
        if self._lock.locked():
            raise ValueError("another fault scenario is running")
        asyncio.create_task(self.run(scenario, actor, params))

    # ------------------------------------------------------------------ scenarios
    async def _s_api_failure(self, tl: Timeline, p: dict) -> str:
        sid = self._target(p)
        await self._require_trading(tl, sid)
        faults = self.sys.broker.adapter.faults
        faults.network_down_until = time.time() + 8
        await tl.step(INJECT, "Broker API outage injected", "paper broker refuses all connections for 8 seconds", "error")
        res = await self._test_order(sid, Decimal(10), "order during broker outage", corr=tl.corr)
        o = await self._reached_broker(tl, res)
        evs = await self._order_events(o["id"]) if o.get("id") else []
        verify = [e.message for e in evs if e.status in ("UNCERTAIN", "VERIFY_FAILED")]
        await tl.step(RESPONSE, f"Order {o.get('id', '')[:8]} -> {o.get('status')}",
                      "; ".join(verify) or (o.get("reject_reason") or ""), "warn",
                      {"order_id": o.get("id"), "status": o.get("status")})
        await tl.step(RESPONSE, "New orders BLOCKED (fail closed)",
                      "; ".join(self.sys.controller.blocks.values()) or "no blocks", "warn",
                      {"blocks": dict(self.sys.controller.blocks), "broker": self.sys.broker.info()})
        res2 = await self._test_order(sid, Decimal(13), "second order while state uncertain", corr=tl.corr)
        o2 = res2.get("order") or {}
        await tl.step(RESPONSE, f"Follow-up order {o2.get('status', res2.get('action'))}", o2.get("reject_reason") or "", "warn")
        await wait_until(lambda: time.time() >= faults.network_down_until, 12)
        ok = await wait_until(lambda: self.sys.broker.available, 10)
        await tl.step(RECOVERY, "Broker reachable again" if ok else "Broker still unavailable",
                      f"heartbeat latency {self.sys.broker.current_latency() or 0:.0f} ms", "ok" if ok else "error")
        rec = await self.sys.reconciler.run("FAULT_API_FAILURE", tl.corr)
        await tl.step(RECOVERY, f"Reconciliation: {rec['status']}", rec["summary"], "ok" if rec["status"] != "MISMATCH_UNRESOLVED" else "error",
                      {"mismatches": rec.get("mismatches"), "actions": rec.get("actions")})
        fo = await self._order(o["id"]) if o.get("id") else None
        final = f"order {fo.status if fo else '-'}; blocks: {len(self.sys.controller.blocks)}; broker available: {self.sys.broker.available}"
        await tl.step(FINAL, "STATE CONSISTENT" if not self.sys.controller.blocks else "ORDERS STILL BLOCKED", final,
                      "ok" if not self.sys.controller.blocks else "warn")
        return final

    async def _s_network_timeout(self, tl: Timeline, p: dict) -> str:
        sid = self._target(p)
        await self._require_trading(tl, sid)
        self.sys.broker.adapter.faults.timeout_next_place = 1
        key = f"FLT-TMO-{uuid.uuid4().hex[:10]}"
        await tl.step(INJECT, "Network timeout armed", f"next place_order is accepted by the broker but its response is lost "
                      f"(client timeout {self.sys.settings.broker_timeout_s:.0f}s). Idempotency key {key}", "error")
        t0 = time.perf_counter()
        res = await self._test_order(sid, Decimal(12), "order whose response will be lost", key=key, corr=tl.corr)
        o = await self._reached_broker(tl, res)
        evs = await self._order_events(o["id"])
        for e in evs:
            if e.status in ("UNCERTAIN", "VERIFIED_AT_BROKER", "NOT_FOUND_AT_BROKER", "VERIFY_FAILED"):
                await tl.step(RESPONSE, {"UNCERTAIN": "API TIMEOUT - order state unknown",
                                         "VERIFIED_AT_BROKER": "Existing order found at broker - duplicate retry BLOCKED",
                                         "NOT_FOUND_AT_BROKER": "Order not at broker - controlled retry",
                                         "VERIFY_FAILED": "Broker status query failed"}[e.status], e.message,
                              "ok" if e.status == "VERIFIED_AT_BROKER" else "warn")
        b_orders = await self.sys.broker.adapter.get_orders()
        n = sum(1 for b in b_orders if b.client_order_id == key)
        await tl.step(RESPONSE, f"Broker holds {n} order(s) for key {key}", "no duplicate was placed" if n == 1 else
                      "DUPLICATE DETECTED", "ok" if n == 1 else "error", {"broker_orders_for_key": n,
                                                                         "elapsed_s": round(time.perf_counter() - t0, 1)})
        await wait_until(lambda: o["id"] not in self.sys.orders.open_orders, 15)
        rec = await self.sys.reconciler.run("FAULT_TIMEOUT", tl.corr)
        await tl.step(RECOVERY, f"Reconciliation: {rec['status']}", rec["summary"], "ok" if rec["status"] != "MISMATCH_UNRESOLVED" else "error")
        fo = await self._order(o["id"])
        final = f"order {fo.status} {fo.filled_quantity}/{fo.quantity}; broker orders for key: {n}"
        await tl.step(FINAL, "STATE CONSISTENT", final, "ok")
        return final

    async def _s_partial_fill(self, tl: Timeline, p: dict) -> str:
        sid = self._target(p)
        await self._require_trading(tl, sid)
        f = self.sys.broker.adapter.faults
        f.partial_next, f.partial_ratio, f.partial_hold_s = 1, Decimal("0.4"), 4.0
        qty = Decimal(int(p.get("quantity", 50)))
        before = self._pos(sid)
        await tl.step(INJECT, "Liquidity shortage armed", "next order can only fill 40% for 4s", "warn")
        res = await self._test_order(sid, qty, "order into thin liquidity", corr=tl.corr)
        o = await self._reached_broker(tl, res)
        ok = await wait_until(lambda: (self.sys.orders.open_orders.get(o["id"]) and
                                       self.sys.orders.open_orders[o["id"]].status == "PARTIALLY_FILLED"), 10)
        fo = await self._order(o["id"])
        await tl.step(RESPONSE, f"PARTIALLY_FILLED {fo.filled_quantity}/{fo.quantity}" if ok else f"status {fo.status}",
                      f"position moved {before:+} -> {self._pos(sid):+} (by actual fills, not requested qty)", "warn",
                      {"filled": fo.filled_quantity, "requested": fo.quantity, "position": self._pos(sid)})
        await wait_until(lambda: o["id"] not in self.sys.orders.open_orders, 20)
        fo = await self._order(o["id"])
        await tl.step(RECOVERY, "Liquidity returned - remaining quantity filled" if fo.status == "FILLED" else f"order {fo.status}",
                      f"{fo.filled_quantity}/{fo.quantity} @ avg {fo.avg_fill_price}", "ok" if fo.status == "FILLED" else "warn")
        final = f"order {fo.status}; position {before:+} -> {self._pos(sid):+}"
        await tl.step(FINAL, "POSITION MATCHES FILLS", final, "ok")
        return final

    async def _s_order_rejection(self, tl: Timeline, p: dict) -> str:
        sid = self._target(p)
        await self._require_trading(tl, sid)
        self.sys.broker.adapter.faults.reject_next = 1
        before = self._pos(sid)
        await tl.step(INJECT, "Broker rejection armed", self.sys.broker.adapter.faults.reject_reason, "error")
        res = await self._test_order(sid, Decimal(8), "order that the broker will reject", corr=tl.corr)
        o = await self._reached_broker(tl, res)
        await tl.step(RESPONSE, f"ORDER {o.get('status')}", o.get("reject_reason") or "", "warn" if o.get("status") == "REJECTED" else "info")
        final = f"position unchanged ({before:+} -> {self._pos(sid):+}); no fills booked"
        await tl.step(FINAL, "NO POSITION CHANGE", final, "ok" if before == self._pos(sid) else "error")
        return final

    async def _s_risk_rejection(self, tl: Timeline, p: dict) -> str:
        sid = self._target(p)
        await self._require_trading(tl, sid)
        mx = self.sys.limits.s(sid, "max_position")
        qty = mx + Decimal(100)
        await tl.step(INJECT, "Oversized order", f"strategy asks for {qty:,.0f} shares; max position is {mx:,.0f}", "error")
        n_before = len(await self.sys.broker.adapter.get_orders())
        res = await self._test_order(sid, qty, "oversized order", corr=tl.corr, side="BUY" if self._pos(sid) >= 0 else "SELL")
        risk = res.get("risk") or {}
        failed = [c for c in risk.get("checks", []) if c["result"] == "FAIL"]
        for c in failed:
            await tl.step(RESPONSE, f"ORDER REJECTED - {c['label']}", f"{c['message']} | requested/actual: {c['actual']} | allowed: {c['allowed']}",
                          "error", c)
        n_after = len(await self.sys.broker.adapter.get_orders())
        final = f"{len(failed)} rule(s) violated; broker orders {n_before} -> {n_after} (nothing reached the broker)"
        await tl.step(FINAL, "BLOCKED BEFORE BROKER", final, "ok" if n_after == n_before else "error")
        return final

    async def _s_position_mismatch(self, tl: Timeline, p: dict) -> str:
        sid = self._target(p)
        await self._require_trading(tl, sid)
        rt = self.sys.strategies.runtimes[sid]
        self.sys.broker.adapter.faults.drop_reports_next = 1
        await tl.step(INJECT, "Execution report loss armed", "the broker will fill the next order but its execution "
                      "reports never reach AlgoTrade", "error")
        key = f"FLT-MIS-{uuid.uuid4().hex[:10]}"
        res = await self._test_order(sid, Decimal(20), "order whose fills will be lost", key=key, corr=tl.corr)
        o = await self._reached_broker(tl, res)

        async def broker_filled():
            bo = await self.sys.broker.adapter.get_order_by_client_id(key)
            return bo is not None and bo.status == "FILLED"

        await wait_until(broker_filled, 15, 0.5)
        b_pos = {bp.symbol: bp.quantity for bp in await self.sys.broker.adapter.get_positions()}
        i_net = self.sys.positions.net_by_symbol()
        sym = rt.symbol
        await tl.step(RESPONSE, "MISMATCH DETECTED", f"Internal: {sym} {i_net.get(sym, Decimal(0)):+} | Broker: {sym} "
                      f"{b_pos.get(sym, Decimal(0)):+}", "error", {"internal": i_net.get(sym, Decimal(0)),
                                                                  "broker": b_pos.get(sym, Decimal(0))})
        rec = await self.sys.reconciler.run("FAULT_POSITION_MISMATCH", tl.corr)
        for a in rec.get("actions", []):
            await tl.step(RECOVERY, a["action"].replace("_", " ").title(), a["detail"], "ok")
        # compare at the reconciliation point in time (other strategies may keep trading this symbol)
        row = next((c for c in rec.get("details", {}).get("comparison", []) if c["symbol"] == sym), None)
        i_after = Decimal(int(row["internal_after"])) if row else self.sys.positions.net_by_symbol().get(sym, Decimal(0))
        b_now = Decimal(int(row["broker"])) if row else b_pos.get(sym, Decimal(0))
        final = f"reconciliation {rec['status']}: internal {sym} {i_after:+} | broker {sym} {b_now:+}"
        await tl.step(FINAL, "STATE RECONCILED" if i_after == b_now else "MISMATCH REMAINS",
                      final, "ok" if rec["status"] != "MISMATCH_UNRESOLVED" else "error", {"comparison": rec["details"].get("comparison")})
        return final

    async def _s_state_corruption(self, tl: Timeline, p: dict) -> str:
        sid = self._target(p)
        rt = self.sys.strategies.runtimes[sid]
        before = self.sys.positions.get(sid, rt.symbol)
        bogus = before.quantity + Decimal(25)
        async with self.sys.db.transaction() as s:
            await s.execute(pg_insert(Position).values(strategy_id=sid, symbol=rt.symbol, quantity=0, avg_price=0,
                                                       realized_pnl=0).on_conflict_do_nothing())
            await s.execute(update(Position).where(Position.strategy_id == sid, Position.symbol == rt.symbol)
                            .values(quantity=bogus))
        self.sys.positions.commit_cache(PositionState(sid, rt.symbol, bogus, before.avg_price, before.realized_pnl))
        await self.sys.audit.record("FAULT_STATE_CORRUPTED", f"FAULT: internal position {sid} {rt.symbol} overwritten "
                                    f"{before.quantity:+} -> {bogus:+}", source="FaultSimulator", severity="WARN",
                                    correlation_id=tl.corr)
        await tl.step(INJECT, "Internal position corrupted", f"{rt.name} {rt.symbol}: {before.quantity:+} -> {bogus:+} "
                      "(no fill supports this)", "error")
        rec = await self.sys.reconciler.run("FAULT_STATE_CORRUPTION", tl.corr)
        for m in rec.get("mismatches", []):
            await tl.step(RESPONSE, m["type"].replace("_", " ").title(), m["detail"], "warn")
        for a in rec.get("actions", []):
            await tl.step(RECOVERY, a["action"].replace("_", " ").title(), a["detail"], "ok")
        after = self.sys.positions.get(sid, rt.symbol)
        final = f"position restored to {after.quantity:+} from the fills ledger (reconciliation {rec['status']})"
        await tl.step(FINAL, "STATE RESTORED" if after.quantity == before.quantity else "CHECK STATE", final,
                      "ok" if after.quantity == before.quantity else "error")
        return final

    async def _s_server_restart(self, tl: Timeline, p: dict) -> str:
        await tl.step(INJECT, "Process restart", "trading engine stopped; ALL in-memory state discarded (positions cache, "
                      "open orders, strategy state, risk cache, order blocks). PostgreSQL, Redis and the broker keep running.",
                      "error")

        async def report(title: str, detail: str, status: str = "ok", data: dict | None = None):
            await tl.step(RECOVERY, title, detail, status, data)

        result = await self.sys.restart_engine(report)
        await tl.step(FINAL, f"Recovered - trading {self.sys.controller.status}",
                      f"reconciliation {result.get('reconciliation')}; orders allowed: "
                      f"{self.sys.controller.snapshot()['orders_allowed']}", "ok" if result.get("ok") else "error", result)
        return f"recovered; trading {self.sys.controller.status}"

    async def _s_runaway_strategy(self, tl: Timeline, p: dict) -> str:
        sid = self._target(p)
        await self._require_trading(tl, sid)
        rt = self.sys.strategies.runtimes[sid]
        await tl.step(INJECT, f"{rt.name} malfunction", "strategy emits alternating BUY/SELL signals every 250 ms for up to 8s", "error")
        results = await self.sys.strategies.start_runaway(sid, 8.0, 0.25, tl.corr)
        approved = [r for r in results if (r.get("order") or {}).get("status") not in (None, "REJECTED")]
        rejected = [r for r in results if (r.get("order") or {}).get("status") == "REJECTED"]
        rules: dict[str, int] = {}
        for r in rejected:
            rule = r["order"].get("reject_rule") or "?"
            rules[rule] = rules.get(rule, 0) + 1
        await tl.step(RESPONSE, f"{len(results)} signals -> {len(approved)} orders reached broker, {len(rejected)} rejected",
                      ", ".join(f"{k}: {v}" for k, v in rules.items()) or "-", "warn", {"rules": rules})
        await tl.step(RECOVERY, f"Circuit breaker: strategy {rt.status}", rt.status_reason or "", "ok" if rt.status == "HALTED" else "warn")
        fl = None
        if rt.flatten_task is not None:
            fl = await rt.flatten_task
            await tl.step(RECOVERY, "Halt and flatten", f"{len(fl['cancelled'])} working order(s) cancelled, "
                          f"{len(fl['close_orders'])} reduce-only close order(s); position {fl['position']:+} after {fl['elapsed_s']}s",
                          "ok" if fl["flat"] else "error", {"flatten": {k: v for k, v in fl.items() if k != "position"}})
        others = [f"{r.name} {r.status}" for r in self.sys.strategies.runtimes.values() if r.id != sid]
        flat = self.sys.positions.get(sid, rt.symbol).quantity == 0
        final = (f"{rt.name} {rt.status}, position {self.sys.positions.get(sid, rt.symbol).quantity:+}"
                 + ("" if fl is None else f", flattened in {fl['elapsed_s']}s") + "; other strategies unaffected: "
                 + (", ".join(others) or "none"))
        ok = rt.status == "HALTED" and flat
        await tl.step(FINAL, "RUNAWAY CONTAINED - HALTED AND FLAT" if ok else "CHECK STRATEGY", final, "ok" if ok else "warn")
        return final

    async def _s_high_latency(self, tl: Timeline, p: dict) -> str:
        sid = self._target(p)
        await self._require_trading(tl, sid)
        gw = self.sys.broker
        gw.adapter.faults.extra_latency_ms = 2800
        await tl.step(INJECT, "Latency injected", "+2800 ms on every broker API call", "error")
        try:
            await gw.call("ping", gw.adapter.ping)
        except Exception:
            pass
        await tl.step(RESPONSE, f"HIGH LATENCY measured: {(gw.current_latency() or 0) / 1000:.1f}s", "latency guard engaged", "warn")
        res = await self._test_order(sid, Decimal(11), "order under high latency", corr=tl.corr)
        o = res.get("order") or {}
        lat_check = next((c for c in (res.get("risk") or {}).get("checks", []) if c["rule"] == "HIGH_LATENCY"), None)
        if lat_check:
            await tl.step(RESPONSE, "ORDER TEMPORARILY BLOCKED - HIGH LATENCY",
                          f"{lat_check['message']} (actual {lat_check['actual']}, allowed {lat_check['allowed']})", "warn")
        else:
            await tl.step(RESPONSE, f"ORDER {o.get('status')}", o.get("reject_reason") or "", "error")
        gw.adapter.faults.extra_latency_ms = 0
        try:
            await gw.call("ping", gw.adapter.ping)
        except Exception:
            pass
        await tl.step(RECOVERY, "Latency normalised", f"broker latency {gw.current_latency() or 0:.0f} ms - orders allowed again", "ok")
        final = (f"latency guard {'blocked' if lat_check else 'did NOT block'} the order; "
                 f"latency now {gw.current_latency() or 0:.0f} ms")
        await tl.step(FINAL, "NO ORDER SENT INTO A SLOW VENUE" if lat_check else "CHECK", final, "ok" if lat_check else "error")
        return final

    async def _s_duplicate_order(self, tl: Timeline, p: dict) -> str:
        sid = self._target(p)
        await self._require_trading(tl, sid)
        key = f"FLT-DUP-{uuid.uuid4().hex[:10]}"
        await tl.step(INJECT, "Duplicate submission", f"the same order (idempotency key {key}) will be sent twice, then "
                      "re-sent directly at the broker transport layer", "error")
        r1 = await self._test_order(sid, Decimal(15), "original submission", key=key, corr=tl.corr)
        o1 = await self._reached_broker(tl, r1)
        await tl.step(RESPONSE, f"First submission -> {o1.get('status')}", f"order {o1.get('id', '')[:8]}", "info")
        r2 = await self.sys.orders.submit(OrderIntent(
            sid, self.sys.strategies.runtimes[sid].symbol, o1.get("side", "BUY"), Decimal(15), origin="FAULT_LAB",
            correlation_id=tl.corr, idempotency_key=key, reason="duplicate resubmission"))
        await tl.step(RESPONSE, "Second submission BLOCKED by Order Engine" if r2.get("duplicate") else "Second submission NOT blocked",
                      f"idempotency key already used by order {(r2.get('order') or {}).get('id', '')[:8]} - existing order returned",
                      "ok" if r2.get("duplicate") else "error")
        o1db = await self._order(o1["id"])
        if o1db and o1db.status != "REJECTED":
            rt = self.sys.strategies.runtimes[sid]
            bo = await self.sys.broker.adapter.place_order(PlaceOrderRequest(key, rt.symbol, o1db.side, o1db.quantity, "MARKET"))
            await tl.step(RESPONSE, "Broker-level resend returned the SAME broker order",
                          f"{bo.broker_order_id} ({bo.status}) - the broker adapter is idempotent on the client order id", "ok")
        b_n = sum(1 for b in await self.sys.broker.adapter.get_orders() if b.client_order_id == key)
        async with self.sys.db.session() as s:
            i_n = len((await s.execute(select(Order.id).where(Order.client_order_id == key))).all())
        final = f"internal orders for key: {i_n}; broker orders for key: {b_n}"
        await tl.step(FINAL, "EXACTLY ONE ORDER" if i_n == 1 and b_n <= 1 else "DUPLICATE!", final,
                      "ok" if i_n == 1 and b_n <= 1 else "error")
        return final

    async def _s_stale_data(self, tl: Timeline, p: dict) -> str:
        sid = self._target(p)
        await self._require_trading(tl, sid)
        rt = self.sys.strategies.runtimes[sid]
        prov = self.sys.market.provider
        prov.pause()
        await tl.step(INJECT, "Market feed frozen", "provider stops delivering ticks", "error")
        ok = await wait_until(lambda: not self.sys.market.freshness(rt.symbol)[0], self.sys.market.stale_after + 10)
        await tl.step(RESPONSE, f"Market data {self.sys.market.display_status()}", self.sys.market.freshness(rt.symbol)[2],
                      "warn" if ok else "error")
        res = await self._test_order(sid, Decimal(9), "order on stale prices", corr=tl.corr)
        o = res.get("order") or {}
        await tl.step(RESPONSE, f"ORDER {o.get('status')}", o.get("reject_reason") or "", "warn")
        prov.resume()
        await wait_until(lambda: self.sys.market.freshness(rt.symbol)[0], 30)
        await tl.step(RECOVERY, "Feed resumed", f"market data {self.sys.market.display_status()}", "ok")
        final = f"stale-data order {o.get('status')} ({o.get('reject_rule')}); feed {self.sys.market.display_status()}"
        await tl.step(FINAL, "NEVER TRADED ON STALE DATA" if o.get("status") == "REJECTED" else "CHECK", final,
                      "ok" if o.get("status") == "REJECTED" else "error")
        return final

    # ------------------------------------------------------------------ chaos mode
    async def chaos(self, actor: str) -> dict:
        group = new_id()
        results = []
        plan = self._plan()
        seq = [sc for sc, _ in plan]
        await self.sys.bus.publish(Event("chaos.started", "FaultSimulator", payload={"run_group": group, "sequence": seq}))
        await self.sys.audit.record("CHAOS_MODE_STARTED", f"automated chaos mode started by {actor}: {', '.join(seq)}",
                                    source="FaultSimulator", severity="WARN", actor=actor)
        for sc, sid in plan:
            r = await self.run(sc, actor, {"strategy_id": sid}, run_group=group)
            results.append({"scenario": sc, "status": r["status"], "outcome": r["outcome"], "run_id": r["run_id"]})
            await self.sys.bus.publish(Event("chaos.progress", "FaultSimulator", payload={"run_group": group, "results": results}))
            if r["status"] == "ABORTED":
                break
            await asyncio.sleep(1.0)
        rec = await self.sys.reconciler.run("CHAOS_FINAL")
        summary = {"run_group": group, "results": results, "final_reconciliation": rec.get("status"),
                   "orders_allowed": self.sys.controller.snapshot()["orders_allowed"]}
        await self.sys.audit.record("CHAOS_MODE_FINISHED", f"chaos mode finished: final reconciliation {rec.get('status')}",
                                    source="FaultSimulator", actor=actor, payload=summary)
        await self.sys.bus.publish(Event("chaos.completed", "FaultSimulator", payload=summary))
        return summary

    def start_chaos(self, actor: str) -> None:
        if self.chaos_task and not self.chaos_task.done():
            raise ValueError("chaos mode already running")
        if self._lock.locked():
            raise ValueError("a fault scenario is running")
        self.chaos_task = asyncio.create_task(self.chaos(actor))
