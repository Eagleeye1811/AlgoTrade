"""BrokerAdapter interface. The trading engine depends only on this interface.

    BrokerAdapter
    ├── PaperBrokerSimulator   (used: no verified 021 API documentation/credentials are available)
    └── (021Adapter)           intentionally NOT implemented - no documented API/credentials were provided.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable


class BrokerError(Exception):
    pass


class BrokerUnavailable(BrokerError):
    """Broker could not be reached - outcome of the call is unknown only for state-changing calls."""


class BrokerTimeout(BrokerError):
    """No response in time. For place_order the order MAY exist at the broker."""


class BrokerRejected(BrokerError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class PlaceOrderRequest:
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    order_type: str = "MARKET"
    limit_price: Decimal | None = None
    tag: str | None = None


@dataclass
class BrokerOrder:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    filled_quantity: Decimal
    avg_price: Decimal | None
    limit_price: Decimal | None
    status: str  # OPEN | PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED
    tag: str | None = None
    reject_reason: str | None = None
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v.isoformat() if isinstance(v, datetime) else v)
                for k, v in self.__dict__.items()}


@dataclass
class ExecutionReport:
    fill_id: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    reference_price: Decimal
    latency_ms: int
    ts: datetime
    cumulative_filled: Decimal
    order_quantity: Decimal


@dataclass
class OrderUpdate:
    broker_order_id: str
    client_order_id: str
    status: str
    reason: str | None = None


@dataclass
class BrokerPosition:
    symbol: str
    quantity: Decimal
    avg_price: Decimal


@dataclass
class BrokerAccount:
    account_id: str
    cash: Decimal
    starting_cash: Decimal
    margin_used: Decimal
    extra: dict[str, Any] = field(default_factory=dict)


class BrokerAdapter(ABC):
    name: str = ""
    is_paper: bool = True

    def __init__(self) -> None:
        self.on_execution: Callable[[ExecutionReport], Awaitable[None]] | None = None
        self.on_order_update: Callable[[OrderUpdate], Awaitable[None]] | None = None

    @abstractmethod
    async def connect(self, account_id: str) -> None: ...

    @abstractmethod
    async def ping(self) -> float: ...

    @abstractmethod
    async def get_account(self) -> BrokerAccount: ...

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    async def get_orders(self) -> list[BrokerOrder]: ...

    @abstractmethod
    async def get_order(self, broker_order_id: str) -> BrokerOrder | None: ...

    @abstractmethod
    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    @abstractmethod
    async def get_fills(self, broker_order_id: str | None = None) -> list[ExecutionReport]: ...

    @abstractmethod
    async def place_order(self, req: PlaceOrderRequest) -> BrokerOrder: ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> BrokerOrder: ...
