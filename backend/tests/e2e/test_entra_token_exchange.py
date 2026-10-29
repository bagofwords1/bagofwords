"""Signed Microsoft boundary fixtures; real OAuth routes, authorization and DB."""

import asyncio
import time
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.oauth_account import OAuthAccount
from app.settings.bow_config import OIDCProvider
from app.settings.config import settings

GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
TYPE = "urn:ietf:params:oauth:token-type:access_token"


@pytest.fixture
def entra(monkeypatch):
    tenant, audience, external, oid = [str(uuid.uuid4()) for _ in range(4)]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    import json

    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test-key"
    cfg = OIDCProvider(
        name="exchange-test",
        enabled=True,
        issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
        client_id=audience,
        client_secret="synthetic",
        scopes=["openid", f"api://{audience}/access_as_user"],
    )
    monkeypatch.setattr(settings.bow_config, "oidc_providers", [cfg])

    async def get(self, url, **kwargs):
        version = "/v2.0/" in url
        issuer = cfg.issuer if version else f"https://sts.windows.net/{tenant}/"
        data = (
            {"issuer": issuer, "jwks_uri": f"https://login.microsoftonline.com/{tenant}/keys"}
            if "openid-configuration" in url
            else {"keys": [jwk]}
        )
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", get)

    def signed(**changes):
        now = int(time.time())
        claims = {
            "ver": "2.0",
            "tid": tenant,
            "oid": oid,
            "sub": "pairwise-sub",
            "aud": audience,
            "iss": cfg.issuer,
            "azp": external,
            "scp": "access_as_user",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        }
        claims.update(changes)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})

    return cfg, external, oid, signed


def setup_app(test_client, create_user, login_user, whoami, entra):
    cfg, external, oid, signed = entra
    user = create_user()
    login = login_user(user["email"], user["password"])
    info = whoami(login)
    org = info["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {login}", "X-Organization-Id": org}
    response = test_client.post(
        "/api/oauth/clients",
        headers=headers,
        json={
            "name": "External test",
            "scopes": "app",
            "redirect_uris": ["http://localhost/callback"],
            "entra_exchange": {"provider": cfg.name, "external_client_id": external},
        },
    )
    assert response.status_code == 200, response.text
    app = response.json()

    # External identity originates from SSO, unavailable through local signup.
    # Seed the verified account binding to isolate token exchange from browser SSO.
    async def link():
        async with async_session_maker() as db:
            db.add(
                OAuthAccount(
                    user_id=info["id"],
                    oauth_name=cfg.name,
                    account_id="pairwise-sub",
                    account_email=user["email"],
                    access_token="unused",
                    entra_identity={
                        "tenant_id": cfg.issuer.split("/")[3],
                        "object_id": oid,
                        "client_id": cfg.client_id,
                    },
                )
            )
            await db.commit()

    asyncio.run(link())
    return app, headers, info


def exchange(test_client, app, token, **params):
    return test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": GRANT,
            "subject_token_type": TYPE,
            "subject_token": token,
            "client_id": app["client_id"],
            "client_secret": app["client_secret"],
            "scope": "app",
            **params,
        },
    )


@pytest.mark.e2e
def test_exchange_issues_org_bound_short_lived_token_and_disable_revokes(
    test_client, create_user, login_user, whoami, entra
):
    app, headers, info = setup_app(test_client, create_user, login_user, whoami, entra)
    response = exchange(test_client, app, entra[3]())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["issued_token_type"] == TYPE
    assert 0 < body["expires_in"] <= 600
    assert "refresh_token" not in body
    auth = {"Authorization": f"Bearer {body['access_token']}"}
    assert test_client.get("/api/users/whoami", headers=auth).status_code == 200
    edit = test_client.patch(f"/api/oauth/clients/{app['id']}", headers=headers, json={"entra_exchange": {}})
    assert edit.status_code == 200, edit.text
    assert test_client.get("/api/users/whoami", headers=auth).status_code == 401


@pytest.mark.e2e
@pytest.mark.parametrize(
    "change",
    [
        {"aud": "wrong"},
        {"tid": str(uuid.uuid4())},
        {"azp": str(uuid.uuid4())},
        {"iss": "https://evil.invalid"},
        {"exp": 1},
        {"scp": ""},
        {"idtyp": "app"},
        {"oid": str(uuid.uuid4())},
        {"nbf": 9999999999},
    ],
)
def test_exchange_rejects_invalid_or_unlinked_subjects(test_client, create_user, login_user, whoami, entra, change):
    app, _, _ = setup_app(test_client, create_user, login_user, whoami, entra)
    response = exchange(test_client, app, entra[3](**change))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
    assert "access_token" not in response.json()


@pytest.mark.e2e
def test_exchange_is_opt_in_confidential_and_rechecks_membership(test_client, create_user, login_user, whoami, entra):
    app, headers, info = setup_app(test_client, create_user, login_user, whoami, entra)
    for secret in ("", "incorrect"):
        response = exchange(test_client, app, entra[3](), client_secret=secret)
        assert response.status_code == 401
    response = exchange(test_client, app, entra[3](), scope="app mcp")
    assert response.json()["error"] == "invalid_scope"
    disabled = test_client.post(
        "/api/oauth/clients", headers=headers, json={"name": "Regular", "scopes": "app", "trusted": True}
    ).json()
    assert exchange(test_client, disabled, entra[3]()).json()["error"] == "unauthorized_client"

    async def remove_membership():
        from datetime import datetime

        from app.models.membership import Membership

        async with async_session_maker() as db:
            row = await db.scalar(select(Membership).where(Membership.user_id == info["id"]))
            row.deleted_at = datetime.utcnow()
            await db.commit()

    asyncio.run(remove_membership())
    assert exchange(test_client, app, entra[3]()).json()["error"] == "invalid_grant"


@pytest.mark.e2e
def test_v1_tokens_and_provider_change(test_client, create_user, login_user, whoami, entra):
    app, headers, _ = setup_app(test_client, create_user, login_user, whoami, entra)
    cfg, external, _, signed = entra
    tenant = cfg.issuer.split("/")[3]
    response = exchange(
        test_client,
        app,
        signed(ver="1.0", appid=external, aud=f"api://{cfg.client_id}", iss=f"https://sts.windows.net/{tenant}/"),
    )
    assert response.status_code == 200, response.text
    bearer = response.json()["access_token"]
    cfg.enabled = False
    assert test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {bearer}"}).status_code == 401
    assert exchange(test_client, app, signed()).json()["error"] == "unauthorized_client"


@pytest.mark.e2e
def test_exchange_rejects_forgery_and_does_not_link_by_email(test_client, create_user, login_user, whoami, entra):
    app, _, info = setup_app(test_client, create_user, login_user, whoami, entra)
    claims = jwt.decode(entra[3](), options={"verify_signature": False})
    forged = jwt.encode(
        claims,
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    assert exchange(test_client, app, forged).json()["error"] == "invalid_grant"
    assert (
        exchange(test_client, app, entra[3](oid=str(uuid.uuid4()), email=info["email"])).json()["error"]
        == "invalid_grant"
    )


@pytest.mark.e2e
def test_exchange_provisions_before_returning_and_preserves_disconnect(
    test_client, create_user, login_user, whoami, entra, create_connection, monkeypatch
):
    app, headers, info = setup_app(test_client, create_user, login_user, whoami, entra)
    cfg, _, _, signed = entra
    tenant = cfg.issuer.split("/")[3]
    login = headers["Authorization"].split()[1]
    conn = create_connection(
        name="Delegated Power BI",
        type="powerbi",
        config={},
        credentials={"tenant_id": tenant, "client_id": cfg.client_id, "client_secret": "synthetic"},
        auth_policy="user_required",
        allowed_user_auth_modes=["oauth"],
        user_token=login,
        org_id=headers["X-Organization-Id"],
    )
    token = signed()

    async def post(self, url, **kwargs):
        assert kwargs["data"]["assertion"] == token
        assert kwargs["data"]["requested_token_use"] == "on_behalf_of"
        return httpx.Response(
            200, request=httpx.Request("POST", url), json={"access_token": "downstream", "expires_in": 3600}
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert exchange(test_client, app, token).status_code == 200

    async def check_and_disconnect():
        from app.models.organization import Organization
        from app.models.user import User
        from app.models.user_connection_credentials import UserConnectionCredentials
        from app.services.connection_service import ConnectionService

        async with async_session_maker() as db:
            row = await db.scalar(
                select(UserConnectionCredentials).where(
                    UserConnectionCredentials.user_id == info["id"],
                    UserConnectionCredentials.connection_id == conn["id"],
                )
            )
            assert row.is_active
            assert row.decrypt_credentials()["access_token"] == "downstream"
            await ConnectionService().delete_user_credentials(
                db, conn["id"], await db.get(Organization, headers["X-Organization-Id"]), await db.get(User, info["id"])
            )

    asyncio.run(check_and_disconnect())
    # A background external-app exchange must not override manual Disconnect.
    assert exchange(test_client, app, token).status_code == 200

    async def check_marker():
        from app.models.user_connection_credentials import UserConnectionCredentials

        async with async_session_maker() as db:
            row = await db.scalar(
                select(UserConnectionCredentials).where(
                    UserConnectionCredentials.user_id == info["id"],
                    UserConnectionCredentials.connection_id == conn["id"],
                )
            )
            assert not row.is_active

    asyncio.run(check_marker())


@pytest.mark.e2e
def test_exchange_settings_do_not_break_pkce_or_refresh(test_client, create_user, login_user, whoami, entra):
    from tests.e2e.test_oauth_app import APP_REDIRECT_URI, _issue_token

    app, headers, _ = setup_app(test_client, create_user, login_user, whoami, entra)
    edit = test_client.patch(
        f"/api/oauth/clients/{app['id']}", headers=headers, json={"redirect_uris": [APP_REDIRECT_URI]}
    )
    assert edit.status_code == 200
    ordinary = _issue_token(test_client, login_token=headers["Authorization"].split()[1], client=app, scope="app")
    external = exchange(test_client, app, entra[3]()).json()
    assert (
        test_client.patch(f"/api/oauth/clients/{app['id']}", headers=headers, json={"entra_exchange": {}}).status_code
        == 200
    )
    assert (
        test_client.get(
            "/api/users/whoami", headers={"Authorization": "Bearer " + ordinary["access_token"]}
        ).status_code
        == 200
    )
    assert (
        test_client.get(
            "/api/users/whoami", headers={"Authorization": "Bearer " + external["access_token"]}
        ).status_code
        == 401
    )
    refreshed = test_client.post(
        "/api/oauth/token",
        data={"grant_type": "refresh_token", "client_id": app["client_id"], "refresh_token": ordinary["refresh_token"]},
    )
    assert refreshed.status_code == 200


@pytest.mark.e2e
def test_exchange_member_cannot_escalate_or_cross_organizations(test_client, create_user, login_user, whoami, entra):
    app, headers, info = setup_app(test_client, create_user, login_user, whoami, entra)
    # A role transition uses the real administration endpoint, then the issued
    # external token must observe the member's current permissions.
    email = f"member-{uuid.uuid4()}@example.com"
    added = test_client.post(
        f"/api/organizations/{headers['X-Organization-Id']}/members",
        headers=headers,
        json={"organization_id": headers["X-Organization-Id"], "email": email, "role": "member"},
    )
    assert added.status_code == 200
    member = create_user(email=email)
    member_login = login_user(member["email"], member["password"])
    member_info = whoami(member_login)
    cfg, _, oid, signed = entra

    async def bind_member():
        async with async_session_maker() as db:
            # Verified SSO account binding cannot be created by local signup API.
            account = await db.scalar(select(OAuthAccount).where(OAuthAccount.user_id == info["id"]))
            account.user_id = member_info["id"]
            await db.commit()

    asyncio.run(bind_member())
    result = exchange(test_client, app, signed())
    assert result.status_code == 200, result.text
    auth = {"Authorization": "Bearer " + result.json()["access_token"]}
    assert test_client.get("/api/users/whoami", headers=auth).status_code == 200
    assert test_client.get("/api/oauth/entra-providers", headers=auth).status_code == 403
    assert (
        test_client.patch(f"/api/oauth/clients/{app['id']}", headers=auth, json={"entra_exchange": {}}).status_code
        == 403
    )
    assert (
        test_client.get("/api/connections", headers={**auth, "X-Organization-Id": str(uuid.uuid4())}).json()
        == test_client.get("/api/connections", headers=auth).json()
    )


@pytest.mark.e2e
def test_exchange_rejects_unsupported_subject_types_and_targets(test_client, create_user, login_user, whoami, entra):
    app, _, _ = setup_app(test_client, create_user, login_user, whoami, entra)
    for parameters in (
        {"subject_token_type": "urn:ietf:params:oauth:token-type:id_token"},
        {"audience": "other"},
        {"resource": "other"},
        {"actor_token": "other"},
        {"requested_token_type": "urn:ietf:params:oauth:token-type:refresh_token"},
    ):
        response = exchange(test_client, app, entra[3](), **parameters)
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.e2e
def test_first_exchange_accepts_valid_invite_without_bow_registration(
    test_client, create_user, login_user, whoami, entra
):
    app, headers, _ = setup_app(test_client, create_user, login_user, whoami, entra)
    email = f"new-{uuid.uuid4()}@example.com"
    invite = test_client.post(
        f"/api/organizations/{headers['X-Organization-Id']}/members",
        headers=headers,
        json={"organization_id": headers["X-Organization-Id"], "email": email, "role": "member"},
    )
    assert invite.status_code == 200
    token = entra[3](oid=str(uuid.uuid4()), preferred_username=email)
    first = exchange(test_client, app, token)
    assert first.status_code == 200, first.text
    auth = {"Authorization": "Bearer " + first.json()["access_token"]}
    info = test_client.get("/api/users/whoami", headers=auth).json()
    assert info["email"] == email
    assert test_client.get("/api/oauth/clients", headers=auth).status_code == 403
    second = exchange(test_client, app, token)
    assert second.status_code == 200
    again = test_client.get(
        "/api/users/whoami", headers={"Authorization": "Bearer " + second.json()["access_token"]}
    ).json()
    assert again["id"] == info["id"]
    members = test_client.get(f"/api/organizations/{headers['X-Organization-Id']}/members", headers=headers).json()
    assert len([m for m in members if m.get("user_id") == info["id"]]) == 1


@pytest.mark.e2e
def test_first_exchange_requires_current_invite_or_domain_policy(test_client, create_user, login_user, whoami, entra):
    from datetime import datetime, timedelta

    from app.models.membership import Membership

    app, headers, _ = setup_app(test_client, create_user, login_user, whoami, entra)
    email = f"new-{uuid.uuid4()}@example.com"
    token = entra[3](oid=str(uuid.uuid4()), preferred_username=email)
    assert exchange(test_client, app, token).status_code == 400
    invite = test_client.post(
        f"/api/organizations/{headers['X-Organization-Id']}/members",
        headers=headers,
        json={"organization_id": headers["X-Organization-Id"], "email": email, "role": "member"},
    ).json()

    async def expire():
        async with async_session_maker() as db:
            # Expired invitation state is a clock boundary, not a public mutation.
            row = await db.scalar(select(Membership).where(Membership.id == invite["id"]))
            row.invite_expires_at = datetime.utcnow() - timedelta(days=1)
            await db.commit()

    asyncio.run(expire())
    assert exchange(test_client, app, token).status_code == 400


@pytest.mark.e2e
def test_first_exchange_uses_org_domain_policy_and_seat_limit(
    test_client, create_user, login_user, whoami, entra, monkeypatch
):
    import app.ee.license as license_module
    from tests.e2e.test_seat_cap_autoprovision import _set_signup_policy

    app, headers, _ = setup_app(test_client, create_user, login_user, whoami, entra)
    monkeypatch.setattr(license_module, "has_feature", lambda feature: True)
    monkeypatch.setattr(license_module, "get_max_users", lambda: 1)

    async def configure():
        async with async_session_maker() as db:
            await _set_signup_policy(db, headers["X-Organization-Id"], ["allowed.example"])

    asyncio.run(configure())
    token = entra[3](oid=str(uuid.uuid4()), preferred_username=f"new-{uuid.uuid4()}@allowed.example")
    assert exchange(test_client, app, token).status_code == 400
    monkeypatch.setattr(license_module, "get_max_users", lambda: -1)
    result = exchange(test_client, app, token)
    assert result.status_code == 200, result.text
    denied = entra[3](oid=str(uuid.uuid4()), preferred_username=f"new-{uuid.uuid4()}@other.example")
    assert exchange(test_client, app, denied).status_code == 400


@pytest.mark.e2e
def test_concurrent_first_exchange_creates_one_identity(test_client, create_user, login_user, whoami, entra, request):
    if request.config.getoption("--db") == "sqlite":
        pytest.skip("Cross-worker advisory lock contract requires PostgreSQL")
    from concurrent.futures import ThreadPoolExecutor

    app, headers, _ = setup_app(test_client, create_user, login_user, whoami, entra)
    email = f"concurrent-{uuid.uuid4()}@example.com"
    invite = test_client.post(
        f"/api/organizations/{headers['X-Organization-Id']}/members",
        headers=headers,
        json={"organization_id": headers["X-Organization-Id"], "email": email, "role": "member"},
    )
    assert invite.status_code == 200
    token = entra[3](oid=str(uuid.uuid4()), preferred_username=email)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: exchange(test_client, app, token), range(3)))
    assert all(r.status_code in (200, 503) for r in results), [r.text for r in results]
    successful = exchange(test_client, app, token)
    assert successful.status_code == 200, successful.text

    async def counts():
        from sqlalchemy import func

        from app.models.user import User

        async with async_session_maker() as db:
            assert await db.scalar(select(func.count()).select_from(User).where(User.email == email)) == 1
            assert (
                await db.scalar(
                    select(func.count()).select_from(OAuthAccount).where(OAuthAccount.account_email == email)
                )
                == 1
            )

    asyncio.run(counts())
