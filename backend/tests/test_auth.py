import time

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core.auth import current_user

SECRET = "test-secret-with-at-least-32-bytes-of-length"


def request_with(token: str | None) -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "headers": headers})


def token(**claims) -> str:
    base = {"sub": "user-1", "email": "a@b.c", "aud": "authenticated", "exp": int(time.time()) + 60}
    return jwt.encode({**base, **claims}, SECRET, algorithm="HS256")


@pytest.fixture
def auth_settings(settings):
    return settings.model_copy(update={"auth_disabled": False, "supabase_jwt_secret": SECRET})


async def test_valid_token_returns_user(auth_settings):
    user = await current_user(request_with(token()), auth_settings)
    assert (user.id, user.email) == ("user-1", "a@b.c")


@pytest.mark.parametrize("bad", [None, "garbage", "expired", "wrong-audience"])
async def test_bad_tokens_are_401(auth_settings, bad):
    value = {"expired": token(exp=int(time.time()) - 10), "wrong-audience": token(aud="anon")}.get(bad, bad)
    with pytest.raises(HTTPException) as err:
        await current_user(request_with(value), auth_settings)
    assert err.value.status_code == 401
