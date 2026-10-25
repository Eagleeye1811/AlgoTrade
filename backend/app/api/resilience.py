from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import bad_request, get_system
from app.core.security import actor, current_user, require_admin, require_trader
from app.database.models import FaultSimulation, ReconciliationEvent, User
from app.engine.system import TradingSystem

router = APIRouter(prefix="/api", tags=["resilience"])


def recon_out(r: ReconciliationEvent) -> dict:
    return {"id": r.id, "trigger": r.trigger, "status": r.status, "summary": r.summary, "mismatches": r.mismatches,
            "actions": r.actions, "details": r.details or {},
            "started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "correlation_id": r.correlation_id}


# ------------------------------------------------------------------ reconciliation
@router.get("/reconciliation")
async def recon_list(limit: int = Query(50, le=200), _: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    async with sys.db.session() as s:
        rows = (await s.execute(select(ReconciliationEvent).order_by(ReconciliationEvent.id.desc()).limit(limit))).scalars().all()
    return {"status": sys.reconciler.status, "runs": [recon_out(r) for r in rows]}


@router.post("/reconciliation/run")
async def recon_run(user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    if sys.account is None:
        raise HTTPException(400, "trading account not configured")
    await sys.audit.record("RECONCILIATION_REQUESTED", f"manual reconciliation by {actor(user)}", source="Reconciler",
                           actor=actor(user))
    return await sys.reconciler.run("MANUAL")


@router.post("/reconciliation/adopt-baseline")
async def recon_adopt_baseline(user: User = Depends(require_admin), sys: TradingSystem = Depends(get_system)):
    """ADMIN: acknowledge broker fills/orders the platform has no record of (only when positions match)."""
    if sys.account is None:
        raise HTTPException(400, "trading account not configured")
    try:
        return await sys.reconciler.adopt_baseline(actor(user))
    except ValueError as e:
        raise HTTPException(400, str(e))


class AdoptIn(BaseModel):
    broker_order_id: str
    strategy_id: str


@router.post("/reconciliation/adopt-order")
async def adopt_order(body: AdoptIn, user: User = Depends(require_admin), sys: TradingSystem = Depends(get_system)):
    """ADMIN: attach a broker order placed outside AlgoTrade to the strategy whose position it closes."""
    try:
        return await sys.orders.adopt_external(body.broker_order_id.strip(), body.strategy_id, actor(user))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/broker/state")
async def broker_state(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    """The broker's own view (021 positions + order book) for side-by-side comparison with the internal books."""
    if sys.account is None:
        raise HTTPException(400, "trading account not configured")
    a = sys.broker.adapter
    try:
        acct = await sys.broker.call("get_account", a.get_account)
        pos = await sys.broker.call("get_positions", a.get_positions)
        orders = await sys.broker.call("get_orders", a.get_orders)
    except Exception as e:
        raise HTTPException(503, f"broker unreachable: {e}")
    return {"account": acct.__dict__, "positions": [p.__dict__ for p in pos],
            "orders": [o.to_dict() for o in sorted(orders, key=lambda o: o.created_at.timestamp() if o.created_at else 0, reverse=True)[:50]], "internal_net": sys.positions.net_by_symbol(),
            "faults": a.faults.to_dict(), "gateway": sys.broker.info()}


# ------------------------------------------------------------------ health / recovery
@router.get("/health")
async def health(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    return {"overall": sys.health.overall(), "components": sys.health.snapshot(), "recovery": sys.last_recovery,
            "trading": sys.controller.snapshot()}


# ------------------------------------------------------------------ fault lab
@router.get("/faults/scenarios")
async def scenarios(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    return {"scenarios": sys.faults.catalog(), "active": sys.faults.active, "autopilot": await sys.faults.autopilot_enabled(),
            "chaos_running": bool(sys.faults.chaos_task and not sys.faults.chaos_task.done()),
            "chaos_sequence": sys.faults.CHAOS_SEQUENCE, "broker_faults": sys.broker.adapter.faults.to_dict()}


class AutopilotIn(BaseModel):
    enabled: bool


@router.put("/faults/autopilot")
async def set_autopilot(body: AutopilotIn, user: User = Depends(require_admin), sys: TradingSystem = Depends(get_system)):
    """ADMIN: self-healing autopilot - after a fault, verify the platform and resume trading only if everything passes."""
    return {"autopilot": await sys.faults.set_autopilot(body.enabled, actor(user))}


class FaultIn(BaseModel):
    strategy_id: str | None = None


@router.post("/faults/run/{scenario}")
async def run_fault(scenario: str, body: FaultIn | None = None, user: User = Depends(require_trader),
                    sys: TradingSystem = Depends(get_system)):
    if scenario not in sys.faults.SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    try:
        sys.faults.start_background(scenario, actor(user), (body.model_dump(exclude_none=True) if body else {}))
    except ValueError as e:
        raise bad_request(e)
    return {"started": scenario}


@router.post("/faults/chaos")
async def chaos(user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    try:
        sys.faults.start_chaos(actor(user))
    except ValueError as e:
        raise bad_request(e)
    return {"started": True, "sequence": sys.faults.CHAOS_SEQUENCE}


@router.get("/faults/runs")
async def runs(limit: int = Query(30, le=200), _: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    async with sys.db.session() as s:
        rows = (await s.execute(select(FaultSimulation).order_by(FaultSimulation.started_at.desc()).limit(limit))).scalars().all()
    return [{"id": str(r.id), "scenario": r.scenario, "status": r.status, "outcome": r.outcome, "steps": r.steps,
             "started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None,
             "triggered_by": r.triggered_by, "run_group": r.run_group, "correlation_id": r.correlation_id} for r in rows]


@router.get("/faults/runs/{run_id}")
async def run_detail(run_id: str, _: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(404, "not found")
    async with sys.db.session() as s:
        r = await s.get(FaultSimulation, rid)
    if r is None:
        raise HTTPException(404, "not found")
    return {"id": str(r.id), "scenario": r.scenario, "status": r.status, "outcome": r.outcome, "steps": r.steps,
            "started_at": r.started_at.isoformat(), "correlation_id": r.correlation_id}


# ------------------------------------------------------------------ demo mode
@router.get("/demo")
async def demo_state(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    return sys.demo.snapshot()


@router.post("/demo/start")
async def demo_start(user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    if sys.account is None:
        raise HTTPException(400, "trading account not configured")
    if sys.controller.status == "SAFE_MODE":
        raise HTTPException(400, "system is in SAFE MODE - reset the demo (or the kill switch) first")
    try:
        sys.demo.start(actor(user))
    except ValueError as e:
        raise bad_request(e)
    return sys.demo.snapshot()


@router.post("/demo/pause")
async def demo_pause(_: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    try:
        await sys.demo.pause()
    except ValueError as e:
        raise bad_request(e)
    return sys.demo.snapshot()


@router.post("/demo/resume")
async def demo_resume(_: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    try:
        await sys.demo.resume()
    except ValueError as e:
        raise bad_request(e)
    return sys.demo.snapshot()


@router.post("/demo/reset")
async def demo_reset(user: User = Depends(require_admin), sys: TradingSystem = Depends(get_system)):
    try:
        return await sys.demo.reset(actor(user))
    except ValueError as e:
        raise bad_request(e)
