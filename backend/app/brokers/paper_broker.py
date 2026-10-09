"""Paper Broker Simulator - an independent backend execution venue.

* Keeps its OWN persistent state (schema `paper_broker`): account cash, orders, fills, positions.
* Matches orders against the live/replayed market ticks it observes (like an exchange would).
  Fill prices include modelled slippage but are clamped to the real traded range of the current bar,
  so a fill never contradicts the market.
* Simulates API latency, timeouts with lost responses, outages, rejections, partial / delayed fills,
  dropped execution reports and duplicate submissions (idempotent on client_order_id).
* Delivers execution reports asynchronously through an outbound queue (like a broker postback/WebSocket).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.brokers.base import (
    BrokerAccount,
    BrokerAdapter,
    BrokerOrder,
    BrokerPosition,
    BrokerRejected,
    BrokerTimeout,
    BrokerUnavailable,
    ExecutionReport,
    OrderUpdate,
    PlaceOrderRequest,
)
from app.database.models import PBAccount, PBFill, PBOrder, PBPosition
from app.database.session import Database
from app.market.models import Tick

log = logging.getLogger("algotrade.paper_broker")

TICK_SIZE = Decimal("0.05")
OPEN_STATES = ("OPEN", "PARTIALLY_FILLED")
FREEZE_QTY = Decimal(100000)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def round_tick(p: Decimal) -> Decimal:
    return ((p / TICK_SIZE).quantize(Decimal(1), rounding=ROUND_HALF_UP) * TICK_SIZE).quantize(Decimal("0.01"))


@dataclass
class BrokerFaults:
    """Fault-injection switches (driven by the Fault Simulation lab)."""

    network_down_until: float = 0.0
    extra_latency_ms: int = 0
    timeout_next_place: int = 0  # accept the order, then lose the response
    reject_next: int = 0
    reject_reason: str = "RMS: order rejected by broker risk system"
    partial_next: int = 0  # cap the next order's fill at `partial_ratio` for `partial_hold_s`
    partial_ratio: Decimal = Decimal("0.4")
    partial_hold_s: float = 4.0
    drop_reports_next: int = 0  # record fills but lose the execution report(s)
    duplicate_attempts: int = 0

    def disarm(self) -> None:
        """Clear one-shot fault switches (time-bound outages expire on their own)."""
        self.extra_latency_ms = 0
        self.timeout_next_place = self.reject_next = self.partial_next = self.drop_reports_next = 0

    def to_dict(self) -> dict:
        now = time.time()
        return {
            "network_down": now < self.network_down_until,
            "network_down_remaining_s": max(0.0, round(self.network_down_until - now, 1)),
            "extra_latency_ms": self.extra_latency_ms,
            "timeout_next_place": self.timeout_next_place,
            "reject_next": self.reject_next,
            "partial_next": self.partial_next,
            "drop_reports_next": self.drop_reports_next,
            "duplicate_attempts_blocked": self.duplicate_attempts,
        }


@dataclass
class _OpenOrder:
    row: dict
    accepted_at: float
    cap: Decimal | None = None
    cap_until: float = 0.0
    drop_reports: bool = False
    pending: bool = field(default=False)


class PaperBrokerSimulator(BrokerAdapter):
    name = "PAPER_BROKER_SIMULATOR"
    is_paper = True

    def __init__(self, db: Database, *, seed: int = 7, base_latency_ms: int = 40, jitter_ms: int = 40,
                 max_participation: Decimal = Decimal("0.05"), slippage_bps: Decimal = Decimal("2"),
                 timeout_s: float = 5.0, margin_rate: Decimal = Decimal("1")):
        super().__init__()
        self.db = db
        self.rng = random.Random(seed)
        self.base_latency_ms = base_latency_ms
        self.jitter_ms = jitter_ms
        self.max_participation = max_participation
        self.slippage_bps = slippage_bps
        self.timeout_s = timeout_s
        self.margin_rate = margin_rate
        self.faults = BrokerFaults()
        self.account_id: str | None = None
        self.open: dict[str, _OpenOrder] = {}
        self.last_price: dict[str, Decimal] = {}
        self.last_tick: dict[str, Tick] = {}
        self._outbox: asyncio.Queue = asyncio.Queue()
        self._delivery_task: asyncio.Task | None = None
        self._match_lock = asyncio.Lock()
        self.calls = 0

    # ------------------------------------------------------------------ simulated network
    async def _network(self, op: str) -> int:
        """Simulated request/response transport. Returns latency in ms or raises."""
        self.calls += 1
        if time.time() < self.faults.network_down_until:
            await asyncio.sleep(0.05)
            raise BrokerUnavailable(f"paper broker unreachable during {op} (connection refused)")
        latency = self.base_latency_ms + self.rng.randint(0, self.jitter_ms) + self.faults.extra_latency_ms
        if latency / 1000 > self.timeout_s:
            await asyncio.sleep(self.timeout_s)
            raise BrokerTimeout(f"{op}: no response within {self.timeout_s:.1f}s")
        await asyncio.sleep(latency / 1000)
        return latency

    # ------------------------------------------------------------------ lifecycle
    async def connect(self, account_id: str) -> None:
        await self._network("connect")
        self.account_id = account_id
        async with self.db.session() as s:
            acct = await s.get(PBAccount, account_id)
            if acct is None:
                raise BrokerUnavailable(f"paper broker account {account_id} does not exist")
            rows = (await s.execute(
                select(PBOrder).where(PBOrder.account_id == account_id, PBOrder.status.in_(OPEN_STATES))
            )).scalars().all()
        self.open = {r.broker_order_id: _OpenOrder(self._row(r), time.time()) for r in rows}
        if self._delivery_task is None or self._delivery_task.done():
            self._delivery_task = asyncio.create_task(self._deliver_loop(), name="paper-broker-delivery")

    async def close(self) -> None:
        if self._delivery_task:
            self._delivery_task.cancel()
            try:
                await self._delivery_task
            except (asyncio.CancelledError, Exception):
                pass
            self._delivery_task = None

    async def open_account(self, account_id: str, cash: Decimal) -> None:
        async with self.db.transaction() as s:
            stmt = pg_insert(PBAccount).values(account_id=account_id, cash=cash, starting_cash=cash)
            await s.execute(stmt.on_conflict_do_nothing(index_elements=[PBAccount.account_id]))

    async def reset_account(self, account_id: str, cash: Decimal) -> None:
        """Wipe venue state (demo reset)."""
        async with self.db.transaction() as s:
            await s.execute(delete(PBFill).where(PBFill.account_id == account_id))
            await s.execute(delete(PBOrder).where(PBOrder.account_id == account_id))
            await s.execute(delete(PBPosition).where(PBPosition.account_id == account_id))
            await s.execute(delete(PBAccount).where(PBAccount.account_id == account_id))
            s.add(PBAccount(account_id=account_id, cash=cash, starting_cash=cash))
        self.open.clear()
        self.faults = BrokerFaults()
        while not self._outbox.empty():
            self._outbox.get_nowait()

    @staticmethod
    def _row(r: PBOrder) -> dict:
        return {c: getattr(r, c) for c in ("broker_order_id", "client_order_id", "symbol", "side", "order_type",
                                            "quantity", "filled_quantity", "avg_price", "limit_price", "status",
                                            "tag", "reject_reason", "created_at")}

    @staticmethod
    def _to_order(r: PBOrder | dict) -> BrokerOrder:
        g = (lambda k: r[k]) if isinstance(r, dict) else (lambda k: getattr(r, k))
        return BrokerOrder(g("broker_order_id"), g("client_order_id"), g("symbol"), g("side"), g("order_type"),
                           g("quantity"), g("filled_quantity"), g("avg_price"), g("limit_price"), g("status"),
                           g("tag"), g("reject_reason"), g("created_at"))

    # ------------------------------------------------------------------ queries
    async def ping(self) -> float:
        t = time.perf_counter()
        await self._network("ping")
        return (time.perf_counter() - t) * 1000

    async def get_account(self) -> BrokerAccount:
        await self._network("get_account")
        async with self.db.session() as s:
            a = await s.get(PBAccount, self.account_id)
            pos = (await s.execute(select(PBPosition).where(PBPosition.account_id == self.account_id))).scalars().all()
        if a is None:
            raise BrokerUnavailable("account not found")
        margin = sum((abs(p.quantity) * self.last_price.get(p.symbol, p.avg_price) * self.margin_rate for p in pos),
                     Decimal(0))
        realized = sum((p.realized_pnl for p in pos), Decimal(0))
        return BrokerAccount(self.account_id, a.cash, a.starting_cash, margin, {"realized_pnl": str(realized)})

    async def get_positions(self) -> list[BrokerPosition]:
        await self._network("get_positions")
        async with self.db.session() as s:
            rows = (await s.execute(select(PBPosition).where(PBPosition.account_id == self.account_id))).scalars().all()
        return [BrokerPosition(r.symbol, r.quantity, r.avg_price) for r in rows if r.quantity != 0]

    async def get_orders(self) -> list[BrokerOrder]:
        await self._network("get_orders")
        async with self.db.session() as s:
            rows = (await s.execute(select(PBOrder).where(PBOrder.account_id == self.account_id)
                                    .order_by(PBOrder.created_at))).scalars().all()
        return [self._to_order(r) for r in rows]

    async def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        await self._network("get_order")
        async with self.db.session() as s:
            r = await s.get(PBOrder, broker_order_id)
        return self._to_order(r) if r else None

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        await self._network("get_order_by_client_id")
        async with self.db.session() as s:
            r = (await s.execute(select(PBOrder).where(PBOrder.account_id == self.account_id,
                                                       PBOrder.client_order_id == client_order_id))).scalar_one_or_none()
        return self._to_order(r) if r else None

    async def get_fills(self, broker_order_id: str | None = None) -> list[ExecutionReport]:
        await self._network("get_fills")
        async with self.db.session() as s:
            q = select(PBFill, PBOrder).join(PBOrder, PBOrder.broker_order_id == PBFill.broker_order_id).where(
                PBFill.account_id == self.account_id)
            if broker_order_id:
                q = q.where(PBFill.broker_order_id == broker_order_id)
            rows = (await s.execute(q.order_by(PBFill.ts))).all()
        out = []
        for f, o in rows:
            out.append(ExecutionReport(f.fill_id, f.broker_order_id, o.client_order_id, f.symbol, f.side, f.quantity,
                                       f.price, f.reference_price, f.latency_ms, f.ts, o.filled_quantity, o.quantity))
        return out

    # ------------------------------------------------------------------ commands
    async def place_order(self, req: PlaceOrderRequest) -> BrokerOrder:
        # Request leg of the network
        await self._network("place_order")
        lose_response = False
        if self.faults.timeout_next_place > 0:
            self.faults.timeout_next_place -= 1
            lose_response = True

        async with self.db.transaction() as s:
            existing = (await s.execute(select(PBOrder).where(PBOrder.account_id == self.account_id,
                                                              PBOrder.client_order_id == req.client_order_id))).scalar_one_or_none()
            if existing is not None:
                # Idempotency: same client_order_id never creates a second order at the venue.
                self.faults.duplicate_attempts += 1
                return self._to_order(existing)
            status, reason = "OPEN", None
            last = self.last_price.get(req.symbol)
            if self.faults.reject_next > 0:
                self.faults.reject_next -= 1
                status, reason = "REJECTED", self.faults.reject_reason
            elif req.quantity <= 0 or req.quantity != req.quantity.to_integral_value():
                status, reason = "REJECTED", "invalid quantity"
            elif req.quantity > FREEZE_QTY:
                status, reason = "REJECTED", f"quantity exceeds freeze limit {FREEZE_QTY}"
            elif last is None:
                status, reason = "REJECTED", f"no market for {req.symbol}"
            elif req.order_type == "LIMIT" and (req.limit_price is None or abs(req.limit_price - last) / last > Decimal("0.2")):
                status, reason = "REJECTED", "limit price outside circuit band (+/-20%)"
            boid = "PB-" + uuid.uuid4().hex[:12].upper()
            row = PBOrder(broker_order_id=boid, client_order_id=req.client_order_id, account_id=self.account_id,
                          symbol=req.symbol, side=req.side, order_type=req.order_type, quantity=req.quantity,
                          filled_quantity=Decimal(0), limit_price=req.limit_price, status=status, tag=req.tag,
                          reject_reason=reason, created_at=utcnow())
            s.add(row)
        if status == "OPEN":
            oo = _OpenOrder(self._row(row), time.time())
            if self.faults.partial_next > 0:
                self.faults.partial_next -= 1
                oo.cap = max(Decimal(1), (req.quantity * self.faults.partial_ratio).to_integral_value())
                oo.cap_until = time.time() + self.faults.partial_hold_s
            if self.faults.drop_reports_next > 0:
                self.faults.drop_reports_next -= 1
                oo.drop_reports = True
            self.open[boid] = oo
        # Response leg of the network
        if lose_response:
            await asyncio.sleep(self.timeout_s)
            raise BrokerTimeout(f"place_order: no response within {self.timeout_s:.1f}s (order {req.client_order_id})")
        if status == "REJECTED":
            raise BrokerRejected(reason or "rejected")
        return self._to_order(row)

    async def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        await self._network("cancel_order")
        async with self._match_lock:
            async with self.db.transaction() as s:
                r = await s.get(PBOrder, broker_order_id, with_for_update=True)
                if r is None:
                    raise BrokerRejected("unknown order")
                if r.status not in OPEN_STATES:
                    raise BrokerRejected(f"order already {r.status}")
                r.status = "CANCELLED"
                r.updated_at = utcnow()
                out = self._to_order(r)
            self.open.pop(broker_order_id, None)
        await self._outbox.put(OrderUpdate(broker_order_id, out.client_order_id, "CANCELLED", "cancelled by client"))
        return out

    # ------------------------------------------------------------------ matching
    def _slippage_bps(self, qty: Decimal, tick_vol: Decimal) -> Decimal:
        impact = Decimal(10) * qty / max(tick_vol, Decimal(1))
        return self.slippage_bps + min(impact, Decimal(20))

    async def on_market_tick(self, tick: Tick) -> None:
        self.last_price[tick.symbol] = tick.price
        self.last_tick[tick.symbol] = tick
        if not any(o.row["symbol"] == tick.symbol for o in self.open.values()):
            return
        async with self._match_lock:
            for boid, oo in list(self.open.items()):
                if oo.row["symbol"] != tick.symbol:
                    continue
                try:
                    await self._match(boid, oo, tick)
                except Exception:
                    log.exception("matching failed for %s", boid)

    async def _match(self, boid: str, oo: _OpenOrder, tick: Tick) -> None:
        r = oo.row
        remaining = r["quantity"] - r["filled_quantity"]
        if remaining <= 0:
            return
        side = r["side"]
        hi = tick.range_high or tick.price
        lo = tick.range_low or tick.price
        if r["order_type"] == "LIMIT":
            lim = r["limit_price"]
            if (side == "BUY" and tick.price > lim) or (side == "SELL" and tick.price < lim):
                return
        liquidity = max(Decimal(1), (tick.volume * self.max_participation).to_integral_value())
        qty = min(remaining, liquidity)
        if oo.cap is not None:
            if time.time() < oo.cap_until:
                qty = min(qty, oo.cap - r["filled_quantity"])
            else:
                oo.cap = None
        if qty <= 0:
            return
        slip = self._slippage_bps(qty, tick.volume)
        sign = Decimal(1) if side == "BUY" else Decimal(-1)
        px = tick.price * (Decimal(1) + sign * slip / Decimal(10000))
        px = min(max(px, lo), hi)  # never outside the real traded range
        if r["order_type"] == "LIMIT":
            px = min(px, r["limit_price"]) if side == "BUY" else max(px, r["limit_price"])
        px = round_tick(px)
        latency_ms = int((time.time() - oo.accepted_at) * 1000)
        fill_id = "PF-" + uuid.uuid4().hex[:12].upper()
        now = utcnow()
        async with self.db.transaction() as s:
            o = await s.get(PBOrder, boid, with_for_update=True)
            if o is None or o.status not in OPEN_STATES:
                self.open.pop(boid, None)
                return
            new_filled = o.filled_quantity + qty
            prev_val = (o.avg_price or Decimal(0)) * o.filled_quantity
            o.avg_price = ((prev_val + px * qty) / new_filled).quantize(Decimal("0.0001"))
            o.filled_quantity = new_filled
            o.status = "FILLED" if new_filled >= o.quantity else "PARTIALLY_FILLED"
            o.updated_at = now
            s.add(PBFill(fill_id=fill_id, broker_order_id=boid, account_id=self.account_id, symbol=o.symbol, side=side,
                         quantity=qty, price=px, reference_price=tick.price, latency_ms=latency_ms, ts=now,
                         market_ts=tick.ts, delivered=not oo.drop_reports))
            acct = await s.get(PBAccount, self.account_id, with_for_update=True)
            acct.cash = acct.cash - sign * qty * px
            await self._apply_position(s, o.symbol, sign * qty, px)
            r["filled_quantity"], r["status"], r["avg_price"] = o.filled_quantity, o.status, o.avg_price
            report = ExecutionReport(fill_id, boid, o.client_order_id, o.symbol, side, qty, px, tick.price, latency_ms,
                                     now, o.filled_quantity, o.quantity)
        if r["status"] == "FILLED":
            self.open.pop(boid, None)
        if oo.drop_reports:
            log.warning("FAULT: execution report %s for %s dropped in transit", fill_id, boid)
            return
        await self._outbox.put(report)

    async def _apply_position(self, s, symbol: str, delta: Decimal, px: Decimal) -> None:
        stmt = pg_insert(PBPosition).values(account_id=self.account_id, symbol=symbol, quantity=0, avg_price=0,
                                            realized_pnl=0).on_conflict_do_nothing()
        await s.execute(stmt)
        p = (await s.execute(select(PBPosition).where(PBPosition.account_id == self.account_id,
                                                      PBPosition.symbol == symbol).with_for_update())).scalar_one()
        from app.engine.position_math import apply_fill_math

        p.quantity, p.avg_price, realized = apply_fill_math(p.quantity, p.avg_price, delta, px)
        p.realized_pnl = p.realized_pnl + realized

    async def _deliver_loop(self) -> None:
        while True:
            msg = await self._outbox.get()
            try:
                if isinstance(msg, ExecutionReport) and self.on_execution:
                    await self.on_execution(msg)
                elif isinstance(msg, OrderUpdate) and self.on_order_update:
                    await self.on_order_update(msg)
            except Exception:
                log.exception("execution report delivery failed - will be recovered by reconciliation")

    # ------------------------------------------------------------------ admin / fault helpers
    async def debug_state(self) -> dict:
        return {"open_orders": len(self.open), "faults": self.faults.to_dict(), "account_id": self.account_id,
                "last_prices": {k: str(v) for k, v in self.last_price.items()}}

    async def mark_undelivered_count(self) -> int:
        async with self.db.session() as s:
            rows = (await s.execute(select(PBFill.fill_id).where(PBFill.account_id == self.account_id,
                                                                 PBFill.delivered.is_(False)))).all()
        return len(rows)

    async def mark_delivered(self, fill_ids: list[str]) -> None:
        if not fill_ids:
            return
        async with self.db.transaction() as s:
            await s.execute(update(PBFill).where(PBFill.fill_id.in_(fill_ids)).values(delivered=True))
