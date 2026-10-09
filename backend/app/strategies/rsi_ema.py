from __future__ import annotations

from app.strategies.base import BUY, EXIT_SPECS, HOLD, SELL, ParamSpec, Strategy, StrategySignal
from app.strategies.indicators import crossed_above, crossed_below, ema, rsi


class RsiEmaStrategy(Strategy):
    kind = "RSI_EMA"
    display_name = "RSI + EMA"
    description = (
        "BUY: fast EMA crosses above slow EMA while RSI is between 50 and overbought, OR RSI recovers above the "
        "oversold level while fast EMA > slow EMA. SELL is the mirror image."
    )
    param_specs = [
        ParamSpec("rsi_period", "RSI period", "int", 2, 50, 14),
        ParamSpec("oversold", "Oversold", "float", 5, 50, 35),
        ParamSpec("overbought", "Overbought", "float", 50, 95, 65),
        ParamSpec("ema_fast", "Fast EMA", "int", 2, 100, 9),
        ParamSpec("ema_slow", "Slow EMA", "int", 3, 300, 21),
    ] + EXIT_SPECS

    @classmethod
    def cross_validate(cls, p):
        if p["ema_fast"] >= p["ema_slow"]:
            raise ValueError("ema_fast must be smaller than ema_slow")
        if p["oversold"] >= p["overbought"]:
            raise ValueError("oversold must be below overbought")

    def _calc(self):
        c = self.closes()
        return ema(c, self.params["ema_fast"]), ema(c, self.params["ema_slow"]), rsi(c, self.params["rsi_period"])

    def generate_signal(self) -> StrategySignal:
        p = self.params
        if len(self.bars) < max(p["ema_slow"], p["rsi_period"]) + 2:
            return StrategySignal(HOLD, f"warming up ({len(self.bars)} bars)")
        f, s, r = self._calc()
        ind = {"rsi": round(r[-1], 2), "ema_fast": round(f[-1], 2), "ema_slow": round(s[-1], 2),
               "close": float(self.bars[-1].close), "oversold": p["oversold"], "overbought": p["overbought"]}
        ema_up = crossed_above(f[-2], s[-2], f[-1], s[-1])
        ema_dn = crossed_below(f[-2], s[-2], f[-1], s[-1])
        rsi_recover = crossed_above(r[-2], p["oversold"], r[-1], p["oversold"])
        rsi_weaken = crossed_below(r[-2], p["overbought"], r[-1], p["overbought"])
        if ema_up and 50 <= r[-1] < p["overbought"]:
            return StrategySignal(BUY, f"EMA{p['ema_fast']} crossed above EMA{p['ema_slow']} with RSI {r[-1]:.1f} in bullish zone", ind)
        if rsi_recover and f[-1] > s[-1]:
            return StrategySignal(BUY, f"RSI recovered above {p['oversold']:g} ({r[-1]:.1f}) with bullish EMA alignment", ind)
        if ema_dn and p["oversold"] < r[-1] <= 50:
            return StrategySignal(SELL, f"EMA{p['ema_fast']} crossed below EMA{p['ema_slow']} with RSI {r[-1]:.1f} in bearish zone", ind)
        if rsi_weaken and f[-1] < s[-1]:
            return StrategySignal(SELL, f"RSI fell below {p['overbought']:g} ({r[-1]:.1f}) with bearish EMA alignment", ind)
        trend = "bullish" if f[-1] > s[-1] else "bearish"
        return StrategySignal(HOLD, f"no setup (EMA {trend}, RSI {r[-1]:.1f})", ind)

    def series(self):
        f, s, r = self._calc()
        return {"ema_fast": self._series(f), "ema_slow": self._series(s), "rsi": self._series(r)}
