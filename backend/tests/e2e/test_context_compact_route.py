"""E2E: the composer's /compact and the divider's summary through the real API.

- POST /api/reports/{id}/context/compact accepts no body (the popover button)
  or {instructions, keep} (the composer command), validated at the edge.
- GET /api/reports/{id}/context/summary returns the compaction state plus the
  rendered summary, owner-only like the transcript itself.
"""
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _owner_and_member(create_user, login_user, whoami, test_client):
    owner_email = f"cmp_owner_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=owner_email, password="test123")
    owner = login_user(email=owner_email, password="test123")
    org_id = whoami(owner)["organizations"][0]["id"]
    member_email = f"cmp_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_headers(owner, org_id),
    )
    create_user(email=member_email, password="test123")
    member = login_user(email=member_email, password="test123")
    return owner, member, org_id


@pytest.mark.e2e
@pytest.mark.parametrize("body", [
    {"instructions": "x" * 801},
    {"keep": 0},
    {"keep": 21},
    {"keep": "many"},
])
def test_compact_rejects_invalid_arguments(body, create_user, login_user, whoami, test_client, create_report):
    owner, _, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    report = create_report(title="Compact args", user_token=owner, org_id=org_id, data_sources=[])
    resp = test_client.post(f"/api/reports/{report['id']}/context/compact", json=body, headers=_headers(owner, org_id))
    assert resp.status_code == 422, resp.json()


@pytest.mark.e2e
@pytest.mark.parametrize("body", [None, {}, {"instructions": "focus on churn"}, {"keep": 4}])
def test_compact_accepts_command_arguments(body, create_user, login_user, whoami, test_client, create_report):
    """Valid arguments get past validation and permission checks. No LLM is
    configured in this environment, so the route stops at model resolution —
    anything but a validation or ownership rejection proves the contract."""
    owner, _, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    report = create_report(title="Compact ok", user_token=owner, org_id=org_id, data_sources=[])
    kwargs = {"json": body} if body is not None else {}
    resp = test_client.post(f"/api/reports/{report['id']}/context/compact", headers=_headers(owner, org_id), **kwargs)
    assert resp.status_code not in (403, 404, 422), resp.json()


@pytest.mark.e2e
def test_context_summary_is_owner_only(create_user, login_user, whoami, test_client, create_report):
    owner, member, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    report = create_report(title="Summary access", user_token=owner, org_id=org_id, data_sources=[])

    resp = test_client.get(f"/api/reports/{report['id']}/context/summary", headers=_headers(owner, org_id))
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["covers_until_completion_id"] is None
    assert data["covered_turns"] == 0
    assert data["summary_text"] == ""

    resp = test_client.get(f"/api/reports/{report['id']}/context/summary", headers=_headers(member, org_id))
    assert resp.status_code == 403
