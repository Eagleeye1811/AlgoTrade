"""Timeout handling, broker failure, reconciliation, recovery, kill switch, runaway, latency guard."""
from __future__ import annotations

import time
import uuid
from decimal import Decimal

from sqlalchemy import select, update

from app.database.models import Order, OrderEvent, Position
from tests.conftest import wait_for

D = Decimal


async def events(sys_, oid):
    async with sys_.db.session() as s:
        return [e.status for e in (await s.execute(select(OrderEvent).where(OrderEvent.order_id == uuid.UUID(oid))
                                                  .order_by(OrderEvent.id))).scalars()]


async def test_timeout_verifies_instead_of_duplicating(trading):
    sys_ = trading
    sys_.broker.adapter.faults.timeout_next_place = 1
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(10), idempotency_key="TMO-1")
    o = r["order"]
    assert r.get("verified_after_timeout") is True and o["status"] == "ACKNOWLEDGED"
    ev = await events(sys_, o["id"])
    assert "UNCERTAIN" in ev and "VERIFIED_AT_BROKER" in ev
    assert sum(1 for b in await sys_.broker.adapter.get_orders() if b.client_order_id == "TMO-1") == 1
    assert not sys_.controller.blocks  # verified -> unblocked


async def test_broker_outage_fails_closed_then_recovers(trading):
    sys_ = trading
    sys_.broker.adapter.faults.network_down_until = time.time() + 2.5
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(10))
    o = r["order"]
    assert o["status"] == "TIMEOUT" and r.get("uncertain") is True
    assert any(k.startswith("uncertain:") for k in sys_.controller.blocks)
    r2 = await sys_.strategies.handle_signal("rsi_ema", "BUY", "x", origin="STRATEGY")
    assert r2["order"]["status"] == "REJECTED"
    assert r2["order"]["reject_rule"] in ("SYSTEM_STATE", "BROKER_HEALTH")
    assert await wait_for(lambda: time.time() > sys_.broker.adapter.faults.network_down_until, 5)
    rec = await sys_.reconciler.run("TEST")
    assert rec["status"] in ("MISMATCH_RESOLVED", "MATCHED")
    assert not sys_.controller.blocks
    async with sys_.db.session() as s:
        assert (await s.get(Order, uuid.UUID(o["id"]))).status == "CANCELLED"  # never reached broker


async def test_reconciliation_recovers_lost_execution_report(trading):
    sys_ = trading
    sys_.broker.adapter.faults.drop_reports_next = 1
    o = (await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY"))["order"]
    await sys_.market.provider.push("RELIANCE", "1000")

    async def broker_filled():
        bo = await sys_.broker.adapter.get_order(o["broker_order_id"])
        return bo.status == "FILLED"

    assert await wait_for(broker_filled)
    assert sys_.positions.get("macd", "RELIANCE").quantity == 0  # report lost
    broker_pos = {p.symbol: p.quantity for p in await sys_.broker.adapter.get_positions()}
    assert broker_pos["RELIANCE"] == D(50)
    rec = await sys_.reconciler.run("TEST")
    assert rec["status"] == "MISMATCH_RESOLVED"
    assert any(m["type"] == "MISSING_FILL" for m in rec["mismatches"])
    assert sys_.positions.get("macd", "RELIANCE").quantity == D(50)
    cmp = next(c for c in rec["details"]["comparison"] if c["symbol"] == "RELIANCE")
    assert cmp["internal_before"] == 0 and cmp["broker"] == 50 and cmp["internal_after"] == 50
    async with sys_.db.session() as s:
        assert (await s.get(Order, uuid.UUID(o["id"]))).status == "FILLED"


async def test_reconciliation_rebuilds_corrupted_position(trading):
    sys_ = trading
    o = (await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY"))["order"]
    await sys_.market.provider.push("RELIANCE", "1000")
    assert await wait_for(lambda: o["id"] not in sys_.orders.open_orders)
    async with sys_.db.transaction() as s:
        await s.execute(update(Position).where(Position.strategy_id == "macd").values(quantity=999))
    await sys_.positions.load()
    rec = await sys_.reconciler.run("TEST")
    assert rec["status"] == "MISMATCH_RESOLVED"
    assert sys_.positions.get("macd", "RELIANCE").quantity == D(50)


async def test_unexplained_broker_position_blocks_orders(trading):
    sys_ = trading
    # an order placed at the broker outside AlgoTrade (no internal record) cannot be reconciled
    from app.brokers.base import PlaceOrderRequest

    await sys_.broker.adapter.place_order(PlaceOrderRequest("EXTERNAL-1", "RELIANCE", "BUY", D(7)))
    await sys_.market.provider.push("RELIANCE", "1000")
    import asyncio

    await asyncio.sleep(0.3)
    rec = await sys_.reconciler.run("TEST")
    assert rec["status"] == "MISMATCH_UNRESOLVED"
    assert "reconciliation" in sys_.controller.blocks
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY")
    assert r["order"]["status"] == "REJECTED" and r["order"]["reject_rule"] == "SYSTEM_STATE"


async def test_reconciliation_failure_blocks_orders(trading):
    sys_ = trading
    sys_.broker.adapter.faults.network_down_until = time.time() + 1.0
    rec = await sys_.reconciler.run("TEST")
    assert rec["status"] == "FAILED" and "reconciliation" in sys_.controller.blocks


async def test_state_recovery_after_restart(trading):
    sys_ = trading
    o = (await sys_.strategies.handle_signal("rsi_ema", "BUY", "x", origin="STRATEGY"))["order"]
    await sys_.market.provider.push("RELIANCE", "1000")
    assert await wait_for(lambda: o["id"] not in sys_.orders.open_orders)
    before = sys_.positions.get("rsi_ema", "RELIANCE")
    await sys_.strategies.set_status("macd", "PAUSED", "test")
    result = await sys_.restart_engine()
    assert result["ok"] and result["reconciliation"] == "MATCHED"
    after = sys_.positions.get("rsi_ema", "RELIANCE")
    assert (after.quantity, after.avg_price) == (before.quantity, before.avg_price)
    assert sys_.controller.status == "PAUSED"  # never auto-resumes trading after restart
    assert sys_.strategies.runtimes["macd"].status == "PAUSED"  # strategy state restored from DB
    assert sys_.account is not None


async def test_restart_with_broker_down_blocks_orders(trading):
    sys_ = trading
    sys_.broker.adapter.faults.network_down_until = time.time() + 30
    result = await sys_.restart_engine()
    assert not result["ok"] and "reconciliation" in sys_.controller.blocks
    sys_.broker.adapter.faults.network_down_until = 0


async def test_kill_switch_flattens_and_enters_safe_mode(trading):
    sys_ = trading
    o = (await sys_.strategies.handle_signal("rsi_ema", "BUY", "x", origin="STRATEGY"))["order"]
    await sys_.market.provider.push("RELIANCE", "1000")
    assert await wait_for(lambda: o["id"] not in sys_.orders.open_orders)
    pending = (await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(10),
                                                   order_type="LIMIT", limit_price=D("900")))["order"]
    state = await sys_.kill_switch.activate("test", "unit test", deadline_s=3)
    assert sys_.controller.status == "SAFE_MODE"
    assert any(c["order_id"] == pending["id"] for c in state["cancelled"])
    assert state["close_orders"][0]["side"] == "SELL" and state["close_orders"][0]["quantity"] == D(100)
    await sys_.market.provider.push("RELIANCE", "1001")
    assert await wait_for(lambda: sys_.positions.get("rsi_ema", "RELIANCE").quantity == 0)
    await sys_.kill_switch._task
    assert sys_.kill_switch.state["phase"] == "COMPLETED" and sys_.kill_switch.state["unresolved"] == []
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="FAULT_LAB", quantity=D(5))
    assert r["order"]["reject_rule"] == "KILL_SWITCH"
    r2 = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY")
    assert r2["action"] == "NOT_TRADED_SAFE_MODE"
    # reset requires successful reconciliation and returns to PAUSED
    await sys_.kill_switch.reset("admin")
    assert sys_.controller.status == "PAUSED"


async def test_kill_switch_reports_unresolved_positions(trading):
    sys_ = trading
    o = (await sys_.strategies.handle_signal("rsi_ema", "BUY", "x", origin="STRATEGY"))["order"]
    await sys_.market.provider.push("RELIANCE", "1000")
    assert await wait_for(lambda: o["id"] not in sys_.orders.open_orders)
    sys_.broker.adapter.faults.network_down_until = time.time() + 60  # broker gone: cannot flatten
    await sys_.kill_switch.activate("test", "unit test", wait=True, deadline_s=1)
    st = sys_.kill_switch.state
    assert st["phase"] == "COMPLETED_WITH_ERRORS"
    assert st["unresolved"] and st["unresolved"][0]["symbol"] == "RELIANCE"
    sys_.broker.adapter.faults.network_down_until = 0


async def test_runaway_strategy_is_halted(trading):
    sys_ = trading
    results = await sys_.strategies.start_runaway("macd", duration_s=4, interval_s=0.05)
    assert sys_.strategies.runtimes["macd"].status == "HALTED"
    rejected = [r for r in results if (r.get("order") or {}).get("status") == "REJECTED"]
    assert len(rejected) >= 5
    # other strategies keep running
    assert sys_.strategies.runtimes["rsi_ema"].status == "RUNNING"


async def test_latency_guard_blocks_orders(trading):
    sys_ = trading
    gw = sys_.broker
    sys_.broker.adapter.timeout_s = 5.0
    sys_.broker.adapter.faults.extra_latency_ms = 2100
    await gw.call("ping", gw.adapter.ping)
    sys_.broker.adapter.faults.extra_latency_ms = 0
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY")
    assert r["order"]["status"] == "REJECTED" and r["order"]["reject_rule"] == "HIGH_LATENCY"
    await gw.call("ping", gw.adapter.ping)
    r2 = await sys_.strategies.handle_signal("rsi_ema", "BUY", "x", origin="STRATEGY")
    assert r2["order"]["status"] == "ACKNOWLEDGED"


async def test_elevated_latency_converts_to_protective_limit(trading):
    sys_ = trading
    gw = sys_.broker
    sys_.broker.adapter.faults.extra_latency_ms = 900
    await gw.call("ping", gw.adapter.ping)
    sys_.broker.adapter.faults.extra_latency_ms = 0
    r = await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY")
    assert r["order"]["order_type"] == "LIMIT" and r["order"]["limit_price"] is not None


async def test_fault_lab_scenario_produces_timeline(trading):
    sys_ = trading
    res = await sys_.faults.run("risk_rejection", "test", {"strategy_id": "macd"})
    assert res["status"] == "COMPLETED"
    phases = [s["phase"] for s in res["steps"]]
    assert phases[0] == "FAULT_INJECTED" and phases[-1] == "FINAL_STATE" and "SYSTEM_RESPONSE" in phases
    assert sys_.broker.adapter.faults.to_dict()["reject_next"] == 0


async def test_fault_lab_disarms_faults_when_aborted(trading):
    sys_ = trading
    await sys_.pause_trading("test")
    res = await sys_.faults.run("network_timeout", "test", {"strategy_id": "macd"})
    assert res["status"] == "ABORTED"
    assert sys_.broker.adapter.faults.timeout_next_place == 0


async def test_kill_switch_flattens_several_strategies_within_10s(trading):
    """L3: stop all strategies, cancel open orders and close positions within 10 seconds."""
    import asyncio as aio
    sys_ = trading
    # long and short books on the same stock plus a position on another stock and a working order
    for sid, side in (("rsi_ema", "BUY"), ("macd", "SELL"), ("breakout", "BUY")):
        await sys_.strategies.handle_signal(sid, side, "x", origin="STRATEGY")
    await sys_.market.provider.push("RELIANCE", "1000")
    await sys_.market.provider.push("INFY", "1500")
    assert await wait_for(lambda: not sys_.orders.open_orders)
    await sys_.strategies.handle_signal("ema_trend", "BUY", "x", origin="FAULT_LAB", quantity=D(5),
                                        order_type="LIMIT", limit_price=D("900"))
    feed_running = True

    async def feed():  # the market keeps ticking so close orders can fill
        while feed_running:
            await sys_.market.provider.push("RELIANCE", "1000")
            await sys_.market.provider.push("INFY", "1500")
            await aio.sleep(0.2)
    t = aio.create_task(feed())
    try:
        await sys_.kill_switch.activate("test", "L3 deadline", wait=True)
    finally:
        feed_running = False
        await t
    st = sys_.kill_switch.state
    assert st["phase"] == "COMPLETED" and st["within_deadline"] and st["elapsed_s"] <= 10
    assert all(p.quantity == 0 for p in sys_.positions.all()) and not sys_.orders.open_orders
    assert {c["strategy_id"] for c in st["close_orders"]} == {"rsi_ema", "macd", "breakout"}


async def test_circuit_breaker_halts_and_flattens_only_the_runaway(trading):
    """Runaway with risk on: the breaker halts the strategy, cancels its working orders and closes its position;
    another strategy's position in the same stock is untouched."""
    import asyncio as aio
    sys_ = trading
    a = (await sys_.strategies.handle_signal("macd", "BUY", "x", origin="STRATEGY"))["order"]
    b = (await sys_.strategies.handle_signal("rsi_ema", "BUY", "x", origin="STRATEGY"))["order"]
    await sys_.market.provider.push("RELIANCE", "1000")
    assert await wait_for(lambda: not sys_.orders.open_orders)
    feeding = True

    async def feed():
        while feeding:
            await sys_.market.provider.push("RELIANCE", "1000", advance_s=0)
            await aio.sleep(0.2)
    t = aio.create_task(feed())
    try:
        await sys_.strategies.start_runaway("macd", 8.0, 0.05)
        rt = sys_.strategies.runtimes["macd"]
        assert rt.status == "HALTED" and "circuit breaker" in rt.status_reason
        fl = await rt.flatten_task
    finally:
        feeding = False
        await t
    assert fl["flat"] and sys_.positions.get("macd", "RELIANCE").quantity == 0
    assert all(c["status"] != "REJECTED" for c in fl["close_orders"])
    assert sys_.positions.get("rsi_ema", "RELIANCE").quantity == D(100)  # untouched
    assert sys_.strategies.runtimes["rsi_ema"].status == "RUNNING"


async def test_adopt_baseline_acknowledges_external_trades_only_when_flat(trading):
    sys_ = trading
    import asyncio

    import pytest

    from app.brokers.base import PlaceOrderRequest

    # trades made at the broker outside AlgoTrade (e.g. the internal ledger was reset afterwards)
    await sys_.broker.adapter.place_order(PlaceOrderRequest("EXTERNAL-1", "RELIANCE", "BUY", D(7)))
    await sys_.market.provider.push("RELIANCE", "1000")
    await asyncio.sleep(0.3)
    with pytest.raises(ValueError, match="positions differ"):  # broker +7 vs platform 0: never acknowledged
        await sys_.reconciler.adopt_baseline("admin")
    await sys_.broker.adapter.place_order(PlaceOrderRequest("EXTERNAL-2", "RELIANCE", "SELL", D(7)))
    await sys_.market.provider.push("RELIANCE", "1001")
    await asyncio.sleep(0.3)
    assert (await sys_.reconciler.run("TEST"))["status"] == "MISMATCH_UNRESOLVED"

    out = await sys_.reconciler.adopt_baseline("admin")
    assert out["acknowledged_fills"] >= 2 and out["acknowledged_orders"] == 2
    assert out["reconciliation"]["status"] == "MATCHED", out["reconciliation"]["details"]["unresolved"]
    assert out["reconciliation"]["details"]["external_acknowledged"]["orders"] == 2
    assert "reconciliation" not in sys_.controller.blocks

    # anything new and unexplained still blocks
    await sys_.broker.adapter.place_order(PlaceOrderRequest("EXTERNAL-3", "RELIANCE", "BUY", D(3)))
    await sys_.market.provider.push("RELIANCE", "1002")
    await asyncio.sleep(0.3)
    assert (await sys_.reconciler.run("TEST"))["status"] == "MISMATCH_UNRESOLVED"
    assert "reconciliation" in sys_.controller.blocks


async def test_fault_lab_test_orders_fit_the_strategy_max_position(trading):
    sys_ = trading
    await sys_.limits.set("STRATEGY", "macd", "max_position", D(5), None)
    await sys_.market.provider.push("RELIANCE", "1000")
    res = await sys_.faults._test_order("macd", D(20), "sizing check")
    assert res["order"]["quantity"] == 5 and res["order"]["status"] != "REJECTED"
    for px in ("1000.5", "1001", "1000.5"):
        await sys_.market.provider.push("RELIANCE", px)
    assert await wait_for(lambda: not any(o.strategy_id == "macd" for o in sys_.orders.open_orders.values()))
    over = await sys_.faults._test_order("macd", D(30), "oversized on purpose", fit=False, side="SELL")
    assert over["order"]["status"] == "REJECTED" and over["order"]["reject_rule"] == "MAX_POSITION"


async def test_demo_reset_keeps_unsubscribed_strategies_stopped(trading):
    sys_ = trading
    await sys_.strategies.unsubscribe("breakout", "test")
    await sys_.demo.reset("admin")
    assert "breakout" not in sys_.strategies.runtimes
    assert {"ema_trend", "rsi_ema", "macd"} <= set(sys_.strategies.runtimes)
