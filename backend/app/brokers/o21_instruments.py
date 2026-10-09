"""021 instrument master (GET /instruments: gzipped CSV, ~1,05,000 rows, refreshed daily before market open).

Downloaded once per IST day and cached on disk (data/instruments/instruments-YYYY-MM-DD.csv). The platform trades
NSE cash equities: rows with exchange NSECM and instrument_type STK, looked up by trading symbol.
Prices (ticksize, circuits) are in paise.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

log = logging.getLogger("algotrade.o21.instruments")
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class Instrument:
    token: int
    exchange: str          # instruments/response name, e.g. NSECM
    symbol: str
    instrument_type: str
    ticksize: int          # paise
    lower_circuit: int     # paise
    upper_circuit: int     # paise
    lot: int               # board_lot_quantity
    freeze_quantity: int

    def to_paise(self, rupees: Decimal) -> int:
        """Rupees -> paise rounded to the nearest valid tick."""
        tick = Decimal(max(1, self.ticksize))
        return int(((Decimal(rupees) * 100) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick)


def parse_instruments(text: str) -> list[Instrument]:
    out: list[Instrument] = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            out.append(Instrument(int(row["token"]), row["exchange"].strip(), row["symbol"].strip(),
                                  row["instrument_type"].strip(), int(row.get("ticksize") or 1),
                                  int(row.get("lower_circuit") or 0), int(row.get("upper_circuit") or 0),
                                  int(row.get("board_lot_quantity") or 1), int(row.get("freeze_quantity") or 0)))
        except (KeyError, ValueError):
            continue
    return out


class InstrumentStore:
    def __init__(self, cache_dir: str | Path):
        p = Path(cache_dir)
        self.cache_dir = p if p.is_absolute() else Path(__file__).resolve().parent.parent.parent / p
        self.by_symbol: dict[str, Instrument] = {}   # NSE cash equities
        self.by_token: dict[tuple[str, int], Instrument] = {}
        self.loaded_for: str | None = None

    def load_rows(self, rows: list[Instrument]) -> None:
        self.by_token = {(r.exchange, r.token): r for r in rows}
        self.by_symbol = {r.symbol: r for r in rows if r.exchange == "NSECM" and r.instrument_type == "STK"}

    async def ensure(self, client) -> None:
        """Load today's instruments: disk cache first, else download (client: O21Client)."""
        today = datetime.now(IST).date().isoformat()
        if self.loaded_for == today and self.by_symbol:
            return
        path = self.cache_dir / f"instruments-{today}.csv"
        if path.exists():
            text = path.read_text()
        else:
            raw = await client.get_raw("/instruments")
            text = (gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8", "replace")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            for old in self.cache_dir.glob("instruments-*.csv"):
                if old != path:
                    old.unlink(missing_ok=True)
        self.load_rows(parse_instruments(text))
        self.loaded_for = today
        log.info("021 instruments loaded: %d NSE equities", len(self.by_symbol))

    def nse(self, symbol: str) -> Instrument:
        inst = self.by_symbol.get(symbol.upper())
        if inst is None:
            raise KeyError(f"{symbol} is not an NSE cash equity in today's 021 instrument list")
        return inst

    def symbol_of(self, exchange: str, token: int) -> str | None:
        inst = self.by_token.get((exchange, int(token)))
        return inst.symbol if inst else None
