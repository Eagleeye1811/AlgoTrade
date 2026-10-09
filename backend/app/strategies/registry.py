from app.strategies.base import Strategy
from app.strategies.breakout import BreakoutStrategy
from app.strategies.macd import MacdStrategy
from app.strategies.rsi_ema import RsiEmaStrategy

REGISTRY: dict[str, type[Strategy]] = {c.kind: c for c in (RsiEmaStrategy, MacdStrategy, BreakoutStrategy)}
