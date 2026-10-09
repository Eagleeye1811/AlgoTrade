"""Position / P&L definitions (Decimal only).

Definitions used across AlgoTrade:
  * quantity        signed net shares from actual fills (+ long, - short)
  * average entry   volume-weighted price of the fills that opened the current position
  * realized P&L    (exit price - average entry) * closed quantity * direction, booked when a fill reduces a position
  * unrealized P&L  (LTP - average entry) * quantity
  * exposure        |quantity| * LTP
  * margin          exposure * margin_rate (virtual margin blocked for the strategy)
  * available capital = starting capital + realized + unrealized - margin used
"""
from __future__ import annotations

from decimal import Decimal

ZERO = Decimal(0)
PX_Q = Decimal("0.0001")


def apply_fill_math(qty: Decimal, avg: Decimal, delta: Decimal, price: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Apply a signed fill `delta` at `price`. Returns (new_qty, new_avg, realized_pnl_delta)."""
    qty, avg, delta, price = Decimal(qty), Decimal(avg), Decimal(delta), Decimal(price)
    if delta == 0:
        return qty, avg, ZERO
    if qty == 0 or (qty > 0) == (delta > 0):
        new_qty = qty + delta
        new_avg = ((abs(qty) * avg + abs(delta) * price) / abs(new_qty)).quantize(PX_Q)
        return new_qty, new_avg, ZERO
    closing = min(abs(delta), abs(qty))
    direction = Decimal(1) if qty > 0 else Decimal(-1)
    realized = (closing * (price - avg) * direction).quantize(PX_Q)
    new_qty = qty + delta
    if new_qty == 0:
        return ZERO, ZERO, realized
    if (new_qty > 0) == (qty > 0):
        return new_qty, avg, realized
    return new_qty, price.quantize(PX_Q), realized  # flipped: remainder opens at fill price


def unrealized(qty: Decimal, avg: Decimal, ltp: Decimal | None) -> Decimal:
    if ltp is None or qty == 0:
        return ZERO
    return ((ltp - avg) * qty).quantize(PX_Q)
