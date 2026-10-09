"""Portfolio / strategy P&L, exposure and virtual margin - computed from fills-derived positions + market LTP."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.core.events import Event
from app.database.models import Order, PnlSnapshot, PositionEvent
from app.engine.position_math import ZERO, unrealized

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

log = logging.getLogger("algotrade.pnl")
IST = ZoneInfo("Asia/Kolkata")
Q2 = Decimal("0.01")


def ist_day_start() -> datetime:
    return datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)


def q(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Q2)


class PnLEngine:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self.realized_today = ZERO
        self.realized_today_by: dict[str, Decimal] = {}
        self.orders_today = 0
        self._task: asyncio.Task | None = None
        self.last: dict[str, Any] | None = None

    async def refresh_daily(self) -> None:
        start = ist_day_start()
        async with self.sys.db.session() as s:
            rows = (await s.execute(select(PositionEvent.strategy_id, func.coalesce(func.sum(PositionEvent.realized_delta), 0))
                                    .where(PositionEvent.created_at >= start).group_by(PositionEvent.strategy_id))).all()
            self.realized_today_by = {sid: Decimal(v) for sid, v in rows}
            self.realized_today = sum(self.realized_today_by.values(), ZERO)
            self.orders_today = (await s.execute(select(func.count()).select_from(Order)
                                                 .where(Order.created_at >= start))).scalar_one()

    def compute(self) -> dict[str, Any]:
        sys = self.sys
        rate = sys.settings.margin_rate
        capital = sys.account.starting_capital if sys.account else ZERO
        strategies: dict[str, dict[str, Any]] = {}
        for rt in sys.strategies.runtimes.values():
            strategies[rt.id] = {"strategy_id": rt.id, "name": rt.name, "allocation": rt.allocation, "realized": ZERO,
                                 "unrealized": ZERO, "exposure": ZERO, "margin_used": ZERO, "reserved_margin": ZERO,
                                 "positions": 0}
        positions = []
        for p in sys.positions.all():
            ltp = sys.market.ltp(p.symbol)
            u = unrealized(p.quantity, p.avg_price, ltp)
            exp = abs(p.quantity) * (ltp if ltp is not None else p.avg_price)
            margin = exp * rate
            st = strategies.setdefault(p.strategy_id, {"strategy_id": p.strategy_id, "name": p.strategy_id,
                                                        "allocation": ZERO, "realized": ZERO, "unrealized": ZERO,
                                                        "exposure": ZERO, "margin_used": ZERO, "reserved_margin": ZERO,
                                                        "positions": 0})
            st["realized"] += p.realized_pnl
            st["unrealized"] += u
            st["exposure"] += exp
            st["margin_used"] += margin
            if p.quantity != 0:
                st["positions"] += 1
                positions.append({"strategy_id": p.strategy_id, "symbol": p.symbol, "side": p.side,
                                  "quantity": p.quantity, "avg_price": p.avg_price, "ltp": ltp, "unrealized": q(u),
                                  "realized": q(p.realized_pnl), "exposure": q(exp), "margin": q(margin)})
            elif p.realized_pnl != 0:
                positions.append({"strategy_id": p.strategy_id, "symbol": p.symbol, "side": "FLAT", "quantity": ZERO,
                                  "avg_price": ZERO, "ltp": ltp, "unrealized": ZERO, "realized": q(p.realized_pnl),
                                  "exposure": ZERO, "margin": ZERO})
        for o in sys.orders.open_orders.values():
            if o.reduce_only or o.strategy_id not in strategies:
                continue
            ref = sys.market.ltp(o.symbol) or o.reference_price or ZERO
            strategies[o.strategy_id]["reserved_margin"] += o.remaining * ref * rate
        tot = {k: sum((s[k] for s in strategies.values()), ZERO)
               for k in ("realized", "unrealized", "exposure", "margin_used", "reserved_margin")}
        for sid, s in strategies.items():
            s["total_pnl"] = s["realized"] + s["unrealized"]
            s["today_pnl"] = q(self.realized_today_by.get(sid, ZERO) + s["unrealized"])
            s["available_margin"] = s["allocation"] - s["margin_used"] - s["reserved_margin"]
            s["utilization_pct"] = float((s["margin_used"] + s["reserved_margin"]) / s["allocation"] * 100) if s["allocation"] else 0.0
            for k in ("allocation", "realized", "unrealized", "exposure", "margin_used", "reserved_margin", "total_pnl", "available_margin"):
                s[k] = q(s[k])
        equity = capital + tot["realized"] + tot["unrealized"]
        portfolio = {
            "starting_capital": q(capital),
            "equity": q(equity),
            "realized": q(tot["realized"]),
            "unrealized": q(tot["unrealized"]),
            "total_pnl": q(tot["realized"] + tot["unrealized"]),
            "realized_today": q(self.realized_today),
            "today_pnl": q(self.realized_today + tot["unrealized"]),
            "margin_used": q(tot["margin_used"]),
            "reserved_margin": q(tot["reserved_margin"]),
            "available_capital": q(equity - tot["margin_used"] - tot["reserved_margin"]),
            "exposure": q(tot["exposure"]),
            "active_positions": sum(1 for p in positions if p["quantity"] != 0),
            "orders_today": self.orders_today,
            "open_orders": len(sys.orders.open_orders),
        }
        self.last = {"portfolio": portfolio, "strategies": strategies, "positions": positions}
        return self.last

    async def publish(self) -> dict[str, Any]:
        snap = self.compute()
        await self.sys.bus.publish(Event("pnl.update", "PnLEngine", payload=snap))
        await self.sys.redis.set_json("risk:portfolio", snap["portfolio"], ttl_s=60)
        return snap

    async def snapshot_to_db(self) -> None:
        snap = self.last or self.compute()
        p = snap["portfolio"]
        async with self.sys.db.transaction() as s:
            s.add(PnlSnapshot(strategy_id=None, realized=p["realized"], unrealized=p["unrealized"],
                              total=p["total_pnl"], exposure=p["exposure"], margin_used=p["margin_used"]))
            for sid, st in snap["strategies"].items():
                s.add(PnlSnapshot(strategy_id=sid, realized=st["realized"], unrealized=st["unrealized"],
                                  total=st["total_pnl"], exposure=st["exposure"], margin_used=st["margin_used"]))

    async def run(self) -> None:
        n = 0
        while True:
            try:
                if self.sys.account is not None:
                    if n % 5 == 0:
                        await self.refresh_daily()
                    await self.publish()
                    if n % int(max(1, self.sys.settings.pnl_snapshot_interval_s)) == 0:
                        await self.snapshot_to_db()
            except Exception:
                log.exception("pnl loop failed")
            n += 1
            await asyncio.sleep(1.0)
