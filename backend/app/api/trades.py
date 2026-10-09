"""L2 output: the exact list of trades (fills) with net P&L after charges from the published charges table."""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app.api.deps import bad_request, get_system
from app.core.security import current_user
from app.database.models import Fill, PositionEvent, Strategy, User
from app.engine.charges import COMPONENTS
from app.engine.system import TradingSystem

router = APIRouter(prefix="/api", tags=["trades"])
IST = timezone(timedelta(hours=5, minutes=30))
ZERO = Decimal(0)


async def build_trades(sys: TradingSystem, day: date | None, strategy_id: str | None, user: User | None) -> dict[str, Any]:
    """Fills of one IST day (None = all days), oldest first, each with its gross realized P&L, its share of the
    order's charges and the net result; plus per-strategy and account totals."""
    table = sys.charges
    async with sys.db.session() as s:
        q = select(Fill, Strategy.name, Strategy.user_id).join(Strategy, Strategy.id == Fill.strategy_id)
        if day is not None:
            start = datetime(day.year, day.month, day.day, tzinfo=IST)
            q = q.where(Fill.filled_at >= start, Fill.filled_at < start + timedelta(days=1))
        if strategy_id:
            q = q.where(Fill.strategy_id == strategy_id)
        if user is not None:
            q = q.where(Strategy.user_id == user.id)
        rows = (await s.execute(q.order_by(Fill.filled_at, Fill.created_at))).all()
        ids = [f.id for f, _, _ in rows]
        realized = {}
        if ids:
            realized = {fid: d for fid, d in (await s.execute(
                select(PositionEvent.fill_id, PositionEvent.realized_delta).where(PositionEvent.fill_id.in_(ids)))).all()}
    by_order: dict[Any, list] = defaultdict(list)
    for f, _, _ in rows:
        by_order[f.order_id].append(f)
    charges: dict[Any, dict[str, Decimal]] = {}
    for oid, fs in by_order.items():
        charges.update(table.allocate(fs[0].side, [(f.id, f.quantity * f.price) for f in fs]))
    trades, per = [], {}
    for f, name, _ in rows:
        c = charges[f.id]
        gross = Decimal(realized.get(f.id, ZERO))
        trades.append({
            "fill_id": str(f.id), "time": f.filled_at.astimezone(IST).isoformat(), "strategy_id": f.strategy_id,
            "strategy": name, "symbol": f.symbol, "side": f.side, "quantity": f.quantity, "price": f.price,
            "turnover": (f.quantity * f.price).quantize(Decimal("0.01")), "order_id": str(f.order_id),
            "broker_fill_id": f.broker_fill_id, "realized_gross": gross, "charges": c, "net": gross - c["total"],
            # execution quality (formerly the separate Fills view)
            "reference_price": f.reference_price, "slippage_bps": f.slippage_bps, "latency_ms": f.latency_ms,
            "source": f.source,
        })
        st = per.setdefault(f.strategy_id, {"strategy_id": f.strategy_id, "strategy": name, "trades": 0, "buy_qty": ZERO,
                                            "sell_qty": ZERO, "turnover": ZERO, "realized_gross": ZERO, "charges": ZERO,
                                            **{f"charges_{k}": ZERO for k in COMPONENTS}})
        st["trades"] += 1
        st["buy_qty" if f.side == "BUY" else "sell_qty"] += f.quantity
        st["turnover"] += f.quantity * f.price
        st["realized_gross"] += gross
        st["charges"] += c["total"]
        for k in COMPONENTS:
            st[f"charges_{k}"] += c[k]
    for st in per.values():
        pos = sys.positions.get(st["strategy_id"], next((t["symbol"] for t in trades if t["strategy_id"] == st["strategy_id"]), ""))
        st["net_realized"] = st["realized_gross"] - st["charges"]
        st["open_quantity"] = st["buy_qty"] - st["sell_qty"] if day is not None else pos.quantity
        st["turnover"] = st["turnover"].quantize(Decimal("0.01"))
    total = {k: sum((st[k] for st in per.values()), ZERO) for k in ("realized_gross", "charges", "net_realized", "turnover")}
    total["trades"] = len(trades)
    return {"date": day.isoformat() if day else None, "charges_table": table.name, "charges_source": table.table.get("source"),
            "trades": trades, "strategies": list(per.values()), "total": total}


def _parse_day(day: str | None, all_days: bool) -> date | None:
    if all_days:
        return None
    try:
        return date.fromisoformat(day) if day else datetime.now(IST).date()
    except ValueError as e:
        raise bad_request(ValueError("date must be YYYY-MM-DD")) from e


@router.get("/trades")
async def trades(day: str | None = Query(None, alias="date"), all_days: bool = False, strategy_id: str | None = None,
                 mine: bool = False, user: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    return await build_trades(sys, _parse_day(day, all_days), strategy_id, user if mine else None)


@router.get("/trades.csv", response_class=PlainTextResponse)
async def trades_csv(day: str | None = Query(None, alias="date"), all_days: bool = False, strategy_id: str | None = None,
                     mine: bool = False, user: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    d = await build_trades(sys, _parse_day(day, all_days), strategy_id, user if mine else None)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_ist", "strategy_id", "symbol", "side", "quantity", "price", "turnover", "realized_gross",
                *COMPONENTS, "charges_total", "net", "order_id", "broker_fill_id", "reference_price", "slippage_bps",
                "latency_ms", "source"])
    for t in d["trades"]:
        c = t["charges"]
        w.writerow([t["time"], t["strategy_id"], t["symbol"], t["side"], t["quantity"], t["price"], t["turnover"],
                    t["realized_gross"], *(c[k] for k in COMPONENTS), c["total"], t["net"], t["order_id"], t["broker_fill_id"],
                    t["reference_price"], t["slippage_bps"], t["latency_ms"], t["source"]])
    w.writerow([])
    w.writerow(["strategy_id", "trades", "turnover", "realized_gross", "charges", "net_realized"])
    for st in d["strategies"]:
        w.writerow([st["strategy_id"], st["trades"], st["turnover"], st["realized_gross"], st["charges"], st["net_realized"]])
    tot = d["total"]
    w.writerow(["TOTAL", tot["trades"], tot["turnover"], tot["realized_gross"], tot["charges"], tot["net_realized"]])
    w.writerow([f"charges table: {d['charges_table']}"])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=trades-{d['date'] or 'all'}.csv"})
