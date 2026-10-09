"""AlgoTrade - Fault-Tolerant Algorithmic Trading & Risk Control Platform (FastAPI entrypoint)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from app.api import account, audit, auth, market, orders, resilience, risk, strategies, trades
from app.config import get_settings
from app.core.security import ws_user
from app.engine.system import TradingSystem

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.env != "dev" and settings.jwt_secret == "change-me-in-.env":
        raise RuntimeError("ALGOTRADE_JWT_SECRET must be set outside dev")
    system = TradingSystem(settings)
    app.state.system = system
    await system.startup()
    try:
        yield
    finally:
        await system.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="AlgoTrade", version="1.0.0", lifespan=lifespan,
                  description="Fault-tolerant algorithmic trading & risk control platform (021 developer sandbox)")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    for r in (auth, account, market, strategies, orders, trades, risk, resilience, audit):
        app.include_router(r.router)

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, token: str = Query(...)):
        try:
            user = await ws_user(ws, token)
        except HTTPException:
            await ws.close(code=4401)
            return
        await ws.accept()
        await app.state.system.ws.serve(ws, user.email)

    return app


app = create_app()
