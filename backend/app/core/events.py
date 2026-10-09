"""Consistent event schema + in-process event bus with Redis pub/sub fan-out.

Producers publish typed events; consumers (WebSocket manager, internal subscribers)
receive them. Every event carries event_id (dedupe), correlation_id (tracing
Signal -> Risk -> Order -> Fill -> Position -> P&L) and its producing source.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

log = logging.getLogger("algotrade.events")

CHANNEL = "algotrade:events"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)  # transport/display only; persisted money stays NUMERIC
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    if hasattr(o, "to_dict"):
        return o.to_dict()
    raise TypeError(f"not serializable: {type(o)}")


@dataclass
class Event:
    event_type: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    strategy_id: str | None = None
    order_id: str | None = None
    symbol: str | None = None
    event_id: str = field(default_factory=new_id)
    timestamp: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "correlation_id": self.correlation_id,
            "source": self.source,
            "strategy_id": self.strategy_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=json_default)


Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    """In-process async pub/sub. External fan-out (WebSocket) goes through Redis when available."""

    def __init__(self) -> None:
        self._subs: list[tuple[str, Subscriber]] = []
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._fanout: Callable[[Event], Awaitable[None]] | None = None

    def subscribe(self, prefix: str, fn: Subscriber) -> None:
        self._subs.append((prefix, fn))

    def set_fanout(self, fn: Callable[[Event], Awaitable[None]] | None) -> None:
        self._fanout = fn

    def _first_time(self, event_id: str) -> bool:
        if event_id in self._seen:
            return False
        self._seen[event_id] = None
        if len(self._seen) > 20000:
            self._seen.popitem(last=False)
        return True

    async def publish(self, event: Event) -> None:
        if not self._first_time(event.event_id):
            return  # duplicate event processing prevention
        for prefix, fn in list(self._subs):
            if event.event_type.startswith(prefix):
                try:
                    await fn(event)
                except Exception:  # a broken consumer must never break the producer
                    log.exception("event subscriber failed for %s", event.event_type)
        if self._fanout is not None:
            try:
                await self._fanout(event)
            except Exception:
                log.exception("event fanout failed")

    def publish_nowait(self, event: Event) -> None:
        asyncio.get_running_loop().create_task(self.publish(event))
