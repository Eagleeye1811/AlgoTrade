"""Risk limits persisted in PostgreSQL, cached in memory/Redis."""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database.models import RiskLimit

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

GLOBAL_DEFAULTS: dict[str, tuple[Decimal, str]] = {
    "max_daily_loss": (Decimal(50000), "Maximum daily loss (INR)"),
    "max_gross_exposure": (Decimal(750000), "Maximum overall gross exposure (INR)"),
    "max_orders_per_min": (Decimal(40), "Global order rate (orders / 60s)"),
    "max_rejections_per_min": (Decimal(5), "Strategy circuit breaker: rejections / 60s before auto-halt"),
    "latency_block_ms": (Decimal(2000), "Broker latency that blocks new orders (ms)"),
    "latency_warn_ms": (Decimal(800), "Broker latency that forces protective LIMIT orders (ms)"),
    "max_volatility_bps": (Decimal(40), "1m volatility that forces protective LIMIT orders (bps)"),
    "max_participation_pct": (Decimal(20), "Max order size vs last 1m volume (%)"),
    "slippage_warn_bps": (Decimal(25), "Recent avg slippage that forces protective LIMIT orders (bps)"),
    "duplicate_window_s": (Decimal(5), "Identical-order suppression window (s)"),
}
STRATEGY_DEFAULTS: dict[str, tuple[Decimal | None, str]] = {
    "max_position": (Decimal(500), "Maximum strategy position (shares)"),
    "max_orders_per_min": (Decimal(15), "Strategy order rate (orders / 60s)"),
    "max_exposure": (None, "Maximum strategy exposure (INR, defaults to allocation)"),
}


class RiskLimitStore:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self.values: dict[tuple[str, str | None, str], Decimal] = {}

    async def ensure_defaults(self, allocations: dict[str, Decimal]) -> None:
        async with self.sys.db.transaction() as s:
            existing = set((await s.execute(select(RiskLimit.name).where(RiskLimit.scope == "GLOBAL"))).scalars())
            for name, (v, _) in GLOBAL_DEFAULTS.items():
                if name not in existing:  # NULL strategy_id: check explicitly (partial unique index backs this up)
                    s.add(RiskLimit(scope="GLOBAL", strategy_id=None, name=name, value=v))
            for sid, alloc in allocations.items():
                for name, (v, _) in STRATEGY_DEFAULTS.items():
                    await s.execute(pg_insert(RiskLimit).values(scope="STRATEGY", strategy_id=sid, name=name,
                                                                value=v if v is not None else alloc)
                                    .on_conflict_do_nothing())
        await self.load()

    async def load(self) -> None:
        async with self.sys.db.session() as s:
            rows = (await s.execute(select(RiskLimit))).scalars().all()
        self.values = {(r.scope, r.strategy_id, r.name): r.value for r in rows}

    def g(self, name: str) -> Decimal:
        return self.values.get(("GLOBAL", None, name), GLOBAL_DEFAULTS[name][0])

    def s(self, strategy_id: str, name: str) -> Decimal:
        v = self.values.get(("STRATEGY", strategy_id, name))
        if v is None:
            d = STRATEGY_DEFAULTS[name][0]
            return d if d is not None else Decimal(0)
        return v

    async def set(self, scope: str, strategy_id: str | None, name: str, value: Decimal, user_id: int | None) -> None:
        if scope == "GLOBAL" and name not in GLOBAL_DEFAULTS:
            raise ValueError(f"unknown global limit {name}")
        if scope == "STRATEGY" and name not in STRATEGY_DEFAULTS:
            raise ValueError(f"unknown strategy limit {name}")
        if value < 0:
            raise ValueError("limit must be >= 0")
        async with self.sys.db.transaction() as s:
            stmt = pg_insert(RiskLimit).values(scope=scope, strategy_id=strategy_id, name=name, value=value,
                                               updated_by=user_id)
            # NULL strategy_id never conflicts on a unique index, so update GLOBAL rows explicitly
            row = (await s.execute(select(RiskLimit).where(RiskLimit.scope == scope, RiskLimit.name == name,
                                                           RiskLimit.strategy_id.is_(None) if strategy_id is None
                                                           else RiskLimit.strategy_id == strategy_id))).scalar_one_or_none()
            if row:
                row.value, row.updated_by = value, user_id
            else:
                await s.execute(stmt)
        self.values[(scope, strategy_id, name)] = value

    def describe(self) -> dict:
        glob = [{"name": n, "label": lbl, "value": self.g(n)} for n, (_, lbl) in GLOBAL_DEFAULTS.items()]
        strat = {}
        for sid in self.sys.strategies.runtimes:
            strat[sid] = [{"name": n, "label": lbl, "value": self.s(sid, n)} for n, (_, lbl) in STRATEGY_DEFAULTS.items()]
        return {"global": glob, "strategy": strat}
