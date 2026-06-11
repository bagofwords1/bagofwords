"""e2e tests for enterprise license quotas: max_users (members/invites) and
max_agents (data sources) per organization.

The license signature path is exercised elsewhere; here we drive the *enforcement*
by seeding the in-process license cache directly (no private signing key needed).
A quota of -1 means unlimited, which is also the default when a license omits it.

Covers:
 - single invite blocked once the seat cap is reached (active members + pending invites)
 - no cap when max_users is unset (-1)
 - bulk CSV/xlsx import reports the seat overflow in both dry-run and real runs
 - data source ("agent") creation blocked once the agent cap is reached
 - no cap when max_agents is unset (-1)
 - GET /license/usage reports current vs allowed for the active org
"""
import contextlib
import io
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import app.ee.license as license_mod
from app.ee.license import LicenseInfo


CHINOOK_DB = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


@contextlib.contextmanager
def _license(*, max_users: int = -1, max_agents: int = -1):
    """Temporarily seed an active enterprise license with the given quotas.

    Writes the module-level cache so every caller — services and routes — reads the
    same live LicenseInfo, regardless of how each module imported the helpers.
    """
    prev_cache = license_mod._cached_license
    prev_init = license_mod._cache_initialized
    license_mod._cached_license = LicenseInfo(
        licensed=True,
        tier="enterprise",
        org_name="Test Org",
        expires_at=datetime.now(timezone.utc) + timedelta(days=365),
        max_users=max_users,
        max_agents=max_agents,
    )
    license_mod._cache_initialized = True
    try:
        yield
    finally:
        license_mod._cached_license = prev_cache
        license_mod._cache_initialized = prev_init


def _auth_headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


def _admin_setup(create_user, login_user, whoami):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return admin, token, org_id


def _invite(test_client, token, org_id, email, role="member"):
    return test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": role},
        headers=_auth_headers(token, org_id),
    )


def _csv_bytes(emails):
    out = io.StringIO()
    out.write("email,note\n")
    for email in emails:
        out.write(f"{email},\n")
    return out.getvalue().encode("utf-8")


def _rand_email(prefix="u"):
    return f"{prefix}_{uuid.uuid4().hex[:8]}@test.com"


# ---------------------------------------------------------------------------
# max_users — single invites
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_user_limit_blocks_invite_when_cap_reached(test_client, create_user, login_user, whoami):
    # Org starts with 1 membership (the admin). Cap at 2 → exactly one more invite.
    _, token, org_id = _admin_setup(create_user, login_user, whoami)
    with _license(max_users=2):
        first = _invite(test_client, token, org_id, _rand_email())
        assert first.status_code == 200, first.json()

        second = _invite(test_client, token, org_id, _rand_email())
        assert second.status_code == 402, second.json()
        assert "limit" in second.json()["detail"].lower()


@pytest.mark.e2e
def test_pending_invites_count_toward_user_limit(test_client, create_user, login_user, whoami):
    """Pending (unaccepted) invites consume seats too — the cap can't be bypassed."""
    _, token, org_id = _admin_setup(create_user, login_user, whoami)
    with _license(max_users=2):
        # admin (1) + one pending invite (2) = full
        assert _invite(test_client, token, org_id, _rand_email()).status_code == 200
        # even though the invite is still pending, the next one is blocked
        assert _invite(test_client, token, org_id, _rand_email()).status_code == 402


@pytest.mark.e2e
def test_user_limit_unlimited_when_unset(test_client, create_user, login_user, whoami):
    _, token, org_id = _admin_setup(create_user, login_user, whoami)
    with _license(max_users=-1):
        for _ in range(5):
            assert _invite(test_client, token, org_id, _rand_email()).status_code == 200


@pytest.mark.e2e
def test_no_license_means_no_user_cap(test_client, create_user, login_user, whoami):
    """Without any license seeded (community mode), invites are unlimited."""
    _, token, org_id = _admin_setup(create_user, login_user, whoami)
    for _ in range(4):
        assert _invite(test_client, token, org_id, _rand_email()).status_code == 200


# ---------------------------------------------------------------------------
# max_users — bulk import
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.parametrize("dry_run", [True, False])
def test_import_respects_user_limit(test_client, create_user, login_user, whoami, dry_run):
    # Org has the admin (1). Cap 2 → only ONE new member fits; the rest overflow.
    _, token, org_id = _admin_setup(create_user, login_user, whoami)
    emails = [_rand_email("imp") for _ in range(3)]
    csv = _csv_bytes(emails)

    with _license(max_users=2):
        resp = test_client.post(
            f"/api/organizations/{org_id}/members/import?dry_run={'true' if dry_run else 'false'}",
            files={"file": ("members.csv", csv, "text/csv")},
            headers=_auth_headers(token, org_id),
        )
    assert resp.status_code == 200, resp.json()
    report = resp.json()
    assert report["summary"]["created"] == 1
    assert report["summary"]["errors"] == 2
    overflow = [r for r in report["rows"] if r["status"] == "error"]
    assert overflow and all("limit" in r["error"].lower() for r in overflow)


# ---------------------------------------------------------------------------
# max_agents — data sources
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_agent_limit_blocks_data_source(test_client, create_data_source, create_user, login_user, whoami):
    if not CHINOOK_DB.exists():
        pytest.skip(f"SQLite test database missing at {CHINOOK_DB}")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    def _make(name):
        return test_client.post(
            "/api/data_sources",
            json={
                "name": name,
                "type": "sqlite",
                "config": {"database": str(CHINOOK_DB)},
                "credentials": {},
                "auth_policy": "system_only",
                "generate_summary": False,
                "generate_conversation_starters": False,
                "generate_ai_rules": False,
            },
            headers=_auth_headers(token, org_id),
        )

    with _license(max_agents=1):
        first = _make("agent-1")
        assert first.status_code == 200, first.json()
        second = _make("agent-2")
        assert second.status_code == 402, second.json()
        assert "limit" in second.json()["detail"].lower()


@pytest.mark.e2e
def test_agent_limit_unlimited_when_unset(test_client, create_data_source, create_user, login_user, whoami):
    if not CHINOOK_DB.exists():
        pytest.skip(f"SQLite test database missing at {CHINOOK_DB}")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    with _license(max_agents=-1):
        for i in range(3):
            ds = create_data_source(
                name=f"agent-{i}",
                type="sqlite",
                config={"database": str(CHINOOK_DB)},
                credentials={},
                user_token=token,
                org_id=org_id,
            )
            assert ds["name"] == f"agent-{i}"


# ---------------------------------------------------------------------------
# usage endpoint
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_license_usage_endpoint(test_client, create_user, login_user, whoami):
    _, token, org_id = _admin_setup(create_user, login_user, whoami)
    # admin (1) + one invite = 2 members
    assert _invite(test_client, token, org_id, _rand_email()).status_code == 200

    with _license(max_users=10, max_agents=5):
        resp = test_client.get("/api/license/usage", headers=_auth_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["max_users"] == 10
    assert body["current_users"] == 2
    assert body["max_agents"] == 5
    assert body["current_agents"] == 0


@pytest.mark.e2e
def test_license_usage_reports_unlimited(test_client, create_user, login_user, whoami):
    _, token, org_id = _admin_setup(create_user, login_user, whoami)
    with _license(max_users=-1, max_agents=-1):
        resp = test_client.get("/api/license/usage", headers=_auth_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["max_users"] == -1
    assert body["max_agents"] == -1
    assert body["current_users"] >= 1  # at least the admin
