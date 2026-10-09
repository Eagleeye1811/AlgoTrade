"""Common strategy interface. Strategies only emit signals - they can never place orders.

Strategy
  -> Signal (BUY / SELL / HOLD)
  -> StrategyEngine -> OrderEngine (which always runs the Risk Firewall) -> BrokerAdapter
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.market.models import Bar

BUY, SELL, HOLD = "BUY", "SELL", "HOLD"


@dataclass
class StrategySignal:
    signal: str
    reason: str
    indicators: dict[str, Any] = field(default_factory=dict)
    bar: Bar | None = None
    # True when the entry conditions are not met but the signal is still a valid exit: it may close an
    # opposite position, never open a new one.
    close_only: bool = False
    # entry order pricing requested by the strategy (default: market order)
    order_type: str = "MARKET"
    limit_price: Decimal | None = None


@dataclass
class ParamSpec:
    name: str
    label: str
    type: str  # int | float
    min: float
    max: float
    default: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# Position exits shared by every strategy. Evaluated by the StrategyEngine on every tick against the
# position's average price; 0 disables the rule. Exits still go through the Risk Firewall like any order.
EXIT_SPECS = [
    ParamSpec("stop_loss_pct", "Stop-loss %", "float", 0, 20, 1.0),
    ParamSpec("take_profit_pct", "Take-profit %", "float", 0, 50, 2.0),
]


class Strategy(ABC):
    kind: str = ""
    display_name: str = ""
    description: str = ""
    param_specs: list[ParamSpec] = []
    # what drives the strategy: closed candles (default), every LTP update, and/or the IST clock
    uses_candles: bool = True
    uses_ticks: bool = False
    uses_clock: bool = False
    enforce_hours: bool = True  # set by the engine from settings.enforce_market_hours
    max_entries_per_day: int | None = None  # platform-enforced; None = unlimited
    exit_at_level: bool = False  # stop-loss / target exits as a limit at the level (marketable), else market

    def __init__(self, strategy_id: str, symbol: str, params: dict[str, Any] | None = None, max_bars: int = 600):
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.params = self.validate_parameters(params or {})
        self.bars: list[Bar] = []
        self.bars5: list[Bar] = []  # closed 5m candles from the candle engine (higher-timeframe filters)
        self.max_bars = max_bars
        self.last_signal: StrategySignal = StrategySignal(HOLD, "warming up")

    # ---------------------------------------------------------------- interface
    @classmethod
    def defaults(cls) -> dict[str, Any]:
        return {p.name: p.default for p in cls.param_specs}

    @classmethod
    def validate_parameters(cls, params: dict[str, Any]) -> dict[str, Any]:
        out = cls.defaults()
        specs = {p.name: p for p in cls.param_specs}
        for k, v in params.items():
            if k not in specs:
                raise ValueError(f"unknown parameter '{k}'")
            sp = specs[k]
            try:
                v = int(v) if sp.type == "int" else float(v)
            except (TypeError, ValueError) as e:
                raise ValueError(f"parameter '{k}' must be {sp.type}") from e
            if not (sp.min <= v <= sp.max):
                raise ValueError(f"parameter '{k}'={v} outside [{sp.min}, {sp.max}]")
            out[k] = v
        cls.cross_validate(out)
        return out

    @classmethod
    def cross_validate(cls, params: dict[str, Any]) -> None:
        return None

    def get_parameters(self) -> dict[str, Any]:
        return dict(self.params)

    def initialize(self, history: list[Bar], history5: list[Bar] | None = None) -> None:
        """Warm indicators from history. History never produces trades."""
        self.bars = list(history[-self.max_bars:])
        self.bars5 = list((history5 or [])[-self.max_bars:])
        if self.bars:
            sig = self.generate_signal()
            self.last_signal = StrategySignal(HOLD, f"warm-up complete ({len(self.bars)} bars); last evaluation: {sig.signal}",
                                              sig.indicators, self.bars[-1])

    def on_candle(self, bar: Bar) -> StrategySignal:
        if self.bars and bar.ts <= self.bars[-1].ts:
            return StrategySignal(HOLD, "duplicate/out-of-order candle ignored", self.last_signal.indicators, bar)
        self.bars.append(bar)
        if len(self.bars) > self.max_bars:
            self.bars = self.bars[-self.max_bars:]
        sig = self.generate_signal()
        sig.bar = bar
        self.last_signal = sig
        return sig

    def on_htf_candle(self, bar: Bar) -> None:
        """A closed 5m candle. Strategies read self.bars5 when their 1m candle closes."""
        if self.bars5 and bar.ts <= self.bars5[-1].ts:
            return
        self.bars5.append(bar)
        if len(self.bars5) > self.max_bars:
            self.bars5 = self.bars5[-self.max_bars:]

    def on_tick(self, price: Decimal, ts: datetime, session_open: Decimal | None) -> StrategySignal | None:
        """Tick-driven strategies: called on every LTP update while the strategy may enter."""
        return None

    def on_clock(self, now_ist: datetime) -> StrategySignal | None:
        """Clock-driven strategies: called every second (IST market time) while the strategy may enter."""
        return None

    def generate_signal(self) -> StrategySignal:
        return StrategySignal(HOLD, "not candle-driven")

    def series(self) -> dict[str, list[dict[str, Any]]]:
        """Indicator series aligned with self.bars for charting."""
        return {}

    @property
    def min_bars(self) -> int:
        return 2

    def closes(self) -> list[float]:
        return [float(b.close) for b in self.bars]

    def _series(self, values: list[float | None]) -> list[dict[str, Any]]:
        return [{"time": int(b.ts.timestamp()), "value": round(v, 4)} for b, v in zip(self.bars, values) if v is not None]
