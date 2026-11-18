"""Who may create an organization, who is an instance administrator, and who
owns instance-wide settings.

Contract:
  * The first organization of a deployment is created by whoever signs up
    first: no flag, no instance administrator, no license.
  * Every later organization needs all three: multiple organizations enabled,
    an instance administrator as the caller, and the ``multi_org`` license
    feature. Each missing condition has its own typed error. Organizations
    that already exist keep working when the license is gone.
  * The creator joins as admin; an optional first admin email becomes an
    ordinary pending admin invite.
  * ``auth.superuser_emails`` (``BOW_SUPERUSER_EMAILS``): a non-empty list is
    authoritative at sign-in and at startup; an empty list changes nothing.
  * Instance-wide settings belong to the organization's admins only while the
    instance has one organization and cannot have more.
  * whoami reports ``is_superuser`` and ``can_create_organization``.
"""
import asyncio
import uuid

import pytest

from app.ee import license as ee_license
from app.services import frame_ancestors_service
from app.settings.config import settings as bow_settings

PASSWORD = "Test1234!"

# These tests are about the real creation rule (the suite-wide fixture in
# tests/e2e/conftest.py waives it for legacy two-organization fixtures).
pytestmark = pytest.mark.usefixtures("enforce_org_creation_policy")


def _h(token, org_id=None):
    headers = {"Authorization": f"Bearer {token}"}
    if org_id:
        headers["X-Organization-Id"] = str(org_id)
    return headers


def _email(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:10]}@test.com"


def _name(prefix="dept"):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def flags(monkeypatch):
    """Feature flags, restored after the test."""
    features = bow_settings.bow_config.features

    def _set(**values):
        for key, value in values.items():
            monkeypatch.setattr(features, key, value)

    return _set


@pytest.fixture
def superuser_emails(monkeypatch):
    """Set the configured instance-administrator list (what BOW_SUPERUSER_EMAILS feeds)."""

    def _set(*emails):
        monkeypatch.setattr(bow_settings.bow_config.auth, "superuser_emails", list(emails))

    return _set


@pytest.fixture
def license_state(monkeypatch):
    """Replace the instance license for this test."""

    def _set(**fields):
        monkeypatch.setattr(ee_license, "_cached_license", ee_license.LicenseInfo(**fields))
        monkeypatch.setattr(ee_license, "_cache_initialized", True)

    return _set


@pytest.fixture
def signup(create_user, login_user):
    def _signup(prefix="user", email=None):
        email = email or _email(prefix)
        create_user(email=email, password=PASSWORD)
        return email, login_user(email, PASSWORD)

    return _signup


def _create(test_client, token, **body):
    body.setdefault("name", _name())
    return test_client.post("/api/organizations", json=body, headers=_h(token))


def _org_roles(whoami, token):
    return {o["id"]: o["role"] for o in whoami(token)["organizations"]}


def _set_superuser_directly(email, value):
    # No API grants or removes the instance-administrator flag by hand (that
    # is the "set it in the database" setup these tests must keep working),
    # so this one column is written directly.
    from sqlalchemy import update
    from app.dependencies import async_session_maker
    from app.models.user import User

    async def _run():
        async with async_session_maker() as db:
            await db.execute(update(User).where(User.email == email).values(is_superuser=value))
            await db.commit()

    asyncio.run(_run())


@pytest.fixture
def instance_admin(flags, superuser_emails, signup):
    """A multi-org instance whose first user is the configured instance administrator."""
    flags(allow_multiple_organizations=True, allow_uninvited_signups=True)
    email = _email("root")
    superuser_emails(email)
    _, token = signup(email=email)
    return {"email": email, "token": token}


# --- the first organization -------------------------------------------------


def test_first_organization_needs_no_flag_instance_admin_or_license(signup, whoami, license_state):
    license_state(licensed=False, tier="community")

    _, token = signup("first")

    me = whoami(token)
    assert me["is_superuser"] is False
    assert [o["role"] for o in me["organizations"]] == ["admin"]


# --- every later organization -----------------------------------------------


def test_instance_admin_creates_another_organization_and_joins_as_admin(test_client, instance_admin, whoami):
    before = _org_roles(whoami, instance_admin["token"])
    name = _name()

    resp = _create(test_client, instance_admin["token"], name=f"  {name}  ")

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == name
    after = _org_roles(whoami, instance_admin["token"])
    assert set(after) - set(before) == {resp.json()["id"]}
    assert after[resp.json()["id"]] == "admin"


def test_user_who_is_not_an_instance_admin_cannot_create_another_organization(
    test_client, instance_admin, signup, whoami
):
    _, token = signup("plain")

    resp = _create(test_client, token)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "organization.create_instance_admin_required"
    assert whoami(token)["organizations"] == []


def test_org_admin_who_is_not_an_instance_admin_cannot_create_another_organization(
    test_client, instance_admin, signup, whoami
):
    admin_email = _email("dept_admin")
    created = _create(test_client, instance_admin["token"], first_admin_email=admin_email)
    assert created.status_code == 200, created.text
    _, token = signup(email=admin_email)
    assert _org_roles(whoami, token) == {created.json()["id"]: "admin"}

    resp = _create(test_client, token)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "organization.create_instance_admin_required"


def test_instance_admin_cannot_create_another_organization_when_multiple_are_not_enabled(
    test_client, instance_admin, flags, whoami
):
    flags(allow_multiple_organizations=False)
    before = _org_roles(whoami, instance_admin["token"])

    resp = _create(test_client, instance_admin["token"])

    assert resp.status_code == 400, resp.text
    assert resp.json()["error_code"] == "organization.multiple_not_enabled"
    assert _org_roles(whoami, instance_admin["token"]) == before


@pytest.mark.parametrize(
    "license_fields",
    [
        {"licensed": False, "tier": "community"},
        {"licensed": True, "tier": "team"},
        {"licensed": True, "tier": "enterprise", "features": ["audit_logs", "custom_roles"]},
    ],
    ids=["community", "team-tier", "enterprise-without-the-feature"],
)
def test_instance_admin_cannot_create_another_organization_without_the_license_feature(
    test_client, instance_admin, license_state, whoami, license_fields
):
    license_state(**license_fields)
    before = _org_roles(whoami, instance_admin["token"])

    resp = _create(test_client, instance_admin["token"])

    assert resp.status_code == 402, resp.text
    assert resp.json()["error_code"] == "organization.create_license_required"
    assert _org_roles(whoami, instance_admin["token"]) == before


def test_enterprise_tier_license_includes_additional_organizations(test_client, instance_admin, license_state):
    license_state(licensed=True, tier="enterprise")

    assert _create(test_client, instance_admin["token"]).status_code == 200


def test_existing_organizations_keep_working_after_the_license_is_gone(
    test_client, instance_admin, license_state
):
    second = _create(test_client, instance_admin["token"])
    assert second.status_code == 200, second.text
    org_id = second.json()["id"]

    license_state(licensed=False, tier="community")

    members = test_client.get(f"/api/organizations/{org_id}/members", headers=_h(instance_admin["token"], org_id))
    assert members.status_code == 200, members.text
    assert [m["role"] for m in members.json()] == ["admin"]
    assert _create(test_client, instance_admin["token"]).status_code == 402


def test_organization_name_must_be_unique_and_not_blank(test_client, instance_admin):
    name = _name()
    assert _create(test_client, instance_admin["token"], name=name).status_code == 200

    taken = _create(test_client, instance_admin["token"], name=name)
    assert taken.status_code == 409, taken.text
    assert taken.json()["error_code"] == "organization.name_taken"

    blank = _create(test_client, instance_admin["token"], name="   ")
    assert blank.status_code == 400, blank.text
    assert blank.json()["error_code"] == "organization.name_required"

    too_long = _create(test_client, instance_admin["token"], name="x" * 300)
    assert too_long.status_code == 400, too_long.text
    assert too_long.json()["error_code"] == "organization.name_too_long"


# --- first admin email ------------------------------------------------------


def test_first_admin_email_becomes_a_pending_admin_invite_that_the_person_accepts(
    test_client, instance_admin, signup, whoami
):
    invited = _email("Dept.Head")

    resp = _create(test_client, instance_admin["token"], first_admin_email=f" {invited.upper()} ")

    assert resp.status_code == 200, resp.text
    org_id = resp.json()["id"]
    members = test_client.get(
        f"/api/organizations/{org_id}/members", headers=_h(instance_admin["token"], org_id)
    ).json()
    pending = [m for m in members if not m.get("user_id")]
    assert [(m["email"], m["role"]) for m in pending] == [(invited.lower(), "admin")]
    # The creator is a member too, as admin.
    assert sorted(m["role"] for m in members) == ["admin", "admin"]

    # Nobody belongs to the new organization on the strength of the address
    # alone: the invited person joins by signing up with the invite.
    _, token = signup(email=invited.lower())
    assert _org_roles(whoami, token) == {org_id: "admin"}
    assert whoami(token)["is_superuser"] is False


def test_organization_without_first_admin_email_has_only_its_creator(test_client, instance_admin):
    resp = _create(test_client, instance_admin["token"])
    org_id = resp.json()["id"]

    members = test_client.get(
        f"/api/organizations/{org_id}/members", headers=_h(instance_admin["token"], org_id)
    ).json()

    assert len(members) == 1 and members[0]["role"] == "admin" and members[0]["user_id"]
    assert resp.json().get("first_admin_invite_status") is None


def test_first_admin_email_must_be_an_email_address(test_client, instance_admin, whoami):
    before = _org_roles(whoami, instance_admin["token"])

    resp = _create(test_client, instance_admin["token"], first_admin_email="not-an-address")

    assert resp.status_code == 422, resp.text
    assert _org_roles(whoami, instance_admin["token"]) == before


def test_creation_is_audited(test_client, instance_admin):
    resp = _create(test_client, instance_admin["token"])
    org_id = resp.json()["id"]

    logs = test_client.get(
        "/api/enterprise/audit", params={"action": "organization.created"},
        headers=_h(instance_admin["token"], org_id),
    )

    assert logs.status_code == 200, logs.text
    items = logs.json()["items"] if isinstance(logs.json(), dict) else logs.json()
    assert any(i["action"] == "organization.created" and i["resource_id"] == org_id for i in items)


# --- BOW_SUPERUSER_EMAILS ---------------------------------------------------


def test_configured_list_is_authoritative_at_sign_in(flags, superuser_emails, signup, login_user, whoami):
    flags(allow_uninvited_signups=True)
    listed, _ = signup("listed")
    hand_set, _ = signup("hand_set")
    _set_superuser_directly(hand_set, True)

    superuser_emails(listed)

    assert whoami(login_user(listed, PASSWORD))["is_superuser"] is True
    assert whoami(login_user(hand_set, PASSWORD))["is_superuser"] is False

    # Taking an address off the list removes the role at the next sign-in.
    superuser_emails(_email("someone_else"))
    assert whoami(login_user(listed, PASSWORD))["is_superuser"] is False


def test_configured_list_matches_account_email_case_insensitively(flags, superuser_emails, signup, whoami):
    flags(allow_uninvited_signups=True)
    email = _email("MixedCase")
    superuser_emails(email.lower())

    _, token = signup(email=email)

    assert whoami(token)["is_superuser"] is True


def test_empty_list_leaves_hand_set_instance_admins_alone(flags, superuser_emails, signup, login_user, whoami):
    from app.core.instance_admin import sync_superusers_at_startup

    flags(allow_uninvited_signups=True)
    superuser_emails()
    hand_set, _ = signup("hand_set")
    plain, _ = signup("plain")
    _set_superuser_directly(hand_set, True)

    asyncio.run(sync_superusers_at_startup())

    assert whoami(login_user(hand_set, PASSWORD))["is_superuser"] is True
    assert whoami(login_user(plain, PASSWORD))["is_superuser"] is False


def test_configured_list_is_applied_to_existing_accounts_at_startup(flags, superuser_emails, signup, whoami):
    from app.core.instance_admin import sync_superusers_at_startup

    flags(allow_uninvited_signups=True)
    listed, listed_token = signup("listed")
    hand_set, hand_set_token = signup("hand_set")
    bystander, bystander_token = signup("bystander")
    _set_superuser_directly(hand_set, True)
    superuser_emails(listed)

    # Sessions opened before the restart: no new sign-in happens here.
    asyncio.run(sync_superusers_at_startup())
    asyncio.run(sync_superusers_at_startup())  # every worker runs it

    assert whoami(listed_token)["is_superuser"] is True
    assert whoami(hand_set_token)["is_superuser"] is False
    assert whoami(bystander_token)["is_superuser"] is False


# --- instance-wide settings -------------------------------------------------


@pytest.fixture
def _fresh_frame_cache():
    frame_ancestors_service.reset_cache()
    yield
    frame_ancestors_service.reset_cache()


def _instance_setting_calls(test_client, headers):
    """One request per instance-wide settings surface -> status code."""
    return {
        "system_logs": test_client.get("/api/system-logs/status", headers=headers).status_code,
        "frame_ancestors": test_client.put(
            "/api/settings/frame-ancestors", json={"origins": []}, headers=headers
        ).status_code,
        "product_updates": test_client.put(
            "/api/product-updates", json={"opted_in": False}, headers=headers
        ).status_code,
        # Not a real key: an allowed caller gets as far as key validation.
        "license": test_client.put("/api/license/key", json={"key": "not-a-key"}, headers=headers).status_code,
    }


def test_single_organization_admin_manages_instance_settings(test_client, signup, whoami, _fresh_frame_cache):
    _, token = signup("owner")
    org_id = whoami(token)["organizations"][0]["id"]

    codes = _instance_setting_calls(test_client, _h(token, org_id))

    assert 403 not in codes.values(), codes
    assert codes["system_logs"] == 200 and codes["frame_ancestors"] == 200
    assert test_client.get("/api/settings/frame-ancestors", headers=_h(token, org_id)).json()["can_edit"] is True
    # whoami tells the UI the same thing, so the pages are offered.
    me = whoami(token)
    assert me["is_superuser"] is False and me["is_instance_settings_admin"] is True


def test_instance_settings_need_an_instance_admin_once_a_second_organization_exists(
    test_client, instance_admin, flags, signup, whoami, _fresh_frame_cache
):
    other_admin = _email("dept_admin")
    created = _create(test_client, instance_admin["token"], first_admin_email=other_admin)
    assert created.status_code == 200, created.text
    org_id = created.json()["id"]
    _, org_admin_token = signup(email=other_admin)

    # Two organizations exist; the flag being off does not hand the instance
    # to one organization's admin.
    flags(allow_multiple_organizations=False)

    codes = _instance_setting_calls(test_client, _h(org_admin_token, org_id))
    assert set(codes.values()) == {403}, codes
    assert test_client.get(
        "/api/settings/frame-ancestors", headers=_h(org_admin_token, org_id)
    ).json()["can_edit"] is False

    assert whoami(org_admin_token)["is_instance_settings_admin"] is False

    codes = _instance_setting_calls(test_client, _h(instance_admin["token"], org_id))
    assert 403 not in codes.values(), codes
    assert whoami(instance_admin["token"])["is_instance_settings_admin"] is True


def test_soft_deleted_organization_does_not_take_instance_settings_from_the_single_org_admin(
    test_client, instance_admin, flags, signup, whoami, _fresh_frame_cache
):
    created = _create(test_client, instance_admin["token"])
    assert created.status_code == 200, created.text
    first_org = next(o for o in _org_roles(whoami, instance_admin["token"]) if o != created.json()["id"])
    plain_admin = _email("owner")
    invited = test_client.post(
        f"/api/organizations/{first_org}/members",
        json={"organization_id": first_org, "email": plain_admin, "role": "admin"},
        headers=_h(instance_admin["token"], first_org),
    )
    assert invited.status_code == 200, invited.text
    _, token = signup(email=plain_admin)

    # No API deletes an organization; a leftover soft-deleted row is a state
    # only the database can hold, so it is written directly.
    from datetime import datetime
    from sqlalchemy import update
    from app.dependencies import async_session_maker
    from app.models.organization import Organization

    async def _soft_delete():
        async with async_session_maker() as db:
            await db.execute(
                update(Organization).where(Organization.id == created.json()["id"]).values(deleted_at=datetime.utcnow())
            )
            await db.commit()

    asyncio.run(_soft_delete())
    flags(allow_multiple_organizations=False)

    me = whoami(token)
    assert me["is_superuser"] is False and me["is_instance_settings_admin"] is True
    codes = _instance_setting_calls(test_client, _h(token, first_org))
    assert 403 not in codes.values(), codes
    assert test_client.get("/api/settings/frame-ancestors", headers=_h(token, first_org)).json()["can_edit"] is True


# --- whoami ------------------------------------------------------------------


def test_whoami_says_who_can_create_an_organization(
    test_client, instance_admin, signup, whoami, flags, license_state
):
    _, plain_token = signup("plain")

    root = whoami(instance_admin["token"])
    assert root["is_superuser"] is True and root["can_create_organization"] is True
    plain = whoami(plain_token)
    assert plain["is_superuser"] is False and plain["can_create_organization"] is False

    # The flag follows the same rule as the endpoint, condition by condition.
    license_state(licensed=False, tier="community")
    assert whoami(instance_admin["token"])["can_create_organization"] is False
    assert _create(test_client, instance_admin["token"]).status_code == 402

    license_state(licensed=True, tier="enterprise")
    assert whoami(instance_admin["token"])["can_create_organization"] is True
    flags(allow_multiple_organizations=False)
    assert whoami(instance_admin["token"])["can_create_organization"] is False
