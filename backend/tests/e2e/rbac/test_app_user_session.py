"""What an app user's own BOW session is, across the surfaces that are not an
artifact page (those are in tests/e2e/dashboards/test_app_users.py).

* ``whoami`` reports the role ``app_user`` and none of the permissions every
  workspace member holds, so the frontend sends them to the launcher. A
  workspace member's and an admin's report is unchanged.
* An admin can invite someone straight in as an app user.
* The gate is per organization: an app user of one organization who owns
  another keeps the workspace of the one they own.
* Other ways into the workspace are closed too: MCP, and an API key they
  created while they still had it.
"""
import pytest

BASELINE = {"view_reports", "create_reports", "update_reports", "delete_reports", "publish_reports",
            "view_members", "manage_files"}
REFUSED = "workspace.access_required"


def _h(token, org_id=None):
    headers = {"Authorization": f"Bearer {token}"}
    if org_id:
        headers["X-Organization-Id"] = str(org_id)
    return headers


@pytest.fixture
def org(create_user, login_user, whoami):
    """An organization and its admin."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    return {"token": token, "id": whoami(token)["organizations"][0]["id"]}


def _org_of(whoami, token, org_id):
    return next(o for o in whoami(token)["organizations"] if o["id"] == org_id)


def _set_role(client, org, member, role):
    r = client.put(f"/api/organizations/{org['id']}/members/{member['membership_id']}", json={"role": role},
                   headers=_h(org["token"], org["id"]))
    assert r.status_code == 200, r.text


@pytest.mark.e2e
def test_whoami_tells_an_app_user_from_a_member_and_an_admin(test_client, org, invite_user_to_org, whoami):
    member = invite_user_to_org(org_id=org["id"], admin_token=org["token"])
    as_member = _org_of(whoami, member["token"], org["id"])
    assert as_member["role"] == "member"
    assert BASELINE | {"access_workspace"} <= set(as_member["permissions"])
    admin = _org_of(whoami, org["token"], org["id"])
    assert admin["role"] == "admin" and "full_admin_access" in admin["permissions"]

    _set_role(test_client, org, member, "app_user")
    as_app_user = _org_of(whoami, member["token"], org["id"])
    assert as_app_user["role"] == "app_user"
    assert not (BASELINE | {"access_workspace", "full_admin_access"}) & set(as_app_user["permissions"])
    # The organizations list (the org switcher) says the same.
    listed = test_client.get("/api/organizations", headers=_h(member["token"]))
    assert listed.status_code == 200 and [o["role"] for o in listed.json()] == ["app_user"]


@pytest.mark.e2e
def test_an_admin_can_invite_someone_as_an_app_user(test_client, org, invite_user_to_org, whoami):
    invited = invite_user_to_org(org_id=org["id"], admin_token=org["token"], role="app_user")
    assert _org_of(whoami, invited["token"], org["id"])["role"] == "app_user"
    refused = test_client.get("/api/reports", headers=_h(invited["token"], org["id"]))
    assert refused.status_code == 403 and refused.json()["error_code"] == REFUSED
    assert test_client.get("/api/apps", headers=_h(invited["token"], org["id"])).status_code == 200


@pytest.mark.e2e
def test_the_gate_is_per_organization(test_client, org, invite_user_to_org, whoami):
    """An app user here who owns an organization elsewhere keeps that one."""
    person = invite_user_to_org(org_id=org["id"], admin_token=org["token"], role="app_user")
    created = test_client.post("/api/organizations", json={"name": "Their own"}, headers=_h(person["token"]))
    # As an app user everywhere, creating an organization is the workspace too.
    assert created.status_code == 403 and created.json()["error_code"] == REFUSED

    _set_role(test_client, org, person, "member")
    own = test_client.post("/api/organizations", json={"name": "Their own"}, headers=_h(person["token"]))
    assert own.status_code == 200, own.text
    _set_role(test_client, org, person, "app_user")

    roles = {o["id"]: o["role"] for o in whoami(person["token"])["organizations"]}
    assert roles == {org["id"]: "app_user", own.json()["id"]: "admin"}
    assert test_client.get("/api/reports", headers=_h(person["token"], own.json()["id"])).status_code == 200
    there = test_client.get("/api/reports", headers=_h(person["token"], org["id"]))
    assert there.status_code == 403 and there.json()["error_code"] == REFUSED


@pytest.mark.e2e
@pytest.mark.parametrize("method,path", [("GET", "/api/mcp/tools"), ("POST", "/api/mcp")])
def test_mcp_is_the_workspace(test_client, org, invite_user_to_org, method, path):
    person = invite_user_to_org(org_id=org["id"], admin_token=org["token"])
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"} if method == "POST" else None
    before = test_client.request(method, path, headers=_h(person["token"], org["id"]), json=body)
    assert before.status_code == 200, before.text
    _set_role(test_client, org, person, "app_user")
    after = test_client.request(method, path, headers=_h(person["token"], org["id"]), json=body)
    assert after.status_code == 403 and after.json()["error_code"] == REFUSED


@pytest.mark.e2e
def test_an_api_key_is_its_owner(test_client, org, invite_user_to_org):
    person = invite_user_to_org(org_id=org["id"], admin_token=org["token"])
    created = test_client.post("/api/api_keys", json={"name": "k"}, headers=_h(person["token"], org["id"]))
    assert created.status_code == 200, created.text
    key = {"Authorization": f"Bearer {created.json()['key']}", "X-Organization-Id": org["id"]}
    assert test_client.get("/api/reports", headers=key).status_code == 200
    _set_role(test_client, org, person, "app_user")
    refused = test_client.get("/api/reports", headers=key)
    assert refused.status_code == 403 and refused.json()["error_code"] == REFUSED
    assert test_client.post("/api/api_keys", json={"name": "k2"},
                            headers=_h(person["token"], org["id"])).status_code == 403
