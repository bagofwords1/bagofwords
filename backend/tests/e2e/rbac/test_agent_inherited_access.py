"""The agent access panel must name everyone who reaches the agent, not only
the principals granted on the agent itself: folder and connection grants also
confer access (see ``app/services/agent_inherited_access.py``)."""

import pytest


@pytest.fixture
def world(
    test_client, enterprise_license, bootstrap_admin, invite_user_to_org,
    create_group, add_user_to_group, sqlite_data_source,
):
    admin = bootstrap_admin("admin")
    org = admin["org_id"]

    def api(actor, method, path, body=None, status=200):
        response = test_client.request(
            method, "/api/" + path, json=body,
            headers={"Authorization": "Bearer " + actor["token"], "X-Organization-Id": str(org)},
        )
        assert response.status_code == status, response.text
        return response.json()

    def invite():
        return invite_user_to_org(org_id=org, admin_token=admin["token"])

    def group(name, *members):
        created = create_group(name=name, user_token=admin["token"], org_id=org)
        assert created.status_code == 200, created.text
        for m in members:
            added = add_user_to_group(group_id=created.json()["id"], user_id=m["user_id"],
                                      user_token=admin["token"], org_id=org)
            assert added.status_code in (200, 201), added.text
        return created.json()["id"]

    def share(folder, kind, principal, role):
        api(admin, "PUT", f"folders/{folder}/share",
            {"principal_type": kind, "principal_id": principal, "role": role})

    root = api(admin, "POST", "folders", {"name": "Finance"})["id"]
    leaf = api(admin, "POST", "folders", {"name": "Forecasts", "parent_id": root})["id"]
    agent = sqlite_data_source(name="forecast-agent", user_token=admin["token"], org_id=org)
    api(admin, "PUT", f"folders/items/agent/{agent['id']}/placement", {"parent_id": leaf})

    def inherited(actor, status=200):
        return api(actor, "GET", f"data_sources/{agent['id']}/inherited_access", status=status)

    return dict(admin=admin, org=org, api=api, invite=invite, group=group, share=share,
                root=root, leaf=leaf, agent=agent, inherited=inherited)


def _rows(payload):
    return {(p["principal_type"], p["principal_id"]): p for p in payload["principals"]}


@pytest.mark.e2e
def test_folder_grants_are_listed_with_their_source(world):
    w = world
    analyst, lead = w["invite"](), w["invite"]()
    analysts = w["group"]("Analysts", analyst)
    w["share"](w["root"], "group", analysts, "Viewer")
    w["share"](w["leaf"], "user", lead["user_id"], "Manager")

    # The premise: these principals really do reach the agent, yet the direct
    # members list does not mention them.
    for actor in (analyst, lead):
        assert w["agent"]["id"] in {d["id"] for d in w["api"](actor, "GET", "data_sources/active")}
    direct = {m["principal_id"] for m in w["api"](w["admin"], "GET", f"data_sources/{w['agent']['id']}/members")}
    assert analysts not in direct and lead["user_id"] not in direct

    payload = w["inherited"](w["admin"])
    rows = _rows(payload)
    assert payload["hidden"] is False
    via_group = rows["group", analysts]
    assert (via_group["access"], via_group["source_type"], via_group["source_id"]) == ("query", "folder", w["root"])
    assert via_group["source_name"] == "Finance" and via_group["principal_name"] == "Analysts"
    via_user = rows["user", lead["user_id"]]
    assert (via_user["access"], via_user["source_type"], via_user["source_id"]) == ("manage", "folder", w["leaf"])


@pytest.mark.e2e
def test_inheritance_boundaries_stop_the_listing(world):
    w = world
    outsider = w["invite"]()
    team = w["group"]("Team", outsider)
    w["share"](w["root"], "group", team, "Viewer")
    assert ("group", team) in _rows(w["inherited"](w["admin"]))

    # A limited-access folder no longer inherits from its parent.
    w["share"](w["leaf"], "user", w["admin"]["user_id"], "Manager")
    w["api"](w["admin"], "PUT", f"folders/{w['leaf']}/inheritance", {"limited_access": True})
    assert ("group", team) not in _rows(w["inherited"](w["admin"]))
    assert w["agent"]["id"] not in {d["id"] for d in w["api"](outsider, "GET", "data_sources/active")}


@pytest.mark.e2e
def test_folder_audience_is_only_named_to_folder_managers(world, test_client):
    w = world
    agent_id = w["agent"]["id"]
    agent_manager, stranger, reader = w["invite"](), w["invite"](), w["invite"]()
    readers = w["group"]("Readers", reader)
    w["share"](w["root"], "group", readers, "Viewer")

    # Manages the agent through a direct grant, but manages no folder.
    w["api"](w["admin"], "POST", f"data_sources/{agent_id}/members",
             {"principal_type": "user", "principal_id": agent_manager["user_id"]})
    grant = next(m for m in w["api"](w["admin"], "GET", f"data_sources/{agent_id}/members")
                 if m["principal_id"] == agent_manager["user_id"])
    updated = test_client.put(
        f"/api/organizations/{w['org']}/resource-grants/{grant['id']}", json={"permissions": ["manage"]},
        headers={"Authorization": "Bearer " + w["admin"]["token"], "X-Organization-Id": str(w["org"])})
    assert updated.status_code == 200, updated.text

    # Told that more exists, not who. A folder viewer is in the same position.
    for actor in (agent_manager, reader):
        payload = w["inherited"](actor)
        assert payload["hidden"] is True
        assert not [p for p in payload["principals"] if p["source_type"] == "folder"]
    assert ("group", readers) in _rows(w["inherited"](w["admin"]))
    # No access to the agent at all: no audience either.
    w["inherited"](stranger, status=403)


@pytest.mark.e2e
def test_connection_managers_are_listed(world, grant_resource):
    w = world
    operator = w["invite"]()
    operators = w["group"]("Operators", operator)
    detail = w["api"](w["admin"], "GET", f"data_sources/{w['agent']['id']}")
    connection = detail["connections"][0]
    assert ("group", operators) not in _rows(w["inherited"](w["admin"]))

    granted = grant_resource(
        resource_type="connection", resource_id=connection["id"], principal_type="group",
        principal_id=operators, permissions=["manage_data_sources"],
        user_token=w["admin"]["token"], org_id=w["org"])
    assert granted.status_code == 200, granted.text

    row = _rows(w["inherited"](w["admin"]))["group", operators]
    assert (row["access"], row["source_type"], row["source_id"]) == ("manage", "connection", connection["id"])
    # The operator manages the agent through the connection, so may see the list too.
    assert ("group", operators) in _rows(w["inherited"](operator))
