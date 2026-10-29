"""Per-agent column visibility through the tables API.

An agent manager can hide columns of a selected table from the agent's
schema context. The management listing keeps every column (so they can be
toggled back) and reports which are hidden; agent-facing reads drop them.
"""
import pytest
from pathlib import Path

CHINOOK = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def chinook_agent(create_data_source, refresh_schema, create_user, login_user, whoami):
    if not CHINOOK.exists():
        pytest.skip(f"SQLite test database missing at {CHINOOK}")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    ds = create_data_source(
        name="Column visibility", type="sqlite",
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


@pytest.mark.e2e
def test_hidden_columns_leave_agent_schema_but_stay_in_management_view(
    test_client, chinook_agent, update_tables_status_delta,
):
    ds, token, org_id = chinook_agent
    tables = _tables(test_client, ds["id"], token, org_id)
    # Any table with enough columns to hide some and keep some.
    target = next(t for t in tables.values() if len(t["columns"]) >= 3)
    names = [c["name"] for c in target["columns"]]
    hidden, kept = names[1:3], [names[0]] + names[3:]

    res = update_tables_status_delta(
        data_source_id=ds["id"], activate=[target["id"]],
        # Case and duplicates must not matter; unknown names are dropped.
        excluded_columns={target["id"]: [hidden[0].upper(), hidden[1], hidden[1], "no_such_col"]},
        user_token=token, org_id=org_id,
    )
    assert res["columns_updated_count"] == 1

    listed = _tables(test_client, ds["id"], token, org_id)[target["id"]]
    assert sorted(listed["excluded_columns"]) == sorted(hidden)
    assert [c["name"] for c in listed["columns"]] == names  # management view: all columns

    agent = test_client.get(f"/api/data_sources/{ds['id']}/schema", headers=_hdr(token, org_id))
    assert agent.status_code == 200, agent.text
    agent_tbl = next(t for t in agent.json() if t["name"] == target["name"])
    agent_cols = {c["name"] for c in agent_tbl["columns"]}
    assert agent_cols == set(kept)
    for key in ("pks",):
        assert not {c["name"] for c in (agent_tbl.get(key) or [])} & set(hidden)
    assert not {fk["column"]["name"] for fk in (agent_tbl.get("fks") or [])} & set(hidden)

    # Sending an empty list shows every column again.
    res = update_tables_status_delta(
        data_source_id=ds["id"], excluded_columns={target["id"]: []},
        user_token=token, org_id=org_id,
    )
    assert res["columns_updated_count"] == 1
    assert not _tables(test_client, ds["id"], token, org_id)[target["id"]]["excluded_columns"]


@pytest.mark.e2e
def test_hidden_columns_survive_schema_refresh(
    test_client, chinook_agent, update_tables_status_delta, refresh_schema,
):
    ds, token, org_id = chinook_agent
    tables = _tables(test_client, ds["id"], token, org_id)
    target = next(t for t in tables.values() if len(t["columns"]) >= 2)
    hide = target["columns"][-1]["name"]
    update_tables_status_delta(
        data_source_id=ds["id"], activate=[target["id"]],
        excluded_columns={target["id"]: [hide]}, user_token=token, org_id=org_id,
    )
    refresh_schema(data_source_id=ds["id"], user_token=token, org_id=org_id)
    after = _tables(test_client, ds["id"], token, org_id)
    row = next(t for t in after.values() if t["name"] == target["name"])
    assert row["excluded_columns"] == [hide]
