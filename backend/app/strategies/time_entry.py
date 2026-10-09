"""Strategy 1 (021-03, given): time-based entry and exit.

  1. At 09:15 IST enter: one order for the subscription's instrument, side (direction) and quantity,
     product INTRADAY (market order).
  2. At 15:15 IST exit: the platform's square-off cancels the strategy's open orders and closes the full position
     with an opposite order sized from GET /portfolio/positions' squareOffQuantity.
One entry per day. If the platform is not running at 09:15 it enters at the first moment it is (before 15:15).
With settings.enforce_market_hours off (021 sandbox, any time of day) it enters as soon as it runs, once per day.
"""
from __future__ import annotations

from datetime import datetime, time as dtime

from app.strategies.base import BUY, SELL, ParamSpec, Strategy, StrategySignal

ENTRY_AT = dtime(9, 15)
EXIT_AT = dtime(15, 15)


class TimeEntryStrategy(Strategy):
    kind = "TIME_ENTRY"
    display_name = "Time-based entry/exit"
    description = ("Given strategy 1: enters at 09:15 IST (market order, INTRADAY) with the configured side and "
                   "quantity; squares off the full position and cancels its open orders at 15:15 IST.")
    param_specs = [ParamSpec("direction", "Side (1 = buy, -1 = sell)", "int", -1, 1, 1)]
    uses_candles = False
    uses_clock = True
    max_entries_per_day = 1

    @classmethod
    def cross_validate(cls, p):
        if p["direction"] not in (1, -1):
            raise ValueError("direction must be 1 (buy) or -1 (sell)")

    def on_clock(self, now_ist: datetime) -> StrategySignal | None:
        if self.enforce_hours and (not (ENTRY_AT <= now_ist.time() < EXIT_AT) or now_ist.weekday() >= 5):
            return None
        side = BUY if self.params["direction"] == 1 else SELL
        return StrategySignal(side, f"time entry at {now_ist:%H:%M:%S} IST (scheduled {ENTRY_AT:%H:%M})",
                              {"scheduled_entry": f"{ENTRY_AT:%H:%M}", "scheduled_exit": f"{EXIT_AT:%H:%M}"})
