"""021 transport: retries only where safe, never on order placement; one login at a time."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.brokers.base import BrokerRejected, BrokerTimeout, BrokerUnavailable
from app.brokers.o21_client import O21Client


def ok(data):
    return httpx.Response(200, json={"success": True, "data": data, "error": None})


class Server:
    """Scripted fake: per path, a list of responses (or exceptions) served in order; the last one repeats."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, str]] = []
        self.logins = 0

    def __call__(self, req: httpx.Request):
        key = f"{req.method} {req.url.path.removeprefix('/v1')}"
        self.calls.append((key, req.headers.get("authorization", "")))
        if key == "POST /auth/token":
            self.logins += 1
            return ok({"accessToken": f"tok{self.logins}", "expiresAt": "2026-10-10T05:00:00+05:30"})
        seq = self.script[key]
        r = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(r, Exception):
            raise r
        return r(req) if callable(r) else r


def client(server, **kw):
    return O21Client("http://021/v1", "HACK1", "pw", transport=httpx.MockTransport(server), rate_per_s=1000, burst=100, **kw)


async def test_safe_call_retries_server_errors():
    s = Server({"GET /orders": [httpx.Response(503), httpx.Response(500), ok([{"orderId": "1"}])]})
    c = client(s)
    assert await c.get("/orders") == [{"orderId": "1"}]
    assert [k for k, _ in s.calls].count("GET /orders") == 3 and c.stats["server_errors"] == 2


async def test_order_placement_is_never_retried_blindly():
    for failure in (httpx.Response(500), httpx.Response(503), httpx.ReadTimeout("slow")):
        s = Server({"POST /orders": [failure, ok({"orderId": "X"})]})
        c = client(s)
        with pytest.raises(BrokerTimeout):
            await c.request("POST", "/orders", {"qty": 1}, safe=False)
        assert [k for k, _ in s.calls].count("POST /orders") == 1  # exactly one attempt: outcome must be verified


async def test_rate_limit_is_retried_even_for_orders():
    s = Server({"POST /orders": [httpx.Response(429, headers={"Retry-After": "0"}), ok({"orderId": "X"})]})
    c = client(s)
    assert (await c.request("POST", "/orders", {}, safe=False))["data"] == {"orderId": "X"}
    assert c.stats["rate_limited"] == 1


async def test_expired_token_relogin_once_for_concurrent_calls():
    def needs_tok2(req):
        return ok("fine") if req.headers["authorization"] == "Bearer tok2" else httpx.Response(401, json={"success": False})
    s = Server({"GET /portfolio/positions": [needs_tok2]})
    c = client(s)
    await c.login()
    res = await asyncio.gather(*(c.get("/portfolio/positions") for _ in range(5)))
    assert res == ["fine"] * 5
    assert s.logins == 2  # initial + exactly one re-login; concurrent calls do not revoke each other's token


async def test_business_rejection_and_exhausted_retries():
    s = Server({"POST /orders": [httpx.Response(400, json={"success": False, "data": None, "error": "insufficient funds"})],
                "GET /trades": [httpx.Response(503)]})
    c = client(s, max_retries=2)
    with pytest.raises(BrokerRejected, match="insufficient funds"):
        await c.request("POST", "/orders", {}, safe=False)
    with pytest.raises(BrokerUnavailable):
        await c.get("/trades")


async def test_missing_credentials():
    c = O21Client("http://021/v1", "", "", transport=httpx.MockTransport(lambda r: ok(None)))
    with pytest.raises(BrokerUnavailable, match="credentials"):
        await c.get("/orders")
