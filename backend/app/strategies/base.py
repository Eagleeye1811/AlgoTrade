"""Common strategy interface. Strategies only emit signals - they can never place orders.

Strategy
  -> Signal (BUY / SELL / HOLD)
  -> StrategyEngine -> OrderEngine (which always runs the Risk Firewall) -> BrokerAdapter
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.market.models import Bar

BUY, SELL, HOLD = "BUY", "SELL", "HOLD"


@dataclass
class StrategySignal:
    signal: str
    reason: str
    indicators: dict[str, Any] = field(default_factory=dict)
    bar: Bar | None = None


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

    def __init__(self, strategy_id: str, symbol: str, params: dict[str, Any] | None = None, max_bars: int = 600):
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.params = self.validate_parameters(params or {})
        self.bars: list[Bar] = []
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

    def initialize(self, history: list[Bar]) -> None:
        """Warm indicators from history. History never produces trades."""
        self.bars = list(history[-self.max_bars:])
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

    @abstractmethod
    def generate_signal(self) -> StrategySignal: ...

    @abstractmethod
    def series(self) -> dict[str, list[dict[str, Any]]]:
        """Indicator series aligned with self.bars for charting."""

    @property
    def min_bars(self) -> int:
        return 2

    def closes(self) -> list[float]:
        return [float(b.close) for b in self.bars]

    def _series(self, values: list[float | None]) -> list[dict[str, Any]]:
        return [{"time": int(b.ts.timestamp()), "value": round(v, 4)} for b, v in zip(self.bars, values) if v is not None]
