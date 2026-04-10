"""
RBAC E2E test fixtures.

These fixtures build the RBAC "cast" (admin, members, groups, roles, grants)
using only the real HTTP API routes — nothing reaches into the DB directly.

Pattern mirrors backend/tests/e2e/test_eval.py:
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
"""
import uuid
from pathlib import Path

import pytest

from app.ee import license as ee_license
from app.settings.config import settings as bow_settings


# Path to the chinook SQLite fixture bundled with the repo.
CHINOOK_PATH = (Path(__file__).resolve().parents[2] / "config" / "chinook.sqlite").resolve()


# ────────────────────────────────────────────────────────────────────────
# RBAC suite needs multi-user, multi-org freedom — patch the feature
# flags for every test in this directory. The dev defaults block
# uninvited signups and additional org creation, both of which are
# essential for outsider / cross-org scenarios.
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _rbac_relaxed_signup_flags():
    flags = bow_settings.bow_config.features
    saved = (
        flags.allow_uninvited_signups,
        flags.allow_multiple_organizations,
    )
    flags.allow_uninvited_signups = True
    flags.allow_multiple_organizations = True
    try:
        yield
    finally:
        flags.allow_uninvited_signups, flags.allow_multiple_organizations = saved


def _headers(token: str, org_id: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }


# ────────────────────────────────────────────────────────────────────────
# Enterprise license stub
# ────────────────────────────────────────────────────────────────────────
#
# Several RBAC routes (custom roles, groups) are gated by
# @require_enterprise. To exercise those codepaths deterministically we
# monkey-patch ``get_license_info`` and ``has_feature`` to report an
# active enterprise license for the duration of a test.
#
# Cache state in the license module is cleared before and after so the
# fake does not leak into other suites.


@pytest.fixture
def enterprise_license(monkeypatch):
    """Force the license layer to report an active enterprise license."""
    fake_info = ee_license.LicenseInfo(
        licensed=True,
        tier="enterprise",
        org_name="e2e-tests",
        features=[
            "audit_logs",
            "step_retention_config",
            "scim",
            "custom_roles",
            "ldap",
        ],
        license_id="e2e-fake",
    )
    monkeypatch.setattr(ee_license, "get_license_info", lambda force_refresh=False: fake_info)
    monkeypatch.setattr(ee_license, "is_enterprise_licensed", lambda: True)
    monkeypatch.setattr(ee_license, "has_feature", lambda feature: True)
    # Also patch the re-exports in routes that imported at module load
    import app.routes.rbac as rbac_routes
    monkeypatch.setattr(rbac_routes, "require_enterprise", lambda feature=None: (lambda fn: fn), raising=False)
    # Clear cached instances
    ee_license._cached_license = fake_info
    ee_license._cache_initialized = True
    yield fake_info
    ee_license._cached_license = None
    ee_license._cache_initialized = False


# ────────────────────────────────────────────────────────────────────────
# Membership / user invite helpers
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def invite_user_to_org(test_client, create_user, login_user, whoami):
    """
    Invite a brand new user into an existing org as a regular member and
    return their bundle {email, password, token, user_id, membership_id}.

    Uses the real POST /organizations/{org_id}/members route (auth-gated
    by manage_members). The invitee then registers + logs in, which
    attaches them to the pending membership via on_after_register.
    """

    def _invite(
        *,
        org_id: str,
        admin_token: str,
        role: str = "member",
        email: str = None,
        password: str = "Test1234!",
    ):
        if email is None:
            email = f"rbac_{uuid.uuid4().hex[:10]}@test.com"

        invite_resp = test_client.post(
            f"/api/organizations/{org_id}/members",
            json={"organization_id": org_id, "email": email, "role": role},
            headers=_headers(admin_token, org_id),
        )
        assert invite_resp.status_code == 200, invite_resp.json()
        membership_id = invite_resp.json().get("id")

        create_user(email=email, password=password)
        token = login_user(email, password)
        info = whoami(token)

        return {
            "email": email,
            "password": password,
            "token": token,
            "user_id": info["id"],
            "membership_id": membership_id,
        }

    return _invite


# ────────────────────────────────────────────────────────────────────────
# Roles / assignments / grants fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def list_roles(test_client):
    def _list(*, user_token: str, org_id: str):
        resp = test_client.get(
            f"/api/organizations/{org_id}/roles",
            headers=_headers(user_token, org_id),
        )
        assert resp.status_code == 200, resp.json()
        return resp.json()

    return _list


@pytest.fixture
def get_system_role(list_roles):
    """Return the system role (admin/member) by name within the given org."""

    def _get(name: str, *, user_token: str, org_id: str):
        roles = list_roles(user_token=user_token, org_id=org_id)
        for r in roles:
            if r["name"] == name:
                return r
        raise AssertionError(f"System role {name!r} not found; have: {[r['name'] for r in roles]}")

    return _get


@pytest.fixture
def create_role(test_client):
    def _create(*, name: str, permissions: list, user_token: str, org_id: str, description: str = None):
        payload = {"name": name, "permissions": permissions}
        if description is not None:
            payload["description"] = description
        resp = test_client.post(
            f"/api/organizations/{org_id}/roles",
            json=payload,
            headers=_headers(user_token, org_id),
        )
        return resp

    return _create


@pytest.fixture
def update_role(test_client):
    def _update(*, role_id: str, user_token: str, org_id: str, name: str = None, permissions: list = None, description: str = None):
        payload = {}
        if name is not None:
            payload["name"] = name
        if permissions is not None:
            payload["permissions"] = permissions
        if description is not None:
            payload["description"] = description
        resp = test_client.put(
            f"/api/organizations/{org_id}/roles/{role_id}",
            json=payload,
            headers=_headers(user_token, org_id),
        )
        return resp

    return _update


@pytest.fixture
def assign_role(test_client):
    def _assign(*, role_id: str, principal_type: str, principal_id: str, user_token: str, org_id: str):
        resp = test_client.post(
            f"/api/organizations/{org_id}/role-assignments",
            json={
                "role_id": role_id,
                "principal_type": principal_type,
                "principal_id": principal_id,
            },
            headers=_headers(user_token, org_id),
        )
        return resp

    return _assign


@pytest.fixture
def list_role_assignments(test_client):
    def _list(*, user_token: str, org_id: str, principal_type: str = None, principal_id: str = None):
        params = {}
        if principal_type is not None:
            params["principal_type"] = principal_type
        if principal_id is not None:
            params["principal_id"] = principal_id
        resp = test_client.get(
            f"/api/organizations/{org_id}/role-assignments",
            headers=_headers(user_token, org_id),
            params=params,
        )
        assert resp.status_code == 200, resp.json()
        return resp.json()

    return _list


@pytest.fixture
def create_group(test_client):
    def _create(*, name: str, user_token: str, org_id: str, description: str = None):
        payload = {"name": name}
        if description is not None:
            payload["description"] = description
        resp = test_client.post(
            f"/api/organizations/{org_id}/groups",
            json=payload,
            headers=_headers(user_token, org_id),
        )
        return resp

    return _create


@pytest.fixture
def add_user_to_group(test_client):
    def _add(*, group_id: str, user_id: str, user_token: str, org_id: str):
        resp = test_client.post(
            f"/api/organizations/{org_id}/groups/{group_id}/members",
            json={"user_id": user_id},
            headers=_headers(user_token, org_id),
        )
        return resp

    return _add


@pytest.fixture
def grant_resource(test_client):
    def _grant(
        *,
        resource_type: str,
        resource_id: str,
        principal_type: str,
        principal_id: str,
        permissions: list,
        user_token: str,
        org_id: str,
    ):
        resp = test_client.post(
            f"/api/organizations/{org_id}/resource-grants",
            json={
                "resource_type": resource_type,
                "resource_id": resource_id,
                "principal_type": principal_type,
                "principal_id": principal_id,
                "permissions": permissions,
            },
            headers=_headers(user_token, org_id),
        )
        return resp

    return _grant


# ────────────────────────────────────────────────────────────────────────
# Data-source factory
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def sqlite_data_source(test_client, create_data_source):
    """Create a SQLite data source pointing at the bundled chinook fixture.

    ``is_public`` defaults to ``False`` because almost every RBAC test
    needs a *private* DS so per-DS grants actually matter. The fixture
    creates via the shared ``create_data_source`` fixture and then flips
    ``is_public`` to the requested value via PUT, asserting the flip
    succeeds so downstream tests never run against the wrong visibility
    model.
    """
    if not CHINOOK_PATH.exists():
        pytest.skip(f"Missing SQLite fixture at {CHINOOK_PATH}")

    def _create(
        *,
        name: str,
        user_token: str,
        org_id: str,
        database: str = None,
        is_public: bool = False,
    ):
        ds = create_data_source(
            name=name,
            type="sqlite",
            config={"database": database or str(CHINOOK_PATH)},
            credentials={},
            user_token=user_token,
            org_id=org_id,
        )
        put_resp = test_client.put(
            f"/api/data_sources/{ds['id']}",
            json={"is_public": is_public},
            headers={
                "Authorization": f"Bearer {user_token}",
                "X-Organization-Id": str(org_id),
            },
        )
        assert put_resp.status_code == 200, (
            f"failed to set is_public={is_public} on {ds['id']}: {put_resp.text}"
        )
        assert put_resp.json()["is_public"] is is_public, put_resp.json()
        ds["is_public"] = is_public
        return ds

    return _create


# ────────────────────────────────────────────────────────────────────────
# Bootstrap helpers
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def bootstrap_admin(test_client, create_user, login_user, whoami):
    """Create a fresh user + brand-new org and return them as the org admin.

    The first user in a clean DB auto-gets an org via
    ``_ensure_org_for_first_uninvited_user``. Subsequent users need an
    org explicitly created via POST /organizations (which is gated only
    by ``allow_multiple_organizations`` — relaxed by the autouse
    ``_rbac_relaxed_signup_flags`` fixture above).
    """

    def _bootstrap(name_prefix: str = "admin"):
        email = f"{name_prefix}_{uuid.uuid4().hex[:10]}@test.com"
        password = "Test1234!"
        create_user(email=email, password=password)
        token = login_user(email, password)
        info = whoami(token)

        # First user in the DB gets an auto-org. Anyone after that needs to create one.
        if info.get("organizations"):
            org = info["organizations"][0]
            org_id = org["id"]
        else:
            org_resp = test_client.post(
                "/api/organizations",
                json={"name": f"org_{uuid.uuid4().hex[:8]}"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert org_resp.status_code == 200, org_resp.text
            org_id = org_resp.json()["id"]
            # Re-fetch whoami so org-level perms reflect the new admin role.
            info = whoami(token)
            org = next((o for o in info["organizations"] if o["id"] == org_id), {})

        return {
            "email": email,
            "password": password,
            "token": token,
            "user_id": info["id"],
            "org_id": org_id,
            "permissions": set(org.get("permissions", [])),
            "roles": org.get("roles", []),
        }

    return _bootstrap
