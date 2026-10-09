"""Trading charges from a published charges table (data file, see settings.charges_table_path).

Brokerage is charged per executed order (on the order's total filled turnover, capped per order); all other
components are percentages of turnover. An order's charges are allocated to its fills in proportion to turnover,
so per-fill charges always add up exactly to the order's charges.
"""
from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

Q2 = Decimal("0.01")
COMPONENTS = ("brokerage", "stt", "exchange_txn", "sebi", "stamp_duty", "gst")


def _d(x: Any) -> Decimal:
    return Decimal(str(x))


def _r(x: Decimal) -> Decimal:
    return x.quantize(Q2, rounding=ROUND_HALF_UP)


class ChargesTable:
    def __init__(self, table: dict[str, Any]):
        self.table = table
        self.name = table.get("name", "charges table")

    @classmethod
    def load(cls, path: str | Path) -> "ChargesTable":
        p = Path(path)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent.parent / p
        return cls(json.loads(p.read_text()))

    def order_charges(self, side: str, turnover: Decimal) -> dict[str, Decimal]:
        """Charges for one executed order of the given side and filled turnover (INR)."""
        t, buy = self.table, side == "BUY"
        out: dict[str, Decimal] = {}
        b = t["brokerage"]
        out["brokerage"] = _r(min(turnover * _d(b["pct_of_turnover"]) / 100, _d(b["max_per_order"])))
        out["stt"] = _r(turnover * _d(t["stt"]["buy_pct" if buy else "sell_pct"]) / 100)
        out["exchange_txn"] = _r(turnover * _d(t["exchange_txn_pct"]) / 100)
        out["sebi"] = _r(turnover * _d(t["sebi_per_crore"]) / Decimal(10_000_000))
        out["stamp_duty"] = _r(turnover * _d(t["stamp_duty"]["buy_pct" if buy else "sell_pct"]) / 100)
        out["gst"] = _r(sum((out[k] for k in t["gst_on"]), Decimal(0)) * _d(t["gst_pct"]) / 100)
        out["total"] = sum((out[k] for k in COMPONENTS), Decimal(0))
        return out

    def allocate(self, side: str, fills: list[tuple[Any, Decimal]]) -> dict[Any, dict[str, Decimal]]:
        """Split one order's charges over its fills [(fill_key, turnover)] proportionally; the last fill takes
        the rounding remainder so the parts sum exactly to the order's charges."""
        total_turnover = sum((t for _, t in fills), Decimal(0))
        order = self.order_charges(side, total_turnover)
        out: dict[Any, dict[str, Decimal]] = {}
        left = {k: order[k] for k in COMPONENTS}
        for i, (key, t) in enumerate(fills):
            if i == len(fills) - 1:
                part = dict(left)
            else:
                share = t / total_turnover if total_turnover else Decimal(0)
                part = {k: _r(order[k] * share) for k in COMPONENTS}
                for k in COMPONENTS:
                    left[k] -= part[k]
            part["total"] = sum((part[k] for k in COMPONENTS), Decimal(0))
            out[key] = part
        return out
