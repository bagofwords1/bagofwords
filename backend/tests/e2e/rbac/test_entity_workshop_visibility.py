"""Who may see an unpublished entity ("query"), and what the tree badge counts.

Entities have two visibility tiers (app/core/entity_scope.py):

* **catalog** — published and not private: readable by anyone who can reach
  every agent it is attached to;
* **workshop** — drafts, suggestions, archived rows: readable only by the
  owner, an org ``manage_entities`` admin, or a manager holding
  ``create_entities`` on every agent it is attached to.

The workshop rule used to live only in the browser, so the API handed every
member the title, slug, description and owner of other people's drafts on any
agent they could reach. These tests pin the rule at the API, for a member who
CAN reach the agent — the case the data-source access filter does not cover.

``/entities/counts`` feeds the per-agent badge in the /agents tree, so it is
asserted against the list the same caller gets rather than against a constant:
a badge that disagrees with its rows is the bug worth catching.
"""
import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _make_entity(test_client, *, token, org_id, title, slug, ds_ids, status="published"):
    resp = test_client.post(
        "/api/entities/global",
        json={
            "type": "model",
            "title": title,
            "slug": slug,
            "code": "select 1 as v",
            "data": {},
            "tags": [],
            "status": status,
            "data_source_ids": ds_ids,
        },
        headers=_hdr(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _make_workshop(test_client, *, admin_token, org_id, entity_id, owner_state):
    """Drive a row into a workshop state through the update route.

    ``owner_state`` is the (private_status, global_status, status) triple the
    dual-status workflow produces: a live suggestion is
    ``("published", "suggested", "draft")`` and a rejected one
    ``("published", "rejected", "archived")``.
    """
    private_status, global_status, status = owner_state
    resp = test_client.put(
        f"/api/entities/{entity_id}",
        json={
            "private_status": private_status,
            "global_status": global_status,
            "status": status,
        },
        headers=_hdr(admin_token, org_id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["private_status"] == private_status
    assert body["global_status"] == global_status
    assert body["status"] == status
    return body


@pytest.fixture
def workshop_world(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    grant_resource,
):
    """One PUBLIC agent everyone can reach, carrying a catalog row and a
    workshop row, plus a second agent used to prove a single-agent manager's
    authority does not extend across agents."""
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    # Public so the data-source access filter admits every member: what is left
    # deciding visibility is the workshop rule under test, nothing else.
    ds_open = sqlite_data_source(
        name="ws_open", user_token=admin["token"], org_id=org_id, is_public=True
    )
    ds_other = sqlite_data_source(
        name="ws_other", user_token=admin["token"], org_id=org_id, is_public=True
    )

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    grant_resource(
        resource_type="data_source",
        resource_id=ds_open["id"],
        principal_type="user",
        principal_id=manager["user_id"],
        permissions=["create_entities"],
        user_token=admin["token"],
        org_id=org_id,
    )

    catalog = _make_entity(
        test_client, token=admin["token"], org_id=org_id,
        title="ws_catalog", slug=f"ws-catalog-{ds_open['id'][:6]}",
        ds_ids=[ds_open["id"]],
    )
    suggestion = _make_entity(
        test_client, token=admin["token"], org_id=org_id,
        title="ws_suggestion", slug=f"ws-suggestion-{ds_open['id'][:6]}",
        ds_ids=[ds_open["id"]],
    )
    _make_workshop(
        test_client, admin_token=admin["token"], org_id=org_id,
        entity_id=suggestion["id"], owner_state=("published", "suggested", "draft"),
    )
    # Spans both agents: the single-agent manager holds authority over only one
    # of them, so it must stay hidden from them.
    spanning = _make_entity(
        test_client, token=admin["token"], org_id=org_id,
        title="ws_spanning", slug=f"ws-spanning-{ds_other['id'][:6]}",
        ds_ids=[ds_open["id"], ds_other["id"]],
    )
    _make_workshop(
        test_client, admin_token=admin["token"], org_id=org_id,
        entity_id=spanning["id"], owner_state=("published", "suggested", "draft"),
    )

    return {
        "org_id": org_id,
        "ds_open": ds_open,
        "ds_other": ds_other,
        "admin": admin,
        "member": member,
        "manager": manager,
        "catalog": catalog,
        "suggestion": suggestion,
        "spanning": spanning,
    }


def _list_ids(test_client, token, org_id, **params):
    resp = test_client.get("/api/entities", headers=_hdr(token, org_id), params=params)
    assert resp.status_code == 200, resp.text
    return {e["id"] for e in resp.json()}


@pytest.mark.e2e
def test_member_sees_catalog_rows_on_an_agent_they_can_reach(test_client, workshop_world):
    w = workshop_world
    seen = _list_ids(test_client, w["member"]["token"], w["org_id"])
    assert w["catalog"]["id"] in seen

    detail = test_client.get(
        f"/api/entities/{w['catalog']['id']}",
        headers=_hdr(w["member"]["token"], w["org_id"]),
    )
    assert detail.status_code == 200, detail.text


@pytest.mark.e2e
def test_member_never_sees_another_users_workshop_row(test_client, workshop_world):
    """The leak: agent access alone must not expose an unpublished row."""
    w = workshop_world
    seen = _list_ids(test_client, w["member"]["token"], w["org_id"])
    assert w["suggestion"]["id"] not in seen
    assert w["spanning"]["id"] not in seen

    # ...and it cannot be reached by guessing the id either.
    for hidden in (w["suggestion"], w["spanning"]):
        detail = test_client.get(
            f"/api/entities/{hidden['id']}",
            headers=_hdr(w["member"]["token"], w["org_id"]),
        )
        assert detail.status_code in (403, 404), detail.text


@pytest.mark.e2e
def test_owner_and_org_admin_see_their_workshop_rows(test_client, workshop_world):
    """Hiding drafts from strangers must not hide them from the people who
    work on them — here the admin, who owns and may review both."""
    w = workshop_world
    seen = _list_ids(test_client, w["admin"]["token"], w["org_id"])
    assert w["suggestion"]["id"] in seen
    assert w["spanning"]["id"] in seen


@pytest.mark.e2e
def test_agent_manager_sees_workshop_rows_only_on_agents_they_manage(test_client, workshop_world):
    """A per-agent ``create_entities`` grant is authority over that agent's
    queue — and stops at its edge: a row spanning a second agent needs
    authority over that one too."""
    w = workshop_world
    seen = _list_ids(test_client, w["manager"]["token"], w["org_id"])
    assert w["suggestion"]["id"] in seen, "manager cannot see their own agent's queue"
    assert w["spanning"]["id"] not in seen, "single-agent grant leaked a row spanning another agent"


@pytest.mark.e2e
@pytest.mark.parametrize("principal", ["admin", "member", "manager"])
def test_counts_agree_with_the_rows_the_same_caller_gets(test_client, workshop_world, principal):
    """The tree badge is computed server-side; it must equal the rows the same
    caller would load on expanding that agent, or the badge flickers from N to
    a different N when the rows arrive."""
    w = workshop_world
    token = w[principal]["token"]
    org_id = w["org_id"]

    counts = test_client.get("/api/entities/counts", headers=_hdr(token, org_id))
    assert counts.status_code == 200, counts.text
    by_agent = counts.json()["by_agent"]

    for ds in (w["ds_open"], w["ds_other"]):
        rows = test_client.get(
            "/api/entities",
            headers=_hdr(token, org_id),
            params={"data_source_ids": ds["id"], "limit": 1000},
        )
        assert rows.status_code == 200, rows.text
        # The tree hides archived rows, and so does the count.
        live = [
            e for e in rows.json()
            if e["status"] != "archived" and e.get("private_status") != "archived"
        ]
        assert by_agent.get(ds["id"], 0) == len(live), (
            f"{principal}: badge {by_agent.get(ds['id'], 0)} != {len(live)} rows on {ds['name']}"
        )


@pytest.mark.e2e
def test_counts_never_reports_an_agent_the_caller_cannot_reach(
    test_client, workshop_world, bootstrap_admin, sqlite_data_source, invite_user_to_org
):
    """A private agent's entity must not even contribute a number to another
    member's badge map — the count is a disclosure too."""
    w = workshop_world
    admin, org_id = w["admin"], w["org_id"]
    private_ds = sqlite_data_source(
        name="ws_private", user_token=admin["token"], org_id=org_id, is_public=False
    )
    _make_entity(
        test_client, token=admin["token"], org_id=org_id,
        title="ws_private_entity", slug=f"ws-private-{private_ds['id'][:6]}",
        ds_ids=[private_ds["id"]],
    )

    counts = test_client.get(
        "/api/entities/counts", headers=_hdr(w["member"]["token"], org_id)
    )
    assert counts.status_code == 200, counts.text
    assert private_ds["id"] not in counts.json()["by_agent"]
