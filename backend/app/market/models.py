from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

# Market data feed status shown in the UI. Stale data is never shown as LIVE.
LIVE, DELAYED, REPLAY, DISCONNECTED, STALE, STOPPED = "LIVE", "DELAYED", "REPLAY", "DISCONNECTED", "STALE", "STOPPED"


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime  # bar start (UTC)
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "time": int(self.ts.timestamp()),
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": float(self.volume),
        }


@dataclass(frozen=True)
class Tick:
    symbol: str
    ts: datetime  # market timestamp (UTC)
    price: Decimal
    volume: Decimal  # incremental traded volume represented by this tick
    source: str
    # The real traded range of the source bar so far; the paper broker never fills outside it.
    range_high: Decimal | None = None
    range_low: Decimal | None = None
