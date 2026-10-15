"""Per-agent relationship visibility through the tables API.

An agent manager can hide relationships (foreign-key edges) of a selected
table from the agent's schema context. The management listing keeps every
edge (so they can be toggled back) and reports which are hidden; agent-facing
reads drop the hidden ones and keep the rest.
"""
import pytest
from pathlib import Path

CHINOOK = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _edge(fk):
    return (fk["column"]["name"], fk["references_name"], fk["references_column"]["name"])


def _entry(fk):
    return {"column": fk["column"]["name"], "references_name": fk["references_name"],
            "references_column": fk["references_column"]["name"]}


@pytest.fixture
def chinook_agent(create_data_source, refresh_schema, create_user, login_user, whoami):
    if not CHINOOK.exists():
        pytest.skip(f"SQLite test database missing at {CHINOOK}")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    ds = create_data_source(
        name="Relationship visibility", type="sqlite",
        config={"database": str(CHINOOK)}, credentials={},
        user_token=token, org_id=org_id,
    )
    refresh_schema(data_source_id=ds["id"], user_token=token, org_id=org_id)
    return ds, token, org_id


def _tables(test_client, ds_id, token, org_id):
    r = test_client.get(
        f"/api/data_sources/{ds_id}/full_schema",
        params={"page": 1, "page_size": 500}, headers=_hdr(token, org_id),
    )
    assert r.status_code == 200, r.text
    return {t["id"]: t for t in r.json()["tables"]}


def _activate_with_targets(update, ds_id, tables, target, token, org_id, **kw):
    """Select the table and every table its edges point at, so no edge is
    pruned for pointing outside the selection — only the manager's choice."""
    names = {fk["references_name"] for fk in target["fks"]} | {target["name"]}
    ids = [t["id"] for t in tables.values() if t["name"] in names]
    return update(data_source_id=ds_id, activate=ids, user_token=token, org_id=org_id, **kw)


@pytest.mark.e2e
def test_hidden_relationship_leaves_agent_schema_but_stays_in_management_view(
    test_client, chinook_agent, update_tables_status_delta,
):
    ds, token, org_id = chinook_agent
    tables = _tables(test_client, ds["id"], token, org_id)
    target = next(t for t in tables.values() if len(t["fks"]) >= 2)
    edges = [_edge(fk) for fk in target["fks"]]
    hidden, kept = target["fks"][0], edges[1:]

    shouted = {k: v.upper() for k, v in _entry(hidden).items()}
    res = _activate_with_targets(
        update_tables_status_delta, ds["id"], tables, target, token, org_id,
        # Case and duplicates must not matter; edges the table lacks are dropped.
        excluded_fks={target["id"]: [shouted, _entry(hidden),
                                     {"column": "nope", "references_name": "x", "references_column": "y"}]},
    )
    assert res["relationships_updated_count"] == 1

    listed = _tables(test_client, ds["id"], token, org_id)[target["id"]]
    assert listed["excluded_fks"] == [_entry(hidden)]  # stored with the catalog's spelling
    assert [_edge(fk) for fk in listed["fks"]] == edges  # management view: every edge

    agent = test_client.get(f"/api/data_sources/{ds['id']}/schema", headers=_hdr(token, org_id))
    assert agent.status_code == 200, agent.text
    agent_tbl = next(t for t in agent.json() if t["name"] == target["name"])
    agent_edges = [_edge(fk) for fk in (agent_tbl.get("fks") or [])]
    assert _edge(hidden) not in agent_edges
    assert sorted(agent_edges) == sorted(kept)
    # Hiding a relationship hides only the edge, never its column.
    assert hidden["column"]["name"] in {c["name"] for c in agent_tbl["columns"]}

    # Sending an empty list shows every relationship again.
    res = update_tables_status_delta(
        data_source_id=ds["id"], excluded_fks={target["id"]: []},
        user_token=token, org_id=org_id,
    )
    assert res["relationships_updated_count"] == 1
    assert not _tables(test_client, ds["id"], token, org_id)[target["id"]]["excluded_fks"]


@pytest.mark.e2e
def test_hidden_relationships_survive_schema_refresh(
    test_client, chinook_agent, update_tables_status_delta, refresh_schema,
):
    ds, token, org_id = chinook_agent
    tables = _tables(test_client, ds["id"], token, org_id)
    target = next(t for t in tables.values() if t["fks"])
    hide = _entry(target["fks"][-1])
    _activate_with_targets(
        update_tables_status_delta, ds["id"], tables, target, token, org_id,
        excluded_fks={target["id"]: [hide]},
    )
    refresh_schema(data_source_id=ds["id"], user_token=token, org_id=org_id)
    after = _tables(test_client, ds["id"], token, org_id)
    row = next(t for t in after.values() if t["name"] == target["name"])
    assert row["excluded_fks"] == [hide]
    assert _edge(target["fks"][-1]) in [_edge(fk) for fk in row["fks"]]
