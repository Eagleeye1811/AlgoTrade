"""Refresh the bundled REAL replay snapshot (last 5 NSE sessions of 1m bars) from the Yahoo chart endpoint.

Usage:  python scripts/fetch_replay_data.py [SYMBOL ...]
The raw API responses are saved unmodified to data/replay/<SYMBOL>_1m.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

BASE = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS"
DEFAULT = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC", "LT",
           "KOTAKBANK", "AXISBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI", "SUNPHARMA"]


def main() -> None:
    syms = [s.upper() for s in sys.argv[1:]] or DEFAULT
    out = Path(__file__).resolve().parent.parent / "data" / "replay"
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=15, headers={"User-Agent": "Mozilla/5.0 (AlgoTrade POC)"}) as c:
        for s in syms:
            r = c.get(BASE.format(sym=s), params={"interval": "1m", "range": "5d"})
            r.raise_for_status()
            data = r.json()
            n = len(data["chart"]["result"][0].get("timestamp") or [])
            (out / f"{s}_1m.json").write_text(json.dumps(data))
            print(f"{s}: {n} bars")


if __name__ == "__main__":
    main()
