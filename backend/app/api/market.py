from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import bad_request, get_system
from app.core.security import actor, current_user, require_admin
from app.database.models import Fill, Signal, User
from app.engine.system import TradingSystem
from app.market.providers import ReplayProvider
from app.market.yahoo import MarketDataError

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/status")
async def status(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    return sys.market.info()


@router.get("/symbols")
async def symbols(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    p = sys.market.provider
    return [{"symbol": s, "exchange": "NSE", "ltp": sys.market.ltp(s)} for s in (p.symbols if p else [])]


IST = timezone(timedelta(hours=5, minutes=30))


@router.get("/watchlist")
async def watchlist(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    """Per-symbol day summary from the 1m candles: change vs previous session close (or today's open when the
    previous session is not loaded), day range, volume, an intraday close sparkline and the net position."""
    p = sys.market.provider
    net: dict[str, Decimal] = {}
    for pos in sys.positions.all():
        if pos.quantity:
            net[pos.symbol] = net.get(pos.symbol, Decimal(0)) + pos.quantity
    out = []
    for sym in (p.symbols if p else []):
        bars = sys.market.candles.candles(sym, "1m", 2000)
        ltp = sys.market.ltp(sym)
        row: dict[str, Any] = {"symbol": sym, "ltp": ltp, "position": net.get(sym, Decimal(0)),
                               "fresh": sys.market.freshness(sym)[0]}
        if bars:
            day = bars[-1].ts.astimezone(IST).date()
            today = [b for b in bars if b.ts.astimezone(IST).date() == day]
            before = [b for b in bars if b.ts.astimezone(IST).date() < day]
            ref = before[-1].close if before else today[0].open
            px = ltp if ltp is not None else today[-1].close
            step = max(1, len(today) // 60)
            row.update({
                "open": today[0].open, "high": max(b.high for b in today), "low": min(b.low for b in today),
                "volume": sum((b.volume for b in today), Decimal(0)), "ref_price": ref,
                "ref_basis": "prev_close" if before else "open",
                "change": px - ref, "change_pct": (px - ref) / ref * 100 if ref else None,
                "spark": [float(b.close) for b in today[::step]] + ([float(px)] if today else []),
                "session": day.isoformat(),
            })
        out.append(row)
    return out


@router.get("/candles/{symbol}")
async def candles(symbol: str, timeframe: str = Query("1m", pattern="^(1m|5m)$"), limit: int = Query(300, le=2000),
                  _: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    symbol = symbol.upper()
    p = sys.market.provider
    if p is None or symbol not in p.symbols:
        raise HTTPException(404, f"unknown symbol {symbol}")
    bars = sys.market.candles.candles(symbol, timeframe, limit)
    if not bars:
        return {"symbol": symbol, "timeframe": timeframe, "candles": [], "markers": []}
    since, until = bars[0].ts, bars[-1].ts
    async with sys.db.session() as s:
        # only signals on candles actually displayed (earlier replay passes may hold later timestamps)
        sigs = (await s.execute(select(Signal).where(Signal.symbol == symbol, Signal.candle_time >= since,
                                                     Signal.candle_time <= until)
                                .order_by(Signal.created_at))).scalars().all()
        fills = (await s.execute(select(Fill).where(Fill.symbol == symbol).order_by(Fill.filled_at.desc()).limit(200))).scalars().all()
    return {
        "symbol": symbol, "timeframe": timeframe, "status": sys.market.display_status(),
        "candles": [b.to_dict() for b in bars],
        "signals": [{"time": int(x.candle_time.timestamp()), "strategy_id": x.strategy_id, "signal": x.signal,
                     "action": x.action, "price": x.price, "reason": x.reason} for x in sigs if x.candle_time],
        "fills": [{"strategy_id": f.strategy_id, "side": f.side, "quantity": f.quantity, "price": f.price,
                   "at": f.filled_at.isoformat()} for f in fills],
    }


class ReplayIn(BaseModel):
    speed: float | None = Field(default=None, ge=1, le=600)
    restart: bool = False


@router.post("/replay")
async def replay_control(body: ReplayIn, user: User = Depends(require_admin), sys: TradingSystem = Depends(get_system)):
    if not isinstance(sys.market.provider, ReplayProvider) and not body.restart:
        raise HTTPException(400, "market data is not in replay mode")
    if body.restart:
        await sys.market.restart_replay(from_start=True)
        await sys.audit.record("REPLAY_RESTARTED", "replay restarted from the beginning of the dataset", source="MarketDataProvider",
                               actor=actor(user))
    if body.speed:
        sys.market.set_speed(body.speed)
        await sys.audit.record("REPLAY_SPEED", f"replay speed set to {body.speed}x", source="MarketDataProvider", actor=actor(user))
    return sys.market.info()


@router.post("/refresh-replay-data")
async def refresh(user: User = Depends(require_admin), sys: TradingSystem = Depends(get_system)):
    p = sys.market.provider
    if not isinstance(p, ReplayProvider):
        raise HTTPException(400, "market data is not in replay mode")
    try:
        out = await p.refresh_from_api()
    except MarketDataError as e:
        raise bad_request(e)
    await sys.audit.record("REPLAY_DATA_REFRESHED", "replay cache refreshed from market-data API", source="MarketDataProvider",
                           actor=actor(user), payload=out)
    return out
