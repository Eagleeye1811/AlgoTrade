"""Pure indicator functions (floats are fine here: indicators drive signals, not money)."""
from __future__ import annotations

from typing import Sequence


def ema(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period  # SMA seed
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period

    def val(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[period] = val(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
        out[i] = val(avg_g, avg_l)
    return out


def macd(values: Sequence[float], fast: int, slow: int, signal: int):
    ef, es = ema(values, fast), ema(values, slow)
    line: list[float | None] = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    sig: list[float | None] = [None] * len(values)
    if first is not None:
        sub = ema([v for v in line[first:]], signal)  # type: ignore[misc]
        for j, v in enumerate(sub):
            sig[first + j] = v
    hist = [m - s if m is not None and s is not None else None for m, s in zip(line, sig)]
    return line, sig, hist


def crossed_above(a_prev, b_prev, a, b) -> bool:
    return None not in (a_prev, b_prev, a, b) and a_prev <= b_prev and a > b


def crossed_below(a_prev, b_prev, a, b) -> bool:
    return None not in (a_prev, b_prev, a, b) and a_prev >= b_prev and a < b
