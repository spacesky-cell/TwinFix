"""本地账号：密码哈希、登录换 token、JWT 鉴权。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from tests.conftest import Harness
from twinfix.api.auth import hash_password, verify_password
from twinfix.api.routes import router
from twinfix.config import Settings
from twinfix.domain.enums import UserRole
from twinfix.domain.models import User
from twinfix.ids import new_id
from twinfix.services import IncidentService

JWT_SECRET = "x" * 48


def test_password_roundtrip() -> None:
    digest = hash_password("s3cret-pass")
    assert digest.startswith("scrypt$")
    assert verify_password("s3cret-pass", digest)
    assert not verify_password("wrong", digest)
    assert not verify_password("x", None)
    assert not verify_password("x", "plain")
    assert hash_password("same") != hash_password("same")  # 每次新 salt


def _app(harness: Harness, settings: Settings, *, auth_disabled: bool) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings.model_copy(
        update={"auth_disabled": auth_disabled, "jwt_secret": SecretStr(JWT_SECRET)}
    )
    app.state.store = harness.rt.store
    app.state.service = IncidentService(harness.rt.store, harness.dispatcher)
    app.state.control = harness.rt.control
    app.include_router(router)
    return app


@pytest.fixture
async def auth_client(harness: Harness, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(harness, settings, auth_disabled=False)), base_url="http://t/api"
    ) as c:
        yield c


async def _user(harness: Harness, name: str, email: str, password: str) -> str:
    async with harness.rt.store.tx() as tx:
        user = User(name=name, email=email, role=UserRole.MAINTAINER, password_hash=hash_password(password))
        tx.session.add(user)
    return name


@pytest.mark.integration
async def test_login_and_me(auth_client: httpx.AsyncClient, harness: Harness) -> None:
    suffix = new_id()[-6:].lower()
    name, email = f"alice-{suffix}", f"alice-{suffix}@example.com"
    await _user(harness, name, email, "pw-123456")

    bad = await auth_client.post("/auth/login", json={"name": name, "password": "wrong-pass"})
    assert bad.status_code == 401
    missing = await auth_client.post("/auth/login", json={"name": f"nobody-{suffix}", "password": "pw-123456"})
    assert missing.status_code == 401

    r = await auth_client.post("/auth/login", json={"name": name, "password": "pw-123456"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert r.json()["user"]["role"] == "maintainer"

    assert (await auth_client.get("/auth/me")).status_code == 401
    me = await auth_client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["name"] == name

    # 邮箱也能当账号
    by_email = await auth_client.post("/auth/login", json={"name": email, "password": "pw-123456"})
    assert by_email.status_code == 200

    bad_token = await auth_client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert bad_token.status_code == 401


@pytest.mark.integration
async def test_auth_disabled_uses_local_admin(harness: Harness, settings: Settings) -> None:
    app = _app(harness, settings, auth_disabled=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/api") as c:
        r = await c.get("/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert {k: body[k] for k in ("id", "name", "role")} == {"id": "local", "name": "local", "role": "admin"}
