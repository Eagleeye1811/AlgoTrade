from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import bad_request, get_system
from app.core.security import actor, current_user, require_admin, require_trader
from app.database.models import User
from app.engine.system import TradingSystem

router = APIRouter(prefix="/api", tags=["account"])


def account_view(sys: TradingSystem) -> dict:
    a = sys.account
    snap = sys.pnl.last or sys.pnl.compute()
    return {
        "configured": a is not None,
        "account": None if a is None else {
            "id": a.id, "name": a.name, "account_type": a.account_type, "broker": a.broker,
            "broker_account_id": a.broker_account_id, "starting_capital": a.starting_capital, "created_at": a.created_at},
        "execution_mode": "PAPER TRADING - LOCAL SIMULATOR" if sys.is_paper else "021 DEVELOPER SANDBOX - SIMULATED EXCHANGE",
        "broker_note": ("Orders are filled by the local paper-trading simulator against the market feed. No real orders are placed "
                        "and no real money is traded." if sys.is_paper else
                        "Orders go to the 021 developer sandbox (POST /orders): virtual funds on a simulated exchange. "
                        "No real money is traded."),
        "portfolio": snap["portfolio"],
        "allocations": {sid: {"name": rt.name, "allocation": rt.allocation} for sid, rt in sys.strategies.runtimes.items()},
        "broker": sys.broker.info(),
        "market": sys.market.info(),
        "trading": sys.controller.snapshot(),
    }


@router.get("/account")
async def get_account(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    return account_view(sys)


class SetupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    starting_capital: Decimal = Field(gt=0, le=Decimal("1000000000"))
    allocations: dict[str, Decimal] | None = None


@router.post("/account/setup")
async def setup(body: SetupIn, user: User = Depends(require_admin), sys: TradingSystem = Depends(get_system)):
    try:
        await sys.setup_account(body.name, body.starting_capital, body.allocations, actor(user), user.id)
    except ValueError as e:
        raise bad_request(e)
    return account_view(sys)


@router.get("/overview")
async def overview(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    snap = sys.pnl.last or sys.pnl.compute()
    return {
        "trading": sys.controller.snapshot(),
        "market": sys.market.info(),
        "broker": sys.broker.info(),
        "health": {"overall": sys.health.overall(), "components": sys.health.snapshot()},
        "portfolio": snap["portfolio"],
        "strategies": snap["strategies"],
        "kill_switch": sys.kill_switch.snapshot(),
        "reconciliation": {"status": sys.reconciler.status, "last": sys.reconciler.last},
        "account_configured": sys.account is not None,
        "execution_mode": "PAPER TRADING - LOCAL SIMULATOR" if sys.is_paper else "021 DEVELOPER SANDBOX - SIMULATED EXCHANGE",
    }


@router.post("/trading/start")
async def start_trading(user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    try:
        await sys.start_trading(actor(user))
    except ValueError as e:
        raise bad_request(e)
    return sys.controller.snapshot()


@router.post("/trading/pause")
async def pause_trading(user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    try:
        await sys.pause_trading(actor(user))
    except ValueError as e:
        raise bad_request(e)
    return sys.controller.snapshot()


class KillIn(BaseModel):
    reason: str = Field(default="manual activation from dashboard", max_length=300)


@router.get("/kill-switch")
async def kill_state(_: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    return sys.kill_switch.snapshot()


@router.post("/kill-switch/activate")
async def kill(body: KillIn, user: User = Depends(require_trader), sys: TradingSystem = Depends(get_system)):
    return await sys.kill_switch.activate(actor(user), body.reason)


@router.post("/kill-switch/retry-close")
async def kill_retry(user: User = Depends(require_admin), sys: TradingSystem = Depends(get_system)):
    try:
        return await sys.kill_switch.retry_close(actor(user))
    except ValueError as e:
        raise bad_request(e)


@router.post("/kill-switch/reset")
async def kill_reset(user: User = Depends(require_admin), sys: TradingSystem = Depends(get_system)):
    try:
        return await sys.kill_switch.reset(actor(user))
    except ValueError as e:
        raise bad_request(e)
