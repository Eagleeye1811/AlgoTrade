from __future__ import annotations

from app.strategies.base import BUY, EXIT_SPECS, HOLD, SELL, ParamSpec, Strategy, StrategySignal
from app.strategies.indicators import crossed_above, crossed_below, macd


class MacdStrategy(Strategy):
    kind = "MACD"
    display_name = "MACD"
    description = "BUY when the MACD line crosses above the signal line; SELL when it crosses below."
    param_specs = [
        ParamSpec("fast", "Fast EMA", "int", 2, 100, 12),
        ParamSpec("slow", "Slow EMA", "int", 3, 300, 26),
        ParamSpec("signal", "Signal EMA", "int", 2, 100, 9),
    ] + EXIT_SPECS

    @classmethod
    def cross_validate(cls, p):
        if p["fast"] >= p["slow"]:
            raise ValueError("fast must be smaller than slow")

    def _calc(self):
        return macd(self.closes(), self.params["fast"], self.params["slow"], self.params["signal"])

    def generate_signal(self) -> StrategySignal:
        p = self.params
        if len(self.bars) < p["slow"] + p["signal"] + 1:
            return StrategySignal(HOLD, f"warming up ({len(self.bars)} bars)")
        line, sig, hist = self._calc()
        ind = {"macd": round(line[-1], 4), "signal": round(sig[-1], 4), "histogram": round(hist[-1], 4),
               "close": float(self.bars[-1].close)}
        if crossed_above(line[-2], sig[-2], line[-1], sig[-1]):
            return StrategySignal(BUY, f"MACD {line[-1]:.3f} crossed above signal {sig[-1]:.3f}", ind)
        if crossed_below(line[-2], sig[-2], line[-1], sig[-1]):
            return StrategySignal(SELL, f"MACD {line[-1]:.3f} crossed below signal {sig[-1]:.3f}", ind)
        side = "above" if line[-1] > sig[-1] else "below"
        return StrategySignal(HOLD, f"no crossover (MACD {side} signal, hist {hist[-1]:.3f})", ind)

    def series(self):
        line, sig, hist = self._calc()
        return {"macd": self._series(line), "signal": self._series(sig), "histogram": self._series(hist)}
