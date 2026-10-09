"""Audit trail (system_events) + persistent system state key/value store."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, EventBus, json_default
from app.database.models import SystemEvent, SystemState
from app.database.session import Database

log = logging.getLogger("algotrade.audit")


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    import json

    return json.loads(json.dumps(d, default=json_default))


class Audit:
    def __init__(self, db: Database, bus: EventBus):
        self.db = db
        self.bus = bus

    async def record(
        self,
        event_type: str,
        message: str,
        *,
        source: str,
        severity: str = "INFO",
        correlation_id: str | None = None,
        strategy_id: str | None = None,
        order_id: str | None = None,
        symbol: str | None = None,
        actor: str | None = None,
        payload: dict[str, Any] | None = None,
        session: AsyncSession | None = None,
        broadcast: bool = True,
    ) -> None:
        payload = _jsonable(payload or {})
        row = SystemEvent(
            event_type=event_type,
            severity=severity,
            source=source,
            message=message,
            correlation_id=correlation_id,
            strategy_id=strategy_id,
            order_id=str(order_id) if order_id else None,
            symbol=symbol,
            actor=actor,
            payload=payload,
        )
        if session is not None:
            session.add(row)
        else:
            try:
                async with self.db.transaction() as s:
                    s.add(row)
            except Exception:
                log.exception("failed to persist audit event %s", event_type)
        if broadcast:
            await self.bus.publish(
                Event(
                    event_type="audit.event",
                    source=source,
                    correlation_id=correlation_id,
                    strategy_id=strategy_id,
                    order_id=str(order_id) if order_id else None,
                    symbol=symbol,
                    payload={"type": event_type, "severity": severity, "message": message, "actor": actor, **payload},
                )
            )


class StateStore:
    """Small persistent key/value table for system-wide operating state."""

    def __init__(self, db: Database):
        self.db = db

    async def get(self, key: str, default: Any = None) -> Any:
        async with self.db.session() as s:
            row = (await s.execute(select(SystemState).where(SystemState.key == key))).scalar_one_or_none()
            return row.value.get("v") if row else default

    async def set(self, key: str, value: Any, session: AsyncSession | None = None) -> None:
        stmt = pg_insert(SystemState).values(key=key, value={"v": _jsonable({"x": value})["x"]})
        stmt = stmt.on_conflict_do_update(index_elements=[SystemState.key], set_={"value": stmt.excluded.value})
        if session is not None:
            await session.execute(stmt)
        else:
            async with self.db.transaction() as s:
                await s.execute(stmt)
