"""
RBAC-specific e2e fixtures.

Reuses the main e2e fixture style (create_user, login_user, whoami). Adds a
small number of thin helpers for the operations not already covered:

- invite_user_to_org: uses the existing POST /organizations/{id}/members
  invite flow followed by create_user to accept (same pattern as
  tests/e2e/test_membership.py).
- grant_ds_membership: POST /data_sources/{id}/members.
- create_sqlite_ds: creates a SQLite data source against the real chinook.sqlite
  fixture, matching tests/e2e/test_data_source.py.
- rbac_principals: builds a cast of users with assorted roles and grants so
  matrix tests can reference them by name.

All fixtures honor the same argument conventions as the existing e2e
fixtures: keyword-only ``user_token`` / ``org_id`` where applicable.
"""
import uuid
from pathlib import Path

import pytest

from app.settings.config import settings


@pytest.fixture(autouse=True)
def _enable_multi_org_for_rbac():
    """Enable uninvited signups + multi-org for RBAC tests.

    Several RBAC scenarios need both an org admin AND a separate "outsider"
    user that lives in a *different* organization. The dev config disables
    both uninvited signups and multi-org, which makes that impossible. We
    flip the flags for the duration of the test and restore them on
    teardown so we don't leak across the test session.
    """
    feats = settings.bow_config.features
    saved = (
        feats.allow_uninvited_signups,
        feats.allow_multiple_organizations,
    )
    feats.allow_uninvited_signups = True
    feats.allow_multiple_organizations = True
    try:
        yield
    finally:
        feats.allow_uninvited_signups = saved[0]
        feats.allow_multiple_organizations = saved[1]


# Path to the real SQLite test DB used elsewhere in the e2e suite.
CHINOOK_SQLITE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "chinook.sqlite"
).resolve()


# ---------------------------------------------------------------------------
# Member invitation
# ---------------------------------------------------------------------------


@pytest.fixture
def invite_user_to_org(test_client, create_user, login_user, whoami):
    """Invite a user to an existing organization and fully accept the invite.

    Mirrors the invite-then-register flow exercised in
    tests/e2e/test_membership.py. Returns a dict with the invited user's
    credentials, access token, user_id, and membership_id so tests can drive
    the user directly.
    """

    def _invite(*, admin_token, org_id, role="member", email=None, password="test123"):
        if admin_token is None:
            pytest.fail("admin_token is required for invite_user_to_org")
        if org_id is None:
            pytest.fail("org_id is required for invite_user_to_org")

        email = email or f"rbac_{uuid.uuid4().hex[:8]}@test.com"

        invite_resp = test_client.post(
            f"/api/organizations/{org_id}/members",
            json={"organization_id": org_id, "email": email, "role": role},
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Organization-Id": str(org_id),
            },
        )
        assert invite_resp.status_code == 200, invite_resp.json()
        membership = invite_resp.json()

        # Register the invited user using the same email — this causes the
        # pending membership to be linked to the new user.
        user = create_user(email=email, password=password)
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        user_id = me["id"]

        # Confirm invite landed us in the expected org.
        org_ids = [o["id"] for o in me["organizations"]]
        assert org_id in org_ids, (
            f"Invited user did not end up in org {org_id}; got {org_ids}"
        )

        return {
            "user": user,
            "token": token,
            "user_id": user_id,
            "membership_id": membership["id"],
            "email": email,
            "role": role,
        }

    return _invite


@pytest.fixture
def set_member_role(test_client):
    """Update a membership's role via PUT /organizations/{id}/members/{mid}."""

    def _set_role(*, admin_token, org_id, membership_id, role):
        resp = test_client.put(
            f"/api/organizations/{org_id}/members/{membership_id}",
            json={"role": role},
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Organization-Id": str(org_id),
            },
        )
        assert resp.status_code == 200, resp.json()
        return resp.json()

    return _set_role


# ---------------------------------------------------------------------------
# Data source helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def create_sqlite_ds(test_client):
    """Create a SQLite data source backed by the chinook test database.

    The DataSourceService requires a valid connection at creation time for
    system-auth data sources, so we point at tests/config/chinook.sqlite.
    """

    def _create(*, name, user_token, org_id, is_public=True):
        if not CHINOOK_SQLITE_PATH.exists():
            pytest.skip(
                f"SQLite test database missing at {CHINOOK_SQLITE_PATH}"
            )
        payload = {
            "name": name,
            "type": "sqlite",
            "config": {"database": str(CHINOOK_SQLITE_PATH)},
            "credentials": {},
            "auth_policy": "system_only",
            "is_public": is_public,
            "generate_summary": False,
            "generate_conversation_starters": False,
            "generate_ai_rules": False,
        }
        resp = test_client.post(
            "/api/data_sources",
            json=payload,
            headers={
                "Authorization": f"Bearer {user_token}",
                "X-Organization-Id": str(org_id),
            },
        )
        assert resp.status_code == 200, resp.json()
        return resp.json()

    return _create


@pytest.fixture
def grant_ds_membership(test_client):
    """POST /data_sources/{id}/members to add a user as DS member."""

    def _grant(*, admin_token, org_id, data_source_id, user_id,
               principal_type="user", config=None):
        payload = {
            "principal_type": principal_type,
            "principal_id": user_id,
            "config": config,
        }
        resp = test_client.post(
            f"/api/data_sources/{data_source_id}/members",
            json=payload,
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Organization-Id": str(org_id),
            },
        )
        assert resp.status_code == 200, resp.json()
        return resp.json()

    return _grant


@pytest.fixture
def revoke_ds_membership(test_client):
    """DELETE /data_sources/{id}/members/{user_id} to remove DS member."""

    def _revoke(*, admin_token, org_id, data_source_id, user_id):
        resp = test_client.delete(
            f"/api/data_sources/{data_source_id}/members/{user_id}",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Organization-Id": str(org_id),
            },
        )
        assert resp.status_code == 204, getattr(resp, "text", "")
        return True

    return _revoke


# ---------------------------------------------------------------------------
# Principal cast — assembles a reusable "world" for matrix tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def rbac_principals(
    test_client,
    create_user,
    login_user,
    whoami,
    invite_user_to_org,
    create_sqlite_ds,
    grant_ds_membership,
):
    """Assemble a cast of principals against two private data sources.

    The returned dict has:
        admin          org admin (DS creator)
        member         org member, no explicit DS grants
        ds_a_member    org member, explicit DSM to ds_a
        ds_b_member    org member, explicit DSM to ds_b
        outsider       org admin of a *different* organization
        org_id         the primary organization id
        ds_a, ds_b     private data source dicts in the primary org

    A public data source (``ds_public``) is also created so tests that care
    about the public code path have something to exercise.
    """

    # Primary org admin (org auto-created on first user registration)
    admin_user = create_user()
    admin_token = login_user(admin_user["email"], admin_user["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    # Two private data sources + one public
    ds_a = create_sqlite_ds(
        name="rbac-ds-a", user_token=admin_token, org_id=org_id, is_public=False
    )
    ds_b = create_sqlite_ds(
        name="rbac-ds-b", user_token=admin_token, org_id=org_id, is_public=False
    )
    ds_public = create_sqlite_ds(
        name="rbac-ds-public", user_token=admin_token, org_id=org_id, is_public=True
    )

    # Invite members
    plain_member = invite_user_to_org(
        admin_token=admin_token, org_id=org_id, role="member"
    )
    ds_a_member = invite_user_to_org(
        admin_token=admin_token, org_id=org_id, role="member"
    )
    ds_b_member = invite_user_to_org(
        admin_token=admin_token, org_id=org_id, role="member"
    )

    # Grant per-DS access
    grant_ds_membership(
        admin_token=admin_token,
        org_id=org_id,
        data_source_id=ds_a["id"],
        user_id=ds_a_member["user_id"],
    )
    grant_ds_membership(
        admin_token=admin_token,
        org_id=org_id,
        data_source_id=ds_b["id"],
        user_id=ds_b_member["user_id"],
    )

    # Outsider: a separate user who lives in a *different* organization.
    # We use a unique email and explicitly POST a new organization for them
    # since auto-org-creation only fires for the very first uninvited user.
    outsider_email = f"rbac_outsider_{uuid.uuid4().hex[:8]}@test.com"
    outsider_user = create_user(email=outsider_email, password="test123")
    outsider_token = login_user(
        outsider_user["email"], outsider_user["password"]
    )

    # Create a new org owned by the outsider — requires
    # allow_multiple_organizations which the autouse fixture above enables.
    new_org_resp = test_client.post(
        "/api/organizations",
        json={"name": f"outsider-org-{uuid.uuid4().hex[:6]}"},
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert new_org_resp.status_code == 200, new_org_resp.json()
    outsider_org_id = new_org_resp.json()["id"]
    assert outsider_org_id != org_id, (
        "Outsider should not be in the primary org"
    )
    outsider_me = whoami(outsider_token)

    return {
        "org_id": org_id,
        "admin_token": admin_token,
        "admin_user_id": whoami(admin_token)["id"],
        "ds_a": ds_a,
        "ds_b": ds_b,
        "ds_public": ds_public,
        "principals": {
            "admin": {
                "token": admin_token,
                "user_id": whoami(admin_token)["id"],
                "role": "admin",
            },
            "member": {
                "token": plain_member["token"],
                "user_id": plain_member["user_id"],
                "role": "member",
                "membership_id": plain_member["membership_id"],
            },
            "ds_a_member": {
                "token": ds_a_member["token"],
                "user_id": ds_a_member["user_id"],
                "role": "member",
                "membership_id": ds_a_member["membership_id"],
            },
            "ds_b_member": {
                "token": ds_b_member["token"],
                "user_id": ds_b_member["user_id"],
                "role": "member",
                "membership_id": ds_b_member["membership_id"],
            },
            "outsider": {
                "token": outsider_token,
                "user_id": outsider_me["id"],
                "role": "outsider",
                "own_org_id": outsider_org_id,
            },
        },
    }
