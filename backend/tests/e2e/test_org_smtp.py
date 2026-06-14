"""E2E: org SMTP (DB) overrides global; backward orgs fall back; password encrypted."""
import asyncio
import uuid

import pytest


def _unique_email(domain="acme-corp.com"):
    return f"smtp_{uuid.uuid4().hex[:8]}@{domain}"


@pytest.mark.e2e
def test_org_smtp_override_backward_and_encryption(create_user, login_user, whoami):
    email = _unique_email()
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]

    from main import app
    from fastapi.testclient import TestClient
    from app.dependencies import async_session_maker
    from app.services.email_client_resolver import resolve_outbound

    client = TestClient(app)
    H = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}

    async def resolve(purpose):
        async with async_session_maker() as db:
            return await resolve_outbound(db, org_id, purpose=purpose)

    # Backward: no DB SMTP -> system mail falls back to global (or none).
    r0 = asyncio.run(resolve("system"))
    assert r0.source in ("global", "none")

    # Set org SMTP via the API.
    put = client.put("/api/organization/smtp", json={
        "enabled": True, "host": "relay.acme.com", "port": 587, "security": "starttls",
        "username": "noreply@acme.com", "password": "s3cret",
        "from_address": "noreply@acme.com", "from_name": "Acme",
    }, headers=H)
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["password_set"] is True
    assert "password" not in body  # never returned

    # GET redacts the password.
    got = client.get("/api/organization/smtp", headers=H).json()
    assert got["host"] == "relay.acme.com"
    assert got["password_set"] is True
    assert "password" not in got

    # Resolver: system mail now uses org SMTP, even decrypting the password,
    # and works regardless of the global client.
    r1 = asyncio.run(resolve("system"))
    assert r1.source == "org_smtp"
    assert r1.smtp_config.host == "relay.acme.com"
    assert r1.smtp_config.password == "s3cret"  # decrypted from password_enc
    assert r1.from_address == "noreply@acme.com"

    # Updating other fields without a password keeps the stored one.
    client.put("/api/organization/smtp", json={
        "enabled": True, "host": "relay2.acme.com", "port": 587, "security": "starttls",
        "username": "noreply@acme.com", "from_address": "noreply@acme.com", "from_name": "Acme2",
    }, headers=H)
    r2 = asyncio.run(resolve("system"))
    assert r2.smtp_config.host == "relay2.acme.com"
    assert r2.smtp_config.password == "s3cret"  # preserved

    # Disabling falls back to global/none again.
    client.put("/api/organization/smtp", json={"enabled": False, "host": "relay2.acme.com"}, headers=H)
    r3 = asyncio.run(resolve("system"))
    assert r3.source in ("global", "none")


@pytest.mark.e2e
def test_org_smtp_noauth_and_validate_certs(create_user, login_user, whoami):
    """Relays with no auth + advanced TLS (validate_certs) round-trip end to end."""
    email = _unique_email()
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]

    from main import app
    from fastapi.testclient import TestClient
    from app.dependencies import async_session_maker
    from app.services.email_client_resolver import resolve_outbound

    client = TestClient(app)
    H = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}

    # Open relay: no username/password, self-signed cert (validate_certs off).
    put = client.put("/api/organization/smtp", json={
        "enabled": True, "host": "internal-relay.acme.local", "port": 25,
        "security": "none", "from_address": "noreply@acme.local",
        "validate_certs": False,
    }, headers=H)
    assert put.status_code == 200, put.text
    got = put.json()
    assert got["password_set"] is False
    assert got["validate_certs"] is False

    async def resolve():
        async with async_session_maker() as db:
            return await resolve_outbound(db, org_id, purpose="system")

    r = asyncio.run(resolve())
    assert r.source == "org_smtp"
    assert r.smtp_config.host == "internal-relay.acme.local"
    assert r.smtp_config.username is None  # no-auth relay
    assert r.smtp_config.password is None
    assert r.smtp_config.validate_certs is False


@pytest.mark.e2e
def test_password_enc_not_plaintext_in_config(create_user, login_user, whoami):
    """The SMTP password must be Fernet-encrypted at rest, not plaintext JSON."""
    email = _unique_email()
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]

    from main import app
    from fastapi.testclient import TestClient
    from app.dependencies import async_session_maker
    from app.models.organization_settings import OrganizationSettings
    from sqlalchemy import select

    client = TestClient(app)
    H = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}
    client.put("/api/organization/smtp", json={
        "enabled": True, "host": "relay.acme.com", "username": "u", "password": "PLAINTEXT_SECRET",
    }, headers=H)

    async def _config():
        async with async_session_maker() as db:
            row = (await db.execute(
                select(OrganizationSettings).where(OrganizationSettings.organization_id == org_id)
            )).scalar_one_or_none()
            return row.config if row else {}

    cfg = asyncio.run(_config())
    smtp = cfg.get("smtp", {})
    assert "password" not in smtp  # no plaintext key
    assert smtp.get("password_enc")
    assert "PLAINTEXT_SECRET" not in str(smtp)  # not anywhere in the stored blob
