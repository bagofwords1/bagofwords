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
    # A fresh invite expires in the future in absolute (UTC) terms.
    assert created_expiry > datetime.now(UTC)

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
