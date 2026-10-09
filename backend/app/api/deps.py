from __future__ import annotations

from fastapi import HTTPException, Request

from app.engine.system import TradingSystem


def get_system(request: Request) -> TradingSystem:
    return request.app.state.system


def bad_request(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))
