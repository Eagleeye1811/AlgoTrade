from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update

from app.api.deps import get_system
from app.core.security import actor, create_token, current_user, hash_password, require_admin, verify_password
from app.database.models import User, UserSession
from app.engine.system import TradingSystem

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


def user_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "created_at": u.created_at}


async def _issue(sys: TradingSystem, u: User) -> dict:
    token, jti, exp = create_token(u)
    async with sys.db.transaction() as s:
        s.add(UserSession(jti=jti, user_id=u.id, expires_at=exp))
    return {"access_token": token, "token_type": "bearer", "expires_at": exp, "user": user_out(u)}


@router.post("/register")
async def register(body: RegisterIn, sys: TradingSystem = Depends(get_system)):
    email = body.email.lower()
    async with sys.db.transaction() as s:
        if (await s.execute(select(User).where(User.email == email))).scalar_one_or_none():
            raise HTTPException(409, "email already registered")
        n = (await s.execute(select(func.count()).select_from(User))).scalar_one()
        # The first account becomes ADMIN (bootstraps the deployment); everyone after is TRADER.
        u = User(email=email, name=body.name.strip(), password_hash=hash_password(body.password),
                 role="ADMIN" if n == 0 else "TRADER")
        s.add(u)
    await sys.audit.record("USER_REGISTERED", f"user {email} registered as {u.role}", source="Auth", actor=email)
    return await _issue(sys, u)


@router.post("/login")
async def login(body: LoginIn, sys: TradingSystem = Depends(get_system)):
    async with sys.db.session() as s:
        u = (await s.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none()
    if u is None or not verify_password(body.password, u.password_hash) or not u.is_active:
        await sys.audit.record("LOGIN_FAILED", f"failed login for {body.email.lower()}", source="Auth", severity="WARN",
                               broadcast=False)
        raise HTTPException(401, "invalid email or password")
    await sys.audit.record("USER_LOGIN", f"{u.email} logged in", source="Auth", actor=u.email, broadcast=False)
    return await _issue(sys, u)


@router.post("/logout")
async def logout(request: Request, user: User = Depends(current_user), sys: TradingSystem = Depends(get_system)):
    async with sys.db.transaction() as s:
        await s.execute(update(UserSession).where(UserSession.jti == request.state.jti)
                        .values(revoked_at=datetime.now(timezone.utc)))
    await sys.audit.record("USER_LOGOUT", f"{user.email} logged out", source="Auth", actor=user.email, broadcast=False)
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return user_out(user)


@router.get("/users")
async def users(_: User = Depends(require_admin), sys: TradingSystem = Depends(get_system)):
    async with sys.db.session() as s:
        rows = (await s.execute(select(User).order_by(User.id))).scalars().all()
    return [user_out(u) for u in rows]


class RoleIn(BaseModel):
    role: str = Field(pattern="^(ADMIN|TRADER)$")


@router.put("/users/{user_id}/role")
async def set_role(user_id: int, body: RoleIn, admin: User = Depends(require_admin), sys: TradingSystem = Depends(get_system)):
    if user_id == admin.id:
        raise HTTPException(400, "cannot change your own role")
    async with sys.db.transaction() as s:
        u = await s.get(User, user_id)
        if u is None:
            raise HTTPException(404, "user not found")
        u.role = body.role
    await sys.audit.record("USER_ROLE_CHANGED", f"{u.email} role -> {body.role}", source="Auth", actor=actor(admin))
    return user_out(u)
