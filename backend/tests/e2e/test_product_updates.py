"""E2E: the instance admin's "Send me product updates" opt-in.

Contract:
  * a self-hosted instance never sends a user's email unless the admin opted in;
  * only the instance's first user is asked at sign-up (the server re-checks —
    a later sign-up cannot set the instance's answer);
  * answering at sign-up (yes or no) counts as a decision, so onboarding does
    not ask again; not being asked (e.g. SSO sign-up) leaves it undecided;
  * opting out sends a flag, never the email;
  * telemetry off (or the cloud deployment) disables the prompt and sends nothing;
  * licensed instances tag telemetry with the license id, never the org name.

The PostHog client is the outbound boundary, so it is replaced with a recorder
and its thread pool with an inline executor (no sleeps, deterministic).
"""
import os
import uuid
from concurrent.futures import Executor, Future
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.utils.user_creds import main_user


class _InlineExecutor(Executor):
    def submit(self, fn, *args, **kwargs):
        f = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except BaseException as e:  # pragma: no cover - surfaced via result()
            f.set_exception(e)
        return f


class _RecordingPostHog:
    """Mirrors the posthog-python 6 client surface the app uses (capture,
    set, group_identify). It deliberately has no ``identify`` — the real
    client doesn't either, so a call to it must fail here too."""

    def __init__(self):
        self.captures = []
        self.identifies = []

    def capture(self, distinct_id, event, properties=None, timestamp=None, groups=None):
        self.captures.append({"distinct_id": distinct_id, "event": event, "properties": properties or {}})

    def set(self, distinct_id=None, properties=None, **kwargs):
        self.identifies.append({"distinct_id": distinct_id, "properties": properties or {}})

    def group_identify(self, *args, **kwargs):
        pass

    def sent_text(self) -> str:
        return repr(self.captures) + repr(self.identifies)


@pytest.fixture
def outbound(monkeypatch):
    """Telemetry on, PostHog replaced by a recorder; config restored after."""
    import app.core.telemetry as telemetry_module
    from app.settings.config import settings

    recorder = _RecordingPostHog()
    monkeypatch.setattr(telemetry_module, "_posthog", recorder)
    monkeypatch.setattr(telemetry_module, "_telemetry_executor", _InlineExecutor())
    monkeypatch.setattr(settings, "TESTING", False)

    cfg = settings.bow_config
    saved = (cfg.telemetry.enabled, cfg.deployment.type)
    cfg.telemetry.enabled = True
    cfg.deployment.type = "self_hosted"
    try:
        yield recorder
    finally:
        cfg.telemetry.enabled, cfg.deployment.type = saved


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _register(test_client, email, **extra):
    body = {"name": f"user_{uuid.uuid4().hex[:6]}", "email": email, "password": main_user["password"], **extra}
    r = test_client.post("/api/auth/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _admin(test_client, login_user, whoami, email):
    token = login_user(email, main_user["password"])
    return token, whoami(token)["organizations"][0]["id"]


@pytest.mark.e2e
def test_prompt_is_offered_only_while_the_instance_is_unclaimed(test_client, outbound):
    assert test_client.get("/api/settings").json()["product_updates_prompt"] is True
    _register(test_client, f"first_{uuid.uuid4().hex[:6]}@acme.io")
    assert test_client.get("/api/settings").json()["product_updates_prompt"] is False


@pytest.mark.e2e
@pytest.mark.parametrize("ticked", [True, False])
def test_first_user_signup_choice_is_recorded_and_email_sent_only_when_ticked(
    test_client, login_user, whoami, outbound, ticked
):
    email = f"owner_{uuid.uuid4().hex[:6]}@example-corp.com"
    _register(test_client, email, product_updates=ticked)

    token, org_id = _admin(test_client, login_user, whoami, email)
    status = test_client.get("/api/product-updates", headers=_h(token, org_id)).json()
    assert status["available"] is True
    assert status["decided"] is True  # either answer means onboarding won't ask again
    assert status["opted_in"] is ticked

    if ticked:
        assert status["email"] == email
        assert any(i["properties"].get("email") == email and i["properties"].get("product_updates") is True
                   for i in outbound.identifies)
        assert any(c["event"] == "product_updates_subscribed" for c in outbound.captures)
    else:
        assert status["email"] is None
        # Declining sends nothing at all — not the email, not even a flag.
        assert email not in outbound.sent_text()
        assert not any(c["event"].startswith("product_updates_") for c in outbound.captures)
        assert not any("product_updates" in i["properties"] for i in outbound.identifies)


@pytest.mark.e2e
def test_signup_without_the_checkbox_leaves_the_instance_undecided(test_client, login_user, whoami, outbound):
    # e.g. the first admin signed up through SSO and never saw the checkbox.
    email = f"sso_{uuid.uuid4().hex[:6]}@example-corp.com"
    _register(test_client, email)
    token, org_id = _admin(test_client, login_user, whoami, email)

    status = test_client.get("/api/product-updates", headers=_h(token, org_id)).json()
    assert status["decided"] is False and status["opted_in"] is False
    assert email not in outbound.sent_text()

    # The onboarding fallback records the answer.
    r = test_client.put("/api/product-updates", json={"opted_in": True, "source": "onboarding"},
                        headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    assert r.json()["decided"] is True and r.json()["email"] == email
    assert any(c["event"] == "product_updates_subscribed" and c["properties"]["source"] == "onboarding"
               for c in outbound.captures)


@pytest.mark.e2e
def test_a_later_signup_cannot_set_the_instance_answer(test_client, login_user, whoami, outbound):
    owner_email = f"owner_{uuid.uuid4().hex[:6]}@example-corp.com"
    _register(test_client, owner_email)
    token, org_id = _admin(test_client, login_user, whoami, owner_email)

    invitee = f"invitee_{uuid.uuid4().hex[:6]}@example-corp.com"
    r = test_client.post(f"/api/organizations/{org_id}/members",
                         json={"organization_id": org_id, "email": invitee, "role": "member"},
                         headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    from tests.fixtures.user import _pending_invite_token
    _register(test_client, invitee, invite_token=_pending_invite_token(invitee), product_updates=True)

    status = test_client.get("/api/product-updates", headers=_h(token, org_id)).json()
    assert status["decided"] is False
    assert invitee not in outbound.sent_text()


@pytest.mark.e2e
def test_opting_out_sends_a_flag_and_never_the_email(test_client, login_user, whoami, outbound):
    email = f"owner_{uuid.uuid4().hex[:6]}@example-corp.com"
    _register(test_client, email, product_updates=True)
    token, org_id = _admin(test_client, login_user, whoami, email)
    outbound.captures.clear(); outbound.identifies.clear()

    r = test_client.put("/api/product-updates", json={"opted_in": False}, headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    assert r.json()["opted_in"] is False and r.json()["email"] is None
    assert email not in outbound.sent_text()
    assert any(i["properties"].get("product_updates") is False for i in outbound.identifies)
    assert any(c["event"] == "product_updates_unsubscribed" for c in outbound.captures)

    # Saving the same answer again sends nothing new.
    outbound.captures.clear(); outbound.identifies.clear()
    test_client.put("/api/product-updates", json={"opted_in": False}, headers=_h(token, org_id))
    assert outbound.captures == [] and outbound.identifies == []


@pytest.mark.e2e
@pytest.mark.parametrize("disable", ["telemetry_off", "cloud"])
def test_unavailable_instances_never_prompt_or_send(test_client, login_user, whoami, outbound, disable):
    from app.settings.config import settings
    if disable == "telemetry_off":
        settings.bow_config.telemetry.enabled = False
    else:
        settings.bow_config.deployment.type = "cloud"

    assert test_client.get("/api/settings").json()["product_updates_prompt"] is False
    email = f"owner_{uuid.uuid4().hex[:6]}@example-corp.com"
    _register(test_client, email, product_updates=True)
    token, org_id = _admin(test_client, login_user, whoami, email)

    status = test_client.get("/api/product-updates", headers=_h(token, org_id)).json()
    assert status["available"] is False and status["decided"] is False
    r = test_client.put("/api/product-updates", json={"opted_in": True}, headers=_h(token, org_id))
    assert r.status_code == 409
    assert email not in outbound.sent_text()


@pytest.mark.e2e
def test_members_cannot_read_or_change_the_answer(test_client, login_user, whoami, outbound):
    owner_email = f"owner_{uuid.uuid4().hex[:6]}@example-corp.com"
    _register(test_client, owner_email)
    token, org_id = _admin(test_client, login_user, whoami, owner_email)

    member = f"member_{uuid.uuid4().hex[:6]}@example-corp.com"
    test_client.post(f"/api/organizations/{org_id}/members",
                     json={"organization_id": org_id, "email": member, "role": "member"},
                     headers=_h(token, org_id))
    from tests.fixtures.user import _pending_invite_token
    _register(test_client, member, invite_token=_pending_invite_token(member))
    member_token = login_user(member, main_user["password"])

    assert test_client.get("/api/product-updates", headers=_h(member_token, org_id)).status_code == 403
    r = test_client.put("/api/product-updates", json={"opted_in": True}, headers=_h(member_token, org_id))
    assert r.status_code == 403
    assert member not in outbound.sent_text()


def _signed_license(private_pem, sub):
    now = datetime.now(timezone.utc)
    payload = {"iss": "bagofwords.com", "sub": sub, "iat": int(now.timestamp()),
               "exp": int((now + timedelta(days=30)).timestamp()), "tier": "enterprise",
               "org_name": "Contoso Holdings"}
    return f"bow_lic_{jwt.encode(payload, private_pem, algorithm='RS256')}"


@pytest.mark.e2e
def test_licensed_instance_telemetry_carries_license_id_not_org_name(test_client, login_user, whoami, outbound):
    import app.ee.license as license_module
    from app.settings.config import settings

    email = f"owner_{uuid.uuid4().hex[:6]}@example-corp.com"
    _register(test_client, email)
    token, org_id = _admin(test_client, login_user, whoami, email)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    license_id = f"lic_{uuid.uuid4().hex[:10]}"

    saved = (license_module.LICENSE_PUBLIC_KEY, settings.bow_config.license.key, os.environ.get("BOW_LICENSE_KEY"))
    license_module.LICENSE_PUBLIC_KEY = public_pem
    settings.bow_config.license.key = _signed_license(private_pem, license_id)
    os.environ.pop("BOW_LICENSE_KEY", None)
    license_module.clear_license_cache()
    try:
        outbound.captures.clear()
        test_client.put("/api/product-updates", json={"opted_in": True}, headers=_h(token, org_id))
        assert outbound.captures, "the opt-in should emit a telemetry event"
        for c in outbound.captures:
            assert c["properties"].get("license_id") == license_id
            assert "Contoso" not in repr(c["properties"])

        # Unlicensed (community) instances send no license id at all.
        settings.bow_config.license.key = None
        license_module.clear_license_cache()
        outbound.captures.clear()
        test_client.put("/api/product-updates", json={"opted_in": False}, headers=_h(token, org_id))
        assert outbound.captures
        assert all("license_id" not in c["properties"] for c in outbound.captures)
    finally:
        license_module.LICENSE_PUBLIC_KEY, settings.bow_config.license.key, env = saved
        if env is not None:
            os.environ["BOW_LICENSE_KEY"] = env
        license_module.clear_license_cache()
