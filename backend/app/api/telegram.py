"""Telegram linking and alert preferences for the signed-in user."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import bad_request, get_system
from app.core.security import actor, current_user
from app.database.models import User
from app.engine.system import TradingSystem
from app.notifications.telegram import CATEGORIES

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


@router.get("")
async def status(user: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    return {**sys.telegram.info(user.id), "categories": list(CATEGORIES)}


@router.post("/link")
async def link(user: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    """One-time deep link (10 minutes) that binds the trader's Telegram chat to this user."""
    try:
        return await sys.telegram.create_link_code(user)
    except ValueError as e:
        raise bad_request(e)


@router.delete("/link")
async def unlink(user: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    await sys.telegram.unlink(user.id, actor(user))
    return sys.telegram.info(user.id)


class PrefsIn(BaseModel):
    muted: bool | None = None
    prefs: dict[str, bool] | None = None


@router.put("/prefs")
async def prefs(body: PrefsIn, user: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    try:
        await sys.telegram.set_prefs(user.id, body.muted, body.prefs)
    except ValueError as e:
        raise bad_request(e)
    return sys.telegram.info(user.id)


@router.post("/test")
async def test(user: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    chat = sys.telegram.by_user.get(user.id)
    if chat is None:
        raise bad_request(ValueError("Telegram is not linked for this user"))
    await sys.telegram.send(chat, "🔔 Test alert from AlgoTrade - alerts for your strategies arrive here.")
    return {"ok": True}
