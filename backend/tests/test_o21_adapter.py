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
            if self.post_mode == "wide_then_circuit" and body["price"] == 0:
                return self.env(None, 500, False, "Market orders are rejected because bid-ask spread is wide. Please place limit orders")
            if self.post_mode == "wide_then_circuit" and body["qty"] < 0 and body["price"] <= 120620:
                return self.env(None, 500, False, f"Price {body['price'] / 100:,.2f} must be higher than the lower circuit limit 1,206.20")
            if self.post_mode == "wide_spread" and body["price"] == 0:
                return self.env(None, 500, False, "Market orders are rejected because bid-ask spread is wide. Please place limit orders")
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


def test_faults_serialize_for_the_fault_lab_api():
    """/api/faults/scenarios and /api/broker/state report the adapter's armed faults (was an HTTP 500 on 021)."""
    import json
    import time as _t

    from app.brokers.o21_adapter import Faults
    f = Faults(timeout_next_place=1, reject_next=2, extra_latency_ms=900)
    f.network_down_until = _t.time() + 5
    d = f.to_dict()
    assert d["timeout_next_place"] == 1 and d["reject_next"] == 2 and d["extra_latency_ms"] == 900
    assert 4 <= d["network_down_s"] <= 5
    json.dumps(d)
    f.disarm()
    assert f.to_dict()["network_down_s"] == 0


def test_trade_time_reads_the_sandbox_shifted_epoch():
    """GET /orders/{id}/trades tradeTime came back 315,513,000 s (the 1980-epoch offset) in the future on the
    sandbox: fills were stored as 2036. The plausible reading is used; garbage becomes the receive time."""
    from datetime import datetime, timezone
    from app.brokers.o21_adapter import trade_time
    from app.brokers.o21_protocol import EPOCH_1980_IST
    now = datetime(2026, 10, 9, 11, 58, 50, tzinfo=timezone.utc).timestamp()
    real = datetime(2026, 10, 9, 11, 58, 42, tzinfo=timezone.utc)
    assert trade_time(int(real.timestamp()) + EPOCH_1980_IST, now) == real   # shifted forward (seen)
    assert trade_time(int(real.timestamp()), now) == real                     # plain Unix seconds
    assert trade_time(int(real.timestamp()) - EPOCH_1980_IST, now) == real   # seconds since 1980
    assert trade_time(None, now).timestamp() == now and trade_time("x", now).timestamp() == now


def test_partial_fill_hold_splits_and_releases_fills():
    """Partial-fill simulation on 021: 40% of the order is reported first (a trade is split if needed, the first
    part keeping 021's trade id), the rest after the hold; the total equals 021's fills exactly."""
    import time as _t
    from datetime import datetime, timezone
    from decimal import Decimal as D
    from app.brokers.base import ExecutionReport
    from app.brokers.o21_adapter import O21Adapter
    a = O21Adapter.__new__(O21Adapter)
    a._split_done = {}
    hold = {"allowed": D(4), "release_at": _t.time() + 60, "emitted": D(0)}
    rep = ExecutionReport("021-77", "9", "AT-1", "RELIANCE", "BUY", D(10), D(1170), D(1170), 0,
                          datetime.now(timezone.utc), D(10), D(10))
    first = a._held(hold, rep)
    assert first.fill_id == "021-77" and first.quantity == 4 and first.cumulative_filled == 4
    assert a._held(hold, rep) is None          # still held
    hold["allowed"] = None                     # released
    rest = a._held(hold, rep)
    assert rest.fill_id == "021-77/2" and rest.quantity == 6 and rest.cumulative_filled == 10
    assert a._held(hold, rep) is None


async def test_wide_spread_market_rejection_is_resent_as_marketable_limit(ex):
    """021 refuses MARKET orders while the spread is wide: the order (confirmed absent) is resent once as a LIMIT at
    the circuit band edge - sells at the lower, buys at the upper limit - so emergency closes still execute."""
    a = adapter(ex)
    ex.post_mode = "wide_spread"
    inst = a.instruments.nse("RELIANCE")
    o = await a.place_order(PlaceOrderRequest("AT-W1", "RELIANCE", "SELL", D(5), "MARKET"))
    assert o.broker_order_id and len(ex.posts) == 2
    assert ex.posts[0]["price"] == 0 and ex.posts[1]["price"] == inst.lower_circuit and ex.posts[1]["qty"] == -5
    await a.place_order(PlaceOrderRequest("AT-W2", "RELIANCE", "BUY", D(5), "MARKET"))
    assert ex.posts[3]["price"] == inst.upper_circuit and a.converted_to_limit == 2
    # any other rejection is still final and never resent
    ex.post_mode = "500_reject"
    with pytest.raises(BrokerRejected, match="insufficient funds"):
        await a.place_order(PlaceOrderRequest("AT-W3", "RELIANCE", "SELL", D(5), "MARKET"))
    assert len(ex.posts) == 5


async def test_sandbox_circuit_limit_from_the_rejection_is_used(ex):
    """The sandbox's own circuit band can differ from the instruments file: the limit 021 names is used (one tick
    inside it) for one more attempt."""
    a = adapter(ex)
    ex.post_mode = "wide_then_circuit"
    o = await a.place_order(PlaceOrderRequest("AT-C1", "RELIANCE", "SELL", D(5), "MARKET"))
    tick = max(1, a.instruments.nse("RELIANCE").ticksize)
    assert o.broker_order_id and [b["price"] for b in ex.posts][-1] == 120620 + tick and len(ex.posts) == 3
