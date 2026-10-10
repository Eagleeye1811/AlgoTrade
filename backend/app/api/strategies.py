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


def _owned(sys: TradingSystem, sid: str, user: User):
    """A trader manages only their own subscriptions; an ADMIN manages all."""
    rt = _rt(sys, sid)
    if user.role != "ADMIN" and rt.user_id != user.id:
        raise HTTPException(403, "not your subscription")
    return rt


LIMIT_NAMES = ("max_daily_loss", "max_position", "max_orders_per_min", "max_exposure")


def _limits(sys: TradingSystem, sid: str) -> dict:
    rt = sys.strategies.runtimes.get(sid)
    out = {n: sys.limits.s(sid, n) for n in LIMIT_NAMES if n != "max_daily_loss"}
    out["max_daily_loss"] = sys.limits.daily_loss_limit(sid, rt.allocation if rt else Decimal(0))
    return out


def signal_out(x: Signal) -> dict:
    return {"id": str(x.id), "strategy_id": x.strategy_id, "symbol": x.symbol, "signal": x.signal, "price": x.price,
            "candle_time": x.candle_time.isoformat() if x.candle_time else None, "indicators": x.indicators,
            "reason": x.reason, "origin": x.origin, "action": x.action, "correlation_id": x.correlation_id,
            "created_at": x.created_at.isoformat()}


@router.get("")
async def list_strategies(mine: bool = False, user: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    """All running subscriptions on the account, or only the caller's with ?mine=true."""
    return [{**sys.strategies.snapshot(rt), "limits": _limits(sys, rt.id)} for rt in sys.strategies.runtimes.values()
            if not mine or rt.user_id == user.id]


@router.get("/catalog")
async def catalog(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    """Strategies a user can subscribe to, with their parameters, plus tradable symbols and free capital."""
    from app.strategies.registry import REGISTRY
    return {
        "strategies": [{"kind": k, "name": c.display_name, "description": c.description,
                        "param_specs": [p.to_dict() for p in c.param_specs]} for k, c in REGISTRY.items()],
        "symbols": sys.strategies.symbols(),
        "unallocated_capital": sys.strategies.unallocated(),
        "account_configured": sys.account is not None,
    }


class SubscribeIn(BaseModel):
    kind: str
    symbol: str
    quantity: Decimal = Field(gt=0, le=100000)
    allocation: Decimal = Field(default=Decimal(0), ge=0)  # 0 = allocate later via Configure
    params: dict[str, float] | None = None
    max_daily_loss: Decimal | None = Field(default=None, gt=0)
    max_position: Decimal | None = Field(default=None, gt=0)
    max_orders_per_min: Decimal | None = Field(default=None, gt=0, le=600)


@router.post("")
async def subscribe(body: SubscribeIn, user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    try:
        rt = await sys.strategies.subscribe(user.id, body.kind, body.symbol, body.quantity, body.allocation,
                                            body.params, actor(user))
        for name in ("max_daily_loss", "max_position", "max_orders_per_min"):
            v = getattr(body, name)
            if v is not None:
                await sys.limits.set("STRATEGY", rt.id, name, v, user.id)
    except ValueError as e:
        raise bad_request(e)
    return {**sys.strategies.snapshot(rt), "limits": _limits(sys, rt.id)}


@router.delete("/{sid}")
async def unsubscribe(sid: str, user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    _owned(sys, sid, user)
    try:
        await sys.strategies.unsubscribe(sid, actor(user))
    except ValueError as e:
        raise bad_request(e)
    return {"ok": True}


@router.get("/{sid}")
async def detail(sid: str, _: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    rt = _rt(sys, sid)
    async with sys.db.session() as s:
        sigs = (await s.execute(select(Signal).where(Signal.strategy_id == sid).order_by(Signal.created_at.desc()).limit(30))).scalars().all()
        orders = (await s.execute(select(Order).where(Order.strategy_id == sid).order_by(Order.created_at.desc()).limit(30))).scalars().all()
        cfgs = (await s.execute(select(StrategyConfig).where(StrategyConfig.strategy_id == sid)
                                .order_by(StrategyConfig.version.desc()))).scalars().all()
    return {**sys.strategies.snapshot(rt), "limits": _limits(sys, sid),
            "signals": [signal_out(x) for x in sigs], "orders": [order_to_dict(o) for o in orders],
            "config_history": [{"version": c.version, "params": c.params, "active": c.is_active,
                                "created_at": c.created_at.isoformat()} for c in cfgs]}


@router.get("/{sid}/chart")
async def chart(sid: str, limit: int = Query(300, le=600), _: User = Depends(current_user),
                sys: TradingSystem = Depends(get_system)):
    rt = _rt(sys, sid)
    sym = rt.symbol
    inst = rt.instance
    # clock/tick strategies do not consume candles: chart the market's own candles for them
    bars = inst.bars[-limit:] if inst.uses_candles else sys.market.candles.candles(sym, rt.timeframe, limit, include_working=False)
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
        "levels": _chart_levels(sys, rt),
    }


def _chart_levels(sys: TradingSystem, rt) -> list[dict[str, Any]]:
    """Horizontal price levels for the strategy chart: the open-breakout triggers, and the open position's entry,
    stop-loss and target (the same levels the platform acts on)."""
    out: list[dict[str, Any]] = []
    if rt.kind == "OPEN_BREAKOUT":
        o = sys.market.session_open(rt.symbol)
        if o:
            b = Decimal(str(rt.params.get("breakout_pct", 1))) / 100
            out += [{"key": "open", "label": "Today's open", "price": o},
                    {"key": "buy_trigger", "label": f"Buy ≥ +{rt.params.get('breakout_pct')}%", "price": (o * (1 + b)).quantize(Decimal("0.01"))},
                    {"key": "sell_trigger", "label": f"Sell ≤ −{rt.params.get('breakout_pct')}%", "price": (o * (1 - b)).quantize(Decimal("0.01"))}]
    pos = sys.positions.get(rt.id, rt.symbol)
    if pos.quantity != 0 and pos.avg_price > 0:
        out.append({"key": "entry", "label": f"Entry {'LONG' if pos.quantity > 0 else 'SHORT'} {abs(pos.quantity):,.0f}", "price": pos.avg_price})
        lv = sys.strategies.exit_levels(rt)
        if lv["stop_loss"] is not None:
            out.append({"key": "stop_loss", "label": f"Stop-loss {rt.params.get('stop_loss_pct')}%", "price": lv["stop_loss"]})
        if lv["take_profit"] is not None:
            out.append({"key": "take_profit", "label": f"Target {rt.params.get('take_profit_pct')}%", "price": lv["take_profit"]})
    return out


@router.post("/{sid}/pause")
async def pause(sid: str, user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    _owned(sys, sid, user)
    await sys.strategies.set_status(sid, "PAUSED", f"paused by {user.email}", actor=actor(user))
    return sys.strategies.snapshot(sys.strategies.runtimes[sid])


@router.post("/{sid}/resume")
async def resume(sid: str, user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    _owned(sys, sid, user)
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
    _owned(sys, sid, user)
    if body.quantity is not None and body.quantity != body.quantity.to_integral_value():
        raise HTTPException(400, "quantity must be a whole number of shares")
    try:
        rt = await sys.strategies.update_config(sid, body.params, body.quantity, body.allocation, user.id, actor(user))
    except ValueError as e:
        raise bad_request(e)
    return sys.strategies.snapshot(rt)
