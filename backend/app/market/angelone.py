"""Angel One SmartAPI market data: live NSE prices (orders stay on the paper broker - nothing is sent to Angel One).

Login: POST /rest/auth/angelbroking/user/v1/loginByPassword with the client code, MPIN and a TOTP generated from the
account's TOTP secret -> jwtToken + feedToken (the session ends at midnight IST; a new one is made each trading day).
Feed: SmartStream websocket v2, "quote" mode (2) for NSE cash (exchange type 1). Each binary packet (little endian,
prices in paise) carries the LTP, the cumulative day volume and the day's open/high/low/previous close.
Instrument tokens come from Angel One's public scrip master (<SYMBOL>-EQ on NSE), cached in `tokens_path`.

Outside the NSE session, on a day without a session (exchange holiday: the feed only sends an earlier day's snapshot)
and when the account cannot log in (missing or rejected credentials), the latest recorded session is replayed from
Yahoo Finance instead (mode REPLAY, source "yahoo-replay"); the live feed takes over when the session opens.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import struct
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Awaitable, Callable

import httpx
import websockets

from app.market.models import DELAYED, DISCONNECTED, LIVE, REPLAY, Tick
from app.market.providers import IST, DayStats, MarketDataProvider, YahooReplayProvider, nse_market_open

log = logging.getLogger("algotrade.market.angelone")

ANGEL_API_URL = "https://apiconnect.angelone.in"
ANGEL_WS_URL = "wss://smartapisocket.angelone.in/smart-stream"
SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
LOGIN_PATH = "/rest/auth/angelbroking/user/v1/loginByPassword"
QUOTE_MODE, NSE_CM = 2, 1
QUOTE_PACKET_LEN = 123
HEARTBEAT_S = 10.0  # the feed expects a "ping" text message at least every 30 s
NO_SESSION_AFTER_S = 120.0  # connected this long in session hours without a packet from today -> no session today
AUTH_RETRY_S = 600.0  # rejected credentials: replay, try logging in again after this long
P100 = Decimal(100)


class AngelAuthError(Exception):
    """Credentials missing or rejected: retrying immediately cannot help."""


def totp(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 time-based one-time password (what the authenticator app shows) from the base32 TOTP secret."""
    s = secret.replace(" ", "").upper()
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return str(code).zfill(digits)


@dataclass(frozen=True)
class AngelQuote:
    mode: int
    exchange: int
    token: str
    exchange_time: datetime | None
    ltp: int  # paise
    volume: int  # cumulative for the day
    open: int
    high: int
    low: int
    close: int  # previous session's close


def parse_quote(frame: bytes) -> AngelQuote | None:
    """A quote/snap-quote packet; None for anything shorter (LTP-only packets, control messages)."""
    if len(frame) < QUOTE_PACKET_LEN:
        return None
    mode, exch = frame[0], frame[1]
    token = frame[2:27].split(b"\x00", 1)[0].decode("ascii", "replace")
    _seq, ts_ms, ltp = struct.unpack_from("<qqq", frame, 27)
    _ltq, _atp, volume = struct.unpack_from("<qqq", frame, 51)
    o, h, lo, c = struct.unpack_from("<qqqq", frame, 91)
    when = datetime.fromtimestamp(ts_ms / 1000, timezone.utc) if ts_ms > 0 else None
    return AngelQuote(mode, exch, token, when, ltp, volume, o, h, lo, c)


def _rupees(paise: int) -> Decimal | None:
    return (Decimal(paise) / P100).quantize(Decimal("0.01")) if paise > 0 else None


class AngelOneProvider(MarketDataProvider):
    def __init__(self, symbols: list[str], api_key: str, client_code: str, pin: str, totp_secret: str,
                 tokens_path: str, replay: YahooReplayProvider, market_open: Callable[[], bool] = nse_market_open,
                 api_url: str = ANGEL_API_URL, ws_url: str = ANGEL_WS_URL,
                 login: Callable[[], Awaitable[dict]] | None = None):
        super().__init__(symbols)
        self.api_key, self.client_code, self.pin, self.totp_secret = api_key, client_code, pin, totp_secret
        self.tokens_path, self.api_url, self.ws_url = Path(tokens_path), api_url, ws_url
        self.market_open = market_open
        self.login = login or self._login
        self.replay = replay
        self.replay._paused = self._paused  # pause / resume (fault simulation) apply to both feeds
        self.replay.on_tick = self._on_replay_tick
        self.replay.on_status = self._on_replay_status
        self.replaying = False
        self.replay_reason = ""
        self._live_day: dict[str, DayStats] = {}
        self.day = self._live_day  # the replay's session values while it runs
        self._session: dict | None = None
        self._session_day: date | None = None
        self.tokens: dict[str, str] = {}  # Angel token -> symbol
        self._no_session_day: date | None = None
        self._auth_failed_at = 0.0
        self._today_seen = False
        self._volume: dict[str, int] = {}
        self._last_price: dict[str, Decimal] = {}
        self._last_ts: dict[str, datetime] = {}
        self.packets = 0
        self.earlier_session_packets = 0
        self.connected_at: float | None = None

    @property
    def mode(self) -> str:  # type: ignore[override]
        return REPLAY if self.replaying else LIVE

    @property
    def source_name(self) -> str:  # type: ignore[override]
        if self.replaying:
            return f"{self.replay.source_name} - {self.replay_reason}"
        return "Angel One SmartAPI live feed (NSE quote packets)"

    def inject_outage(self, seconds: float) -> None:
        super().inject_outage(seconds)
        self.replay.inject_outage(seconds)

    # ------------------------------------------------------------------ main loop
    async def _run(self) -> None:
        backoff = 1.0
        while True:
            today = datetime.now(IST).date()
            if not self.market_open():
                await self._replay_while("market closed - live prices resume at 09:15 IST",
                                         lambda: not self.market_open())
                continue
            if self._no_session_day == today:
                await self._replay_while("no NSE session today (exchange holiday)",
                                         lambda: datetime.now(IST).date() == today)
                continue
            if self._auth_failed_at and time.monotonic() - self._auth_failed_at < AUTH_RETRY_S:
                until = self._auth_failed_at + AUTH_RETRY_S
                await self._replay_while(f"Angel One login failed: {self.last_error}",
                                         lambda: time.monotonic() < until and self.market_open())
                continue
            try:
                await self._stream()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except AngelAuthError as e:
                self.last_error, self._auth_failed_at = str(e), time.monotonic()
                log.warning("Angel One login failed: %s - replaying the recorded session", e)
            except Exception as e:
                self.last_error = str(e)
                if isinstance(e, websockets.exceptions.InvalidStatus) and e.response.status_code in (401, 403):
                    self._session = None  # expired session: log in again
                await self._set_status(DISCONNECTED, f"Angel One feed: {e}")
                log.warning("Angel One feed: %s - reconnecting in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    async def _replay_while(self, reason: str, cond: Callable[[], bool]) -> None:
        self.replay_reason = reason
        if not self.replaying:
            self.replaying = True
            self.day = self.replay.day
            await self.replay.start()
        await self._set_status(REPLAY if self.replay.bars else DELAYED, reason)
        while cond():
            await asyncio.sleep(5)
        self.replaying = False
        self.replay.on_status = None  # its STOPPED is not this feed's status
        await self.replay.stop()
        self.replay.on_status = self._on_replay_status
        self.day = self._live_day

    async def _on_replay_tick(self, tick: Tick) -> None:
        if self.replaying and self.on_tick:
            await self.on_tick(tick)

    async def _on_replay_status(self, status: str, detail: str) -> None:
        if self.replaying:
            await self._set_status(status, f"{self.replay_reason} - {detail}" if status != REPLAY else self.replay_reason)

    # ------------------------------------------------------------------ live feed
    async def _login(self) -> dict:
        if not (self.api_key and self.client_code and self.pin and self.totp_secret):
            raise AngelAuthError("ALGOTRADE_ANGEL_API_KEY / _CLIENT_CODE / _PIN / _TOTP_SECRET not set in .env")
        try:
            code = totp(self.totp_secret)
        except Exception as e:
            raise AngelAuthError(f"ALGOTRADE_ANGEL_TOTP_SECRET is not a valid base32 secret ({e})") from None
        headers = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER",
                   "X-SourceID": "WEB", "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
                   "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": self.api_key}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(self.api_url + LOGIN_PATH, headers=headers,
                             json={"clientcode": self.client_code, "password": self.pin, "totp": code})
        try:
            body = r.json()
        except ValueError:
            body = {}
        data = body.get("data") or {}
        if r.status_code in (401, 403) or (body and not body.get("status")) or not data.get("jwtToken"):
            if r.status_code >= 500 or not body:
                raise RuntimeError(f"Angel One login: HTTP {r.status_code}")
            raise AngelAuthError(f"{body.get('message') or 'rejected'} ({body.get('errorcode') or r.status_code})")
        return data

    async def _ensure_session(self) -> dict:
        today = datetime.now(IST).date()
        if self._session is None or self._session_day != today:
            self._session = await self.login()
            self._session_day = today
            self._auth_failed_at = 0.0
        return self._session

    async def _ensure_tokens(self) -> None:
        cached = {}
        if self.tokens_path.exists():
            cached = json.loads(self.tokens_path.read_text())
        if any(s not in cached for s in self.symbols):
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.get(SCRIP_MASTER_URL)
                r.raise_for_status()
                rows = r.json()
            want = {f"{s}-EQ": s for s in self.symbols}
            cached = {want[x["symbol"]]: x["token"] for x in rows
                      if x.get("exch_seg") == "NSE" and x.get("symbol") in want}
            self.tokens_path.parent.mkdir(parents=True, exist_ok=True)
            self.tokens_path.write_text(json.dumps(cached, indent=1, sort_keys=True))
        for s in self.symbols:
            if s not in cached:
                log.warning("Angel One scrip master has no NSE instrument %s-EQ", s)
        self.tokens = {str(cached[s]): s for s in self.symbols if s in cached}

    async def _stream(self) -> None:
        await self._set_status(DELAYED, "logging in to Angel One SmartAPI")
        session = await self._ensure_session()
        await self._ensure_tokens()
        headers = {"Authorization": f"Bearer {session['jwtToken']}", "x-api-key": self.api_key,
                   "x-client-code": self.client_code, "x-feed-token": session.get("feedToken", "")}
        async with websockets.connect(self.ws_url, additional_headers=headers, max_size=2 ** 20) as ws:
            await ws.send(json.dumps({"correlationID": "algotrade", "action": 1, "params": {
                "mode": QUOTE_MODE, "tokenList": [{"exchangeType": NSE_CM, "tokens": list(self.tokens)}]}}))
            self.connected_at = time.time()
            started, last_ping = time.monotonic(), time.monotonic()
            self._today_seen = False
            self.day = self._live_day
            await self._set_status(LIVE, f"subscribed {len(self.tokens)} NSE instruments")
            while self.market_open():
                if time.monotonic() - last_ping >= HEARTBEAT_S:
                    await ws.send("ping")
                    last_ping = time.monotonic()
                if not self._today_seen and time.monotonic() - started > NO_SESSION_AFTER_S:
                    self._no_session_day = datetime.now(IST).date()
                    log.info("Angel One feed: no packet from today after %.0fs - no NSE session today", NO_SESSION_AFTER_S)
                    return
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                if not isinstance(msg, (bytes, bytearray)) or not self._paused.is_set() or time.time() < self.outage_until:
                    continue
                q = parse_quote(bytes(msg))
                if q is not None:
                    await self.on_quote(q)

    async def on_quote(self, q: AngelQuote) -> None:
        sym = self.tokens.get(q.token)
        if sym is None or q.exchange != NSE_CM:
            return
        self.packets += 1
        now = datetime.now(timezone.utc)
        today = now.astimezone(IST).date()
        if q.exchange_time is None or q.exchange_time.astimezone(IST).date() != today:
            self.earlier_session_packets += 1  # snapshot of an earlier session: not a price of today
            return
        self._today_seen = True
        self.last_latency_ms = max(0.0, (now - q.exchange_time).total_seconds() * 1000)
        d = self._live_day.setdefault(sym, DayStats())
        if d.trade_day != today:
            self._live_day[sym] = d = DayStats(trade_day=today)
            self._volume.pop(sym, None)
        d.open = _rupees(q.open) or d.open
        d.high = _rupees(q.high) or d.high
        d.low = _rupees(q.low) or d.low
        d.prev_close = _rupees(q.close) or d.prev_close
        price = _rupees(q.ltp)
        if price is None:
            return
        prev_vol = self._volume.get(sym)
        dv = max(0, q.volume - prev_vol) if prev_vol is not None else 0
        self._volume[sym] = d.volume = q.volume
        if price == self._last_price.get(sym) and dv == 0:
            return
        self._last_price[sym] = price
        # candles need non-decreasing times per symbol; never stamp a tick in the future
        ts = min(max(q.exchange_time, self._last_ts.get(sym, q.exchange_time)), now)
        self._last_ts[sym] = ts
        if self.on_tick:
            await self.on_tick(Tick(sym, ts, price, Decimal(dv), "angelone"))

    async def stop(self) -> None:
        await super().stop()
        if self.replaying:
            self.replaying = False
            self.replay.on_status = None
            await self.replay.stop()
            self.replay.on_status = self._on_replay_status

    def info(self) -> dict:
        d = super().info()
        d.update({"feed": "replay" if self.replaying else "angelone", "replay_reason": self.replay_reason if self.replaying else None,
                  "replay_day": self.replay.replay_day.isoformat() if self.replaying and self.replay.replay_day else None,
                  "instruments": len(self.tokens), "packets": self.packets,
                  "earlier_session_packets": self.earlier_session_packets, "connected_since": self.connected_at})
        return d
