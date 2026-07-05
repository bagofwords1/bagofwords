"""
REPRODUCTION (do not fix): admin creates an entity for a data source (agent)
and cannot see it on /queries.

The /queries page (frontend/pages/queries/index.vue::loadEntities) passes the
persisted agent selection (useAgent -> localStorage 'bow_selected_agents') as
``?data_source_ids=<selected>``. list_entities then keeps only entities that
have an association row for one of those data sources:

    if data_source_ids:
        stmt = stmt.where(exists(... entity_data_source_association ...
                          data_source_id.in_(data_source_ids)))

Consequences reproduced here:

A. If the persisted agent selection points at a DIFFERENT data source than the
   one the entity was created for, the entity is filtered out — even for an
   admin, even though it is published. The selection is sticky across pages
   (localStorage), so a stale selection silently hides just-created entities.

B. If the entity has NO data sources attached, ANY active agent selection
   hides it (the exists() can never match), so it only ever appears under the
   "All" selection.
"""
import uuid

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _create_global_entity(test_client, *, token, org_id, title, ds_ids):
    resp = test_client.post(
        "/api/entities/global",
        json={
            "type": "model",
            "title": title,
            "slug": f"{title.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
            "code": "select 1 as v",
            "data": {},
            "tags": [],
            "status": "published",
            "data_source_ids": ds_ids,
        },
        headers=_hdr(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _list_ids(test_client, *, token, org_id, data_source_ids=None):
    url = "/api/entities"
    if data_source_ids:
        url += "?data_source_ids=" + ",".join(data_source_ids)
    resp = test_client.get(url, headers=_hdr(token, org_id))
    assert resp.status_code == 200, resp.text
    return {e["id"] for e in resp.json()}


@pytest.mark.e2e
def test_admin_entity_hidden_when_agent_filter_points_elsewhere(
    test_client, bootstrap_admin, sqlite_data_source
):
    """Case A: a stale/other agent selection hides an admin's published entity."""
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    ds_target = sqlite_data_source(name="agent_target", user_token=admin["token"], org_id=org_id, is_public=False)
    ds_other = sqlite_data_source(name="agent_other", user_token=admin["token"], org_id=org_id, is_public=False)

    ent = _create_global_entity(
        test_client, token=admin["token"], org_id=org_id,
        title="Yossi Rost June", ds_ids=[ds_target["id"]],
    )

    # Baseline: with no agent filter ("All"), the admin sees it.
    assert ent["id"] in _list_ids(test_client, token=admin["token"], org_id=org_id)

    # Filtering by the SAME data source -> visible.
    assert ent["id"] in _list_ids(
        test_client, token=admin["token"], org_id=org_id, data_source_ids=[ds_target["id"]]
    )

    # BUG: a persisted selection of a DIFFERENT agent hides the entity, even
    # for the admin who just created it.
    hidden = _list_ids(
        test_client, token=admin["token"], org_id=org_id, data_source_ids=[ds_other["id"]]
    )
    assert ent["id"] not in hidden, "unexpectedly visible under the other-agent filter"


@pytest.mark.e2e
def test_admin_entity_without_ds_hidden_under_any_agent_filter(
    test_client, bootstrap_admin, sqlite_data_source
):
    """Case B: an entity with no data sources is hidden whenever any agent is selected."""
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    ds = sqlite_data_source(name="some_agent", user_token=admin["token"], org_id=org_id, is_public=False)

    ent = _create_global_entity(
        test_client, token=admin["token"], org_id=org_id,
        title="Orphan Entity", ds_ids=[],  # no data sources attached
    )

    # With "All" (no filter) it shows.
    assert ent["id"] in _list_ids(test_client, token=admin["token"], org_id=org_id)

    # BUG: selecting ANY agent hides it, because it has no association row to match.
    filtered = _list_ids(
        test_client, token=admin["token"], org_id=org_id, data_source_ids=[ds["id"]]
    )
    assert ent["id"] not in filtered, "unexpectedly visible; expected agent filter to hide no-DS entity"
