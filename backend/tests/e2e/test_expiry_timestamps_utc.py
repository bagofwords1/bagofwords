"""Expiry timestamps in API responses must carry an explicit UTC offset.

The columns are naive TIMESTAMP values holding UTC. Serialized without an
offset, browsers parse them as *local* time, so a viewer east of UTC sees an
invite or key expire hours early (and west of UTC, hours late). Every expiry
field the API returns must therefore be offset-aware and equal to the stored
UTC instant.
"""
import uuid
from datetime import UTC, datetime

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


def _assert_utc_aware(value):
    assert isinstance(value, str) and value, f"expected a timestamp, got {value!r}"
    assert value.endswith("Z") or value.endswith("+00:00"), (
        f"timestamp {value!r} has no UTC offset; browsers will parse it as local time"
    )
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.utcoffset().total_seconds() == 0
    return parsed


@pytest.fixture
def admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    return token, whoami(token)["organizations"][0]["id"]


@pytest.mark.e2e
def test_invite_expiry_is_serialized_with_utc_offset(test_client, admin):
    token, org_id = admin
    email = f"tz_{uuid.uuid4().hex[:8]}@test.com"

    created = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": "member"},
        headers=_hdr(token, org_id),
    )
    assert created.status_code == 200, created.json()
    created_expiry = _assert_utc_aware(created.json()["invite_expires_at"])
    members = test_client.get(f"/api/organizations/{org_id}/members", headers=_hdr(token, org_id))
    assert members.status_code == 200, members.json()
    pending = [m for m in members.json() if m.get("email") == email]
    assert len(pending) == 1
    assert _assert_utc_aware(pending[0]["invite_expires_at"]) == created_expiry


@pytest.mark.e2e
@pytest.mark.parametrize("naive_input", ["2031-03-04T05:06:07", "2040-11-30T23:59:00"])
def test_api_key_expiry_naive_input_is_treated_as_utc(test_client, admin, naive_input):
    """Clients that send a naive expires_at keep working; it means UTC."""
    token, org_id = admin
    expected = datetime.fromisoformat(naive_input).replace(tzinfo=UTC)

    created = test_client.post(
        "/api/api_keys", json={"name": "tz", "expires_at": naive_input}, headers=_hdr(token, org_id)
    )
    assert created.status_code == 200, created.json()
    assert _assert_utc_aware(created.json()["expires_at"]) == expected

    listed = test_client.get("/api/api_keys", headers=_hdr(token, org_id))
    assert listed.status_code == 200, listed.json()
    key = next(k for k in listed.json() if k["id"] == created.json()["id"])
    assert _assert_utc_aware(key["expires_at"]) == expected


@pytest.mark.e2e
def test_service_account_key_expiry_is_serialized_with_utc_offset(test_client, admin):
    token, org_id = admin
    naive_input = "2033-07-08T09:10:11"
    expected = datetime(2033, 7, 8, 9, 10, 11, tzinfo=UTC)

    sa = test_client.post(
        "/api/service_accounts", json={"name": f"sa_{uuid.uuid4().hex[:8]}"}, headers=_hdr(token, org_id)
    )
    assert sa.status_code == 200, sa.text
    sa_id = sa.json()["id"]

    key = test_client.post(
        f"/api/service_accounts/{sa_id}/keys",
        json={"name": "k", "expires_at": naive_input},
        headers=_hdr(token, org_id),
    )
    assert key.status_code == 200, key.text
    assert _assert_utc_aware(key.json()["expires_at"]) == expected

    detail = test_client.get(f"/api/service_accounts/{sa_id}", headers=_hdr(token, org_id))
    assert detail.status_code == 200, detail.text
    listed = next(k for k in detail.json()["keys"] if k["id"] == key.json()["id"])
    assert _assert_utc_aware(listed["expires_at"]) == expected


# --- Offset-aware expiry input -------------------------------------------
#
# The columns are naive and the services compare them with naive utcnow(), so
# an aware expires_at must be stored as the same instant in naive UTC. Before
# the schema normalized it, SQLite dropped the offset (storing local wall time
# as if it were UTC) and Postgres rejected the aware value outright.

AWARE_INPUT = "2030-01-01T12:00:00+02:00"
AWARE_EXPECTED = datetime(2030, 1, 1, 10, 0, tzinfo=UTC)


@pytest.mark.e2e
def test_api_key_expiry_aware_input_is_stored_as_the_utc_instant(test_client, admin, api_key_request):
    token, org_id = admin

    created = test_client.post(
        "/api/api_keys", json={"name": "tz-aware", "expires_at": AWARE_INPUT}, headers=_hdr(token, org_id)
    )
    assert created.status_code == 200, created.text
    assert _assert_utc_aware(created.json()["expires_at"]) == AWARE_EXPECTED

    listed = test_client.get("/api/api_keys", headers=_hdr(token, org_id))
    assert listed.status_code == 200, listed.text
    key = next(k for k in listed.json() if k["id"] == created.json()["id"])
    assert _assert_utc_aware(key["expires_at"]) == AWARE_EXPECTED

    # The expiry check compares the stored value with naive utcnow(): it must
    # not raise, and a key that expires in 2030 still authenticates.
    who = api_key_request(method="GET", url="/api/users/whoami", api_key=created.json()["key"])
    assert who.status_code == 200, who.text


@pytest.mark.e2e
def test_service_account_key_expiry_aware_input_is_stored_as_the_utc_instant(test_client, admin, api_key_request):
    token, org_id = admin

    sa = test_client.post(
        "/api/service_accounts", json={"name": f"sa_{uuid.uuid4().hex[:8]}"}, headers=_hdr(token, org_id)
    )
    assert sa.status_code == 200, sa.text
    sa_id = sa.json()["id"]

    key = test_client.post(
        f"/api/service_accounts/{sa_id}/keys",
        json={"name": "k", "expires_at": AWARE_INPUT},
        headers=_hdr(token, org_id),
    )
    assert key.status_code == 200, key.text
    assert _assert_utc_aware(key.json()["expires_at"]) == AWARE_EXPECTED

    detail = test_client.get(f"/api/service_accounts/{sa_id}", headers=_hdr(token, org_id))
    assert detail.status_code == 200, detail.text
    listed = next(k for k in detail.json()["keys"] if k["id"] == key.json()["id"])
    assert _assert_utc_aware(listed["expires_at"]) == AWARE_EXPECTED

    who = api_key_request(method="GET", url="/api/users/whoami", api_key=key.json()["key"])
    assert who.status_code == 200, who.text


@pytest.mark.e2e
def test_user_credential_expiry_aware_input_is_stored_as_the_utc_instant(test_client, admin, create_connection):
    from tests.fixtures.connection_tool import _skip_network

    token, org_id = admin
    h = _hdr(token, org_id)
    with _skip_network():
        conn = create_connection(
            name=f"sf_{uuid.uuid4().hex[:8]}", type="snowflake",
            config={"account": "acme", "warehouse": "wh", "database": "db"},
            credentials={}, auth_policy="user_required",
            allowed_user_auth_modes=["userpass"],
            user_token=token, org_id=org_id,
        )
        ds = test_client.post("/api/data_sources", headers=h, json={
            "name": "sf-agent", "connection_ids": [conn["id"]],
            "generate_summary": False, "generate_conversation_starters": False,
            "generate_ai_rules": False,
        })
    assert ds.status_code == 200, ds.text
    url = f"/api/data_sources/{ds.json()['id']}/my-credentials"

    created = test_client.post(url, headers=h, json={
        "auth_mode": "userpass",
        "credentials": {"user": "u", "password": "pw"},
        "expires_at": AWARE_INPUT,
    })
    assert created.status_code == 200, created.text
    assert _assert_utc_aware(created.json()["expires_at"]) == AWARE_EXPECTED

    stored = test_client.get(url, headers=h)
    assert stored.status_code == 200, stored.text
    assert _assert_utc_aware(stored.json()["expires_at"]) == AWARE_EXPECTED


@pytest.mark.e2e
def test_scim_token_expiry_aware_input_is_stored_as_the_utc_instant(test_client, admin):
    token, org_id = admin

    created = test_client.post(
        "/api/enterprise/scim/tokens",
        json={"name": "tz-aware", "expires_at": AWARE_INPUT},
        headers=_hdr(token, org_id),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    # The token response carries an explicit UTC offset on both timestamps.
    assert _assert_utc_aware(body["expires_at"]) == AWARE_EXPECTED
    _assert_utc_aware(body["created_at"])

    listed = test_client.get("/api/enterprise/scim/tokens", headers=_hdr(token, org_id))
    assert listed.status_code == 200, listed.text
    row = next(t for t in listed.json() if t["id"] == body["id"])
    assert _assert_utc_aware(row["expires_at"]) == AWARE_EXPECTED
    _assert_utc_aware(row["created_at"])

    # The SCIM auth check compares with naive utcnow(); the token still works.
    users = test_client.get("/scim/v2/Users", headers={"Authorization": f"Bearer {body['token']}"})
    assert users.status_code == 200, users.text
