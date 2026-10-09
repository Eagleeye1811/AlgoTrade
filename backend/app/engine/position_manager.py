"""Per-strategy positions derived ONLY from actual fills. Updated atomically with fill persistence."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Fill, Position, PositionEvent
from app.engine.position_math import ZERO, apply_fill_math

if TYPE_CHECKING:
    from app.engine.system import TradingSystem


@dataclass
class PositionState:
    strategy_id: str
    symbol: str
    quantity: Decimal = ZERO
    avg_price: Decimal = ZERO
    realized_pnl: Decimal = ZERO

    @property
    def side(self) -> str:
        return "LONG" if self.quantity > 0 else "SHORT" if self.quantity < 0 else "FLAT"


class PositionManager:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self.cache: dict[tuple[str, str], PositionState] = {}

    async def load(self) -> None:
        async with self.sys.db.session() as s:
            rows = (await s.execute(select(Position))).scalars().all()
        self.cache = {(r.strategy_id, r.symbol): PositionState(r.strategy_id, r.symbol, r.quantity, r.avg_price,
                                                               r.realized_pnl) for r in rows}

    def get(self, strategy_id: str, symbol: str) -> PositionState:
        return self.cache.get((strategy_id, symbol)) or PositionState(strategy_id, symbol)

    def all(self) -> list[PositionState]:
        return list(self.cache.values())

    def net_by_symbol(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for p in self.cache.values():
            out[p.symbol] = out.get(p.symbol, ZERO) + p.quantity
        return out

    async def _lock_row(self, s: AsyncSession, strategy_id: str, symbol: str) -> Position:
        await s.execute(pg_insert(Position).values(strategy_id=strategy_id, symbol=symbol, quantity=0, avg_price=0,
                                                   realized_pnl=0).on_conflict_do_nothing())
        return (await s.execute(select(Position).where(Position.strategy_id == strategy_id, Position.symbol == symbol)
                                .with_for_update())).scalar_one()

    async def apply_fill(self, s: AsyncSession, fill: Fill, correlation_id: str | None) -> PositionState:
        """Must be called inside the same transaction that inserts the fill."""
        p = await self._lock_row(s, fill.strategy_id, fill.symbol)
        delta = fill.quantity if fill.side == "BUY" else -fill.quantity
        before_q, before_a = p.quantity, p.avg_price
        p.quantity, p.avg_price, realized = apply_fill_math(p.quantity, p.avg_price, delta, fill.price)
        p.realized_pnl = p.realized_pnl + realized
        s.add(PositionEvent(strategy_id=fill.strategy_id, symbol=fill.symbol, event_type="FILL", fill_id=fill.id,
                            qty_before=before_q, qty_after=p.quantity, avg_before=before_a, avg_after=p.avg_price,
                            realized_delta=realized, correlation_id=correlation_id,
                            data={"fill_qty": str(fill.quantity), "fill_price": str(fill.price), "side": fill.side}))
        return PositionState(p.strategy_id, p.symbol, p.quantity, p.avg_price, p.realized_pnl)

    def commit_cache(self, st: PositionState) -> None:
        self.cache[(st.strategy_id, st.symbol)] = st

    async def rebuild_from_ledger(self, s: AsyncSession, strategy_id: str, symbol: str, reason: str,
                                  correlation_id: str | None = None) -> tuple[PositionState, PositionState]:
        """Recompute a position from the fills ledger (authoritative executions). Returns (before, after)."""
        p = await self._lock_row(s, strategy_id, symbol)
        before = PositionState(strategy_id, symbol, p.quantity, p.avg_price, p.realized_pnl)
        fills = (await s.execute(select(Fill).where(Fill.strategy_id == strategy_id, Fill.symbol == symbol)
                                 .order_by(Fill.filled_at, Fill.created_at))).scalars().all()
        q, a, r = ZERO, ZERO, ZERO
        for f in fills:
            q, a, d = apply_fill_math(q, a, f.quantity if f.side == "BUY" else -f.quantity, f.price)
            r += d
        p.quantity, p.avg_price, p.realized_pnl = q, a, r
        after = PositionState(strategy_id, symbol, q, a, r)
        if (before.quantity, before.avg_price, before.realized_pnl) != (q, a, r):
            s.add(PositionEvent(strategy_id=strategy_id, symbol=symbol, event_type="RECON_REBUILD", qty_before=before.quantity,
                                qty_after=q, avg_before=before.avg_price, avg_after=a,
                                realized_delta=r - before.realized_pnl, correlation_id=correlation_id,
                                data={"reason": reason, "fills": len(fills)}))
        return before, after
