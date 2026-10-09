"""Strategy 2 (021-03, given): 1% breakout from today's open, 5% target and 5% stop-loss.

  1. Today's open = the `open` field of the 021 full packet for the subscribed stock.
  2. On every LTP update: LTP >= open x 1.01 -> BUY; LTP <= open x 0.99 -> SELL (qty negative).
     The entry is a limit order at the LTP (paise, rounded to the tick size), product INTRADAY.
  3. Once the entry fills at price P the platform manages the exit itself (no broker SL/target orders):
     long: target P x 1.05, stop-loss P x 0.95; short: target P x 0.95, stop-loss P x 1.05 - checked on every LTP;
     the exit is an opposite order of the same quantity, a limit at the level (made marketable if the LTP already
     passed it). One entry per day; an open position is squared off at 15:15 IST.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.strategies.base import BUY, SELL, ParamSpec, Strategy, StrategySignal


class OpenBreakoutStrategy(Strategy):
    kind = "OPEN_BREAKOUT"
    display_name = "1% Breakout from Open"
    description = ("Given strategy 2: buy when LTP >= today's open x 1.01, sell when LTP <= open x 0.99 (limit at "
                   "the LTP); platform-managed 5% target and 5% stop-loss from the fill price; one entry per day.")
    param_specs = [
        ParamSpec("breakout_pct", "Breakout from open %", "float", 0.1, 10, 1.0),
        ParamSpec("take_profit_pct", "Target %", "float", 0, 50, 5.0),
        ParamSpec("stop_loss_pct", "Stop-loss %", "float", 0, 20, 5.0),
    ]
    uses_candles = False
    uses_ticks = True
    max_entries_per_day = 1
    exit_at_level = True

    def on_tick(self, price: Decimal, ts: datetime, session_open: Decimal | None) -> StrategySignal | None:
        if not session_open:
            return None
        b = Decimal(str(self.params["breakout_pct"])) / 100
        up, down = session_open * (1 + b), session_open * (1 - b)
        ind = {"open": float(session_open), "ltp": float(price), "buy_above": round(float(up), 2),
               "sell_below": round(float(down), 2)}
        if price >= up:
            return StrategySignal(BUY, f"LTP {price} >= open {session_open} x {1 + b:.2f} ({up:.2f})", ind,
                                  order_type="LIMIT", limit_price=price)
        if price <= down:
            return StrategySignal(SELL, f"LTP {price} <= open {session_open} x {1 - b:.2f} ({down:.2f})", ind,
                                  order_type="LIMIT", limit_price=price)
        return None
