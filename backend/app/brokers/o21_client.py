"""HTTP transport for the 021 developer API (https://devapi.021.trade/api/developer-api/v1).

What the collection documents and this client relies on:
  POST /auth/token {username, password} -> data.accessToken, data.expiresAt   (valid until 5 AM IST;
       a new login revokes the previous token - so logins are serialised and never done concurrently)
  every response is an envelope {"success": bool, "data": ..., "error": str|null}
  GET /websocket/ephemeral-key -> data.token (websocket ?token=, valid 24 h)

The sandbox misbehaves on purpose (rate limits, HTTP 500/503, timeouts). Policy:
  * 401          -> log in again once, then repeat the call (the call was not executed)
  * 429          -> wait (Retry-After or backoff) and repeat (the call was not executed)
  * 500/503/timeout/network on a SAFE call (GET, and DELETE/PUT which are idempotent at the venue)
                 -> retry with exponential backoff, then BrokerUnavailable
  * 500/503/timeout/network on POST /orders (NOT safe: the order may exist)
                 -> BrokerTimeout immediately; the order engine verifies against the order book instead of
                    resending (a blind retry could double the position)
  * success=false with 4xx (400 bad request, 422 blocked by 021 safe mode) -> BrokerRejected(error)
  * 403 (account inactive) / 421 (wrong cluster) -> BrokerUnavailable (needs the organisers, not a retry)
Login, order actions and errors use the envelope; list endpoints (orders, trades, positions) return a plain JSON
array, and /instruments returns gzipped CSV bytes (get_raw).
A token bucket keeps us under the sandbox's request rate.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from app.brokers.base import BrokerRejected, BrokerTimeout, BrokerUnavailable

log = logging.getLogger("algotrade.o21")


MAX_RELOGINS = 3                 # fresh logins per request before 021 counts as unavailable
RELOGIN_BACKOFF_S = 0.75
SESSION_CONFLICT_WINDOW_S = 300  # >= SESSION_CONFLICT_LOGINS logins in this window = someone else keeps logging in
SESSION_CONFLICT_LOGINS = 3

class RateLimiter:
    def __init__(self, rate_per_s: float, burst: int):
        self.rate, self.capacity = rate_per_s, float(burst)
        self.tokens, self.t = float(burst), time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class UncertainOrder(BrokerTimeout):
    """A state-changing call failed in a way that does not prove it was not executed (5xx / timeout / network)."""

    def __init__(self, message: str, status: int | None = None, error: str | None = None):
        super().__init__(message)
        self.status, self.error = status, error


class O21Client:
    def __init__(self, base_url: str, username: str, password: str, timeout_s: float = 5.0,
                 rate_per_s: float = 5.0, burst: int = 10, max_retries: int = 3,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.username, self.password = username, password
        self.max_retries = max_retries
        self.limiter = RateLimiter(rate_per_s, burst)
        self._http = httpx.AsyncClient(timeout=timeout_s, transport=transport)
        self.token: str | None = None
        self.token_expires_at: str | None = None
        self._login_lock = asyncio.Lock()
        self._login_times: list[float] = []  # monotonic times of recent logins (session-conflict detection)
        self.stats = {"requests": 0, "retries": 0, "relogins": 0, "rate_limited": 0, "server_errors": 0, "timeouts": 0}
        self.last_latency_ms: float | None = None

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ auth
    async def login(self, stale_token: str | None = None) -> str:
        """Log in unless another task already replaced `stale_token` (avoids revoking a fresh token)."""
        async with self._login_lock:
            if self.token is not None and self.token != stale_token:
                return self.token
            if not self.username or not self.password:
                raise BrokerUnavailable("021 credentials not configured (ALGOTRADE_O21_USERNAME / ALGOTRADE_O21_PASSWORD)")
            body = await self._send("POST", "/auth/token", {"username": self.username, "password": self.password},
                                    auth=False, safe=True)
            data = (body.get("data") if isinstance(body, dict) else None) or {}
            if not data.get("accessToken"):
                raise BrokerUnavailable(f"021 login returned no access token: {body.get('error')}")
            self.token, self.token_expires_at = data["accessToken"], data.get("expiresAt")
            self.stats["relogins"] += 1
            now = time.monotonic()
            self._login_times = [t for t in self._login_times if now - t < SESSION_CONFLICT_WINDOW_S] + [now]
            log.info("021 login ok, token expires %s", self.token_expires_at)
            return self.token

    # ------------------------------------------------------------------ requests
    async def get(self, path: str, params: dict | None = None) -> Any:
        """JSON body of a GET: the envelope's data, or the plain array of list endpoints."""
        body = await self.request("GET", path, params=params, safe=True)
        return body.get("data") if isinstance(body, dict) and "success" in body else body

    async def get_raw(self, path: str) -> bytes:
        body = await self.request("GET", path, safe=True, raw=True)
        return body

    @property
    def session_conflict(self) -> bool:
        """021 keeps revoking our token: another session (021 web app, another backend with the same credentials) is
        logging in to this account - every login revokes the previous token."""
        now = time.monotonic()
        return sum(1 for t in self._login_times if now - t < SESSION_CONFLICT_WINDOW_S) >= SESSION_CONFLICT_LOGINS

    async def request(self, method: str, path: str, body: Any = None, params: dict | None = None, safe: bool = True,
                      raw: bool = False) -> Any:
        if self.token is None:
            await self.login()
        relogins = 0
        while True:
            token = self.token
            try:
                return await self._send(method, path, body, params=params, safe=safe, token=token, raw=raw)
            except _Unauthorized:
                # a stray login elsewhere revokes our token: log in again (a few times, backing off) before giving up
                if relogins >= MAX_RELOGINS:
                    raise BrokerUnavailable(
                        "021 rejected the access token after a fresh login"
                        + (" - another session is logging in to this 021 account (021 web app or another AlgoTrade "
                           "backend with the same credentials); each login revokes the previous token. Close it."
                           if self.session_conflict else "")) from None
                if relogins:
                    await asyncio.sleep(RELOGIN_BACKOFF_S * relogins)
                relogins += 1
                await self.login(stale_token=token)

    async def _send(self, method: str, path: str, body: Any = None, params: dict | None = None, *, safe: bool,
                    auth: bool = True, token: str | None = None, raw: bool = False) -> Any:
        attempt = 0
        while True:
            attempt += 1
            await self.limiter.acquire()
            headers = {"Authorization": f"Bearer {token}"} if auth and token else {}
            t0 = time.perf_counter()
            self.stats["requests"] += 1
            try:
                r = await self._http.request(method, self.base_url + path, json=body, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                self.stats["timeouts"] += 1
                kind = "timeout" if isinstance(e, httpx.TimeoutException) else f"network error: {e}"
                if not safe:
                    raise UncertainOrder(f"021 {method} {path}: {kind} - outcome unknown") from e
                if attempt > self.max_retries:
                    raise BrokerUnavailable(f"021 {method} {path}: {kind} after {attempt} attempts") from e
                await self._backoff(attempt)
                continue
            self.last_latency_ms = (time.perf_counter() - t0) * 1000
            if r.status_code == 401 and auth:
                raise _Unauthorized()
            if r.status_code == 429:
                self.stats["rate_limited"] += 1
                if attempt > self.max_retries + 2:
                    raise BrokerUnavailable(f"021 {method} {path}: rate limited (HTTP 429)")
                await self._backoff(attempt, retry_after=r.headers.get("Retry-After"))
                continue
            if r.status_code in (403, 421):
                raise BrokerUnavailable(f"021 {method} {path}: HTTP {r.status_code} {self._error_text(r)}")
            if r.status_code >= 500:
                self.stats["server_errors"] += 1
                if not safe:
                    err = self._error_text(r)
                    raise UncertainOrder(f"021 {method} {path}: HTTP {r.status_code} {err or ''} - outcome unknown".strip(),
                                         r.status_code, err)
                if attempt > self.max_retries:
                    raise BrokerUnavailable(f"021 {method} {path}: HTTP {r.status_code} after {attempt} attempts")
                await self._backoff(attempt)
                continue
            if raw and r.is_success:
                return r.content
            try:
                payload = r.json()
            except ValueError:
                payload = None
            env_failed = isinstance(payload, dict) and payload.get("success") is False
            if not r.is_success or env_failed:
                err = (payload.get("error") if isinstance(payload, dict) else None) or f"HTTP {r.status_code}"
                if r.status_code == 404:
                    raise NotFound(str(err))
                raise BrokerRejected(str(err))
            return payload

    @staticmethod
    def _error_text(r: httpx.Response) -> str | None:
        try:
            body = r.json()
        except ValueError:
            return None
        return body.get("error") if isinstance(body, dict) else None

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        self.stats["retries"] += 1
        try:
            delay = float(retry_after) if retry_after else 0.25 * 2 ** (attempt - 1)
        except ValueError:
            delay = 0.25 * 2 ** (attempt - 1)
        await asyncio.sleep(min(5.0, delay) * (1 + random.random() * 0.2))


class _Unauthorized(Exception):
    pass


class NotFound(BrokerRejected):
    pass
