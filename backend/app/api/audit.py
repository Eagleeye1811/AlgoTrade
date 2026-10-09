from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select

from app.api.deps import get_system
from app.api.orders import fill_out, risk_out
from app.api.strategies import signal_out
from app.core.security import current_user
from app.database.models import Fill, Order, OrderEvent, PositionEvent, RiskEvent, Signal, SystemEvent, User
from app.engine.order_engine import order_to_dict
from app.engine.system import TradingSystem

router = APIRouter(prefix="/api/audit", tags=["audit"])


def ev_out(e: SystemEvent) -> dict:
    return {"id": e.id, "event_type": e.event_type, "severity": e.severity, "source": e.source, "message": e.message,
            "correlation_id": e.correlation_id, "strategy_id": e.strategy_id, "order_id": e.order_id, "symbol": e.symbol,
            "actor": e.actor, "payload": e.payload, "created_at": e.created_at.isoformat()}


@router.get("")
async def audit(event_type: str | None = None, severity: str | None = None, q: str | None = None,
                limit: int = Query(200, le=1000), _: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    stmt = select(SystemEvent).order_by(SystemEvent.id.desc()).limit(limit)
    if event_type:
        stmt = stmt.where(SystemEvent.event_type == event_type)
    if severity:
        stmt = stmt.where(SystemEvent.severity == severity.upper())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(SystemEvent.message.ilike(like), SystemEvent.correlation_id == q, SystemEvent.order_id == q))
    async with sys.db.session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return [ev_out(e) for e in rows]


@router.get("/trace/{correlation_id}")
async def trace(correlation_id: str, _: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    """Answer 'why did this order happen?': Signal -> Risk -> Order lifecycle -> Fills -> Position changes."""
    async with sys.db.session() as s:
        sigs = (await s.execute(select(Signal).where(Signal.correlation_id == correlation_id))).scalars().all()
        risks = (await s.execute(select(RiskEvent).where(RiskEvent.correlation_id == correlation_id).order_by(RiskEvent.id))).scalars().all()
        orders = (await s.execute(select(Order).where(Order.correlation_id == correlation_id).order_by(Order.created_at))).scalars().all()
        oids = [o.id for o in orders]
        oevents = (await s.execute(select(OrderEvent).where(OrderEvent.order_id.in_(oids)).order_by(OrderEvent.id))).scalars().all() if oids else []
        fills = (await s.execute(select(Fill).where(Fill.order_id.in_(oids)).order_by(Fill.filled_at))).scalars().all() if oids else []
        pevents = (await s.execute(select(PositionEvent).where(PositionEvent.correlation_id == correlation_id)
                                   .order_by(PositionEvent.id))).scalars().all()
        sevents = (await s.execute(select(SystemEvent).where(SystemEvent.correlation_id == correlation_id)
                                   .order_by(SystemEvent.id))).scalars().all()
    return {
        "correlation_id": correlation_id,
        "signals": [signal_out(x) for x in sigs],
        "risk": [risk_out(r) for r in risks],
        "orders": [{**order_to_dict(o), "lifecycle": [{"status": e.status, "message": e.message, "at": e.created_at.isoformat()}
                                                       for e in oevents if e.order_id == o.id]} for o in orders],
        "fills": [fill_out(f) for f in fills],
        "position_events": [{"strategy_id": p.strategy_id, "symbol": p.symbol, "event_type": p.event_type,
                             "qty_before": p.qty_before, "qty_after": p.qty_after, "avg_after": p.avg_after,
                             "realized_delta": p.realized_delta, "at": p.created_at.isoformat()} for p in pevents],
        "events": [ev_out(e) for e in sevents],
    }
