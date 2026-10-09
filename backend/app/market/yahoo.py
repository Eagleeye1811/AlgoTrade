"""Adapter for Yahoo Finance's public chart endpoint (no API key).

Endpoint (verified reachable during development):
    GET {base}/v8/finance/chart/{SYMBOL}.NS?interval=1m&range=1d|5d

Caveats, stated honestly in the UI:
  * This is a public endpoint used by Yahoo's own site, not an officially documented,
    contractually supported API. It may rate-limit (HTTP 429) or change without notice.
  * Intraday NSE quotes are best-effort and may be delayed; we label the feed DELAYED
    whenever the newest bar lags wall-clock time.
All responses are validated; malformed/null bars are dropped, never patched with invented values.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from app.market.models import Bar

log = logging.getLogger("algotrade.yahoo")

NSE_SUFFIX = ".NS"


class MarketDataError(Exception):
    pass


class RateLimited(MarketDataError):
    pass


def _dec(x) -> Decimal | None:
    if x is None:
        return None
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return d.quantize(Decimal("0.01"))


def parse_chart(symbol: str, data: dict) -> tuple[list[Bar], dict, int]:
    """Parse a chart response. Returns (bars, meta, dropped_count)."""
    try:
        res = data["chart"]["result"][0]
        ts_list = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
        meta = res.get("meta", {})
    except (KeyError, IndexError, TypeError) as e:
        err = (data or {}).get("chart", {}).get("error") if isinstance(data, dict) else None
        raise MarketDataError(f"malformed chart response for {symbol}: {err or e}") from e
    bars: list[Bar] = []
    dropped = 0
    for i, t in enumerate(ts_list):
        try:
            o, h, l, c = (_dec(q[k][i]) for k in ("open", "high", "low", "close"))
            v = q["volume"][i]
        except (IndexError, KeyError):
            dropped += 1
            continue
        if None in (o, h, l, c) or v is None or l <= 0 or h < l or not (l <= o <= h and l <= c <= h):
            dropped += 1
            continue
        bars.append(Bar(symbol, datetime.fromtimestamp(int(t), tz=timezone.utc), o, h, l, c, Decimal(int(v))))
    bars.sort(key=lambda b: b.ts)
    # de-duplicate timestamps (keep last)
    dedup: dict[datetime, Bar] = {b.ts: b for b in bars}
    return list(dedup.values()), meta, dropped


class YahooChartClient:
    def __init__(self, base_url: str, timeout_s: float = 8.0, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = httpx.AsyncClient(
            timeout=timeout_s, headers={"User-Agent": "Mozilla/5.0 (AlgoTrade POC)"}, transport=transport
        )
        self.last_latency_ms: float | None = None
        self.backoff_until = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_bars(self, symbol: str, interval: str = "1m", range_: str = "1d") -> tuple[list[Bar], dict, int]:
        if time.time() < self.backoff_until:
            raise RateLimited(f"rate-limit backoff active ({self.backoff_until - time.time():.0f}s left)")
        url = f"{self.base_url}/v8/finance/chart/{symbol}{NSE_SUFFIX}"
        t = time.perf_counter()
        try:
            r = await self._client.get(url, params={"interval": interval, "range": range_})
        except httpx.TimeoutException as e:
            raise MarketDataError(f"timeout fetching {symbol}") from e
        except httpx.HTTPError as e:
            raise MarketDataError(f"network error fetching {symbol}: {e}") from e
        self.last_latency_ms = (time.perf_counter() - t) * 1000
        if r.status_code == 429:
            self.backoff_until = time.time() + 60
            raise RateLimited("HTTP 429 from market-data API")
        if r.status_code != 200:
            raise MarketDataError(f"HTTP {r.status_code} for {symbol}")
        try:
            data = r.json()
        except ValueError as e:
            raise MarketDataError(f"non-JSON response for {symbol}") from e
        return parse_chart(symbol, data)
