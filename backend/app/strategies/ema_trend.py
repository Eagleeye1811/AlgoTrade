"""Reference strategy (published in the README with these exact rules).

On every closed 1-minute candle:
  fast = EMA(fast_period) of 1m closes, slow = EMA(slow_period) of 1m closes
  trend = EMA(trend_period) of closed 5m closes; the 5m filter compares the last closed 5m close with it
  BUY  when fast crosses above slow (prev fast <= prev slow and fast > slow)
       - opens a long only if the last closed 5m close > 5m trend EMA, otherwise it only closes a short
  SELL when fast crosses below slow - opens a short only if the last closed 5m close < 5m trend EMA,
       otherwise it only closes a long
Exits (platform, same for every strategy): opposite crossover, stop-loss / take-profit from the average entry
price on every tick, and the intraday square-off. Size: the subscription's fixed quantity; the whole
position is closed on exit, never added to.
"""
from __future__ import annotations

from app.strategies.base import BUY, EXIT_SPECS, HOLD, SELL, ParamSpec, Strategy, StrategySignal
from app.strategies.indicators import crossed_above, crossed_below, ema


class EmaTrendStrategy(Strategy):
    kind = "EMA_TREND"
    display_name = "EMA Crossover + 5m Trend"
    description = (
        "Reference strategy. 1m EMA fast/slow crossover; entries only in the direction of the 5m trend "
        "(last closed 5m close vs 5m EMA). An opposite crossover always closes the position."
    )
    param_specs = [
        ParamSpec("fast_period", "Fast EMA (1m)", "int", 2, 50, 9),
        ParamSpec("slow_period", "Slow EMA (1m)", "int", 3, 200, 21),
        ParamSpec("trend_period", "Trend EMA (5m)", "int", 2, 100, 20),
    ] + EXIT_SPECS

    @classmethod
    def cross_validate(cls, p):
        if p["fast_period"] >= p["slow_period"]:
            raise ValueError("fast_period must be smaller than slow_period")

    def _calc(self):
        c = self.closes()
        return ema(c, self.params["fast_period"]), ema(c, self.params["slow_period"])

    def _trend(self) -> tuple[float | None, float | None]:
        closes5 = [float(b.close) for b in self.bars5]
        t = ema(closes5, self.params["trend_period"])
        return (closes5[-1], t[-1]) if closes5 and t[-1] is not None else (None, None)

    def generate_signal(self) -> StrategySignal:
        p = self.params
        if len(self.bars) < p["slow_period"] + 1:
            return StrategySignal(HOLD, f"warming up ({len(self.bars)} 1m bars)")
        f, s = self._calc()
        close5, trend5 = self._trend()
        ind = {"ema_fast": round(f[-1], 2), "ema_slow": round(s[-1], 2), "close": float(self.bars[-1].close),
               "close_5m": close5, "ema_trend_5m": round(trend5, 2) if trend5 is not None else None,
               "bars_5m": len(self.bars5)}
        up, dn = crossed_above(f[-2], s[-2], f[-1], s[-1]), crossed_below(f[-2], s[-2], f[-1], s[-1])
        if not (up or dn):
            side = "above" if f[-1] > s[-1] else "below"
            return StrategySignal(HOLD, f"no crossover (EMA{p['fast_period']} {side} EMA{p['slow_period']})", ind)
        cross = (f"EMA{p['fast_period']} crossed {'above' if up else 'below'} EMA{p['slow_period']} "
                 f"({f[-1]:.2f} vs {s[-1]:.2f})")
        if trend5 is None:
            return StrategySignal(BUY if up else SELL, f"{cross}; 5m trend not ready ({len(self.bars5)} 5m bars) - exit only",
                                  ind, close_only=True)
        with_trend = close5 > trend5 if up else close5 < trend5
        rel = ">" if close5 > trend5 else "<" if close5 < trend5 else "="
        trend_txt = f"5m close {close5:.2f} {rel} 5m EMA{p['trend_period']} {trend5:.2f}"
        if with_trend:
            return StrategySignal(BUY if up else SELL, f"{cross}; {trend_txt} - entry allowed", ind)
        return StrategySignal(BUY if up else SELL, f"{cross}; {trend_txt} - against trend, exit only", ind, close_only=True)

    def series(self):
        f, s = self._calc()
        closes5 = [float(b.close) for b in self.bars5]
        t = ema(closes5, self.params["trend_period"])
        # the trend EMA of a 5m candle is known once that candle closes (its start + 5 minutes)
        trend = [{"time": int(b.ts.timestamp()) + 300, "value": round(v, 4)} for b, v in zip(self.bars5, t) if v is not None]
        return {"ema_fast": self._series(f), "ema_slow": self._series(s), "trend_5m": trend}
