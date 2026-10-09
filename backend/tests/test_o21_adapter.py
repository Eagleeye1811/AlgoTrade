"""021 broker adapter against a fake 021 exchange that speaks the API Guide's request/response formats."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from decimal import Decimal

import httpx
import pytest

import app.brokers.o21_adapter as mod
from app.brokers.base import BrokerRejected, PlaceOrderRequest
from app.brokers.o21_adapter import O21Adapter
from app.brokers.o21_client import O21Client
from app.brokers.o21_instruments import IST, Instrument, InstrumentStore

D = Decimal


class FakeExchange:
    def __init__(self):
        self.orders: dict[int, dict] = {}
        self.trades: dict[int, list[dict]] = {}
        self.posts: list[dict] = []
        self.deletes: list[tuple[int, dict]] = []
        self.next_id, self.next_trade = 1042, 5001
        self.post_mode = "ok"  # ok | 500_after_create | 500_reject

    def env(self, data, code=200, ok=True, error=None):
        return httpx.Response(code, json={"data": data, "success": ok, "error": error})

    def fill(self, oid: int, qty: int, price: int):
        o = self.orders[oid]
        sign = 1 if o["qtyRemaining"] > 0 else -1
        o["qtyRemaining"] -= sign * qty
        o["qtyTraded"] += qty
        o["status"] = "Executed" if o["qtyRemaining"] == 0 else "Pending"
        self.trades.setdefault(oid, []).append({"tradeId": self.next_trade, "tradeTime": int(time.time()), "quantity": sign * qty,
                                                "token": o["token"], "exchange": "NSECM", "product": "INTRADAY",
                                                "price": price, "symbol": "RELIANCE"})
        self.next_trade += 1

    def __call__(self, req: httpx.Request):
        path = req.url.path.split("/v1", 1)[1]
        if path == "/auth/token":
            return self.env({"accessToken": "tok", "tokenType": "Bearer", "expiresAt": "2026-10-10T05:00:00+05:30"})
        if req.method == "POST" and path == "/orders":
            body = json.loads(req.content)
            self.posts.append(body)
            if self.post_mode == "500_reject":
                return self.env(None, 500, False, "RMS: insufficient funds")
            oid = self.next_id
            self.next_id += 1
            self.orders[oid] = {"orderId": oid, "time": int(time.time()), "lastActivity": int(time.time()), "token": body["token"],
                                "exchange": "NSECM", "product": body["product"], "qtyRemaining": body["qty"], "qtyTraded": 0,
                                "price": body["price"], "triggerPrice": 0, "book": "RL", "validity": "Day",
                                "status": "Placed", "reason": "", "placedBy": "User"}
            if self.post_mode == "500_after_create":
                return self.env(None, 500, False, "internal error")
            return self.env({"type": "SUCCESS", "message": "Order placed successfully", "orderId": str(oid)})
        if req.method == "GET" and path == "/orders":
            return httpx.Response(200, json=sorted(self.orders.values(), key=lambda o: -o["orderId"]))
        if path.startswith("/orders/"):
            parts = path.split("/")
            oid = int(parts[2])
            if len(parts) == 4:
                return httpx.Response(200, json=self.trades.get(oid, []))
            if oid not in self.orders:
                return self.env(None, 404, False, "order not found")
            if req.method == "DELETE":
                self.deletes.append((oid, json.loads(req.content)))
                self.orders[oid]["status"] = "Cancelled"
                return self.env({"type": "SUCCESS", "message": "cancel requested"})
            return httpx.Response(200, json=[self.orders[oid]])
        if path == "/portfolio/positions":
            return httpx.Response(200, json=[{"exchange": "NSECM", "token": 2885, "product": "INTRADAY", "netQuantity": 10,
                                              "netPrice": 140000, "carryForwardQuantity": 0, "longPosition": True,
                                              "squareOffQuantity": -10, "buyPrice": 140000, "sellPrice": 0, "prevClose": 139200}])
        return httpx.Response(404, json={"success": False, "error": "no route"})


@pytest.fixture
def ex(monkeypatch):
    monkeypatch.setattr(mod, "ABSENCE_GRACE_S", 0.0)
    return FakeExchange()


def adapter(ex) -> O21Adapter:
    client = O21Client("http://021/api/developer-api/v1", "HACK1", "pw", transport=httpx.MockTransport(ex), rate_per_s=1000, burst=100)
    store = InstrumentStore("data/instruments")
    store.load_rows([Instrument(2885, "NSECM", "RELIANCE", "STK", 5, 120000, 160000, 1, 50000)])
    store.loaded_for = datetime.now(IST).date().isoformat()
    a = O21Adapter(client, store, "wss://unused")
    a.reports, a.updates = [], []

    async def on_exec(r):
        a.reports.append(r)

    async def on_upd(u):
        a.updates.append(u)
    a.on_execution, a.on_order_update = on_exec, on_upd
    return a


async def settle():
    await asyncio.sleep(0.05)


async def test_order_body_follows_the_guide_and_fills_become_reports(ex):
    a = adapter(ex)
    bo = await a.place_order(PlaceOrderRequest("AT-1", "RELIANCE", "BUY", D(1), "LIMIT", D("1414.03")))
    assert ex.posts[0] == {"exchange": "NSE", "token": 2885, "qty": 1, "price": 141405, "book": "RL", "trigger": 0,
                           "discQuantity": 0, "product": "INTRADAY", "validity": "Day", "amo": False}  # paise, tick 5
    assert bo.broker_order_id == "1042" and bo.status == "OPEN"
    sell = await a.place_order(PlaceOrderRequest("AT-2", "RELIANCE", "SELL", D(2), "MARKET"))
    assert ex.posts[1]["qty"] == -2 and ex.posts[1]["price"] == 0  # sign = side, 0 = market
    ex.fill(1042, 1, 141400)
    await a.refresh("1042")
    await a.refresh("1042")  # replayed / duplicate events never double-count a trade
    assert len(a.reports) == 1
    r = a.reports[0]
    assert (r.fill_id, r.client_order_id, r.side, r.quantity, r.price, r.cumulative_filled) == \
           ("021-5001", "AT-1", "BUY", D(1), D("1414.00"), D(1))
    assert sell.broker_order_id == "1043"


async def test_partial_fills_and_terminal_states(ex):
    a = adapter(ex)
    await a.place_order(PlaceOrderRequest("AT-1", "RELIANCE", "SELL", D(10), "MARKET"))
    ex.fill(1042, 4, 140000)
    await a.refresh("1042")
    assert [(r.quantity, r.cumulative_filled, r.order_quantity) for r in a.reports] == [(D(4), D(4), D(10))]
    assert "1042" in a.open_ids
    ex.orders[1042]["status"] = "Cancelled"
    await a.refresh("1042")
    assert a.updates[-1].status == "CANCELLED" and "1042" not in a.open_ids


async def test_ambiguous_500_order_found_in_book_is_adopted_not_resent(ex):
    a = adapter(ex)
    ex.post_mode = "500_after_create"
    bo = await a.place_order(PlaceOrderRequest("AT-9", "RELIANCE", "BUY", D(3), "MARKET"))
    assert bo.broker_order_id == "1042" and len(ex.posts) == 1
    ex.post_mode = "ok"
    again = await a.place_order(PlaceOrderRequest("AT-9", "RELIANCE", "BUY", D(3), "MARKET"))  # idempotent resend
    assert again.broker_order_id == "1042" and len(ex.posts) == 1


async def test_500_risk_rejection_confirmed_absent_is_rejected(ex):
    a = adapter(ex)
    ex.post_mode = "500_reject"
    with pytest.raises(BrokerRejected, match="insufficient funds"):
        await a.place_order(PlaceOrderRequest("AT-5", "RELIANCE", "BUY", D(1000), "MARKET"))
    assert len(ex.posts) == 1  # never re-sent


async def test_fingerprint_ignores_orders_claimed_by_others(ex):
    a = adapter(ex)
    await a.place_order(PlaceOrderRequest("AT-1", "RELIANCE", "BUY", D(3), "MARKET"))   # 1042, claimed
    ex.post_mode = "500_after_create"
    bo = await a.place_order(PlaceOrderRequest("AT-2", "RELIANCE", "BUY", D(3), "MARKET"))  # identical order 1043
    assert bo.broker_order_id == "1043"


async def test_positions_square_off_and_cancel(ex):
    a = adapter(ex)
    pos = await a.get_positions()
    assert [(p.symbol, p.quantity, p.avg_price) for p in pos] == [("RELIANCE", D(10), D("1400.00"))]
    assert a.square_off == {"RELIANCE": -10}
    await a.place_order(PlaceOrderRequest("AT-1", "RELIANCE", "BUY", D(1), "LIMIT", D("1300")))
    await a.cancel_order("1042")
    assert ex.deletes == [(1042, {"exchange": "NSE", "token": 2885, "product": "INTRADAY"})]
    acct = await a.get_account()
    assert acct.extra["funds_api"] is False
