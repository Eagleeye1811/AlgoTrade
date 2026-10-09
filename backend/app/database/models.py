"""PostgreSQL schema. PostgreSQL is the permanent financial source of truth.

Monetary/price/quantity values use NUMERIC and are handled as Decimal in Python.
The paper broker keeps its own state in a separate `paper_broker` schema so that it
behaves like an independent execution venue (reconciliation compares the two).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MONEY = Numeric(20, 4)
PRICE = Numeric(16, 4)
QTY = Numeric(16, 0)


class Base(DeclarativeBase):
    # fetch server-generated values (created_at / updated_at) via RETURNING so objects stay usable after commit
    __mapper_args__ = {"eager_defaults": True}


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- auth
class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="TRADER")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (CheckConstraint("role in ('ADMIN','TRADER')", name="ck_users_role"),)


class UserSession(TimestampMixin, Base):
    __tablename__ = "user_sessions"
    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- account / system state
class TradingAccount(TimestampMixin, Base):
    __tablename__ = "trading_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    account_type: Mapped[str] = mapped_column(String(16), nullable=False, default="PAPER")
    broker: Mapped[str] = mapped_column(String(32), nullable=False, default="PAPER_BROKER_SIMULATOR")
    broker_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    starting_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (CheckConstraint("starting_capital > 0", name="ck_account_capital_pos"),)


class SystemState(Base):
    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# --------------------------------------------------------------------------- strategies
class Strategy(TimestampMixin, Base):
    __tablename__ = "strategies"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="1m")
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PAUSED")
    status_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        CheckConstraint("status in ('RUNNING','PAUSED','HALTED')", name="ck_strategy_status"),
        CheckConstraint("quantity > 0", name="ck_strategy_qty_pos"),
    )


class StrategyConfig(TimestampMixin, Base):
    __tablename__ = "strategy_configs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (UniqueConstraint("strategy_id", "version", name="uq_strategy_config_version"),)


class StrategyAllocation(Base):
    __tablename__ = "strategy_allocations"
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), primary_key=True)
    allocated_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (CheckConstraint("allocated_capital >= 0", name="ck_alloc_nonneg"),)


class Signal(TimestampMixin, Base):
    __tablename__ = "signals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    signal: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    candle_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indicators: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="STRATEGY")
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    __table_args__ = (Index("ix_signals_strategy_created", "strategy_id", "created_at"),)


# --------------------------------------------------------------------------- orders / fills / positions
class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # idempotency key
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    order_type: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False, default=0)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(PRICE)
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    reference_price: Mapped[Decimal | None] = mapped_column(PRICE)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reject_rule: Mapped[str | None] = mapped_column(String(64))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("signals.id"))
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="STRATEGY")
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    submit_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        CheckConstraint("side in ('BUY','SELL')", name="ck_order_side"),
        CheckConstraint("order_type in ('MARKET','LIMIT')", name="ck_order_type"),
        CheckConstraint("quantity > 0", name="ck_order_qty_pos"),
        CheckConstraint("filled_quantity >= 0 and filled_quantity <= quantity", name="ck_order_filled_range"),
        CheckConstraint(
            "status in ('CREATED','RISK_CHECK','APPROVED','SUBMITTED','ACKNOWLEDGED','PARTIALLY_FILLED',"
            "'FILLED','REJECTED','CANCELLED','TIMEOUT')",
            name="ck_order_status",
        ),
        Index("ix_orders_strategy_created", "strategy_id", "created_at"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_created", "created_at"),
    )


class OrderEvent(TimestampMixin, Base):
    __tablename__ = "order_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class Fill(TimestampMixin, Base):
    __tablename__ = "fills"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    broker_fill_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # dedupe execution reports
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    reference_price: Mapped[Decimal | None] = mapped_column(PRICE)
    slippage_bps: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="EXECUTION_REPORT")
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_fill_qty_pos"),
        Index("ix_fills_strategy_symbol", "strategy_id", "symbol"),
    )


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False, default=0)  # signed: + long / - short
    avg_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("strategy_id", "symbol", name="uq_position_strategy_symbol"),)


class PositionEvent(TimestampMixin, Base):
    __tablename__ = "position_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    fill_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fills.id"))
    qty_before: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    qty_after: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    avg_before: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    avg_after: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    realized_delta: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    __table_args__ = (Index("ix_position_events_strategy", "strategy_id", "created_at"),)


class PnlSnapshot(Base):
    __tablename__ = "pnl_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    strategy_id: Mapped[str | None] = mapped_column(String(32))  # NULL = portfolio
    realized: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    unrealized: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    exposure: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    margin_used: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    __table_args__ = (Index("ix_pnl_snapshots_ts", "strategy_id", "ts"),)


# --------------------------------------------------------------------------- risk / audit / resilience
class RiskLimit(Base):
    __tablename__ = "risk_limits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # GLOBAL | STRATEGY
    strategy_id: Mapped[str | None] = mapped_column(ForeignKey("strategies.id"))
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (
        UniqueConstraint("scope", "strategy_id", "name", name="uq_risk_limit"),
        Index("uq_risk_limit_global", "name", unique=True, postgresql_where=text("strategy_id IS NULL")),
        CheckConstraint("value >= 0", name="ck_risk_limit_nonneg"),
    )


class RiskEvent(TimestampMixin, Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_id: Mapped[str | None] = mapped_column(String(32))
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    rule: Mapped[str | None] = mapped_column(String(64))
    actual: Mapped[str | None] = mapped_column(Text)
    allowed: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    checks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    __table_args__ = (Index("ix_risk_events_created", "created_at"),)


class ReconciliationEvent(Base):
    __tablename__ = "reconciliation_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    mismatches: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    actions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (Index("ix_recon_started", "started_at"),)


class SystemEvent(TimestampMixin, Base):
    """Audit trail."""

    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False, default="INFO")
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(32))
    order_id: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    __table_args__ = (Index("ix_system_events_created", "created_at"), Index("ix_system_events_type", "event_type"))


class FaultSimulation(Base):
    __tablename__ = "fault_simulations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    scenario: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    steps: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    outcome: Mapped[str | None] = mapped_column(Text)
    triggered_by: Mapped[str | None] = mapped_column(String(255))
    run_group: Mapped[str | None] = mapped_column(String(64))  # chaos / demo run id
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)


class MarketBar(Base):
    """Cache of REAL historical bars fetched from the market-data API (used for replay)."""

    __tablename__ = "market_bars"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    interval: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    high: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    low: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    volume: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)


# --------------------------------------------------------------------------- paper broker (separate venue state)
PB = {"schema": "paper_broker"}


class PBAccount(TimestampMixin, Base):
    __tablename__ = "pb_accounts"
    __table_args__ = PB
    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    starting_cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class PBOrder(TimestampMixin, Base):
    __tablename__ = "pb_orders"
    broker_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    order_type: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False, default=0)
    avg_price: Mapped[Decimal | None] = mapped_column(PRICE)
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    tag: Mapped[str | None] = mapped_column(String(64))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    max_fill_quantity: Mapped[Decimal | None] = mapped_column(QTY)  # partial-fill fault cap
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("account_id", "client_order_id", name="uq_pb_client_order"), PB)


class PBFill(Base):
    __tablename__ = "pb_fills"
    fill_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    broker_order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = PB


class PBPosition(Base):
    __tablename__ = "pb_positions"
    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False, default=0)
    avg_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    __table_args__ = PB
