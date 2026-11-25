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


# ── popup → frame handoff ────────────────────────────────────────────────────
# A framed BOW signs in through a popup. The popup gets the login code, the
# frame has to do the exchange, and the two may share no browser channel.


def _challenge(verifier: str) -> str:
    import hashlib

    return hashlib.sha256(verifier.encode()).hexdigest()


@pytest.mark.asyncio
async def test_handed_off_code_is_redeemed_by_the_verifier_holder(create_user, sso_client, test_client):
    """The frame, holding only its own verifier, ends up with the user's session."""
    email = _register(create_user)
    user = await _load_user(email)
    token = test_client.post(
        "/api/auth/jwt/login", data={"username": email, "password": "Test1234!"}
    ).json()["access_token"]
    code = await issue_login_code(str(user.id), token)
    verifier = uuid.uuid4().hex + uuid.uuid4().hex
    status_url = f"/api/auth/exchange/handoff/{_challenge(verifier)}"

    assert sso_client.get(status_url).json() == {"status": "pending"}
    handoff = sso_client.post(
        "/api/auth/exchange/handoff",
        json={"login_code": code, "code_challenge": _challenge(verifier)},
    )
    assert handoff.status_code == 204, handoff.text
    assert sso_client.get(status_url).json() == {"status": "ready"}

    resp = sso_client.post("/api/auth/exchange", json={"login_code": verifier})
    assert resp.status_code == 200, resp.text
    me = test_client.get(
        "/api/users/whoami", headers={"Authorization": f"Bearer {resp.json()['access_token']}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email
    # What tells the popup it may close.
    assert sso_client.get(status_url).json() == {"status": "consumed"}


@pytest.mark.asyncio
async def test_handoff_spends_the_login_code(create_user, sso_client):
    """After a handoff the popup's code opens nothing, and neither does the
    challenge: only the verifier does."""
    user = await _load_user(_register(create_user))
    code = await issue_login_code(str(user.id), "a-token")
    challenge = _challenge("the-frames-secret-verifier")

    assert sso_client.post(
        "/api/auth/exchange/handoff", json={"login_code": code, "code_challenge": challenge}
    ).status_code == 204
    for spent in (code, challenge):
        replay = sso_client.post("/api/auth/exchange", json={"login_code": spent})
        assert replay.status_code == 400
        assert "access_token" not in replay.text
    again = sso_client.post(
        "/api/auth/exchange/handoff",
        json={"login_code": code, "code_challenge": _challenge("someone-else")},
    )
    assert again.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("challenge", ["", "abc", "Z" * 64, "A" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n"])
async def test_malformed_challenge_is_refused_and_the_code_survives(create_user, sso_client, challenge):
    """A refused handoff must not burn the code the browser still needs."""
    user = await _load_user(_register(create_user))
    code = await issue_login_code(str(user.id), "a-token")

    resp = sso_client.post(
        "/api/auth/exchange/handoff", json={"login_code": code, "code_challenge": challenge}
    )
    assert resp.status_code == 400
    assert sso_client.post("/api/auth/exchange", json={"login_code": code}).status_code == 200


@pytest.mark.asyncio
async def test_challenge_already_in_use_is_refused_and_the_code_survives(create_user, sso_client):
    """Two handoffs to one challenge: the second is refused whole, so its code
    is not consumed on the way to failing."""
    user = await _load_user(_register(create_user))
    first = await issue_login_code(str(user.id), "first-token")
    second = await issue_login_code(str(user.id), "second-token")
    challenge = _challenge("one-frame")

    assert sso_client.post(
        "/api/auth/exchange/handoff", json={"login_code": first, "code_challenge": challenge}
    ).status_code == 204
    assert sso_client.post(
        "/api/auth/exchange/handoff", json={"login_code": second, "code_challenge": challenge}
    ).status_code == 400
    kept = sso_client.post("/api/auth/exchange", json={"login_code": second})
    assert kept.status_code == 200
    assert kept.json()["access_token"] == "second-token"


def test_unknown_login_code_cannot_be_handed_off(sso_client):
    challenge = _challenge("a-frame")
    resp = sso_client.post(
        "/api/auth/exchange/handoff", json={"login_code": "not-a-real-code", "code_challenge": challenge}
    )
    assert resp.status_code == 400
    assert sso_client.get(f"/api/auth/exchange/handoff/{challenge}").json() == {"status": "pending"}


@pytest.mark.asyncio
async def test_a_handoff_nobody_took_reads_as_expired_not_as_taken(create_user, sso_client):
    """The popup closes once the frame took the session. A handoff that simply
    ran out must not look like that, or the popup closes on a sign-in that
    never reached the frame."""
    from sqlalchemy import update

    from app.dependencies import async_session_maker
    from app.models.login_exchange_code import LoginExchangeCode

    user = await _load_user(_register(create_user))
    code = await issue_login_code(str(user.id), "a-token")
    challenge = _challenge("a-frame-that-went-away")
    status_url = f"/api/auth/exchange/handoff/{challenge}"
    assert sso_client.post(
        "/api/auth/exchange/handoff", json={"login_code": code, "code_challenge": challenge}
    ).status_code == 204
    assert sso_client.get(status_url).json() == {"status": "ready"}

    async with async_session_maker() as db:
        await db.execute(
            update(LoginExchangeCode)
            .where(LoginExchangeCode.code_hash == challenge)
            .values(expires_at=datetime.utcnow() - timedelta(seconds=1))
        )
        await db.commit()

    assert sso_client.get(status_url).json() == {"status": "expired"}
    assert sso_client.post("/api/auth/exchange", json={"login_code": "a-frame-that-went-away"}).status_code == 400

