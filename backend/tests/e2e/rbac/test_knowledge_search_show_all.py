"""GET /api/knowledge/search?show_all=… — the /agents "Search everything" box.

The agents the search matches must track the same visibility the /agents tree
shows: an admin who sees a private agent only via the tree's "Show all agents"
toggle (they neither created nor joined it) could previously never find that
agent by name, because the search ignored show_all. These tests pin that the
toggle now reaches the search, and that a plain member can't widen the result
by passing the flag.
"""
import uuid

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _search_agent_ids(test_client, token, org_id, q, **params):
    r = test_client.get(
        "/api/knowledge/search",
        params={"q": q, "limit": 50, **params},
        headers=_hdr(token, org_id),
    )
    assert r.status_code == 200, r.text[:400]
    return {a["id"] for a in r.json()["agents"]}


@pytest.mark.e2e
def test_show_all_makes_admin_only_agents_searchable(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source
):
    """A full admin who neither created nor joined a private agent can't find it
    by name without show_all, but can with show_all=true — mirroring the tree."""
    creator = bootstrap_admin("creator")
    org_id = creator["org_id"]

    tag = uuid.uuid4().hex[:8]
    ds = sqlite_data_source(
        name=f"hidden-{tag}", user_token=creator["token"], org_id=org_id
    )
    ds_id = ds["id"]

    second_admin = invite_user_to_org(
        org_id=org_id, admin_token=creator["token"], role="admin"
    )

    # Without the toggle the search hides it (the admin isn't a member) — the
    # exact gap the fix closes.
    assert ds_id not in _search_agent_ids(
        test_client, second_admin["token"], org_id, tag
    )

    # With show_all=true the agent becomes searchable for the admin.
    assert ds_id in _search_agent_ids(
        test_client, second_admin["token"], org_id, tag, show_all=True
    )


@pytest.mark.e2e
def test_show_all_is_ignored_for_members_without_governance(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source
):
    """A plain member can't surface a private agent they don't belong to by
    passing show_all — the flag is gated on org-wide governance, same as the
    /data_sources list."""
    creator = bootstrap_admin("creator")
    org_id = creator["org_id"]

    tag = uuid.uuid4().hex[:8]
    ds = sqlite_data_source(
        name=f"hidden-{tag}", user_token=creator["token"], org_id=org_id
    )
    ds_id = ds["id"]

    member = invite_user_to_org(org_id=org_id, admin_token=creator["token"])

    assert ds_id not in _search_agent_ids(
        test_client, member["token"], org_id, tag, show_all=True
    )


@pytest.mark.e2e
def test_member_still_finds_their_own_agent_without_show_all(
    test_client, bootstrap_admin, sqlite_data_source
):
    """The default path is unchanged: an agent the caller created (and is a
    member of) is searchable by name with no toggle."""
    creator = bootstrap_admin("creator")
    org_id = creator["org_id"]

    tag = uuid.uuid4().hex[:8]
    ds = sqlite_data_source(
        name=f"mine-{tag}", user_token=creator["token"], org_id=org_id
    )

    assert ds["id"] in _search_agent_ids(
        test_client, creator["token"], org_id, tag
    )
