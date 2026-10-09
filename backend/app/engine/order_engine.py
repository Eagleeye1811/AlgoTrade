"""Order Engine - the ONLY path to the broker.

Lifecycle: CREATED -> RISK_CHECK -> APPROVED -> SUBMITTED -> ACKNOWLEDGED -> PARTIALLY_FILLED -> FILLED
Alternatives: REJECTED | CANCELLED | TIMEOUT. Every transition is persisted (orders + order_events).

Timeout handling never blindly retries:
    submit -> timeout -> query broker by idempotency key -> found? adopt (no duplicate) : controlled retry
    -> still unknown? TIMEOUT + block new orders + reconciliation
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.brokers.base import BrokerOrder, BrokerRejected, BrokerTimeout, BrokerUnavailable, ExecutionReport, OrderUpdate, \
    PlaceOrderRequest
from app.core.events import Event, utcnow
from app.database.models import Fill, Order, OrderEvent
from app.engine.types import OPEN_STATUSES, TERMINAL, OpenOrderInfo, OrderIntent

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

log = logging.getLogger("algotrade.orders")

ALLOWED = {
    "CREATED": {"RISK_CHECK", "REJECTED", "CANCELLED"},
    "RISK_CHECK": {"APPROVED", "REJECTED"},
    "APPROVED": {"SUBMITTED", "CANCELLED", "REJECTED"},
    "SUBMITTED": {"SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "REJECTED", "TIMEOUT", "CANCELLED"},
    "ACKNOWLEDGED": {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "TIMEOUT", "REJECTED"},
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "TIMEOUT"},
    # TIMEOUT means "outcome unknown/expired"; broker truth (late reports, reconciliation) may override it
    "TIMEOUT": {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED"},
}


def order_to_dict(o: Order) -> dict[str, Any]:
    return {
        "id": str(o.id), "client_order_id": o.client_order_id, "strategy_id": o.strategy_id, "symbol": o.symbol,
        "side": o.side, "order_type": o.order_type, "quantity": o.quantity, "filled_quantity": o.filled_quantity,
        "remaining_quantity": o.quantity - o.filled_quantity, "avg_fill_price": o.avg_fill_price,
        "limit_price": o.limit_price, "reference_price": o.reference_price, "status": o.status,
        "reject_rule": o.reject_rule, "reject_reason": o.reject_reason, "broker_order_id": o.broker_order_id,
        "correlation_id": o.correlation_id, "signal_id": str(o.signal_id) if o.signal_id else None, "origin": o.origin,
        "reduce_only": o.reduce_only, "submit_attempts": o.submit_attempts, "latency_ms": o.latency_ms,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
        "acknowledged_at": o.acknowledged_at.isoformat() if o.acknowledged_at else None,
        "completed_at": o.completed_at.isoformat() if o.completed_at else None,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
    }


class OrderEngine:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self.open_orders: dict[str, OpenOrderInfo] = {}
        self.slippage: deque[float] = deque(maxlen=20)
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._cancel_intent: dict[str, tuple[str, str]] = {}
        self._task: asyncio.Task | None = None
        self.stats = {"submitted": 0, "filled": 0, "rejected": 0, "timeouts": 0, "duplicates_blocked": 0}

    # ------------------------------------------------------------------ state
    async def load(self) -> None:
        async with self.sys.db.session() as s:
            rows = (await s.execute(select(Order).where(Order.status.in_(OPEN_STATUSES + ("TIMEOUT",))))).scalars().all()
        self.open_orders = {}
        for o in rows:
            if o.status == "TIMEOUT" and o.reject_rule != "STATUS_UNKNOWN":
                continue
            if o.status in ("CREATED", "RISK_CHECK", "APPROVED"):
                continue  # never reached the broker; startup reconciliation closes these
            self.open_orders[str(o.id)] = self._info(o)
            if o.status == "TIMEOUT":
                await self.sys.controller.block(f"uncertain:{o.id}", f"order {str(o.id)[:8]} status unknown since before restart")

    def _info(self, o: Order) -> OpenOrderInfo:
        return OpenOrderInfo(str(o.id), o.client_order_id, o.strategy_id, o.symbol, o.side, o.quantity,
                             o.filled_quantity, o.order_type, o.reduce_only, o.reference_price, o.broker_order_id,
                             o.status, time.monotonic(), o.origin)

    def recent_slippage_bps(self) -> float | None:
        return sum(self.slippage) / len(self.slippage) if len(self.slippage) >= 3 else None

    def _lock(self, strategy_id: str, symbol: str) -> asyncio.Lock:
        return self._locks.setdefault((strategy_id, symbol), asyncio.Lock())

    async def _transition(self, order_id: str, status: str, message: str, data: dict | None = None,
                          force: bool = False, **fields) -> Order | None:
        async with self.sys.db.transaction() as s:
            o = await s.get(Order, uuid.UUID(order_id), with_for_update=True)
            if o is None:
                return None
            if not force and status not in ALLOWED.get(o.status, set()):
                log.warning("ignored invalid transition %s -> %s for %s", o.status, status, order_id)
                return None
            o.status = status
            for k, v in fields.items():
                setattr(o, k, v)
            if status in TERMINAL and o.completed_at is None:
                o.completed_at = utcnow()
            s.add(OrderEvent(order_id=o.id, status=status, message=message, data=data or {}))
        await self._publish_order(o, status, message)
        return o

    async def _event(self, order_id: str, label: str, message: str, data: dict | None = None) -> None:
        """Lifecycle annotation that is not a status change (e.g. UNCERTAIN, VERIFIED_AT_BROKER)."""
        async with self.sys.db.transaction() as s:
            s.add(OrderEvent(order_id=uuid.UUID(order_id), status=label, message=message, data=data or {}))
        await self.sys.bus.publish(Event("order.event", "OrderEngine", order_id=order_id,
                                         payload={"label": label, "message": message, **(data or {})}))

    async def _publish_order(self, o: Order, status: str, message: str) -> None:
        info = self.open_orders.get(str(o.id))
        if info is not None:
            info.status, info.filled, info.broker_order_id = o.status, o.filled_quantity, o.broker_order_id
            if o.status in TERMINAL and not (o.status == "TIMEOUT" and o.reject_rule == "STATUS_UNKNOWN"):
                self.open_orders.pop(str(o.id), None)
        self.sys.health.beat("order_engine", detail=f"{len(self.open_orders)} open orders")
        await self.sys.bus.publish(Event("order.update", "OrderEngine", correlation_id=o.correlation_id,
                                         strategy_id=o.strategy_id, order_id=str(o.id), symbol=o.symbol,
                                         payload={"order": order_to_dict(o), "transition": status, "message": message}))

    # ------------------------------------------------------------------ submit
    async def submit(self, intent: OrderIntent) -> dict[str, Any]:
        key = intent.idempotency_key or f"AT-{uuid.uuid4().hex[:20]}"
        async with self._lock(intent.strategy_id, intent.symbol):
            ref = self.sys.market.ltp(intent.symbol)
            try:
                async with self.sys.db.transaction() as s:
                    o = Order(client_order_id=key, strategy_id=intent.strategy_id, symbol=intent.symbol,
                              side=intent.side, order_type=intent.order_type, quantity=intent.quantity,
                              filled_quantity=Decimal(0), limit_price=intent.limit_price, reference_price=ref,
                              status="CREATED", correlation_id=intent.correlation_id, signal_id=intent.signal_id,
                              origin=intent.origin, reduce_only=intent.reduce_only)
                    s.add(o)
                    await s.flush()
                    s.add(OrderEvent(order_id=o.id, status="CREATED",
                                     message=f"{intent.origin}: {intent.side} {intent.quantity} {intent.symbol} {intent.order_type}"
                                             + (f" - {intent.reason}" if intent.reason else ""),
                                     data={"idempotency_key": key, "reduce_only": intent.reduce_only}))
            except IntegrityError:
                return await self._duplicate_key(key, intent)
            oid = str(o.id)
            await self._publish_order(o, "CREATED", "order created")
            await self.sys.audit.record("ORDER_CREATED", f"order {oid[:8]} created: {intent.side} {intent.quantity} {intent.symbol} for {intent.strategy_id} "
                                        f"({intent.origin})",
                                        source="OrderEngine", correlation_id=intent.correlation_id,
                                        strategy_id=intent.strategy_id, order_id=oid, symbol=intent.symbol,
                                        payload={"origin": intent.origin, "idempotency_key": key}, broadcast=False)

            await self._transition(oid, "RISK_CHECK", "evaluating Risk Firewall")
            decision = await self.sys.risk.evaluate(intent, oid)
            if not decision.approved:
                f = decision.primary_failure
                self.stats["rejected"] += 1
                o = await self._transition(oid, "REJECTED", f"risk rejected: {f.message or f.label}",
                                           decision.to_dict(), reject_rule=f.rule,
                                           reject_reason=f"{f.label}: {f.message} (actual {f.actual}, allowed {f.allowed})")
                return {"order": order_to_dict(o), "risk": decision.to_dict()}
            changes = {}
            if decision.order_type != intent.order_type:
                changes = {"order_type": decision.order_type, "limit_price": decision.limit_price}
            o = await self._transition(oid, "APPROVED", "all risk checks passed" + (
                f" - converted to protective LIMIT @ {decision.limit_price}" if changes else ""), decision.to_dict(), **changes)
            self.open_orders[oid] = self._info(o)
            result = await self._send(oid)
            result["risk"] = decision.to_dict()
            return result

    async def _duplicate_key(self, key: str, intent: OrderIntent) -> dict[str, Any]:
        self.stats["duplicates_blocked"] += 1
        async with self.sys.db.session() as s:
            existing = (await s.execute(select(Order).where(Order.client_order_id == key))).scalar_one()
        await self.sys.audit.record("DUPLICATE_ORDER_BLOCKED",
                                    f"duplicate submission with idempotency key {key} blocked - existing order "
                                    f"{str(existing.id)[:8]} ({existing.status}) returned instead",
                                    source="OrderEngine", severity="WARN", correlation_id=intent.correlation_id,
                                    strategy_id=intent.strategy_id, order_id=str(existing.id), symbol=intent.symbol)
        return {"duplicate": True, "order": order_to_dict(existing)}

    async def _send(self, oid: str) -> dict[str, Any]:
        async with self.sys.db.session() as s:
            o = await s.get(Order, uuid.UUID(oid))
        attempt = o.submit_attempts + 1
        o = await self._transition(oid, "SUBMITTED", f"sent to {self.sys.broker.adapter.name} (attempt {attempt})",
                                   {"attempt": attempt}, submit_attempts=attempt, submitted_at=utcnow())
        self.stats["submitted"] += 1
        req = PlaceOrderRequest(o.client_order_id, o.symbol, o.side, o.quantity, o.order_type, o.limit_price,
                                tag=o.strategy_id)
        t0 = time.perf_counter()
        try:
            bo = await self.sys.broker.call(
                "place_order", lambda: asyncio.wait_for(self.sys.broker.adapter.place_order(req),
                                                        self.sys.settings.broker_timeout_s + 1))
        except BrokerRejected as e:
            self.stats["rejected"] += 1
            o = await self._transition(oid, "REJECTED", f"broker rejected: {e.reason}", reject_rule="BROKER_REJECTED",
                                       reject_reason=e.reason, latency_ms=int((time.perf_counter() - t0) * 1000))
            await self.sys.audit.record("BROKER_REJECTED", f"broker rejected order {oid[:8]}: {e.reason}", source="BrokerAdapter",
                                        severity="WARN", correlation_id=o.correlation_id, strategy_id=o.strategy_id,
                                        order_id=oid, symbol=o.symbol)
            return {"order": order_to_dict(o)}
        except (BrokerTimeout, BrokerUnavailable, asyncio.TimeoutError) as e:
            return await self._resolve_uncertain(oid, str(e) or type(e).__name__)
        latency = int((time.perf_counter() - t0) * 1000)
        o = await self._on_ack(oid, bo, latency)
        return {"order": order_to_dict(o)}

    async def _on_ack(self, oid: str, bo: BrokerOrder, latency_ms: int | None, note: str = "") -> Order:
        async with self.sys.db.session() as s:
            cur = await s.get(Order, uuid.UUID(oid))
        fields = {"broker_order_id": bo.broker_order_id, "acknowledged_at": utcnow()}
        if latency_ms is not None:
            fields["latency_ms"] = latency_ms
        if cur.status in ("SUBMITTED", "TIMEOUT"):
            o = await self._transition(oid, "ACKNOWLEDGED", f"broker acknowledged as {bo.broker_order_id}{note}",
                                       {"broker_status": bo.status}, **fields)
            return o or cur
        # fills arrived before the ack (fast venue): just record broker id
        async with self.sys.db.transaction() as s:
            o = await s.get(Order, uuid.UUID(oid), with_for_update=True)
            for k, v in fields.items():
                setattr(o, k, v)
            s.add(OrderEvent(order_id=o.id, status="ACK_RECEIVED", message=f"late acknowledgement {bo.broker_order_id}{note}"))
        return o

    async def _resolve_uncertain(self, oid: str, err: str) -> dict[str, Any]:
        async with self.sys.db.session() as s:
            o = await s.get(Order, uuid.UUID(oid))
        corr = o.correlation_id
        await self._event(oid, "UNCERTAIN", f"broker call failed ({err}) - order state UNKNOWN, NOT retrying blindly")
        await self.sys.controller.block(f"uncertain:{oid}", f"order {oid[:8]} status uncertain after broker failure")
        await self.sys.audit.record("ORDER_STATUS_UNCERTAIN", f"order {oid[:8]}: {err}; querying broker by idempotency key "
                                    f"{o.client_order_id}", source="OrderEngine", severity="WARN", correlation_id=corr,
                                    strategy_id=o.strategy_id, order_id=oid, symbol=o.symbol)
        for attempt, backoff in enumerate((0.5, 1.0, 2.0), start=1):
            try:
                bo = await self.sys.broker.call(
                    "get_order_by_client_id",
                    lambda: asyncio.wait_for(self.sys.broker.adapter.get_order_by_client_id(o.client_order_id),
                                             self.sys.settings.broker_timeout_s + 1))
            except (BrokerTimeout, BrokerUnavailable, asyncio.TimeoutError) as e:
                await self._event(oid, "VERIFY_FAILED", f"status query {attempt}/3 failed: {e or type(e).__name__}")
                await asyncio.sleep(backoff)
                continue
            if bo is not None:
                self.stats["duplicates_blocked"] += 1
                await self._event(oid, "VERIFIED_AT_BROKER",
                                  f"order FOUND at broker as {bo.broker_order_id} ({bo.status}) - duplicate retry suppressed",
                                  {"broker_status": bo.status})
                await self.sys.audit.record("DUPLICATE_ORDER_PREVENTED",
                                            f"order {oid[:8]} existed at broker after timeout - retry suppressed, no duplicate placed",
                                            source="OrderEngine", severity="WARN", correlation_id=corr,
                                            strategy_id=o.strategy_id, order_id=oid, symbol=o.symbol)
                if bo.status == "REJECTED":
                    o2 = await self._transition(oid, "REJECTED", f"broker rejected: {bo.reject_reason}",
                                                reject_rule="BROKER_REJECTED", reject_reason=bo.reject_reason,
                                                broker_order_id=bo.broker_order_id)
                else:
                    o2 = await self._on_ack(oid, bo, None, " (verified after timeout)")
                await self.sys.controller.unblock(f"uncertain:{oid}", "- order state verified at broker")
                await self.sys.reconciler.request("ORDER_TIMEOUT", correlation_id=corr)
                return {"order": order_to_dict(o2), "verified_after_timeout": True}
            # Broker answered definitively: the order does not exist -> one controlled retry with the SAME key
            if o.submit_attempts < 2:
                await self._event(oid, "NOT_FOUND_AT_BROKER", "order not found at broker - controlled retry with same idempotency key")
                await self.sys.controller.unblock(f"uncertain:{oid}", "- order confirmed absent at broker")
                return await self._send(oid)
            break
        self.stats["timeouts"] += 1
        o2 = await self._transition(oid, "TIMEOUT", "broker status could not be confirmed - reconciliation required",
                                    reject_rule="STATUS_UNKNOWN", reject_reason=f"unconfirmed after broker failure: {err}")
        await self.sys.reconciler.request("ORDER_TIMEOUT", correlation_id=corr)
        return {"order": order_to_dict(o2) if o2 else None, "uncertain": True}

    # ------------------------------------------------------------------ execution reports
    async def on_execution(self, rep: ExecutionReport) -> None:
        await self.ingest_fill(rep, "EXECUTION_REPORT")

    async def ingest_fill(self, rep: ExecutionReport, source: str) -> bool:
        """Idempotent fill processing. Fill + order + position are updated in ONE transaction."""
        async with self.sys.db.transaction() as s:
            o = (await s.execute(select(Order).where(Order.client_order_id == rep.client_order_id)
                                 .with_for_update())).scalar_one_or_none()
            if o is None:
                await self.sys.audit.record("ORPHAN_FILL", f"fill {rep.fill_id} for unknown order {rep.client_order_id}",
                                            source="OrderEngine", severity="ERROR", symbol=rep.symbol, session=s)
                return False
            ins = pg_insert(Fill).values(
                id=uuid.uuid4(), broker_fill_id=rep.fill_id, order_id=o.id, strategy_id=o.strategy_id, symbol=rep.symbol,
                side=rep.side, quantity=rep.quantity, price=rep.price, reference_price=rep.reference_price,
                slippage_bps=((rep.price - rep.reference_price) / rep.reference_price * 10000 *
                              (1 if rep.side == "BUY" else -1)).quantize(Decimal("0.01")),
                latency_ms=rep.latency_ms, filled_at=rep.ts, source=source,
            ).on_conflict_do_nothing(index_elements=[Fill.broker_fill_id]).returning(Fill.id)
            fid = (await s.execute(ins)).scalar_one_or_none()
            if fid is None:
                return False  # duplicate execution report - already processed
            fill = await s.get(Fill, fid)
            prev_filled = o.filled_quantity
            new_filled = min(o.quantity, prev_filled + rep.quantity)
            prev_val = (o.avg_fill_price or Decimal(0)) * prev_filled
            o.avg_fill_price = ((prev_val + rep.price * rep.quantity) / (prev_filled + rep.quantity)).quantize(Decimal("0.0001"))
            o.filled_quantity = new_filled
            if o.broker_order_id is None:
                o.broker_order_id = rep.broker_order_id
            old_status = o.status
            new_status = "FILLED" if new_filled >= o.quantity else "PARTIALLY_FILLED"
            if new_status in ALLOWED.get(old_status, set()) or old_status in ("CANCELLED",):
                o.status = new_status
            if o.status == "FILLED" and o.completed_at is None:
                o.completed_at = utcnow()
            if old_status == "TIMEOUT":
                o.reject_rule, o.reject_reason = None, None
            s.add(OrderEvent(order_id=o.id, status=o.status,
                             message=f"fill {rep.quantity} @ {rep.price} ({source.lower().replace('_', ' ')})",
                             data={"fill_id": rep.fill_id, "qty": str(rep.quantity), "price": str(rep.price),
                                   "cumulative": str(new_filled), "latency_ms": rep.latency_ms}))
            pos = await self.sys.positions.apply_fill(s, fill, o.correlation_id)
        self.sys.positions.commit_cache(pos)
        self.slippage.append(float(fill.slippage_bps or 0))
        if old_status == "TIMEOUT" and o.reject_rule is None:
            await self.sys.controller.unblock(f"uncertain:{o.id}", "- broker fill confirmed order")
        self.stats["filled"] += 1
        oid = str(o.id)
        if o.status in TERMINAL:
            self.open_orders.pop(oid, None)
        elif oid in self.open_orders:
            self.open_orders[oid].filled, self.open_orders[oid].status = o.filled_quantity, o.status
        else:
            self.open_orders[oid] = self._info(o)
        bus = self.sys.bus
        await bus.publish(Event("fill.received", "Broker", correlation_id=o.correlation_id, strategy_id=o.strategy_id,
                                order_id=oid, symbol=o.symbol, payload={
                                    "fill_id": rep.fill_id, "side": rep.side, "quantity": rep.quantity, "price": rep.price,
                                    "reference_price": rep.reference_price, "slippage_bps": fill.slippage_bps,
                                    "latency_ms": rep.latency_ms, "source": source}))
        await self._publish_order(o, o.status, f"fill {rep.quantity} @ {rep.price}")
        await bus.publish(Event("position.update", "PositionManager", correlation_id=o.correlation_id,
                                strategy_id=o.strategy_id, symbol=o.symbol, payload={
                                    "quantity": pos.quantity, "avg_price": pos.avg_price, "realized_pnl": pos.realized_pnl,
                                    "side": pos.side}))
        await self.sys.audit.record("FILL_RECEIVED", f"{o.strategy_id}: {rep.side} {rep.quantity} {rep.symbol} @ {rep.price} -> strategy "
                                    f"position {pos.quantity} ({source})", source="Broker", correlation_id=o.correlation_id,
                                    strategy_id=o.strategy_id, order_id=oid, symbol=o.symbol,
                                    payload={"fill_id": rep.fill_id, "latency_ms": rep.latency_ms}, broadcast=False)
        await self.sys.pnl.publish()
        return True

    async def on_order_update(self, upd: OrderUpdate) -> None:
        async with self.sys.db.session() as s:
            o = (await s.execute(select(Order).where(Order.client_order_id == upd.client_order_id))).scalar_one_or_none()
        if o is None:
            return
        if upd.status == "CANCELLED":
            final, reason = self._cancel_intent.pop(o.client_order_id, ("CANCELLED", upd.reason or "cancelled"))
            await self._transition(str(o.id), final, reason, {"broker_status": upd.status})

    async def cancel(self, order_id: str, reason: str = "cancelled by user", final_status: str = "CANCELLED") -> dict:
        async with self.sys.db.session() as s:
            o = await s.get(Order, uuid.UUID(order_id))
        if o is None:
            raise ValueError("order not found")
        if o.status not in ("APPROVED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "CREATED"):
            raise ValueError(f"order is {o.status} - cannot cancel")
        if o.broker_order_id is None:
            o2 = await self._transition(order_id, final_status, reason)
            return order_to_dict(o2 or o)
        self._cancel_intent[o.client_order_id] = (final_status, reason)
        try:
            await self.sys.broker.call("cancel_order", lambda: self.sys.broker.adapter.cancel_order(o.broker_order_id))
        except BrokerRejected as e:
            self._cancel_intent.pop(o.client_order_id, None)
            raise ValueError(f"broker refused cancel: {e.reason}")
        await self.sys.audit.record("ORDER_CANCEL_REQUESTED", f"cancel requested for {order_id[:8]}: {reason}",
                                    source="OrderEngine", order_id=order_id, strategy_id=o.strategy_id, symbol=o.symbol,
                                    correlation_id=o.correlation_id)
        return order_to_dict(o)

    # ------------------------------------------------------------------ background: limit order expiry
    async def sweeper(self) -> None:
        while True:
            try:
                ttl = self.sys.settings.limit_order_ttl_s
                now = time.monotonic()
                for info in list(self.open_orders.values()):
                    if info.order_type == "LIMIT" and info.status in ("ACKNOWLEDGED", "PARTIALLY_FILLED") \
                            and now - info.created_monotonic > ttl and info.client_order_id not in self._cancel_intent:
                        try:
                            await self.cancel(info.order_id, f"limit order expired after {ttl:.0f}s", "TIMEOUT")
                        except Exception as e:
                            log.warning("expiry cancel failed for %s: %s", info.order_id, e)
                self.sys.health.beat("order_engine", detail=f"{len(self.open_orders)} open orders")
            except Exception:
                log.exception("sweeper failed")
            await asyncio.sleep(2.0)
