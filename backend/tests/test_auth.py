"""Authentication, sessions and RBAC through the real HTTP API."""
from __future__ import annotations

import httpx
import pytest_asyncio

from app.engine.system import TradingSystem
from app.main import create_app
from tests.conftest import _truncate, make_settings


@pytest_asyncio.fixture
async def client():
    _truncate()
    app = create_app()
    sys_ = TradingSystem(make_settings())  # DB-backed; engine not needed for auth
    app.state.system = sys_
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    await sys_.db.dispose()


async def register(c, email, pw="password123"):
    return await c.post("/api/auth/register", json={"email": email, "name": "U", "password": pw})


async def test_first_user_is_admin_then_traders(client):
    r1 = await register(client, "a@x.io")
    r2 = await register(client, "b@x.io")
    assert r1.status_code == 200 and r1.json()["user"]["role"] == "ADMIN"
    assert r2.json()["user"]["role"] == "TRADER"
    assert (await register(client, "a@x.io")).status_code == 409


async def test_login_and_protected_routes(client):
    await register(client, "a@x.io")
    assert (await client.get("/api/auth/me")).status_code == 401
    bad = await client.post("/api/auth/login", json={"email": "a@x.io", "password": "wrongpass"})
    assert bad.status_code == 401
    ok = await client.post("/api/auth/login", json={"email": "a@x.io", "password": "password123"})
    tok = ok.json()["access_token"]
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert me.status_code == 200 and me.json()["email"] == "a@x.io"
    assert (await client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"})).status_code == 401


async def test_logout_revokes_session(client):
    tok = (await register(client, "a@x.io")).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert (await client.post("/api/auth/logout", headers=h)).status_code == 200
    assert (await client.get("/api/auth/me", headers=h)).status_code == 401


async def test_password_is_hashed(client):
    await register(client, "a@x.io", "supersecret1")
    sys_ = client._transport.app.state.system
    from sqlalchemy import select

    from app.database.models import User

    async with sys_.db.session() as s:
        u = (await s.execute(select(User))).scalar_one()
    assert u.password_hash.startswith("$2") and "supersecret1" not in u.password_hash


async def test_rbac_trader_cannot_admin(client):
    await register(client, "admin@x.io")
    tok = (await register(client, "t@x.io")).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    r = await client.post("/api/account/setup", json={"name": "x", "starting_capital": 1000000}, headers=h)
    assert r.status_code == 403
    assert (await client.get("/api/auth/users", headers=h)).status_code == 403
    assert (await client.post("/api/demo/reset", headers=h)).status_code == 403


async def test_input_validation(client):
    r = await client.post("/api/auth/register", json={"email": "not-an-email", "name": "U", "password": "password123"})
    assert r.status_code == 422
    r = await client.post("/api/auth/register", json={"email": "a@x.io", "name": "U", "password": "short"})
    assert r.status_code == 422
