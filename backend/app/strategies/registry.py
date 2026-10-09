from app.strategies.base import Strategy
from app.strategies.breakout import BreakoutStrategy
from app.strategies.ema_trend import EmaTrendStrategy
from app.strategies.macd import MacdStrategy
from app.strategies.open_breakout import OpenBreakoutStrategy
from app.strategies.rsi_ema import RsiEmaStrategy
from app.strategies.time_entry import TimeEntryStrategy

REGISTRY: dict[str, type[Strategy]] = {c.kind: c for c in (TimeEntryStrategy, OpenBreakoutStrategy, EmaTrendStrategy,
                                                              RsiEmaStrategy, MacdStrategy, BreakoutStrategy)}
