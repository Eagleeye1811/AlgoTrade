"""Password hashing (bcrypt), JWT access tokens bound to a persistent server-side session (revocable), RBAC."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.config import get_settings
from app.database.models import User, UserSession

bearer = HTTPBearer(auto_error=False)


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt(rounds=12)).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    except ValueError:
        return False


def create_token(user: User) -> tuple[str, str, datetime]:
    s = get_settings()
    jti = uuid.uuid4().hex
    exp = datetime.now(timezone.utc) + timedelta(minutes=s.jwt_ttl_minutes)
    token = jwt.encode({"sub": str(user.id), "role": user.role, "jti": jti, "exp": exp}, s.jwt_secret,
                       algorithm=s.jwt_algorithm)
    return token, jti, exp


def decode_token(token: str) -> dict:
    s = get_settings()
    try:
        return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")


async def _user_from_token(request_app, token: str) -> tuple[User, str]:
    claims = decode_token(token)
    db = request_app.state.system.db
    async with db.session() as s:
        sess = await s.get(UserSession, claims["jti"])
        if sess is None or sess.revoked_at is not None or sess.expires_at < datetime.now(timezone.utc):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session revoked or expired")
        user = (await s.execute(select(User).where(User.id == int(claims["sub"])))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user disabled")
    return user, claims["jti"]


async def current_user(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> User:
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    user, jti = await _user_from_token(request.app, cred.credentials)
    request.state.jti = jti
    return user


async def ws_user(ws: WebSocket, token: str) -> User:
    user, _ = await _user_from_token(ws.app, token)
    return user


def require_role(*roles: str):
    async def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role: {' or '.join(roles)}")
        return user

    return dep


require_admin = require_role("ADMIN")
require_trader = require_role("ADMIN", "TRADER")


def actor(user: User) -> str:
    return f"{user.email} ({user.role})"
