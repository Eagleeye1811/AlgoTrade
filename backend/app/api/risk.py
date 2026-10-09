from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import bad_request, get_system
from app.api.orders import risk_out
from app.core.security import actor, current_user, require_admin
from app.database.models import RiskEvent, User
from app.engine.system import TradingSystem

router = APIRouter(prefix="/api/risk", tags=["risk"])


def level(used: float, limit: float) -> str:
    if limit <= 0:
        return "BREACH" if used > 0 else "SAFE"
    r = used / limit
    return "BREACH" if r >= 1 else "WARNING" if r >= 0.75 else "SAFE"


def meter(label: str, used, limit, unit: str) -> dict:
    u, l = float(used), float(limit)
    return {"label": label, "used": u, "limit": l, "unit": unit, "pct": round(u / l * 100, 1) if l else 0.0,
            "level": level(u, l)}


@router.get("")
async def risk_overview(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    snap = sys.pnl.compute()
    port = snap["portfolio"]
    lim = sys.limits
    g_orders = await sys.redis.count_in_window("orders:global", 60)
    loss = -min(port["today_pnl"], Decimal(0))
    portfolio = [
        meter("Daily loss", loss, lim.g("max_daily_loss"), "INR"),
        meter("Overall exposure", port["exposure"], lim.g("max_gross_exposure"), "INR"),
        meter("Capital in use (margin)", port["margin_used"] + port["reserved_margin"], port["equity"], "INR"),
        meter("Orders (last 60s)", g_orders, lim.g("max_orders_per_min"), "orders"),
    ]
    strategies = {}
    for sid, rt in sys.strategies.runtimes.items():
        st = snap["strategies"].get(sid, {})
        pos = abs(sys.positions.get(sid, rt.symbol).quantity)
        s_orders = await sys.redis.count_in_window(f"orders:{sid}", 60)
        strategies[sid] = {
            "name": rt.name, "status": rt.status, "status_reason": rt.status_reason, "symbol": rt.symbol,
            "meters": [
                meter("Position", pos, lim.s(sid, "max_position"), "shares"),
                meter("Loss today", -min(st.get("today_pnl", 0), 0), lim.daily_loss_limit(sid, rt.allocation), "INR"),
                meter("Virtual margin", st.get("margin_used", 0) + st.get("reserved_margin", 0), rt.allocation, "INR"),
                meter("Exposure", st.get("exposure", 0), lim.s(sid, "max_exposure"), "INR"),
                meter("Orders (last 60s)", s_orders, lim.s(sid, "max_orders_per_min"), "orders"),
            ],
            "allocation": rt.allocation, "margin_used": st.get("margin_used"), "reserved_margin": st.get("reserved_margin"),
            "available_margin": st.get("available_margin"),
        }
    guard = {"broker_latency_ms": sys.broker.current_latency(),
             "latency_block_ms": lim.g("latency_block_ms"), "latency_warn_ms": lim.g("latency_warn_ms"),
             "recent_slippage_bps": sys.orders.recent_slippage_bps(), "slippage_warn_bps": lim.g("slippage_warn_bps"),
             "volatility_bps": {s: sys.risk.guard.volatility_bps(s) for s in (sys.market.provider.symbols if sys.market.provider else [])},
             "max_volatility_bps": lim.g("max_volatility_bps")}
    return {"portfolio": portfolio, "strategies": strategies, "guard": guard, "limits": lim.describe(),
            "trading": sys.controller.snapshot(), "stats": sys.risk.stats}


@router.get("/events")
async def events(decision: str | None = None, limit: int = Query(100, le=500), _: User = Depends(current_user),
                 sys: TradingSystem = Depends(get_system)):
    q = select(RiskEvent).order_by(RiskEvent.id.desc()).limit(limit)
    if decision:
        q = q.where(RiskEvent.decision == decision.upper())
    async with sys.db.session() as s:
        rows = (await s.execute(q)).scalars().all()
    return [risk_out(r) for r in rows]


class LimitIn(BaseModel):
    scope: str = Field(pattern="^(GLOBAL|STRATEGY)$")
    strategy_id: str | None = None
    name: str
    value: Decimal = Field(ge=0)


@router.put("/limits")
async def set_limit(body: LimitIn, user: User = Depends(require_admin), sys: TradingSystem = Depends(get_system)):
    if body.scope == "STRATEGY" and body.strategy_id not in sys.strategies.runtimes:
        raise bad_request(ValueError("unknown strategy"))
    sid = body.strategy_id if body.scope == "STRATEGY" else None
    try:
        old = sys.limits.s(sid, body.name) if sid else sys.limits.g(body.name)
        await sys.limits.set(body.scope, sid, body.name, body.value, user.id)
    except (ValueError, KeyError) as e:
        raise bad_request(e)
    await sys.audit.record("RISK_LIMIT_CHANGED", f"{body.scope}{'/' + sid if sid else ''} {body.name}: {old} -> {body.value}",
                           source="RiskFirewall", actor=actor(user), strategy_id=sid)
    return sys.limits.describe()
