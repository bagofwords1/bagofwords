"""e2e: SSO hands the browser a one-time code, never the session token.

The SSO callback used to redirect to ``/users/sign-in?access_token=<jwt>``,
putting a 7-day credential into browser history, Referer headers and the access
log of every proxy in front of a self-hosted install. It now redirects with a
single-use code that the SPA trades for the token over POST.

The exchange route lives on the SSO router, which the test config (auth mode
``local_only``) does not mount — so these tests mount that router on their own
app. The token it returns is still checked against the real application, which
is what makes the round trip meaningful.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from starlette.testclient import TestClient

from app.models.user import User
from app.routes import auth as auth_routes
from app.services.login_exchange_service import issue_login_code

pytestmark = pytest.mark.e2e


@pytest.fixture
def sso_client():
    """A minimal app exposing the SSO auth router (incl. /auth/exchange)."""
    app = FastAPI()
    app.include_router(auth_routes.router, prefix="/api")
    return TestClient(app)


async def _load_user(email: str) -> User:
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        return (
            await db.execute(select(User).where(User.email == email))
        ).scalar_one()


def _register(create_user):
    email = f"exch_{uuid.uuid4().hex[:10]}@test.com"
    create_user(email=email, password="Test1234!")
    return email


@pytest.mark.asyncio
async def test_sso_redirect_carries_a_code_and_not_the_token(create_user):
    """The regression that matters: no session JWT anywhere in the redirect."""
    from app.services.auth_providers import _login_redirect

    user = await _load_user(_register(create_user))
    location = (await _login_redirect(user)).headers["location"]

    assert "login_code=" in location
    assert "access_token" not in location
    # A JWT is three base64 segments joined by dots — nothing like it may appear
    # in a URL, under any parameter name.
    assert location.count(".") == 0 or "eyJ" not in location


@pytest.mark.asyncio
async def test_exchanged_code_yields_a_working_session(create_user, sso_client, test_client):
    """The other half of the contract: the browser really can trade the code for
    a token, and that token authenticates against the real API."""
    from app.services.auth_providers import _login_redirect

    email = _register(create_user)
    user = await _load_user(email)
    location = (await _login_redirect(user)).headers["location"]
    code = location.split("login_code=")[1].split("&")[0]

    resp = sso_client.post("/api/auth/exchange", json={"login_code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]

    me = test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email


@pytest.mark.asyncio
async def test_a_code_can_only_be_exchanged_once(create_user, sso_client):
    """A code scraped from a log after the browser used it is worthless."""
    user = await _load_user(_register(create_user))
    code = await issue_login_code(str(user.id), "a-token")

    assert sso_client.post("/api/auth/exchange", json={"login_code": code}).status_code == 200
    replay = sso_client.post("/api/auth/exchange", json={"login_code": code})
    assert replay.status_code == 400
    assert "access_token" not in replay.text


@pytest.mark.asyncio
async def test_expired_code_is_refused(create_user, sso_client):
    from sqlalchemy import update

    from app.dependencies import async_session_maker
    from app.models.login_exchange_code import LoginExchangeCode

    user = await _load_user(_register(create_user))
    code = await issue_login_code(str(user.id), "a-token")

    # Age the row past its lifetime. Done in SQL because the service deliberately
    # exposes no way to mint a code with a caller-chosen expiry.
    async with async_session_maker() as db:
        await db.execute(
            update(LoginExchangeCode).values(
                expires_at=datetime.utcnow() - timedelta(seconds=1)
            )
        )
        await db.commit()

    assert sso_client.post("/api/auth/exchange", json={"login_code": code}).status_code == 400


@pytest.mark.parametrize("bogus", ["", "not-a-real-code", "x" * 64])
def test_unknown_codes_are_refused(sso_client, bogus):
    resp = sso_client.post("/api/auth/exchange", json={"login_code": bogus})
    assert resp.status_code == 400
    assert "access_token" not in resp.text
