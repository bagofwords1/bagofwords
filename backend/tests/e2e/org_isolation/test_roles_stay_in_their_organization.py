"""A role held in one organization gives nothing in another.

Contract: permissions are resolved for the organization the request is for.
Someone who is an admin of A and a plain member of B can do admin things in A
and only member things in B.
"""
import uuid

import pytest

from ._shared_member import h, invite, org_ids, two_orgs  # noqa: F401


@pytest.fixture
def admin_of_a_member_of_b(test_client, two_orgs, whoami):
    o = two_orgs
    me = whoami(o["a_token"])
    added = invite(test_client, admin_token=o["b_token"], org_id=o["b_org"], email=me["email"], role="member")
    assert added["user_id"] == me["id"]  # an existing account joins directly
    assert org_ids(whoami, o["a_token"]) == {o["a_org"], o["b_org"]}
    return {**o, "token": o["a_token"]}


def _invite_someone(test_client, token, org_id):
    return test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": f"new-{uuid.uuid4().hex[:8]}@example.com", "role": "member"},
        headers=h(token, org_id),
    )


def test_admin_actions_work_at_home_and_are_refused_in_the_other_organization(
    test_client, admin_of_a_member_of_b,
):
    o = admin_of_a_member_of_b

    # Member management is an admin permission.
    assert _invite_someone(test_client, o["token"], o["a_org"]).status_code == 200
    assert _invite_someone(test_client, o["token"], o["b_org"]).status_code == 403

    # So is managing LLM providers.
    assert test_client.get("/api/llm/providers", headers=h(o["token"], o["a_org"])).status_code == 200
    assert test_client.get("/api/llm/providers", headers=h(o["token"], o["b_org"])).status_code == 403


def test_member_level_access_in_the_other_organization_still_works(test_client, admin_of_a_member_of_b):
    o = admin_of_a_member_of_b
    for path in ("/api/notifications/count", "/api/reports"):
        assert test_client.get(path, headers=h(o["token"], o["b_org"])).status_code == 200, path


def test_the_role_reported_for_each_organization_is_its_own(test_client, admin_of_a_member_of_b, whoami):
    o = admin_of_a_member_of_b
    roles = {org["id"]: org.get("role") for org in whoami(o["token"])["organizations"]}
    assert roles[o["a_org"]] == "admin"
    assert roles[o["b_org"]] == "member"
