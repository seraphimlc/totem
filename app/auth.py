from __future__ import annotations

import secrets
from typing import Optional

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

_SALT = "tutan-cad-session"


def _serializer() -> URLSafeTimedSerializer:
    secret = config.SESSION_SECRET
    if not secret:
        raise RuntimeError("SESSION_SECRET is not set")
    return URLSafeTimedSerializer(secret, salt=_SALT)


def issue_session(username: str) -> str:
    return _serializer().dumps({"u": username})


def read_session(token: str) -> Optional[str]:
    try:
        data = _serializer().loads(token, max_age=config.SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired, Exception):
        return None
    user = data.get("u") if isinstance(data, dict) else None
    return user if isinstance(user, str) and user else None


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=config.SESSION_COOKIE,
        value=token,
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=config.SESSION_SECURE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(config.SESSION_COOKIE, path="/")


def credentials_ok(username: str, password: str) -> bool:
    if not config.ACCESS_USERNAME or not config.ACCESS_PASSWORD:
        return False
    # Single shared account for v1.
    u_ok = secrets.compare_digest(username or "", config.ACCESS_USERNAME)
    p_ok = secrets.compare_digest(password or "", config.ACCESS_PASSWORD)
    return u_ok and p_ok


def current_user(request: Request) -> Optional[str]:
    token = request.cookies.get(config.SESSION_COOKIE)
    if not token:
        return None
    return read_session(token)


def require_user(request: Request) -> str:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录后再使用工作台。")
    return user
