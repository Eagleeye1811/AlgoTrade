"""End-to-end engine tests: Market data -> Strategy signal -> Risk -> Order -> Broker -> Fill -> Position -> P&L."""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.brokers.base import ExecutionReport
from app.database.models import Fill, Order, OrderEvent, RiskEvent
from tests.conftest import wait_for

D = Decimal


async def lifecycle(sys_, order_id: str) -> list[str]:
    async with sys_.db.session() as s:
        return [e.status for e in (await s.execute(select(OrderEvent).where(OrderEvent.order_id == uuid.UUID(order_id))
                                                  .order_by(OrderEvent.id))).scalars()]


async def fill_order(sys_, symbol: str, price: str, order_id: str, timeout=5.0):
    feed = sys_.market.provider
    await feed.push(symbol, price)
    return await wait_for(lambda: order_id not in sys_.orders.open_orders, timeout)


async def test_signal_to_pnl_full_loop(trading):
    sys_ = trading
    res = await sys_.strategies.handle_signal("macd", "BUY", "test crossover", {"macd": 1}, origin="STRATEGY")
    o = res["order"]
    assert res["action"] == "ORDER" and o["status"] == "ACKNOWLEDGED" and o["broker_order_id"]
    assert await fill_order(sys_, "RELIANCE", "1002.00", o["id"])
    assert await lifecycle(sys_, o["id"]) == ["CREATED", "RISK_CHECK", "APPROVED", "SUBMITTED", "ACKNOWLEDGED", "FILLED"]
    pos = sys_.positions.get("macd", "RELIANCE")
    assert pos.quantity == D(50) and pos.side == "LONG"
    async with sys_.db.session() as s:
        f = (await s.execute(select(Fill))).scalar_one()
        assert f.quantity == D(50) and f.price <= D("1002.00")  # clamped inside traded range
        r = (await s.execute(select(RiskEvent))).scalar_one()
        assert r.decision == "APPROVED" and r.correlation_id == res["correlation_id"]
    await sys_.market.provider.push("RELIANCE", "1012.00")
    snap = sys_.pnl.compute()
    assert snap["strategies"]["macd"]["unrealized"] == ((D("1012") - pos.avg_price) * 50).quantize(D("0.01"))
    assert snap["portfolio"]["exposure"] == D("50600.00")
    # SELL closes the position and books realized P&L from actual fill prices
    res2 = await sys_.strategies.handle_signal("macd", "SELL", "test cross down", origin="STRATEGY")
    assert res2["order"]["reduce_only"] is True and res2["order"]["quantity"] == D(50)
    assert await fill_order(sys_, "RELIANCE", "1012.00", res2["order"]["id"])
    pos2 = sys_.positions.get("macd", "RELIANCE")
    assert pos2.quantity == 0 and pos2.realized_pnl == ((D("1012") - pos.avg_price) * 50).quantize(D("0.0001"))


async def test_already_long_signal_is_ignored(trading):
    sys_ = trading
    o = (await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY"))["order"]
    await fill_order(sys_, "RELIANCE", "1000", o["id"])
    res = await sys_.strategies.handle_signal("macd", "BUY", "again", origin="STRATEGY")
    assert res["action"] == "IGNORED_ALREADY_LONG" and "order" not in res


async def test_risk_rejection_reports_rule_actual_allowed(trading):
    sys_ = trading
    res = await sys_.strategies.handle_signal("macd", "BUY", "oversized", origin="FAULT_LAB", quantity=D(600))
    o = res["order"]
    assert o["status"] == "REJECTED" and o["reject_rule"] == "MAX_POSITION"
    chk = next(c for c in res["risk"]["checks"] if c["rule"] == "MAX_POSITION")
    assert chk["result"] == "FAIL" and chk["actual"] == "600" and chk["allowed"] == "500"
    assert "Maximum Position Size Exceeded" in chk["message"]
    assert await sys_.broker.adapter.get_orders() == []  # never reached the broker


async def test_virtual_margin_isolation(trading):
    sys_ = trading
    await sys_.strategies.update_config("macd", None, None, D(60000), None, "test")
    o = (await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(50)))["order"]
    assert o["status"] == "ACKNOWLEDGED"
    await fill_order(sys_, "RELIANCE", "1000", o["id"])
    # 40 more shares need ~₹40,000 margin but only ~₹10,000 of macd's ₹60,000 allocation is free
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(40))
    assert r["order"]["status"] == "REJECTED" and r["order"]["reject_rule"] == "STRATEGY_MARGIN"
    # ...while another strategy's reserved allocation is untouched
    r2 = await sys_.strategies.handle_signal("rsi_ema", "BUY", "x", origin="STRATEGY")
    assert r2["order"]["status"] == "ACKNOWLEDGED"


async def test_strategy_isolation_same_symbol(trading):
    sys_ = trading
    a = (await sys_.strategies.handle_signal("rsi_ema", "BUY", "x", origin="STRATEGY"))["order"]
    b = (await sys_.strategies.handle_signal("macd", "SELL", "x", origin="STRATEGY"))["order"]
    await sys_.market.provider.push("RELIANCE", "1000")
    assert await wait_for(lambda: not sys_.orders.open_orders)
    assert sys_.positions.get("rsi_ema", "RELIANCE").quantity == D(100)
    assert sys_.positions.get("macd", "RELIANCE").quantity == D(-50)
    assert sys_.positions.net_by_symbol()["RELIANCE"] == D(50)
    snap = sys_.pnl.compute()
    assert snap["strategies"]["rsi_ema"]["exposure"] == D("100000.00")
    assert snap["strategies"]["macd"]["exposure"] == D("50000.00")
    assert snap["portfolio"]["exposure"] == D("150000.00")  # gross, not netted
    assert a["strategy_id"] != b["strategy_id"]


async def test_partial_fill_position_follows_actual_fills(trading):
    sys_ = trading
    f = sys_.broker.adapter.faults
    f.partial_next, f.partial_ratio, f.partial_hold_s = 1, D("0.4"), 0.5
    o = (await sys_.strategies.handle_signal("rsi_ema", "BUY", "x", origin="STRATEGY"))["order"]
    await sys_.market.provider.push("RELIANCE", "1000")
    assert await wait_for(lambda: sys_.positions.get("rsi_ema", "RELIANCE").quantity == D(40))
    assert sys_.orders.open_orders[o["id"]].status == "PARTIALLY_FILLED"
    import asyncio

    await asyncio.sleep(0.6)
    assert await fill_order(sys_, "RELIANCE", "1001", o["id"])
    assert sys_.positions.get("rsi_ema", "RELIANCE").quantity == D(100)
    assert "PARTIALLY_FILLED" in await lifecycle(sys_, o["id"])


async def test_duplicate_idempotency_key_creates_one_order(trading):
    sys_ = trading
    r1 = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(5), idempotency_key="K-1")
    from app.engine.types import OrderIntent

    r2 = await sys_.orders.submit(OrderIntent("macd", "RELIANCE", "BUY", D(5), origin="FAULT_LAB", idempotency_key="K-1"))
    assert r2["duplicate"] is True and r2["order"]["id"] == r1["order"]["id"]
    async with sys_.db.session() as s:
        assert len((await s.execute(select(Order).where(Order.client_order_id == "K-1"))).all()) == 1
    assert sum(1 for b in await sys_.broker.adapter.get_orders() if b.client_order_id == "K-1") == 1


async def test_pending_order_blocks_second_order(trading):
    sys_ = trading
    r1 = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(5))
    r2 = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(6))
    assert r1["order"]["status"] == "ACKNOWLEDGED"
    assert r2["order"]["reject_rule"] == "DUPLICATE_ORDER"


async def test_fill_dedupe(trading):
    sys_ = trading
    o = (await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY"))["order"]
    await fill_order(sys_, "RELIANCE", "1000", o["id"])
    fills = await sys_.broker.adapter.get_fills()
    assert await sys_.orders.ingest_fill(fills[0], "RECONCILIATION") is False  # already processed
    assert sys_.positions.get("macd", "RELIANCE").quantity == D(50)


async def test_stale_market_data_blocks_orders(trading):
    sys_ = trading
    import asyncio

    await asyncio.sleep(3.2)  # stale_after_s_replay = 3s in tests
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY")
    assert r["order"]["status"] == "REJECTED" and r["order"]["reject_rule"] == "STALE_MARKET_DATA"


async def test_trading_paused_produces_no_orders(trading):
    sys_ = trading
    await sys_.pause_trading("test")
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY")
    assert r["action"] == "NOT_TRADED_PAUSED" and "order" not in r


async def test_daily_loss_limit(trading):
    sys_ = trading
    o = (await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY"))["order"]
    await fill_order(sys_, "RELIANCE", "1000", o["id"])
    await sys_.limits.set("GLOBAL", None, "max_daily_loss", D(100), None)
    await sys_.market.provider.push("RELIANCE", "995")  # unrealized -250 (inside the 1% stop-loss)
    r = await sys_.strategies.handle_signal("rsi_ema", "BUY", "x", origin="STRATEGY")
    assert r["order"]["reject_rule"] == "DAILY_LOSS"
    # risk-reducing orders are still allowed
    r2 = await sys_.strategies.handle_signal("macd", "SELL", "close", origin="STRATEGY")
    assert r2["order"]["status"] == "ACKNOWLEDGED"


async def test_broker_rejection(trading):
    sys_ = trading
    sys_.broker.adapter.faults.reject_next = 1
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY")
    assert r["order"]["status"] == "REJECTED" and r["order"]["reject_rule"] == "BROKER_REJECTED"
    assert sys_.positions.get("macd", "RELIANCE").quantity == 0


async def test_limit_order_respects_price(trading):
    sys_ = trading
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(10),
                                            order_type="LIMIT", limit_price=D("995.00"))
    oid = r["order"]["id"]
    await sys_.market.provider.push("RELIANCE", "1001")
    import asyncio

    await asyncio.sleep(0.3)
    assert oid in sys_.orders.open_orders  # price above limit: no fill
    await sys_.market.provider.push("RELIANCE", "994")
    assert await wait_for(lambda: oid not in sys_.orders.open_orders)
    async with sys_.db.session() as s:
        f = (await s.execute(select(Fill))).scalar_one()
    assert f.price <= D("995.00")


async def test_cancel_order(trading):
    sys_ = trading
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(10),
                                            order_type="LIMIT", limit_price=D("950.00"))
    await sys_.orders.cancel(r["order"]["id"], "test")
    assert await wait_for(lambda: r["order"]["id"] not in sys_.orders.open_orders)
    assert (await lifecycle(sys_, r["order"]["id"]))[-1] == "CANCELLED"


async def test_strategy_cannot_reach_broker_directly():
    """Architecture guard: strategy modules never import the order engine or brokers."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "strategies"
    for f in root.glob("*.py"):
        src = f.read_text()
        assert "brokers" not in src and "order_engine" not in src and "place_order" not in src, f.name


async def test_manual_execution_report_for_unknown_order_is_flagged(trading):
    from datetime import datetime, timezone

    rep = ExecutionReport("PF-X", "PB-X", "nope", "RELIANCE", "BUY", D(1), D(1000), D(1000), 1,
                          datetime.now(timezone.utc), D(1), D(1))
    assert await trading.orders.ingest_fill(rep, "EXECUTION_REPORT") is False
