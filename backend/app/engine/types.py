from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.core.events import new_id

TERMINAL = ("FILLED", "REJECTED", "CANCELLED", "TIMEOUT")
OPEN_STATUSES = ("CREATED", "RISK_CHECK", "APPROVED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED")


@dataclass
class OrderIntent:
    strategy_id: str
    symbol: str
    side: str
    quantity: Decimal
    order_type: str = "MARKET"
    limit_price: Decimal | None = None
    origin: str = "STRATEGY"  # STRATEGY | RUNAWAY | FAULT_LAB | DEMO | KILL_SWITCH | MANUAL
    reduce_only: bool = False
    correlation_id: str = field(default_factory=new_id)
    signal_id: Any = None
    idempotency_key: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        q = Decimal(self.quantity)
        if q != q.to_integral_value():
            raise ValueError("quantity must be a whole number of shares")
        self.quantity = Decimal(int(q))  # normalise (e.g. 600.0000 -> 600)


@dataclass
class RiskCheck:
    rule: str
    label: str
    result: str  # PASS | FAIL | WARN | SKIP
    actual: str = ""
    allowed: str = ""
    message: str = ""

    @property
    def failed(self) -> bool:
        return self.result == "FAIL"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class RiskDecision:
    approved: bool
    checks: list[RiskCheck]
    order_type: str
    limit_price: Decimal | None = None
    reference_price: Decimal | None = None

    @property
    def primary_failure(self) -> RiskCheck | None:
        return next((c for c in self.checks if c.failed), None)

    def to_dict(self) -> dict[str, Any]:
        f = self.primary_failure
        return {"approved": self.approved, "checks": [c.to_dict() for c in self.checks], "order_type": self.order_type,
                "limit_price": str(self.limit_price) if self.limit_price is not None else None,
                "primary_failure": f.to_dict() if f else None}


@dataclass
class OpenOrderInfo:
    order_id: str
    client_order_id: str
    strategy_id: str
    symbol: str
    side: str
    quantity: Decimal
    filled: Decimal
    order_type: str
    reduce_only: bool
    reference_price: Decimal | None
    broker_order_id: str | None
    status: str
    created_monotonic: float
    origin: str = "STRATEGY"

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled
