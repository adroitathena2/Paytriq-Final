"""Verifies the Supabase access token sent by the frontend as ``Authorization: Bearer <jwt>``.

New Supabase projects sign tokens with asymmetric keys published at ``/auth/v1/.well-known/jwks.json``;
older projects use the shared HS256 "JWT secret". Both are supported. ``AUTH_DISABLED=true`` is for
local development only and maps every request to a fixed dev user.
"""

from __future__ import annotations

from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.core.config import Settings, get_settings

DEV_USER_ID = "00000000-0000-0000-0000-000000000001"


class User(BaseModel):
    id: str
    email: str | None = None


@lru_cache
def _jwks_client(supabase_url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json", cache_keys=True)


async def decode_token(token: str, settings: Settings) -> dict:
    if settings.supabase_jwt_secret:
        return jwt.decode(token, settings.supabase_jwt_secret, algorithms=["HS256"], audience="authenticated")
    if not settings.supabase_url:
        raise HTTPException(500, "Server auth is not configured (SUPABASE_URL missing)")
    signing_key = await run_in_threadpool(_jwks_client(settings.supabase_url).get_signing_key_from_jwt, token)
    return jwt.decode(token, signing_key.key, algorithms=["ES256", "RS256"], audience="authenticated")


async def current_user(request: Request, settings: Settings = Depends(get_settings)) -> User:
    if settings.auth_disabled:
        return User(id=DEV_USER_ID, email="dev@localhost")
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in required")
    try:
        claims = await decode_token(header.split(" ", 1)[1], settings)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(401, f"Invalid or expired session: {type(exc).__name__}") from exc
    return User(id=claims["sub"], email=claims.get("email"))
