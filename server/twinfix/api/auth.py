"""鉴权：JWT（HS256，本地账号）。TWINFIX_AUTH_DISABLED=true 时以本地管理员身份放行，仅限开发。"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status

from twinfix.config import Settings
from twinfix.domain.enums import UserRole


@dataclass(frozen=True)
class Principal:
    user_id: str | None
    name: str
    role: UserRole

    @property
    def actor(self) -> str:
        return f"user:{self.user_id or self.name}"


LOCAL_ADMIN = Principal(user_id=None, name="local", role=UserRole.ADMIN)

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """scrypt 哈希，格式 `scrypt$n$r$p$salt_hex$digest_hex`；不引入额外依赖。"""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# 账号不存在时也做一次等价开销的校验，避免用响应时间枚举用户名
DUMMY_PASSWORD_HASH = hash_password("twinfix-dummy-password")


def issue_token(settings: Settings, user_id: str, name: str, role: UserRole, ttl_s: int = 12 * 3600) -> str:
    secret = settings.jwt_secret.get_secret_value()
    if not secret:
        raise RuntimeError("TWINFIX_JWT_SECRET is not configured")
    now = datetime.now(UTC)
    claims = {"sub": user_id, "name": name, "role": role.value, "iat": now, "exp": now + timedelta(seconds=ttl_s)}
    return jwt.encode(claims, secret, algorithm="HS256")


def current_principal(request: Request) -> Principal:
    settings: Settings = request.app.state.settings
    if settings.auth_disabled:
        return LOCAL_ADMIN
    header = request.headers.get("authorization", "")
    # EventSource 无法设置请求头，SSE 允许用 query 参数携带 token
    token = (
        header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else request.query_params.get("token")
    )
    secret = settings.jwt_secret.get_secret_value()
    if not token or not secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
        return Principal(user_id=claims["sub"], name=claims.get("name", ""), role=UserRole(claims["role"]))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_role(*roles: UserRole):  # type: ignore[no-untyped-def]
    def dep(p: CurrentPrincipal) -> Principal:
        if p.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role {', '.join(r.value for r in roles)}")
        return p

    return Depends(dep)


Maintainer = Annotated[Principal, require_role(UserRole.ADMIN, UserRole.MAINTAINER)]
Admin = Annotated[Principal, require_role(UserRole.ADMIN)]
