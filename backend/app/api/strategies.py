from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import bad_request, get_system
from app.core.security import actor, current_user, require_trader
from app.database.models import Fill, Order, Signal, StrategyConfig, User
from app.engine.order_engine import order_to_dict
from app.engine.system import TradingSystem

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


def _rt(sys: TradingSystem, sid: str):
    rt = sys.strategies.runtimes.get(sid)
    if rt is None:
        raise HTTPException(404, "strategy not found")
    return rt


def signal_out(x: Signal) -> dict:
    return {"id": str(x.id), "strategy_id": x.strategy_id, "symbol": x.symbol, "signal": x.signal, "price": x.price,
            "candle_time": x.candle_time.isoformat() if x.candle_time else None, "indicators": x.indicators,
            "reason": x.reason, "origin": x.origin, "action": x.action, "correlation_id": x.correlation_id,
            "created_at": x.created_at.isoformat()}


@router.get("")
async def list_strategies(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    return [sys.strategies.snapshot(rt) for rt in sys.strategies.runtimes.values()]


@router.get("/{sid}")
async def detail(sid: str, _: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    rt = _rt(sys, sid)
    async with sys.db.session() as s:
        sigs = (await s.execute(select(Signal).where(Signal.strategy_id == sid).order_by(Signal.created_at.desc()).limit(30))).scalars().all()
        orders = (await s.execute(select(Order).where(Order.strategy_id == sid).order_by(Order.created_at.desc()).limit(30))).scalars().all()
        cfgs = (await s.execute(select(StrategyConfig).where(StrategyConfig.strategy_id == sid)
                                .order_by(StrategyConfig.version.desc()))).scalars().all()
    return {**sys.strategies.snapshot(rt), "limits": {n: sys.limits.s(sid, n) for n in ("max_position", "max_orders_per_min", "max_exposure")},
            "signals": [signal_out(x) for x in sigs], "orders": [order_to_dict(o) for o in orders],
            "config_history": [{"version": c.version, "params": c.params, "active": c.is_active,
                                "created_at": c.created_at.isoformat()} for c in cfgs]}


@router.get("/{sid}/chart")
async def chart(sid: str, limit: int = Query(300, le=600), symbol: str | None = None, _: User = Depends(current_user),
                sys: TradingSystem = Depends(get_system)):
    rt = _rt(sys, sid)
    sym = (symbol or rt.symbol).upper()
    inst = rt.instances.get(sym)
    if inst is None:
        raise HTTPException(404, f"{rt.name} does not trade {sym}")
    bars = inst.bars[-limit:]
    since = bars[0].ts if bars else None
    series: dict[str, Any] = inst.series()
    t0 = int(since.timestamp()) if since else 0
    series = {k: [p for p in v if p["time"] >= t0] for k, v in series.items()}
    working = sys.market.candles.working.get((sym, rt.timeframe))
    async with sys.db.session() as s:
        sigs = (await s.execute(select(Signal).where(Signal.strategy_id == sid, Signal.symbol == sym, Signal.candle_time.is_not(None),
                                                     Signal.candle_time >= since, Signal.candle_time <= bars[-1].ts)
                                if since else
                                select(Signal).where(Signal.strategy_id == sid).limit(0))).scalars().all()
        fills = (await s.execute(select(Fill).where(Fill.strategy_id == sid, Fill.symbol == sym).order_by(Fill.filled_at.desc()).limit(100))).scalars().all()
    return {
        "strategy_id": sid, "symbol": sym, "timeframe": rt.timeframe, "kind": rt.kind,
        "candles": [b.to_dict() for b in bars] + ([working.to_dict()] if working and (not bars or working.ts > bars[-1].ts) else []),
        "series": series, "params": rt.params,
        "markers": [{"time": int(x.candle_time.timestamp()), "signal": x.signal, "action": x.action, "price": x.price,
                     "reason": x.reason} for x in sigs],
        "fills": [{"side": f.side, "quantity": f.quantity, "price": f.price, "at": f.filled_at.isoformat()} for f in fills],
    }


@router.post("/{sid}/pause")
async def pause(sid: str, user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    _rt(sys, sid)
    await sys.strategies.set_status(sid, "PAUSED", f"paused by {user.email}", actor=actor(user))
    return sys.strategies.snapshot(sys.strategies.runtimes[sid])


@router.post("/{sid}/resume")
async def resume(sid: str, user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    _rt(sys, sid)
    if sys.controller.status == "SAFE_MODE":
        raise HTTPException(400, "system is in SAFE MODE")
    await sys.strategies.set_status(sid, "RUNNING", None, actor=actor(user))
    return sys.strategies.snapshot(sys.strategies.runtimes[sid])


class ConfigIn(BaseModel):
    params: dict[str, float] | None = None
    quantity: Decimal | None = Field(default=None, gt=0, le=100000)
    allocation: Decimal | None = Field(default=None, ge=0)


@router.put("/{sid}/config")
async def configure(sid: str, body: ConfigIn, user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    _rt(sys, sid)
    if body.quantity is not None and body.quantity != body.quantity.to_integral_value():
        raise HTTPException(400, "quantity must be a whole number of shares")
    try:
        rt = await sys.strategies.update_config(sid, body.params, body.quantity, body.allocation, user.id, actor(user))
    except ValueError as e:
        raise bad_request(e)
    return sys.strategies.snapshot(rt)
