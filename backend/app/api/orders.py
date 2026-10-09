from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app.api.deps import bad_request, get_system
from app.core.security import actor, current_user, require_trader
from app.database.models import Fill, Order, OrderEvent, PnlSnapshot, PositionEvent, RiskEvent, Signal, User
from app.engine.order_engine import order_to_dict
from app.engine.system import TradingSystem

router = APIRouter(prefix="/api", tags=["orders"])


def fill_out(f: Fill) -> dict:
    return {"id": str(f.id), "broker_fill_id": f.broker_fill_id, "order_id": str(f.order_id), "strategy_id": f.strategy_id,
            "symbol": f.symbol, "side": f.side, "quantity": f.quantity, "price": f.price,
            "reference_price": f.reference_price, "slippage_bps": f.slippage_bps, "latency_ms": f.latency_ms,
            "filled_at": f.filled_at.isoformat(), "source": f.source}


def risk_out(r: RiskEvent) -> dict:
    return {"id": r.id, "strategy_id": r.strategy_id, "order_id": str(r.order_id) if r.order_id else None,
            "correlation_id": r.correlation_id, "decision": r.decision, "rule": r.rule, "actual": r.actual,
            "allowed": r.allowed, "message": r.message, "checks": r.checks, "created_at": r.created_at.isoformat()}


@router.get("/orders")
async def list_orders(status: str | None = None, strategy_id: str | None = None, limit: int = Query(200, le=1000),
                      _: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    q = select(Order).order_by(Order.created_at.desc()).limit(limit)
    if status:
        q = q.where(Order.status == status.upper())
    if strategy_id:
        q = q.where(Order.strategy_id == strategy_id)
    async with sys.db.session() as s:
        rows = (await s.execute(q)).scalars().all()
    return [order_to_dict(o) for o in rows]


@router.get("/orders/{order_id}")
async def order_detail(order_id: str, _: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    try:
        oid = uuid.UUID(order_id)
    except ValueError:
        raise HTTPException(404, "order not found")
    async with sys.db.session() as s:
        o = await s.get(Order, oid)
        if o is None:
            raise HTTPException(404, "order not found")
        events = (await s.execute(select(OrderEvent).where(OrderEvent.order_id == oid).order_by(OrderEvent.id))).scalars().all()
        fills = (await s.execute(select(Fill).where(Fill.order_id == oid).order_by(Fill.filled_at))).scalars().all()
        risk = (await s.execute(select(RiskEvent).where(RiskEvent.order_id == oid).order_by(RiskEvent.id))).scalars().all()
        sig = await s.get(Signal, o.signal_id) if o.signal_id else None
    from app.api.strategies import signal_out

    return {"order": order_to_dict(o),
            "lifecycle": [{"status": e.status, "message": e.message, "data": e.data, "at": e.created_at.isoformat()} for e in events],
            "fills": [fill_out(f) for f in fills], "risk": [risk_out(r) for r in risk],
            "signal": signal_out(sig) if sig else None}


@router.post("/orders/{order_id}/cancel")
async def cancel(order_id: str, user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    try:
        return await sys.orders.cancel(order_id, f"cancelled by {actor(user)}")
    except ValueError as e:
        raise bad_request(e)


@router.get("/fills")
async def fills(limit: int = Query(200, le=1000), _: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    async with sys.db.session() as s:
        rows = (await s.execute(select(Fill).order_by(Fill.filled_at.desc()).limit(limit))).scalars().all()
    return [fill_out(f) for f in rows]


@router.get("/positions")
async def positions(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    snap = sys.pnl.compute()
    return {"positions": snap["positions"], "portfolio": snap["portfolio"], "strategies": snap["strategies"]}


@router.get("/positions/events")
async def position_events(limit: int = Query(100, le=500), _: User = Depends(current_user),
                          sys: TradingSystem = Depends(get_system)):
    async with sys.db.session() as s:
        rows = (await s.execute(select(PositionEvent).order_by(PositionEvent.id.desc()).limit(limit))).scalars().all()
    return [{"id": r.id, "strategy_id": r.strategy_id, "symbol": r.symbol, "event_type": r.event_type,
             "qty_before": r.qty_before, "qty_after": r.qty_after, "avg_before": r.avg_before, "avg_after": r.avg_after,
             "realized_delta": r.realized_delta, "correlation_id": r.correlation_id, "data": r.data,
             "created_at": r.created_at.isoformat()} for r in rows]


@router.get("/pnl/history")
async def pnl_history(strategy_id: str | None = None, limit: int = Query(360, le=2000), _: User = Depends(current_user),
                      sys: TradingSystem = Depends(get_system)):
    q = select(PnlSnapshot).order_by(PnlSnapshot.ts.desc()).limit(limit)
    q = q.where(PnlSnapshot.strategy_id == strategy_id) if strategy_id else q.where(PnlSnapshot.strategy_id.is_(None))
    async with sys.db.session() as s:
        rows = (await s.execute(q)).scalars().all()
    return [{"time": int(r.ts.timestamp()), "realized": r.realized, "unrealized": r.unrealized, "total": r.total,
             "exposure": r.exposure, "margin_used": r.margin_used} for r in reversed(rows)]
