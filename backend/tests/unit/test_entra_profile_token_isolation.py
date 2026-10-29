"""Directory sync must not overwrite data-source login assertions."""

import asyncio
import time
import uuid
from urllib.parse import parse_qs

import httpx
import pytest

import main  # noqa: F401
from app.dependencies import async_session_maker
from app.ee.oidc.profile_service import fetch_profile_fields, sync_profile_on_login
from app.models.membership import Membership
from app.models.oauth_account import OAuthAccount
from app.models.organization import Organization
from app.models.user import User
from app.settings.bow_config import OIDCProvider
from app.settings.config import settings

pytestmark = pytest.mark.db


@pytest.mark.parametrize("role", ["admin", "member"])
@pytest.mark.parametrize("fresh_login", [True, False])
def test_directory_sync_with_separate_client_preserves_fabric_assertion(monkeypatch, role, fresh_login):
    provider = "entra-" + uuid.uuid4().hex
    monkeypatch.setattr(
        settings.bow_config,
        "oidc_providers",
        [
            OIDCProvider(
                name=provider,
                enabled=True,
                issuer="https://login.microsoftonline.com/test/v2.0",
                client_id="sso-client",
                client_secret="sso-secret",
                scopes=["openid", "profile", "email", "offline_access", "api://fabric-client/access"],
            )
        ],
    )
    original_client = httpx.AsyncClient

    def respond(request):
        if "well-known" in str(request.url):
            return httpx.Response(200, json={"token_endpoint": "https://login.microsoftonline.com/test/token"})
        if request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form["grant_type"] != "refresh_token":
                return httpx.Response(400, json={"error_codes": [50013]})
            assert form["client_id"] == "sso-client"
            assert form["refresh_token"] in {"sso-refresh", "rotated-sso-refresh"}
            assert "https://graph.microsoft.com/User.Read" in form["scope"].split()
            assert "api://fabric-client/access" not in form["scope"]
            return httpx.Response(
                200, json={"access_token": "graph-access", "refresh_token": "rotated-sso-refresh", "expires_in": 3600}
            )
        if request.headers.get("authorization") != "Bearer graph-access":
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        return httpx.Response(200, json={"department": "Engineering", "jobTitle": "Analyst"})

    def client(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "AsyncClient", client)

    async def scenario():
        async with async_session_maker() as db:
            # Historical OAuth state cannot be produced by registration APIs without Entra.
            user = User(
                name="Directory test", email=uuid.uuid4().hex + "@example.com", hashed_password="unused", is_active=True
            )
            org = Organization(name="Directory isolation")
            db.add_all([user, org])
            await db.flush()
            membership = Membership(user_id=user.id, organization_id=org.id, role=role)
            expiry = int(time.time()) + (3600 if fresh_login else -60)
            account = OAuthAccount(
                user_id=user.id,
                oauth_name=provider,
                account_id=str(uuid.uuid4()),
                account_email=user.email,
                access_token="fabric-assertion",
                refresh_token="sso-refresh",
                expires_at=expiry,
            )
            db.add_all([membership, account])
            await db.commit()
            if fresh_login:
                result = await sync_profile_on_login(db, user, org.id, ["department", "jobTitle"], "fabric-assertion")
                await db.refresh(membership)
                assert membership.profile_attributes == result
            else:
                result = await fetch_profile_fields(db, user, ["department", "jobTitle"])
            assert result["department"] == "Engineering"
            await db.refresh(account)
            assert account.access_token == "fabric-assertion"
            assert account.expires_at == expiry
            assert account.refresh_token == "rotated-sso-refresh"
            assert (await fetch_profile_fields(db, user, ["department"]))["department"] == "Engineering"
            await db.refresh(account)
            assert account.access_token == "fabric-assertion"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case",
    ["graph_login", "same_client_obo", "no_refresh", "revoked_refresh", "empty_access", "timeout", "concurrent_login"],
)
def test_directory_auth_fallbacks_preserve_login_and_existing_profile(monkeypatch, case, caplog):
    import logging

    from sqlalchemy import update

    from app.ee.oidc.profile_service import EntraReauthRequired

    provider = "entra-" + uuid.uuid4().hex
    monkeypatch.setattr(
        settings.bow_config,
        "oidc_providers",
        [
            OIDCProvider(
                name=provider,
                enabled=True,
                issuer="https://login.microsoftonline.com/fallback/v2.0",
                client_id="sso-client",
                client_secret="secret",
                scopes=["openid", "api://fabric-client/access"],
            )
        ],
    )
    logging.getLogger("app.ee.oidc.profile_service").disabled = False
    private = "private-token-must-not-appear"
    original_client = httpx.AsyncClient
    account_id = None

    async def respond(request):
        if "well-known" in str(request.url):
            return httpx.Response(200, json={"token_endpoint": "https://login.microsoftonline.com/fallback/token"})
        if request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if case == "timeout":
                raise httpx.ReadTimeout(private, request=request)
            if case == "concurrent_login":
                async with async_session_maker() as other:
                    await other.execute(
                        update(OAuthAccount)
                        .where(OAuthAccount.id == account_id)
                        .values(access_token="new-login", refresh_token="new-refresh", expires_at=2000000001)
                    )
                    await other.commit()
                return httpx.Response(200, json={"access_token": "graph-access", "refresh_token": "stale-result"})
            if case == "same_client_obo" and form["grant_type"].endswith("jwt-bearer"):
                return httpx.Response(
                    200, json={"access_token": "graph-access", "refresh_token": "obo-refresh-not-login-refresh"}
                )
            if case == "empty_access":
                return httpx.Response(200, json={"access_token": "", "refresh_token": "must-not-save"})
            return httpx.Response(400, json={"error_codes": [65001], "error_description": private})
        if request.headers.get("authorization") == "Bearer graph-access":
            return httpx.Response(200, json={"department": "New department"})
        return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})

    def client(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "AsyncClient", client)

    async def scenario():
        nonlocal account_id
        async with async_session_maker() as db:
            # Historical external-login state, unavailable through local registration.
            user = User(
                name="Existing user", email=uuid.uuid4().hex + "@example.com", hashed_password="unused", is_active=True
            )
            org = Organization(name=uuid.uuid4().hex)
            db.add_all([user, org])
            await db.flush()
            membership = Membership(
                user_id=user.id,
                organization_id=org.id,
                role="member",
                profile_attributes={"department": "Previous department"},
            )
            original = "graph-access" if case == "graph_login" else "login-assertion"
            refresh = None if case in {"graph_login", "same_client_obo", "no_refresh"} else "login-refresh"
            account = OAuthAccount(
                user_id=user.id,
                oauth_name=provider,
                account_id=str(uuid.uuid4()),
                account_email=user.email,
                access_token=original,
                refresh_token=refresh,
                expires_at=2000000000,
            )
            db.add_all([membership, account])
            await db.commit()
            account_id = account.id
            success = case in {"graph_login", "same_client_obo", "concurrent_login"}
            if success:
                assert (await sync_profile_on_login(db, user, org.id, ["department"], original))[
                    "department"
                ] == "New department"
            else:
                with pytest.raises(EntraReauthRequired):
                    await sync_profile_on_login(db, user, org.id, ["department"], original)
                await db.refresh(membership)
                assert membership.profile_attributes == {"department": "Previous department"}
            await db.refresh(account)
            assert account.access_token == ("new-login" if case == "concurrent_login" else original)
            assert account.refresh_token == ("new-refresh" if case == "concurrent_login" else refresh)
            assert account.expires_at == (2000000001 if case == "concurrent_login" else 2000000000)

    asyncio.run(scenario())
    assert private not in caplog.text
