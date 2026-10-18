"""View-level and owner-level callers must not exceed their authority.

Pentest WP2 (entity scope + data-source viewer leaks):

1. Entity scope: an empty ``data_source_ids`` IS org-wide scope. The owner of
   an unapproved entity may edit its content and narrow it to agents they can
   reach, but clearing the scope (making it global) needs ``manage_entities``,
   the same gate /entities/global applies.
2. ``GET /data_sources/{id}/test_connection``: a view-level member may run the
   probe and read the result, but only a caller with ``manage`` on the agent
   (or org ``manage_connections``) persists the outcome (``is_active``).
3. ``GET /data_sources/{id}``: connection ``config`` is a manage-tier field.
   Viewers get the agent without it; managers and admins keep it.
4. ``full_schema``: columns and relationships hidden by a manager are
   stripped for read-only callers on BOTH the paginated and non-paginated
   paths. Managers keep the full lists in the paginated selector so they can
   toggle them back.

World: admin (full admin) creates two public SQLite agents A and B.
    owner   — plain member; reads A and B through ``is_public``; authors a
              suggested (unapproved) entity
    viewer  — plain member; read-only on A through ``is_public``
    manager — ``manage`` grant on A, no org ``manage_entities``
"""
import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.entity import Entity
from tests.e2e.rbac.test_rbac_entity_creation import _create_report, _run, _seed_step


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source,
          grant_resource, refresh_schema):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ds_a = sqlite_data_source(name="agent_a", user_token=admin["token"], org_id=org_id, is_public=True)
    ds_b = sqlite_data_source(name="agent_b", user_token=admin["token"], org_id=org_id, is_public=True)
    refresh_schema(data_source_id=ds_a["id"], user_token=admin["token"], org_id=org_id)

    owner = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    viewer = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    g = grant_resource(resource_type="data_source", resource_id=ds_a["id"],
                       principal_type="user", principal_id=manager["user_id"],
                       permissions=["manage"], user_token=admin["token"], org_id=org_id)
    assert g.status_code == 200, g.text
    return {"org_id": org_id, "admin": admin, "owner": owner, "viewer": viewer,
            "manager": manager, "ds_a": ds_a, "ds_b": ds_b}


def _h(w, who):
    return _hdr(w[who]["token"], w["org_id"])


# ── 1. Entity scope ───────────────────────────────────────────────────────

def _owner_draft(test_client, w, ds_ids):
    report_id = _create_report(test_client, w["owner"]["token"], w["org_id"], [w["ds_a"]["id"]])
    step_id = _run(_seed_step(report_id))
    r = test_client.post(f"/api/entities/from_step/{step_id}",
                         json={"title": "owner draft", "publish": False, "data_source_ids": ds_ids},
                         headers=_h(w, "owner"))
    assert r.status_code == 200, r.text
    assert r.json()["global_status"] != "approved"
    return r.json()["id"]


async def _entity_scope(entity_id):
    from sqlalchemy.orm import selectinload
    async with async_session_maker() as db:
        e = (await db.execute(
            select(Entity).options(selectinload(Entity.data_sources)).where(Entity.id == entity_id)
        )).scalar_one()
        return sorted(str(ds.id) for ds in e.data_sources)


@pytest.mark.e2e
def test_entity_owner_cannot_clear_scope_to_org_wide(test_client, world):
    w = world
    entity_id = _owner_draft(test_client, w, [w["ds_a"]["id"]])
    before = _run(_entity_scope(entity_id))
    assert before == [w["ds_a"]["id"]]

    r = test_client.put(f"/api/entities/{entity_id}", json={"data_source_ids": []}, headers=_h(w, "owner"))
    assert r.status_code == 403, r.text
    assert _run(_entity_scope(entity_id)) == before


@pytest.mark.e2e
def test_entity_owner_can_edit_content_and_narrow_scope(test_client, world):
    w = world
    both = sorted([w["ds_a"]["id"], w["ds_b"]["id"]])
    entity_id = _owner_draft(test_client, w, both)
    assert _run(_entity_scope(entity_id)) == both

    r = test_client.put(f"/api/entities/{entity_id}",
                        json={"title": "renamed", "data_source_ids": [w["ds_b"]["id"]]},
                        headers=_h(w, "owner"))
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "renamed"
    assert _run(_entity_scope(entity_id)) == [w["ds_b"]["id"]]


@pytest.mark.e2e
def test_admin_can_make_entity_org_wide(test_client, world):
    w = world
    entity_id = _owner_draft(test_client, w, [w["ds_a"]["id"]])
    r = test_client.put(f"/api/entities/{entity_id}", json={"data_source_ids": []}, headers=_h(w, "admin"))
    assert r.status_code == 200, r.text
    assert _run(_entity_scope(entity_id)) == []


# ── 2. test_connection persistence ────────────────────────────────────────

async def _set_inactive(ds_id):
    # No API marks a healthy connection inactive; the sweeper does it on a
    # failed probe. Seed that state directly so a successful probe would flip it.
    from app.models.domain_connection import domain_connection
    async with async_session_maker() as db:
        ds = await db.get(DataSource, ds_id)
        ds.is_active = False
        conn_ids = (await db.execute(select(domain_connection.c.connection_id).where(
            domain_connection.c.data_source_id == str(ds_id)))).scalars().all()
        for cid in conn_ids:
            (await db.get(Connection, cid)).is_active = False
        await db.commit()
        return list(conn_ids)


async def _active_state(ds_id, conn_ids):
    async with async_session_maker() as db:
        ds = await db.get(DataSource, ds_id)
        conns = [(await db.get(Connection, cid)).is_active for cid in conn_ids]
        return bool(ds.is_active), conns


@pytest.mark.e2e
def test_viewer_test_connection_returns_result_without_persisting(test_client, world):
    w = world
    conn_ids = _run(_set_inactive(w["ds_a"]["id"]))
    r = test_client.get(f"/api/data_sources/{w['ds_a']['id']}/test_connection", headers=_h(w, "viewer"))
    assert r.status_code == 200, r.text
    assert r.json().get("success") is True
    assert _run(_active_state(w["ds_a"]["id"], conn_ids)) == (False, [False] * len(conn_ids))


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["manager", "admin"])
def test_manager_test_connection_persists_state(test_client, world, who):
    w = world
    conn_ids = _run(_set_inactive(w["ds_a"]["id"]))
    r = test_client.get(f"/api/data_sources/{w['ds_a']['id']}/test_connection", headers=_h(w, who))
    assert r.status_code == 200, r.text
    assert r.json().get("success") is True
    assert _run(_active_state(w["ds_a"]["id"], conn_ids)) == (True, [True] * len(conn_ids))


# ── 3. Connection config on the agent detail ──────────────────────────────

@pytest.mark.e2e
def test_viewer_detail_omits_connection_config(test_client, world):
    w = world
    r = test_client.get(f"/api/data_sources/{w['ds_a']['id']}", headers=_h(w, "viewer"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == w["ds_a"]["id"] and body["name"]
    assert body["connections"], "viewer still sees the linked connections"
    for c in body["connections"]:
        assert c["id"] and c["type"]
        assert not c.get("config")
    assert not body.get("config")


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["manager", "admin"])
def test_manager_detail_keeps_connection_config(test_client, world, who):
    w = world
    r = test_client.get(f"/api/data_sources/{w['ds_a']['id']}", headers=_h(w, who))
    assert r.status_code == 200, r.text
    conns = r.json()["connections"]
    assert conns and all((c.get("config") or {}).get("database") for c in conns)


# ── 4. Column hiding on full_schema ───────────────────────────────────────

def _hide_first_column(test_client, w):
    r = test_client.get(f"/api/data_sources/{w['ds_a']['id']}/full_schema",
                        params={"page": 1, "page_size": 500},
                        headers=_h(w, "admin"))
    assert r.status_code == 200, r.text
    table = next(t for t in r.json()["tables"] if len(t["columns"]) > 1)
    hidden = table["columns"][0]["name"]
    # Readers only see selected tables; select this one and hide one column.
    r = test_client.put(f"/api/data_sources/{w['ds_a']['id']}/update_tables_status",
                        json={"activate": [table["id"]], "excluded_columns": {table["id"]: [hidden]}},
                       headers=_h(w, "admin"))
    assert r.status_code == 200, r.text
    return table, hidden


def _paginated_columns(test_client, w, who, table_id):
    r = test_client.get(f"/api/data_sources/{w['ds_a']['id']}/full_schema",
                        params={"page": 1, "page_size": 500}, headers=_h(w, who))
    assert r.status_code == 200, r.text
    t = next(t for t in r.json()["tables"] if t["id"] == table_id)
    return {c["name"] for c in t["columns"]}, {c["name"] for c in t.get("pks") or []}


def _legacy_columns(test_client, w, who, table_name):
    r = test_client.get(f"/api/data_sources/{w['ds_a']['id']}/full_schema", headers=_h(w, who))
    assert r.status_code == 200, r.text
    t = next(t for t in r.json() if t["name"] == table_name)
    return {c["name"] for c in t["columns"]}


@pytest.mark.e2e
def test_hidden_columns_stripped_for_reader_on_both_paths(test_client, world):
    w = world
    table, hidden = _hide_first_column(test_client, w)
    all_cols = {c["name"] for c in table["columns"]}

    cols, pks = _paginated_columns(test_client, w, "viewer", table["id"])
    assert hidden not in cols and hidden not in pks
    assert cols == all_cols - {hidden}
    assert hidden not in _legacy_columns(test_client, w, "viewer", table["name"])


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["manager", "admin"])
def test_manager_paginated_schema_keeps_every_column(test_client, world, who):
    w = world
    table, hidden = _hide_first_column(test_client, w)
    cols, _ = _paginated_columns(test_client, w, who, table["id"])
    assert cols == {c["name"] for c in table["columns"]}


# ── 5. Relationship hiding on full_schema ─────────────────────────────────

def _edge(fk):
    return (fk["column"]["name"], fk["references_name"], fk["references_column"]["name"])


def _hide_first_relationship(test_client, w):
    r = test_client.get(f"/api/data_sources/{w['ds_a']['id']}/full_schema",
                        params={"page": 1, "page_size": 500},
                        headers=_h(w, "admin"))
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    table = next(t for t in tables if len(t["fks"]) > 1)
    fk = table["fks"][0]
    # Select the table and its targets so only the manager's choice drops an edge.
    targets = {f["references_name"] for f in table["fks"]} | {table["name"]}
    r = test_client.put(f"/api/data_sources/{w['ds_a']['id']}/update_tables_status",
                        json={"activate": [t["id"] for t in tables if t["name"] in targets],
                              "excluded_fks": {table["id"]: [{
                                  "column": fk["column"]["name"], "references_name": fk["references_name"],
                                  "references_column": fk["references_column"]["name"]}]}},
                        headers=_h(w, "admin"))
    assert r.status_code == 200, r.text
    return table, _edge(fk)


def _paginated_edges(test_client, w, who, table_id):
    r = test_client.get(f"/api/data_sources/{w['ds_a']['id']}/full_schema",
                        params={"page": 1, "page_size": 500}, headers=_h(w, who))
    assert r.status_code == 200, r.text
    t = next(t for t in r.json()["tables"] if t["id"] == table_id)
    return {_edge(fk) for fk in t.get("fks") or []}


def _legacy_edges(test_client, w, who, table_name):
    r = test_client.get(f"/api/data_sources/{w['ds_a']['id']}/full_schema", headers=_h(w, who))
    assert r.status_code == 200, r.text
    t = next(t for t in r.json() if t["name"] == table_name)
    return {_edge(fk) for fk in t.get("fks") or []}


@pytest.mark.e2e
def test_hidden_relationships_stripped_for_reader_on_both_paths(test_client, world):
    w = world
    table, hidden = _hide_first_relationship(test_client, w)
    all_edges = {_edge(fk) for fk in table["fks"]}
    assert _paginated_edges(test_client, w, "viewer", table["id"]) == all_edges - {hidden}
    assert hidden not in _legacy_edges(test_client, w, "viewer", table["name"])


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["manager", "admin"])
def test_manager_paginated_schema_keeps_every_relationship(test_client, world, who):
    w = world
    table, _ = _hide_first_relationship(test_client, w)
    assert _paginated_edges(test_client, w, who, table["id"]) == {_edge(fk) for fk in table["fks"]}


@pytest.mark.e2e
@pytest.mark.parametrize("who, sees_list", [("admin", True), ("manager", True), ("viewer", False)])
def test_legacy_full_schema_reports_hidden_relationships_to_managers_only(test_client, world, who, sees_list):
    """The non-paginated full_schema carries excluded_fks next to excluded
    columns for managers; a reader gets the stripped fks, not the list."""
    w = world
    table, hidden = _hide_first_relationship(test_client, w)
    r = test_client.get(f"/api/data_sources/{w['ds_a']['id']}/full_schema", headers=_h(w, who))
    assert r.status_code == 200, r.text
    t = next(t for t in r.json() if t["name"] == table["name"])
    listed = [(e["column"], e["references_name"], e["references_column"]) for e in (t.get("excluded_fks") or [])]
    assert listed == ([hidden] if sees_list else [])
