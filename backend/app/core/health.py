"""Component health registry: status, latency, heartbeat, last success, recent errors."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.events import utcnow

HEALTHY, WARNING, FAILED = "HEALTHY", "WARNING", "FAILED"

COMPONENTS = [
    ("market_data", "Market Data"),
    ("broker", "Broker Adapter"),
    ("strategy_engine", "Strategy Engine"),
    ("risk_firewall", "Risk Firewall"),
    ("order_engine", "Order Engine"),
    ("postgres", "PostgreSQL"),
    ("redis", "Redis"),
    ("websocket", "WebSocket"),
    ("reconciliation", "Reconciliation"),
    ("workers", "Background Workers"),
    ("telegram", "Telegram Bot"),
]


@dataclass
class ComponentHealth:
    key: str
    name: str
    status: str = WARNING
    detail: str = "not started"
    latency_ms: float | None = None
    last_heartbeat: datetime | None = None
    last_success: datetime | None = None
    errors: deque = field(default_factory=lambda: deque(maxlen=10))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 1) if self.latency_ms is not None else None,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "recent_errors": list(self.errors),
        }


class HealthRegistry:
    def __init__(self) -> None:
        self.c: dict[str, ComponentHealth] = {k: ComponentHealth(k, n) for k, n in COMPONENTS}

    def beat(self, key: str, status: str = HEALTHY, detail: str | None = None, latency_ms: float | None = None,
             success: bool = True) -> None:
        h = self.c[key]
        h.status = status
        if detail is not None:
            h.detail = detail
        if latency_ms is not None:
            h.latency_ms = latency_ms
        h.last_heartbeat = utcnow()
        if success and status != FAILED:
            h.last_success = h.last_heartbeat

    def error(self, key: str, message: str, status: str = FAILED) -> None:
        h = self.c[key]
        h.status = status
        h.detail = message
        h.last_heartbeat = utcnow()
        h.errors.appendleft({"at": utcnow().isoformat(), "message": message})

    def snapshot(self) -> list[dict[str, Any]]:
        return [h.to_dict() for h in self.c.values()]

    def overall(self) -> str:
        sts = [h.status for h in self.c.values()]
        if FAILED in sts:
            return FAILED
        if WARNING in sts:
            return WARNING
        return HEALTHY
