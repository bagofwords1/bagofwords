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


@pytest.mark.e2e
def test_enabled_smtp_requires_a_from_address(create_user, login_user, whoami):
    """An enabled relay with no sender is a guaranteed send-time failure.

    ``build_email`` refuses to construct a message with no From address and every
    relay rejects one, so this must be caught when the admin saves rather than at
    3am in a scheduled report.
    """
    email = _unique_email()
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]

    from main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    H = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}

    r = client.put("/api/organization/smtp", json={
        "enabled": True, "host": "relay.acme.com", "port": 587,
        "security": "starttls",  # no username, no from_address
    }, headers=H)
    assert r.status_code == 400, r.text
    assert "From address" in r.json()["detail"]

    # A From address alone is enough (open relay, no auth).
    r = client.put("/api/organization/smtp", json={
        "enabled": True, "host": "relay.acme.com", "port": 25,
        "security": "none", "from_address": "noreply@acme.com",
    }, headers=H)
    assert r.status_code == 200, r.text

    # Disabling never requires it — the config is kept as-is.
    r = client.put("/api/organization/smtp", json={
        "enabled": False, "host": "relay.acme.com",
    }, headers=H)
    assert r.status_code == 200, r.text


@pytest.mark.e2e
def test_smtp_get_reports_the_active_transport(create_user, login_user, whoami):
    """The page must be able to show which transport system mail really uses."""
    email = _unique_email()
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]

    from main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    H = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}

    before = client.get("/api/organization/smtp", headers=H).json()
    assert before["active_source"] in ("global", "none")
    assert before["enabled"] is False

    client.put("/api/organization/smtp", json={
        "enabled": True, "host": "relay.acme.com", "port": 587, "security": "starttls",
        "username": "noreply@acme.com", "password": "s3cret",
        "from_address": "noreply@acme.com",
    }, headers=H)
    after = client.get("/api/organization/smtp", headers=H).json()
    assert after["active_source"] == "org_smtp"

    # Toggling off flips the readout back without discarding the settings.
    client.put("/api/organization/smtp", json={
        "enabled": False, "host": "relay.acme.com", "port": 587, "security": "starttls",
        "username": "noreply@acme.com", "from_address": "noreply@acme.com",
    }, headers=H)
    off = client.get("/api/organization/smtp", headers=H).json()
    assert off["active_source"] in ("global", "none")
    assert off["host"] == "relay.acme.com"   # configuration survived
    assert off["password_set"] is True       # password survived


@pytest.mark.e2e
def test_smtp_test_only_sends_to_the_caller(create_user, login_user, whoami):
    """The test endpoint sends real mail through the org's relay.

    An arbitrary recipient would make it a spam relay for anyone holding
    ``manage_settings``, so it only ever sends to the caller's own address.
    """
    email = _unique_email()
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]

    from main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    H = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}

    client.put("/api/organization/smtp", json={
        "enabled": True, "host": "127.0.0.1", "port": 1,  # nothing listening
        "security": "none", "from_address": "noreply@acme.com",
    }, headers=H)

    r = client.post("/api/organization/smtp/test",
                    json={"to": "someone-else@elsewhere.test"}, headers=H)
    assert r.status_code == 400, r.text
    assert "your own address" in r.json()["detail"]

    # Defaulting to the caller is allowed and reaches the transport (and fails
    # there, because nothing is listening on port 1 — which is the point: the
    # test reports a real transport failure instead of "Connection OK").
    r = client.post("/api/organization/smtp/test", json={}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is False
    assert body["source"] == "org_smtp"
    assert body["recipient"] == email
    assert body["stage"] in ("connect", "send")
    assert body["error"]
