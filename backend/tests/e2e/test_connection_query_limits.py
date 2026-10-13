"""Per-connection query limits: timeout and concurrency overrides.

Both limits already resolve connection-config > org default > built-in at
query time; these tests cover the API that lets an admin set them:
  - the detail payload reports the override (null = inherit) and the org
    default it replaces,
  - PUT sets / clears (0) an override without touching the rest of the config,
  - editing the connection's own fields keeps the overrides,
  - out-of-range values are rejected, and a plain member cannot set them,
  - a connection carrying overrides still builds a working client.
"""
import uuid
from pathlib import Path

import pytest

CONNECTION_TEST_DB_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite"
).resolve()


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def admin_conn(create_user, login_user, whoami, create_connection):
    if not CONNECTION_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {CONNECTION_TEST_DB_PATH}")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    conn = create_connection(
        name=f"Limits {uuid.uuid4().hex[:6]}",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    return token, org_id, conn["id"]


@pytest.mark.e2e
def test_detail_reports_inherited_limits_and_org_defaults(admin_conn, get_connection):
    token, org_id, cid = admin_conn
    detail = get_connection(connection_id=cid, user_token=token, org_id=org_id)
    assert detail["query_timeout_seconds"] is None
    assert detail["max_concurrent_queries"] is None
    assert detail["default_query_timeout_seconds"] > 0
    assert detail["default_max_concurrent_queries"] > 0


@pytest.mark.e2e
def test_set_and_clear_overrides_keeps_connection_config(admin_conn, get_connection, update_connection):
    token, org_id, cid = admin_conn

    update_connection(connection_id=cid, payload={"query_timeout_seconds": 45, "max_concurrent_queries": 2},
                      user_token=token, org_id=org_id)
    detail = get_connection(connection_id=cid, user_token=token, org_id=org_id)
    assert detail["query_timeout_seconds"] == 45
    assert detail["max_concurrent_queries"] == 2
    assert detail["config"]["database"] == str(CONNECTION_TEST_DB_PATH)

    # Clearing one override leaves the other alone.
    update_connection(connection_id=cid, payload={"query_timeout_seconds": 0},
                      user_token=token, org_id=org_id)
    detail = get_connection(connection_id=cid, user_token=token, org_id=org_id)
    assert detail["query_timeout_seconds"] is None
    assert detail["max_concurrent_queries"] == 2


@pytest.mark.e2e
def test_editing_connection_fields_preserves_overrides(admin_conn, get_connection, update_connection):
    token, org_id, cid = admin_conn
    update_connection(connection_id=cid, payload={"query_timeout_seconds": 90, "max_concurrent_queries": 3},
                      user_token=token, org_id=org_id)

    # The connection form sends only the type's own fields.
    update_connection(connection_id=cid, payload={"config": {"database": str(CONNECTION_TEST_DB_PATH)}},
                      user_token=token, org_id=org_id)
    detail = get_connection(connection_id=cid, user_token=token, org_id=org_id)
    assert detail["query_timeout_seconds"] == 90
    assert detail["max_concurrent_queries"] == 3


@pytest.mark.e2e
@pytest.mark.parametrize("payload", [
    {"query_timeout_seconds": -1},
    {"query_timeout_seconds": 999_999},
    {"max_concurrent_queries": -5},
    {"max_concurrent_queries": 10_000},
])
def test_out_of_range_limits_are_rejected(admin_conn, test_client, get_connection, payload):
    token, org_id, cid = admin_conn
    resp = test_client.put(f"/api/connections/{cid}", json=payload, headers=_headers(token, org_id))
    assert resp.status_code == 422
    detail = get_connection(connection_id=cid, user_token=token, org_id=org_id)
    assert detail["query_timeout_seconds"] is None
    assert detail["max_concurrent_queries"] is None


@pytest.mark.e2e
def test_member_cannot_set_limits(admin_conn, test_client, create_user, login_user, get_connection):
    token, org_id, cid = admin_conn
    email = f"limits-member-{uuid.uuid4().hex[:8]}@example.com"
    invited = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": "member"},
        headers=_headers(token, org_id),
    )
    assert invited.status_code == 200, invited.json()
    member = create_user(email=email)
    member_token = login_user(member["email"], member["password"])

    resp = test_client.put(f"/api/connections/{cid}", json={"query_timeout_seconds": 5},
                           headers=_headers(member_token, org_id))
    assert resp.status_code == 403
    detail = get_connection(connection_id=cid, user_token=token, org_id=org_id)
    assert detail["query_timeout_seconds"] is None


@pytest.mark.e2e
def test_connection_with_overrides_still_builds_a_client(admin_conn, update_connection, test_connection_connectivity):
    """The overrides live in `config`; they must never reach a client
    constructor that does not take them."""
    token, org_id, cid = admin_conn
    update_connection(connection_id=cid, payload={"query_timeout_seconds": 30, "max_concurrent_queries": 1},
                      user_token=token, org_id=org_id)
    probe = test_connection_connectivity(connection_id=cid, user_token=token, org_id=org_id)
    assert probe["success"] is True
