"""Who may run a saved entity with parameter VALUES.

A values run never rewrites the shared snapshot, so it needs only what
reading the entity needs: the entity visible to the caller and access to
its data sources. Refreshing the shared snapshot (a run WITHOUT values)
keeps the stronger authoring gate.

Run:
    cd backend && uv run pytest tests/e2e/rbac/test_entity_run_params_access.py -v
"""
import uuid

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    return pd.DataFrame({"year": [params.get("year")], "clients": [len(ds_clients)]})
"""
YEAR_SPEC = {"name": "year", "type": "number", "source": "input", "default": None, "required": False}


def _entity(test_client, token, org_id, ds_ids):
    resp = test_client.post(
        "/api/entities/global",
        json={
            "type": "model", "title": "Sales by year", "slug": f"sales-{uuid.uuid4().hex[:8]}",
            "code": CODE, "data": {}, "status": "published",
            "parameters": [YEAR_SPEC], "data_source_ids": ds_ids,
        },
        headers=_hdr(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ds_public = sqlite_data_source(name="pub_agent", user_token=admin["token"], org_id=org_id, is_public=True)
    ds_private = sqlite_data_source(name="prv_agent", user_token=admin["token"], org_id=org_id)
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    return {"org_id": org_id, "admin": admin, "member": member, "ds_public": ds_public, "ds_private": ds_private}


@pytest.mark.e2e
def test_member_with_access_can_run_values_but_not_rewrite_the_snapshot(test_client, world):
    ent = _entity(test_client, world["admin"]["token"], world["org_id"], [world["ds_public"]["id"]])
    h = _hdr(world["member"]["token"], world["org_id"])

    values = test_client.post(f"/api/entities/{ent['id']}/run", json={"params": {"year": 2022}}, headers=h)
    assert values.status_code == 200, values.text
    assert values.json()["data"]["rows"][0]["year"] == 2022
    assert values.json()["data"]["rows"][0]["clients"] >= 1, "member's own credentials built the clients"

    refresh = test_client.post(f"/api/entities/{ent['id']}/run", json={}, headers=h)
    assert refresh.status_code == 403, refresh.text


@pytest.mark.e2e
def test_member_without_access_cannot_run_values(test_client, world):
    ent = _entity(test_client, world["admin"]["token"], world["org_id"], [world["ds_private"]["id"]])
    h = _hdr(world["member"]["token"], world["org_id"])
    resp = test_client.post(f"/api/entities/{ent['id']}/run", json={"params": {"year": 2022}}, headers=h)
    assert resp.status_code in (403, 404), resp.text


@pytest.mark.e2e
def test_admin_runs_values_and_refreshes(test_client, world):
    ent = _entity(test_client, world["admin"]["token"], world["org_id"], [world["ds_public"]["id"]])
    h = _hdr(world["admin"]["token"], world["org_id"])
    assert test_client.post(f"/api/entities/{ent['id']}/run", json={"params": {"year": 2021}}, headers=h).status_code == 200
    assert test_client.post(f"/api/entities/{ent['id']}/run", json={}, headers=h).status_code == 200


@pytest.mark.e2e
def test_dsless_snapshot_refresh_stays_an_admin_capability(test_client, world):
    ent = _entity(test_client, world["admin"]["token"], world["org_id"], [])
    h = _hdr(world["member"]["token"], world["org_id"])
    assert test_client.post(f"/api/entities/{ent['id']}/run", json={}, headers=h).status_code == 403
    assert test_client.post(f"/api/entities/{ent['id']}/run", json={"params": {"year": 2020}}, headers=h).status_code == 200
