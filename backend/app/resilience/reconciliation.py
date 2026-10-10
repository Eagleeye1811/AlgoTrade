"""Reconciliation: internal state vs broker (paper broker) state. The broker's executions are authoritative.

Compares orders, fills, positions and account cash. Repairs what can be repaired from authoritative
broker data (missing fills, stale order statuses, position cache drift). Anything that cannot be
explained blocks new orders (fail closed).

External baseline: broker fills/orders the platform never recorded (e.g. the internal ledger was reset while the
broker kept the day's trades) can be acknowledged by an ADMIN - only while the broker's net position equals the
platform's for every symbol. Acknowledged ids are stored (system_state "recon_external") and audited, and are
reported instead of blocking; anything new and unexplained still blocks.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.brokers.base import BrokerError
from app.core.events import Event, new_id, utcnow
from app.core.health import HEALTHY, WARNING
from app.database.models import Fill, Order, Position, ReconciliationEvent, SystemState

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

log = logging.getLogger("algotrade.recon")

FAILED_RETRY_S = 5.0
MATCHED, RESOLVED, UNRESOLVED, RECON_FAILED = "MATCHED", "MISMATCH_RESOLVED", "MISMATCH_UNRESOLVED", "FAILED"
EXTERNAL_KEY = "recon_external"
BROKER_TO_INTERNAL = {"OPEN": "ACKNOWLEDGED", "PARTIALLY_FILLED": "PARTIALLY_FILLED", "FILLED": "FILLED",
                      "CANCELLED": "CANCELLED", "REJECTED": "REJECTED"}


class Reconciler:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self.status = "UNKNOWN"
        self.last: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._pending: asyncio.Task | None = None
        self._task: asyncio.Task | None = None

    async def request(self, trigger: str, correlation_id: str | None = None, delay_s: float = 0.3) -> None:
        """Schedule a reconciliation soon (coalesces bursts of requests)."""
        if self._pending is not None and not self._pending.done():
            return

        async def later():
            await asyncio.sleep(delay_s)
            await self.run(trigger, correlation_id)

        self._pending = asyncio.create_task(later())

    def cancel_pending(self) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
        self._pending = None

    async def _external(self) -> dict[str, set[str]]:
        async with self.sys.db.session() as s:
            row = await s.get(SystemState, EXTERNAL_KEY)
        v = row.value if row else {}
        return {"fills": set(v.get("fills", [])), "orders": set(v.get("orders", []))}

    async def adopt_baseline(self, actor: str) -> dict[str, Any]:
        """Acknowledge broker fills/orders the platform cannot attribute as external to it. Refused unless the
        broker's net position equals the platform's for every symbol (nothing unexplained is left open)."""
        sys = self.sys
        gw = sys.broker
        async with self._lock:
            b_orders = await gw.call("get_orders", gw.adapter.get_orders)
            b_fills = await gw.call("get_fills", gw.adapter.get_fills)
            b_positions = await gw.call("get_positions", gw.adapter.get_positions)
            internal_net = sys.positions.net_by_symbol()
            broker_net = {p.symbol: p.quantity for p in b_positions}
            diff = {sym: (internal_net.get(sym, Decimal(0)), broker_net.get(sym, Decimal(0)))
                    for sym in set(internal_net) | set(broker_net)
                    if internal_net.get(sym, Decimal(0)) != broker_net.get(sym, Decimal(0))}
            if diff:
                raise ValueError("broker and platform positions differ (" + ", ".join(
                    f"{sym}: platform {i:+} vs broker {b:+}" for sym, (i, b) in sorted(diff.items()))
                    + ") - flatten on the broker or resolve the mismatch first")
            async with sys.db.session() as s:
                known_f = set((await s.execute(select(Fill.broker_fill_id))).scalars())
                known_o = set((await s.execute(select(Order.client_order_id))).scalars())
            ext = await self._external()
            new_f = {f.fill_id for f in b_fills if f.fill_id not in known_f} - ext["fills"]
            new_o = {o.broker_order_id for o in b_orders if o.client_order_id not in known_o} - ext["orders"]
            val = {"fills": sorted(ext["fills"] | new_f), "orders": sorted(ext["orders"] | new_o),
                   "adopted_at": utcnow().isoformat(), "by": actor}
            async with sys.db.transaction() as s:
                row = await s.get(SystemState, EXTERNAL_KEY)
                if row:
                    row.value = val
                else:
                    s.add(SystemState(key=EXTERNAL_KEY, value=val))
        await sys.audit.record("RECON_BASELINE_ADOPTED", f"{actor} acknowledged {len(new_f)} broker fill(s) and {len(new_o)} "
                               "broker order(s) the platform has no record of as external (positions matched)",
                               source="Reconciler", severity="WARN", actor=actor,
                               payload={"fills": sorted(new_f), "orders": sorted(new_o)})
        return {"acknowledged_fills": len(new_f), "acknowledged_orders": len(new_o),
                "reconciliation": await self.run("BASELINE")}

    async def run(self, trigger: str, correlation_id: str | None = None) -> dict[str, Any]:
        async with self._lock:
            try:
                return await self._run(trigger, correlation_id or new_id())
            except Exception as e:
                log.exception("reconciliation crashed")
                self.status = RECON_FAILED
                self.sys.health.error("reconciliation", f"crashed: {e}")
                await self.sys.controller.block("reconciliation", f"reconciliation failed: {e}")
                return {"status": RECON_FAILED, "summary": str(e)}

    async def _run(self, trigger: str, corr: str) -> dict[str, Any]:
        sys = self.sys
        gw = sys.broker
        started = utcnow()
        async with sys.db.transaction() as s:
            rec = ReconciliationEvent(trigger=trigger, status="RUNNING", correlation_id=corr, started_at=started)
            s.add(rec)
        await sys.bus.publish(Event("reconciliation.started", "Reconciler", correlation_id=corr,
                                    payload={"id": rec.id, "trigger": trigger}))
        mismatches: list[dict] = []
        actions: list[dict] = []
        unresolved: list[dict] = []

        # ---------------------------------------------------------- fetch authoritative broker state
        try:
            b_orders = await gw.call("get_orders", gw.adapter.get_orders)
            b_fills = await gw.call("get_fills", gw.adapter.get_fills)
            b_positions = await gw.call("get_positions", gw.adapter.get_positions)
            b_account = await gw.call("get_account", gw.adapter.get_account)
        except (BrokerError, asyncio.TimeoutError) as e:
            summary = f"broker unreachable - cannot verify state ({e or type(e).__name__}); new orders BLOCKED"
            await self._finish(rec.id, RECON_FAILED, [], [], summary, {})
            self.status = RECON_FAILED
            self.sys.health.error("reconciliation", summary)
            await sys.controller.block("reconciliation", "reconciliation failed: broker state unverifiable")
            return self.last

        before_net = sys.positions.net_by_symbol()

        # ---------------------------------------------------------- 1. fills
        async with sys.db.session() as s:
            known = set((await s.execute(select(Fill.broker_fill_id))).scalars())
        ext = await self._external()
        missing = [f for f in b_fills if f.fill_id not in known and f.fill_id not in ext["fills"]]
        for f in missing:
            mismatches.append({"type": "MISSING_FILL", "detail": f"broker fill {f.fill_id} ({f.side} {f.quantity} {f.symbol} "
                                                                 f"@ {f.price}) missing internally"})
            ok = await sys.orders.ingest_fill(f, "RECONCILIATION")
            if ok:
                actions.append({"action": "FILL_INGESTED", "detail": f"applied broker fill {f.fill_id} to internal ledger"})
            else:
                unresolved.append({"type": "MISSING_FILL", "detail": f"could not attribute fill {f.fill_id}"})

        # ---------------------------------------------------------- 2. orders
        by_client = {o.client_order_id: o for o in b_orders}
        now = utcnow()
        async with sys.db.session() as s:
            internal_open = (await s.execute(select(Order).where(Order.status.in_(
                ("CREATED", "RISK_CHECK", "APPROVED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "TIMEOUT"))))).scalars().all()
        for o in internal_open:
            bo = by_client.get(o.client_order_id)
            oid = str(o.id)
            age = (now - (o.submitted_at or o.created_at)).total_seconds()
            if bo is None:
                if o.status in ("SUBMITTED", "TIMEOUT") and age > 3:
                    mismatches.append({"type": "ORDER_NOT_AT_BROKER", "detail": f"order {oid[:8]} is {o.status} internally but unknown to broker"})
                    await sys.orders._transition(oid, "CANCELLED", "reconciliation: order never reached the broker",
                                                 reject_rule="NOT_AT_BROKER", reject_reason="never reached broker (reconciliation)")
                    await sys.controller.unblock(f"uncertain:{oid}", "- reconciliation confirmed order absent at broker")
                    actions.append({"action": "ORDER_CLOSED", "detail": f"order {oid[:8]} marked CANCELLED (absent at broker)"})
                elif o.status in ("CREATED", "RISK_CHECK", "APPROVED") and (
                        age > 30 or (trigger in ("STARTUP", "RESTART") and oid not in sys.orders.open_orders)):
                    # after a restart nothing is evaluating these anymore
                    mismatches.append({"type": "STUCK_ORDER", "detail": f"order {oid[:8]} stuck in {o.status}"})
                    await sys.orders._transition(oid, "CANCELLED", "reconciliation: stuck before submission", force=True)
                    actions.append({"action": "ORDER_CLOSED", "detail": f"stuck order {oid[:8]} cancelled"})
                continue
            target = BROKER_TO_INTERNAL.get(bo.status, o.status)
            async with sys.db.session() as s:
                cur = await s.get(Order, o.id)
            if cur.status != target or cur.broker_order_id != bo.broker_order_id or cur.filled_quantity != bo.filled_quantity:
                if cur.status != target or cur.filled_quantity != bo.filled_quantity:
                    mismatches.append({"type": "ORDER_STATUS", "detail": f"order {oid[:8]}: internal {cur.status} "
                                       f"{cur.filled_quantity}/{cur.quantity}, broker {bo.status} {bo.filled_quantity}/{bo.quantity}"})
                if cur.status != target:
                    await sys.orders._transition(oid, target, f"reconciliation: broker reports {bo.status}",
                                                 {"broker_status": bo.status}, force=True, broker_order_id=bo.broker_order_id,
                                                 reject_rule=None if target != "REJECTED" else "BROKER_REJECTED",
                                                 reject_reason=bo.reject_reason if target == "REJECTED" else None)
                    actions.append({"action": "ORDER_STATUS_SYNCED", "detail": f"order {oid[:8]} -> {target} (broker truth)"})
                elif cur.broker_order_id != bo.broker_order_id:
                    await sys.orders._transition(oid, target, "reconciliation: broker id attached", force=True,
                                                 broker_order_id=bo.broker_order_id)
                if cur.filled_quantity != bo.filled_quantity:
                    unresolved.append({"type": "ORDER_FILL_QTY", "detail": f"order {oid[:8]} filled {cur.filled_quantity} "
                                                                           f"internally vs {bo.filled_quantity} at broker"})
            if target in ("ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED"):
                await sys.controller.unblock(f"uncertain:{oid}", "- reconciled with broker")
                if target in ("FILLED", "CANCELLED", "REJECTED"):
                    sys.orders.open_orders.pop(oid, None)
        async with sys.db.session() as s:
            all_ids = set((await s.execute(select(Order.client_order_id))).scalars())
        unnumbered = 0
        for bo in b_orders:
            if str(bo.broker_order_id or "0") == "0" and not bo.filled_quantity:
                # 021 lists orders it rejected before numbering them with orderId 0; nothing filled, no effect on
                # positions, and they cannot be matched to anything - informational, never a reason to block
                unnumbered += 1
                continue
            if bo.client_order_id not in all_ids and bo.broker_order_id not in ext["orders"]:
                unresolved.append({"type": "ORPHAN_BROKER_ORDER",
                                   "detail": f"broker order {bo.broker_order_id} ({bo.side} {bo.quantity} {bo.symbol}) has no internal record"})

        # ---------------------------------------------------------- 3. position cache vs fills ledger
        async with sys.db.session() as s:
            keys = [(p.strategy_id, p.symbol) for p in (await s.execute(select(Position))).scalars().all()]
            fill_keys = set((await s.execute(select(Fill.strategy_id, Fill.symbol).distinct())).all())
        for key in set(keys) | {tuple(k) for k in fill_keys}:
            async with sys.db.transaction() as s:
                before, after = await sys.positions.rebuild_from_ledger(s, key[0], key[1], f"reconciliation ({trigger})", corr)
            sys.positions.commit_cache(after)
            if (before.quantity, before.avg_price, before.realized_pnl) != (after.quantity, after.avg_price, after.realized_pnl):
                mismatches.append({"type": "POSITION_DRIFT", "detail": f"{key[0]} {key[1]}: internal position {before.quantity} "
                                   f"@ {before.avg_price} disagreed with fills ledger ({after.quantity} @ {after.avg_price})"})
                actions.append({"action": "POSITION_REBUILT", "detail": f"{key[0]} {key[1]} rebuilt from executions -> {after.quantity}"})

        # ---------------------------------------------------------- 4. aggregate positions vs broker
        internal_net = sys.positions.net_by_symbol()
        broker_net = {p.symbol: p.quantity for p in b_positions}
        comparison = []
        for sym in sorted(set(internal_net) | set(broker_net) | set(before_net)):
            i_b, i_a, b = before_net.get(sym, Decimal(0)), internal_net.get(sym, Decimal(0)), broker_net.get(sym, Decimal(0))
            if i_b == 0 and i_a == 0 and b == 0:
                continue
            st = "MATCH" if i_a == b else "MISMATCH"
            comparison.append({"symbol": sym, "internal_before": i_b, "broker": b, "internal_after": i_a,
                               "status": st if i_b == b else ("RESOLVED" if st == "MATCH" else "MISMATCH")})
            if i_b != b:
                mismatches.append({"type": "POSITION_MISMATCH", "detail": f"{sym}: internal {i_b:+} vs broker {b:+}"})
            if i_a != b:
                unresolved.append({"type": "POSITION_MISMATCH", "detail": f"{sym}: internal {i_a:+} vs broker {b:+} after repair"})

        # ---------------------------------------------------------- 5. account cash
        if b_account.extra.get("funds_api") is False:
            account = {"funds_api": False, "note": f"{gw.adapter.name} exposes no funds endpoint - cash not reconciled"}
        else:
            ext_cash = sum(((-f.quantity * f.price) if f.side == "BUY" else (f.quantity * f.price)
                            for f in b_fills if f.fill_id in ext["fills"]), Decimal(0))
            account = await self._cash_check(b_account, unresolved, ext_cash)

        # ---------------------------------------------------------- outcome
        if unresolved:
            status = UNRESOLVED
        elif mismatches:
            status = RESOLVED
        else:
            status = MATCHED
        summary = {MATCHED: "internal state matches broker", RESOLVED: f"{len(mismatches)} mismatch(es) detected and repaired "
                   "from broker executions", UNRESOLVED: f"{len(unresolved)} unresolved mismatch(es) - new orders BLOCKED"}[status]
        details = {"comparison": comparison, "account": account, "unresolved": unresolved,
                   "broker_orders": len(b_orders), "broker_fills": len(b_fills), "unnumbered_rejected_at_broker": unnumbered,
                   "external_acknowledged": {"fills": sum(1 for f in b_fills if f.fill_id in ext["fills"]),
                                             "orders": sum(1 for o in b_orders if o.broker_order_id in ext["orders"])}}
        await self._finish(rec.id, status, mismatches, actions, summary, details)
        self.status = status
        if status == UNRESOLVED:
            await sys.controller.block("reconciliation", "unresolved reconciliation mismatch: " + unresolved[0]["detail"])
            sys.health.error("reconciliation", summary, WARNING)
        else:
            await sys.controller.unblock("reconciliation", f"- reconciliation {status}")
            sys.health.beat("reconciliation", HEALTHY, f"{status} ({trigger})")
        sev = "INFO" if status == MATCHED else "WARN"
        await sys.audit.record("RECONCILIATION", f"reconciliation ({trigger}): {summary}", source="Reconciler", severity=sev,
                               correlation_id=corr, payload={"status": status, "mismatches": len(mismatches),
                                                             "actions": len(actions)})
        await sys.pnl.publish()
        return self.last

    async def _cash_check(self, b_account, unresolved: list, external_cash: Decimal = Decimal(0)) -> dict:
        async with self.sys.db.session() as s:
            fills = (await s.execute(select(Fill.side, Fill.quantity, Fill.price))).all()
        cash_flow = sum(((-q * p) if side == "BUY" else (q * p) for side, q, p in fills), Decimal(0))
        expected_cash = b_account.starting_cash + cash_flow + external_cash  # + acknowledged external trades
        cash_diff = (b_account.cash - expected_cash).quantize(Decimal("0.01"))
        if abs(cash_diff) > Decimal("1"):
            unresolved.append({"type": "CASH_MISMATCH", "detail": f"broker cash differs from internal ledger by {cash_diff}"})
        return {"broker_cash": b_account.cash, "internal_expected_cash": expected_cash.quantize(Decimal("0.01")),
                "difference": cash_diff, "broker_margin_used": b_account.margin_used}

    async def _finish(self, rec_id: int, status: str, mismatches: list, actions: list, summary: str, details: dict) -> None:
        import json

        from app.core.events import json_default

        safe = json.loads(json.dumps({"m": mismatches, "a": actions, "d": details}, default=json_default))
        async with self.sys.db.transaction() as s:
            rec = await s.get(ReconciliationEvent, rec_id)
            rec.status, rec.mismatches, rec.summary, rec.finished_at = status, safe["m"], summary, utcnow()
            rec.actions, rec.details = safe["a"], safe["d"]
            out = {"id": rec.id, "trigger": rec.trigger, "status": status, "summary": summary, "mismatches": safe["m"],
                   "actions": safe["a"], "details": safe["d"], "started_at": rec.started_at.isoformat(),
                   "finished_at": rec.finished_at.isoformat(), "correlation_id": rec.correlation_id}
        self.last = out
        await self.sys.bus.publish(Event("reconciliation.completed", "Reconciler", correlation_id=rec.correlation_id,
                                         payload=out))

    async def periodic(self) -> None:
        while True:
            # after a failed run (e.g. 021 briefly unreachable) re-check soon, so a transient failure does not keep
            # orders blocked for a whole interval
            failed = self.status == RECON_FAILED
            await asyncio.sleep(min(FAILED_RETRY_S, self.sys.settings.reconcile_interval_s) if failed
                                else self.sys.settings.reconcile_interval_s)
            if self.sys.account is None:
                continue
            await self.run("PERIODIC")
