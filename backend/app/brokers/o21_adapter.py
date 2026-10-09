"""021 developer sandbox broker adapter (021-03: orders go through POST /orders).

Orders: NSE cash, product INTRADAY, book RL, validity Day; price 0 = market, otherwise a limit in paise rounded to
the instrument's tick size; the sign of qty is the side.

Source of truth (API Guide, good practice): REST - GET /orders, /orders/{id}/trades, /portfolio/positions.
The orders websocket is only a trigger: every event for one of our orders makes the adapter re-read that order and
its trades over REST; new trades (deduplicated by tradeId) become execution reports, terminal states become order
updates. Events are not replayed after a disconnect, so a poll loop re-reads every open order every 2 s and the
socket reconnects with a fresh ephemeral key (daily restart at 08:00 IST included).

021 has no client order id. Every placement is journalled before it is sent (token, signed qty, price, time). After
an ambiguous failure (timeout / 5xx) the order book is searched for an unclaimed order with the same fingerprint
placed after the send; only if none is found is the order treated as absent. A 500 that carries an error text and
whose order is confirmed absent is a risk rejection; it is never re-sent.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

import websockets

from app.brokers.base import (BrokerAccount, BrokerAdapter, BrokerOrder, BrokerPosition, BrokerRejected, BrokerTimeout,
                              BrokerUnavailable, ExecutionReport, OrderUpdate, PlaceOrderRequest)
from app.brokers.o21_client import NotFound, O21Client, UncertainOrder
from app.brokers.o21_instruments import InstrumentStore
from app.brokers.o21_protocol import ProtocolError, parse_order_frame

log = logging.getLogger("algotrade.o21.adapter")
P100 = Decimal(100)
TERMINAL = {"Executed", "Rejected", "Cancelled"}
STATUS_MAP = {"Executed": "FILLED", "Rejected": "REJECTED", "Cancelled": "CANCELLED"}
ABSENCE_GRACE_S = 3.0   # an order may take a moment to show in GET /orders after the send
POLL_S = 2.0


def rupees(paise: Any) -> Decimal:
    return (Decimal(int(paise or 0)) / P100).quantize(Decimal("0.01"))


@dataclass
class Journal:
    client_order_id: str
    symbol: str
    token: int
    qty: int                 # signed
    price: int               # paise, 0 = market
    sent_at: float           # unix seconds
    order_id: str | None = None


@dataclass
class Faults:
    """Fault injection for the Fault Lab, applied at the adapter boundary (the sandbox misbehaves on its own too)."""
    network_down_until: float = 0.0
    timeout_next_place: int = 0     # send the order, then lose the response
    reject_next: int = 0
    reject_reason: str = "simulated RMS rejection (fault injection)"
    drop_reports_next: int = 0      # lose the next execution report (reconciliation must recover it)
    extra_latency_ms: int = 0

    def disarm(self) -> None:
        self.network_down_until = 0.0
        self.timeout_next_place = self.reject_next = self.drop_reports_next = self.extra_latency_ms = 0

    def to_dict(self) -> dict[str, Any]:
        return {"network_down_s": max(0.0, round(self.network_down_until - time.time(), 1)),
                "timeout_next_place": self.timeout_next_place, "reject_next": self.reject_next,
                "drop_reports_next": self.drop_reports_next, "extra_latency_ms": self.extra_latency_ms}


class O21Adapter(BrokerAdapter):
    name = "021 Developer Sandbox"
    is_paper = False
    funds_api = False   # 021 exposes no funds endpoint
    ping_returns_latency = True

    def __init__(self, client: O21Client, instruments: InstrumentStore, ws_orders_url: str):
        super().__init__()
        self.client, self.instruments, self.ws_url = client, instruments, ws_orders_url
        self.faults = Faults()
        # filled in by the platform: broker order id -> our client order id (orders placed before a restart)
        self.client_id_for: Callable[[str], Awaitable[str | None]] | None = None
        self.journal: dict[str, Journal] = {}
        self.by_order: dict[str, str] = {}           # 021 order id -> client order id
        self.open_ids: set[str] = set()
        self.seen_trades: set[str] = set()
        self.last_status: dict[str, str] = {}
        self.square_off: dict[str, int] = {}         # symbol -> squareOffQuantity (latest positions read)
        self._tasks: list[asyncio.Task] = []
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self.ws_connected = False
        self.ws_events = 0
        self._last_ok = 0.0

    # ------------------------------------------------------------------ lifecycle
    async def connect(self, account_id: str) -> None:
        await self.client.login()
        await self.instruments.ensure(self.client)
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._orders_socket(), name="o21-orders-ws"),
                           asyncio.create_task(self._poll_loop(), name="o21-orders-poll")]

    async def close(self) -> None:
        """Stops the socket and poll tasks. The HTTP client is shared with the market feed and owned by the system."""
        for t in self._tasks:
            t.cancel()
        self._tasks = []

    async def ping(self) -> float:
        """Latency of recent real calls; only calls the API when nothing succeeded for 10 s (saves rate limit)."""
        self._check_faults()
        if time.time() - self._last_ok > 10:
            await self.get_positions()
        return float(self.client.last_latency_ms or 0) + self.faults.extra_latency_ms

    def _check_faults(self) -> None:
        if time.time() < self.faults.network_down_until:
            raise BrokerUnavailable("021 API unreachable (injected outage)")

    async def _call(self, coro):
        self._check_faults()
        if self.faults.extra_latency_ms:
            await asyncio.sleep(self.faults.extra_latency_ms / 1000)
        res = await coro
        self._last_ok = time.time()
        return res

    # ------------------------------------------------------------------ orders
    async def place_order(self, req: PlaceOrderRequest) -> BrokerOrder:
        prev = self.journal.get(req.client_order_id)
        if prev is not None:  # idempotent on our client order id: never send the same order twice
            found = await (self.get_order(prev.order_id) if prev.order_id else self._find_unclaimed(prev))
            if found is not None:
                return found
        if self.faults.reject_next > 0:
            self.faults.reject_next -= 1
            raise BrokerRejected(self.faults.reject_reason)
        inst = self.instruments.nse(req.symbol)
        qty = int(req.quantity) * (1 if req.side == "BUY" else -1)
        price = 0 if req.order_type == "MARKET" or req.limit_price is None else inst.to_paise(req.limit_price)
        body = {"exchange": "NSE", "token": inst.token, "qty": qty, "price": price, "book": "RL", "trigger": 0,
                "discQuantity": 0, "product": "INTRADAY", "validity": "Day", "amo": False}
        j = Journal(req.client_order_id, req.symbol, inst.token, qty, price, time.time())
        self.journal[req.client_order_id] = j
        try:
            res = await self._call(self.client.request("POST", "/orders", body, safe=False))
        except UncertainOrder as e:
            found = await self._find_unclaimed(j)
            if found is not None:
                return found
            if e.status == 500 and e.error:  # confirmed absent + an error text: 021 risk rejection, not retried
                raise BrokerRejected(e.error) from e
            raise
        oid = str((res.get("data") or {}).get("orderId") or "")
        if not oid:
            raise BrokerRejected(f"021 returned no order id: {res}")
        self._claim(j, oid)
        if self.faults.timeout_next_place > 0:  # the order exists at 021, but we "lose" the response
            self.faults.timeout_next_place -= 1
            raise BrokerTimeout("021 POST /orders: response lost (injected timeout) - outcome unknown")
        asyncio.create_task(self.refresh(oid))
        return BrokerOrder(oid, req.client_order_id, req.symbol, req.side, req.order_type, req.quantity, Decimal(0), None,
                           req.limit_price, "OPEN", req.tag, created_at=datetime.now(timezone.utc))

    def _claim(self, j: Journal, oid: str) -> None:
        j.order_id = oid
        self.by_order[oid] = j.client_order_id
        self.open_ids.add(oid)

    async def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        row = await self._order_row(broker_order_id)
        if row is None:
            raise BrokerRejected(f"021 order {broker_order_id} not found")
        body = {"exchange": _req_exchange(row.get("exchange")), "token": int(row["token"]), "product": row.get("product", "INTRADAY")}
        await self._call(self.client.request("DELETE", f"/orders/{broker_order_id}", body, safe=True))
        asyncio.create_task(self.refresh(broker_order_id))
        o = await self.get_order(broker_order_id)
        return o or self._to_order(row)

    async def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        row = await self._order_row(broker_order_id)
        return self._to_order(row) if row else None

    async def _order_row(self, oid: str) -> dict | None:
        try:
            rows = await self._call(self.client.get(f"/orders/{oid}"))
        except NotFound:
            return None
        return rows[0] if isinstance(rows, list) and rows else None

    async def get_orders(self) -> list[BrokerOrder]:
        rows = await self._call(self.client.get("/orders")) or []
        return [self._to_order(r) for r in rows]

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        j = self.journal.get(client_order_id)
        if j is None:
            return None  # never sent by this process; the platform resolves older orders by broker id
        if j.order_id:
            return await self.get_order(j.order_id)
        return await self._find_unclaimed(j)

    async def _find_unclaimed(self, j: Journal) -> BrokerOrder | None:
        """Search today's order book for our order: same token, side, quantity and price, placed at/after the send
        and not claimed by another client order. Waits ABSENCE_GRACE_S after the send before concluding absence."""
        wait = ABSENCE_GRACE_S - (time.time() - j.sent_at)
        if wait > 0:
            await asyncio.sleep(wait)
        rows = await self._call(self.client.get("/orders")) or []
        claimed = set(self.by_order)
        cands = []
        for r in rows:
            oid = str(r.get("orderId"))
            if oid in claimed or int(r.get("token", -1)) != j.token or int(r.get("time") or 0) < int(j.sent_at) - 2:
                continue
            if abs(_order_qty(r)) != abs(j.qty) or (j.price and int(r.get("price") or 0) != j.price):
                continue
            side = await self._order_side(r)
            if side is not None and (side > 0) != (j.qty > 0):
                continue
            cands.append(r)
        if not cands:
            return None
        r = min(cands, key=lambda x: (int(x.get("time") or 0), str(x.get("orderId"))))
        self._claim(j, str(r["orderId"]))
        asyncio.create_task(self.refresh(str(r["orderId"])))
        return self._to_order(r)

    async def _order_side(self, r: dict) -> int | None:
        """+1 buy / -1 sell. qtyRemaining carries the sign while open; once filled the trades carry it."""
        for k in ("qty", "quantity", "orderQty", "qtyRemaining"):
            v = int(r.get(k) or 0)
            if v:
                return 1 if v > 0 else -1
        trades = await self._call(self.client.get(f"/orders/{r['orderId']}/trades")) or []
        for t in trades:
            v = int(t.get("quantity") or 0)
            if v:
                return 1 if v > 0 else -1
        return None

    def _to_order(self, r: dict) -> BrokerOrder:
        oid = str(r.get("orderId"))
        status = r.get("status", "")
        traded = abs(int(r.get("qtyTraded") or 0))
        total = Decimal(abs(_order_qty(r)))
        st = STATUS_MAP.get(status) or ("PARTIALLY_FILLED" if traded else "OPEN")
        j = self.journal.get(self.by_order.get(oid, ""))
        side = ("BUY" if j.qty > 0 else "SELL") if j else ("BUY" if int(r.get("qtyRemaining") or 0) >= 0 else "SELL")
        price = int(r.get("price") or 0)
        sym = self.instruments.symbol_of(r.get("exchange", "NSECM"), int(r.get("token", 0))) or str(r.get("token"))
        return BrokerOrder(oid, self.by_order.get(oid, ""), sym, side, "LIMIT" if price else "MARKET", total,
                           Decimal(traded), None, rupees(price) if price else None, st,
                           reject_reason=r.get("reason") or None,
                           created_at=datetime.fromtimestamp(int(r.get("time") or 0), tz=timezone.utc))

    # ------------------------------------------------------------------ fills
    async def get_fills(self, broker_order_id: str | None = None) -> list[ExecutionReport]:
        ids = [broker_order_id] if broker_order_id else [str(r.get("orderId")) for r in
                                                         (await self._call(self.client.get("/orders")) or [])
                                                         if abs(int(r.get("qtyTraded") or 0)) > 0]
        out: list[ExecutionReport] = []
        for oid in ids:
            out.extend(await self._order_fills(oid))
        return out

    async def _order_fills(self, oid: str) -> list[ExecutionReport]:
        trades = await self._call(self.client.get(f"/orders/{oid}/trades")) or []
        cid = await self._client_id(oid)
        row = await self._order_row(oid) if trades else None
        total = Decimal(abs(_order_qty(row))) if row else Decimal(0)
        out, cum = [], Decimal(0)
        for t in sorted(trades, key=lambda x: (int(x.get("tradeTime") or 0), str(x.get("tradeId")))):
            q = int(t.get("quantity") or 0)
            cum += abs(q)
            px = rupees(t.get("price"))
            sym = t.get("symbol") or self.instruments.symbol_of(t.get("exchange", "NSECM"), int(t.get("token", 0))) or ""
            out.append(ExecutionReport(f"021-{t.get('tradeId')}", oid, cid or "", sym, "BUY" if q > 0 else "SELL",
                                       Decimal(abs(q)), px, px, 0,
                                       datetime.fromtimestamp(int(t.get("tradeTime") or time.time()), tz=timezone.utc),
                                       cum, max(total, cum)))
        return out

    async def _client_id(self, oid: str) -> str | None:
        cid = self.by_order.get(oid)
        if cid is None and self.client_id_for is not None:
            cid = await self.client_id_for(oid)
            if cid:
                self.by_order[oid] = cid
        return cid

    async def refresh(self, oid: str) -> None:
        """Re-read one order + its trades over REST and emit what is new (fills first, then a terminal state)."""
        lock = self._refresh_locks.setdefault(oid, asyncio.Lock())
        async with lock:
            try:
                row = await self._order_row(oid)
                if row is None:
                    return
                cid = await self._client_id(oid)
                if not cid:
                    return  # not placed by this platform (e.g. manual order in the web app): reconciliation reports it
                if abs(int(row.get("qtyTraded") or 0)) > 0:
                    for rep in await self._order_fills(oid):
                        if rep.fill_id in self.seen_trades:
                            continue
                        self.seen_trades.add(rep.fill_id)
                        if self.faults.drop_reports_next > 0:
                            self.faults.drop_reports_next -= 1
                            log.warning("fault injection: execution report %s dropped", rep.fill_id)
                            continue
                        if self.on_execution:
                            await self.on_execution(rep)
                status = row.get("status", "")
                if status in TERMINAL:
                    self.open_ids.discard(oid)
                if status != self.last_status.get(oid):
                    self.last_status[oid] = status
                    if status in ("Cancelled", "Rejected") and self.on_order_update:
                        await self.on_order_update(OrderUpdate(oid, cid, STATUS_MAP[status], row.get("reason") or None))
                elif status not in TERMINAL:
                    self.open_ids.add(oid)
            except (BrokerUnavailable, BrokerTimeout, BrokerRejected) as e:
                log.warning("021 refresh of order %s failed: %s", oid, e)

    async def _poll_loop(self) -> None:
        while True:
            for oid in list(self.open_ids):
                await self.refresh(oid)
            await asyncio.sleep(POLL_S)

    async def _orders_socket(self) -> None:
        backoff = 1.0
        while True:
            try:
                key = (await self.client.get("/websocket/ephemeral-key") or {}).get("token")
                async with websockets.connect(f"{self.ws_url}?token={key}", ping_interval=None) as ws:
                    self.ws_connected, backoff = True, 1.0
                    for oid in list(self.open_ids):  # events are not replayed: catch up over REST
                        asyncio.create_task(self.refresh(oid))
                    async for msg in ws:
                        if not isinstance(msg, (bytes, bytearray)):
                            continue
                        try:
                            ev = parse_order_frame(bytes(msg))
                        except ProtocolError as e:
                            log.warning("bad orders-socket frame: %s", e)
                            continue
                        if ev is None:
                            continue
                        self.ws_events += 1
                        if ev.order_id in self.by_order:
                            self.open_ids.add(ev.order_id)
                        asyncio.create_task(self.refresh(ev.order_id))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("021 orders socket: %s - reconnecting in %.0fs", e, backoff)
            self.ws_connected = False
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    # ------------------------------------------------------------------ portfolio
    async def get_positions(self) -> list[BrokerPosition]:
        rows = await self._call(self.client.get("/portfolio/positions")) or []
        out: dict[str, BrokerPosition] = {}
        sq: dict[str, int] = {}
        for r in rows:
            if r.get("exchange") != "NSECM":
                continue
            sym = self.instruments.symbol_of("NSECM", int(r.get("token", 0))) or str(r.get("token"))
            q = Decimal(int(r.get("netQuantity") or 0))
            prev = out.get(sym)
            out[sym] = BrokerPosition(sym, q + (prev.quantity if prev else 0), rupees(r.get("netPrice")))
            sq[sym] = sq.get(sym, 0) + int(r.get("squareOffQuantity") or 0)
        self.square_off = sq
        return list(out.values())

    async def get_account(self) -> BrokerAccount:
        """021 has no funds endpoint (virtual ₹10,00,000, topped up every trading day): cash is not reported."""
        return BrokerAccount(self.client.username, Decimal(0), Decimal(0), Decimal(0),
                             {"funds_api": False, "token_expires_at": self.client.token_expires_at})

    def info(self) -> dict[str, Any]:
        return {"orders_socket": self.ws_connected, "orders_socket_events": self.ws_events,
                "open_orders_tracked": len(self.open_ids), "token_expires_at": self.client.token_expires_at,
                "http": dict(self.client.stats)}


def _order_qty(r: dict | None) -> int:
    """Order size: an explicit quantity field if present, else traded + remaining."""
    if not r:
        return 0
    for k in ("qty", "quantity", "orderQty"):
        if r.get(k):
            return int(r[k])
    return abs(int(r.get("qtyTraded") or 0)) + abs(int(r.get("qtyRemaining") or 0))


def _req_exchange(resp_name: str | None) -> str:
    return {"NSECM": "NSE", "NSEFO": "NSEFO", "BSEEQ": "BSE", "BSEEQD": "BSEFO"}.get(resp_name or "NSECM", "NSE")
