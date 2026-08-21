import base64
import binascii
import hashlib
import hmac
import json
import time
from os import getenv
from secrets import compare_digest
from typing import Any

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel

ADMIN_ROLE = "admin"
SESSION_COOKIE = "stacksniffer_admin_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
SESSION_COOKIE_PATH = "/api"


class AdminLoginRequest(BaseModel):
    username: str
    password: str


def admin_username() -> str:
    return getenv("ADMIN_USERNAME", "admin")


def admin_password() -> str:
    password = getenv("ADMIN_PASSWORD")
    if not password:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD is not configured")
    return password


def _signing_secret() -> str:
    return getenv("REVIEW_SESSION_SECRET") or admin_password()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode())


def _json_b64url(data: dict[str, Any]) -> str:
    return _b64url_encode(json.dumps(data, separators=(",", ":")).encode())


def create_admin_token(subject: str) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": subject,
        "role": ADMIN_ROLE,
        "iat": now,
        "exp": now + SESSION_TTL_SECONDS,
    }
    unsigned = f"{_json_b64url(header)}.{_json_b64url(payload)}"
    signature = hmac.new(
        _signing_secret().encode(),
        unsigned.encode(),
        hashlib.sha256,
    ).digest()
    return f"{unsigned}.{_b64url_encode(signature)}"


def verify_admin_token(token: str | None) -> dict[str, Any] | None:
    try:
        header_b64, payload_b64, signature_b64 = (token or "").split(".", 2)
        unsigned = f"{header_b64}.{payload_b64}"
        expected = hmac.new(
            _signing_secret().encode(),
            unsigned.encode(),
            hashlib.sha256,
        ).digest()
        if not compare_digest(_b64url_decode(signature_b64), expected):
            return None
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        if header.get("alg") != "HS256" or payload.get("exp", 0) <= int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error):
        return None


def _request_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token.strip()
    return request.cookies.get(SESSION_COOKIE)


def require_admin(request: Request) -> str:
    payload = verify_admin_token(_request_token(request))
    if not payload or payload.get("role") != ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Admin session required")
    return str(payload.get("sub") or ADMIN_ROLE)


def create_admin_session(response: Response, username: str, password: str) -> dict[str, Any]:
    if not (
        compare_digest(username, admin_username())
        and compare_digest(password, admin_password())
    ):
        raise HTTPException(status_code=403, detail="Invalid admin credentials")
    token = create_admin_token(username)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=getenv("REVIEW_COOKIE_SECURE", "false").lower() == "true",
        samesite="lax",
        path=SESSION_COOKIE_PATH,
    )
    return {
        "authenticated": True,
        "access_token": token,
        "token_type": "bearer",
        "role": ADMIN_ROLE,
        "expires_in": SESSION_TTL_SECONDS,
    }


def clear_admin_session(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE, path=SESSION_COOKIE_PATH)
    return {"authenticated": False}
