from __future__ import annotations

from app.strategies.base import BUY, EXIT_SPECS, HOLD, SELL, ParamSpec, Strategy, StrategySignal


class BreakoutStrategy(Strategy):
    kind = "BREAKOUT"
    display_name = "Breakout"
    description = (
        "Resistance = highest high and support = lowest low of the `lookback` candles preceding the confirmation "
        "window. BUY when `confirmation` consecutive closes exceed resistance by `buffer_pct`; SELL on the mirror "
        "breakdown below support. Signals fire once per breakout."
    )
    param_specs = [
        ParamSpec("lookback", "Lookback candles", "int", 5, 200, 20),
        ParamSpec("confirmation", "Confirmation candles", "int", 1, 5, 1),
        ParamSpec("buffer_pct", "Breakout buffer %", "float", 0, 2, 0.05),
    ] + EXIT_SPECS

    def _levels(self, end: int):
        """Support/resistance from the lookback window that ends before index `end - confirmation + 1`."""
        lb, cf = self.params["lookback"], self.params["confirmation"]
        start_conf = end - cf + 1
        win = self.bars[max(0, start_conf - lb): start_conf]
        if len(win) < lb:
            return None, None
        return max(float(b.high) for b in win), min(float(b.low) for b in win)

    def _state(self, end: int) -> str:
        res, sup = self._levels(end)
        if res is None:
            return "NA"
        buf = self.params["buffer_pct"] / 100
        cf = self.params["confirmation"]
        closes = [float(b.close) for b in self.bars[end - cf + 1: end + 1]]
        if all(c > res * (1 + buf) for c in closes):
            return "UP"
        if all(c < sup * (1 - buf) for c in closes):
            return "DOWN"
        return "IN"

    def generate_signal(self) -> StrategySignal:
        lb, cf = self.params["lookback"], self.params["confirmation"]
        n = len(self.bars)
        if n < lb + cf + 1:
            return StrategySignal(HOLD, f"warming up ({n} bars)")
        res, sup = self._levels(n - 1)
        close = float(self.bars[-1].close)
        ind = {"resistance": round(res, 2), "support": round(sup, 2), "close": close,
               "distance_to_resistance_pct": round((res - close) / close * 100, 3)}
        now, prev = self._state(n - 1), self._state(n - 2)
        if now == "UP" and prev != "UP":
            return StrategySignal(BUY, f"confirmed breakout: close {close:.2f} > resistance {res:.2f} for {cf} candle(s)", ind)
        if now == "DOWN" and prev != "DOWN":
            return StrategySignal(SELL, f"confirmed breakdown: close {close:.2f} < support {sup:.2f} for {cf} candle(s)", ind)
        return StrategySignal(HOLD, f"inside range {sup:.2f} - {res:.2f}", ind)

    def series(self):
        res_s, sup_s = [], []
        for i, b in enumerate(self.bars):
            r, s = self._levels(i)
            if r is not None:
                t = int(b.ts.timestamp())
                res_s.append({"time": t, "value": round(r, 2)})
                sup_s.append({"time": t, "value": round(s, 2)})
        return {"resistance": res_s, "support": sup_s}

