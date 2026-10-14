"""E2E: activating / removing the enterprise license key from Settings → License.

Covers the contract the UI depends on: a stored key overrides BOW_LICENSE_KEY,
an invalid or expired key is rejected without disturbing the active license,
removing the stored key falls back to the environment, and the key itself is
never returned over the API.
"""
import os
import uuid
from datetime import datetime, timezone, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


def _keypair():
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


TEST_PRIVATE_KEY, TEST_PUBLIC_KEY = _keypair()


def _license(org_name="Acme Corporation", tier="enterprise", expired=False, private_key=TEST_PRIVATE_KEY):
    now = datetime.now(timezone.utc)
    exp = now - timedelta(days=1) if expired else now + timedelta(days=365)
    payload = {
        "iss": "bagofwords.com",
        "sub": f"lic_{org_name.lower().replace(' ', '_')}",
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "tier": tier,
        "org_name": org_name,
    }
    return f"bow_lic_{jwt.encode(payload, private_key, algorithm='RS256')}"


@pytest.fixture
def license_sandbox():
    """Trust the test signing key and restore all license state afterwards."""
    import app.ee.license as license_module
    from app.settings.config import settings

    original_pem = license_module.LICENSE_PUBLIC_KEY
    original_env = os.environ.get("BOW_LICENSE_KEY")
    original_cfg = getattr(settings.bow_config.license, "key", None)

    license_module.LICENSE_PUBLIC_KEY = TEST_PUBLIC_KEY
    license_module.clear_license_cache()

    yield license_module

    license_module.LICENSE_PUBLIC_KEY = original_pem
    settings.bow_config.license.key = original_cfg
    if original_env is not None:
        os.environ["BOW_LICENSE_KEY"] = original_env
    elif "BOW_LICENSE_KEY" in os.environ:
        del os.environ["BOW_LICENSE_KEY"]
    license_module.clear_license_cache()


def _client_and_headers(create_user, login_user, whoami):
    from main import app
    from fastapi.testclient import TestClient

    email = f"license_{uuid.uuid4().hex[:8]}@acme-corp.com"
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]
    return TestClient(app), {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


@pytest.mark.e2e
def test_stored_key_overrides_env_and_can_be_removed(
    create_user, login_user, whoami, license_sandbox
):
    from app.settings.config import settings

    client, H = _client_and_headers(create_user, login_user, whoami)

    # The deployment ships a demo key (as docker-compose does).
    settings.bow_config.license.key = _license(org_name="Demo Sandbox")
    license_sandbox.clear_license_cache()

    status = client.get("/api/license/key", headers=H).json()
    assert status["source"] == "config"
    assert status["config_key_present"] is True
    assert status["license"]["org_name"] == "Demo Sandbox"

    # The admin pastes the purchased key: it overrides the env key.
    purchased = _license(org_name="Acme Corporation")
    put = client.put("/api/license/key", json={"key": purchased}, headers=H)
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["source"] == "database"
    assert body["config_key_present"] is True  # env key still there, just overridden
    assert body["license"]["licensed"] is True
    assert body["license"]["org_name"] == "Acme Corporation"

    # The key is never echoed back — only a masked tail.
    assert purchased not in put.text
    assert body["masked_key"] and body["masked_key"].endswith(purchased[-6:])

    # The public status endpoint reflects the new license too.
    assert client.get("/api/license").json()["org_name"] == "Acme Corporation"

    # Removing it falls back to the deployment's key rather than to nothing.
    delete = client.delete("/api/license/key", headers=H).json()
    assert delete["source"] == "config"
    assert delete["license"]["org_name"] == "Demo Sandbox"


@pytest.mark.e2e
def test_invalid_and_expired_keys_are_rejected_without_changing_state(
    create_user, login_user, whoami, license_sandbox
):
    from app.settings.config import settings

    client, H = _client_and_headers(create_user, login_user, whoami)

    settings.bow_config.license.key = _license(org_name="Demo Sandbox")
    license_sandbox.clear_license_cache()

    # Garbage, and a well-formed key signed by the wrong authority.
    other_private, _ = _keypair()
    for bad_key in ("not-a-license", _license(org_name="Forged", private_key=other_private)):
        res = client.put("/api/license/key", json={"key": bad_key}, headers=H)
        assert res.status_code == 400, res.text
        assert "not valid" in res.json()["detail"]

    # An already-expired key gets its own message and is also not stored.
    res = client.put("/api/license/key", json={"key": _license(org_name="Lapsed", expired=True)}, headers=H)
    assert res.status_code == 400, res.text
    assert "expired" in res.json()["detail"]

    # None of the rejects disturbed the working demo license.
    status = client.get("/api/license/key", headers=H).json()
    assert status["source"] == "config"
    assert status["license"]["licensed"] is True
    assert status["license"]["org_name"] == "Demo Sandbox"


@pytest.mark.e2e
def test_license_key_endpoints_require_admin(create_user, login_user, whoami, license_sandbox):
    """Non-admin members cannot read or change the instance license key."""
    from main import app
    from fastapi.testclient import TestClient

    client, admin_H = _client_and_headers(create_user, login_user, whoami)
    org_id = admin_H["X-Organization-Id"]

    # Invite a plain member into the admin's org, then register against the invite.
    member_email = f"member_{uuid.uuid4().hex[:8]}@acme-corp.com"
    invite = client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=admin_H,
    )
    assert invite.status_code in (200, 201), invite.text
    create_user(email=member_email)
    member_token = login_user(member_email, "test123")

    member_H = {"Authorization": f"Bearer {member_token}", "X-Organization-Id": org_id}
    assert client.get("/api/license/key", headers=member_H).status_code == 403
    assert client.put(
        "/api/license/key", json={"key": _license()}, headers=member_H
    ).status_code == 403
    assert client.delete("/api/license/key", headers=member_H).status_code == 403
