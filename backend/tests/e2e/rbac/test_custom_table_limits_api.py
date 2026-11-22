"""An organization's custom-table limits, through the API.

The limits live in organization settings and must hold on every way a table
is filled: creating it, refreshing it, an incremental refresh and a full
rebuild. Each test here moves a limit and watches the same request change
its answer, so a path that ignored the setting would fail it.
"""
import sqlite3
import uuid

import pytest

from app.data_sources.fast import limits as fast_limits

ROW_FLOOR = fast_limits.SETTINGS["custom_table_max_rows"].low


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _fill(db_path, first, last):
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, seq INTEGER)")
    con.executemany("INSERT INTO orders VALUES (?, ?)", [(i, i) for i in range(first, last + 1)])
    con.commit()
    con.close()


def _set(test_client, shop, **values):
    return test_client.put("/api/organization/settings", headers=shop["h"],
                           json={"config": {k: {"value": v} for k, v in values.items()}})


@pytest.fixture
def shop(tmp_path, test_client, create_user, login_user, whoami, create_connection):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    db_path = tmp_path / f"shop_{uuid.uuid4().hex[:8]}.sqlite"
    _fill(db_path, 1, ROW_FLOOR - 100)
    conn = create_connection(name=f"Shop {uuid.uuid4().hex[:6]}", type="sqlite",
                             config={"database": str(db_path)}, user_token=token, org_id=org_id)
    shop = {"token": token, "org_id": org_id, "h": _headers(token, org_id), "db_path": db_path,
            "rows": ROW_FLOOR - 100, "base": f"/api/connections/{conn['id']}/custom-queries"}
    assert _set(test_client, shop, enable_custom_queries=True).status_code == 200
    return shop


FULL = {"name": "orders_copy", "definition_sql": "SELECT * FROM orders"}
MERGE = {**FULL, "refresh_mode": "merge",
         "refresh_config": {"cursor_column": "seq", "unique_key": ["id"], "lookback_units": 0}}


@pytest.mark.e2e
def test_limits_default_to_the_shipped_values_and_are_readable(test_client, shop):
    config = test_client.get("/api/organization/settings", headers=shop["h"]).json()["config"]
    assert {k: config[k]["value"] for k in fast_limits.SETTINGS} == {
        k: bound.default for k, bound in fast_limits.SETTINGS.items()}


@pytest.mark.e2e
@pytest.mark.parametrize("key", sorted(fast_limits.SETTINGS))
def test_a_limit_outside_its_range_is_refused_and_nothing_changes(test_client, shop, key):
    bound = fast_limits.SETTINGS[key]
    for bad in (bound.low - 1, bound.high + 1, 0, -5, "many", 1.5, True, None):
        res = _set(test_client, shop, **{key: bad})
        assert res.status_code == 400, f"{key}={bad!r} was accepted: {res.text}"
    stored = test_client.get("/api/organization/settings", headers=shop["h"]).json()["config"][key]
    assert stored["value"] == bound.default
    for edge in (bound.low, bound.high):
        assert _set(test_client, shop, **{key: edge}).status_code == 200


@pytest.mark.e2e
def test_only_someone_who_manages_settings_can_move_a_limit(test_client, shop, invite_user_to_org):
    member = invite_user_to_org(org_id=shop["org_id"], admin_token=shop["token"])
    res = test_client.put("/api/organization/settings", headers=_headers(member["token"], shop["org_id"]),
                          json={"config": {"custom_table_max_rows": {"value": 50_000_000}}})
    assert res.status_code == 403
    config = test_client.get("/api/organization/settings", headers=shop["h"]).json()["config"]
    assert config["custom_table_max_rows"]["value"] == fast_limits.SETTINGS["custom_table_max_rows"].default


@pytest.mark.e2e
@pytest.mark.parametrize("body", [FULL, MERGE], ids=["full", "incremental"])
def test_a_lowered_row_limit_refuses_a_table_the_default_would_load(test_client, shop, body):
    _fill(shop["db_path"], shop["rows"] + 1, ROW_FLOOR + 300)
    assert _set(test_client, shop, custom_table_max_rows=ROW_FLOOR).status_code == 200
    refused = test_client.post(shop["base"], json=body, headers=shop["h"])
    assert refused.status_code == 400, refused.text
    assert f"{ROW_FLOOR:,}" in refused.json()["detail"], "the message names the limit in force"

    assert _set(test_client, shop, custom_table_max_rows=ROW_FLOOR * 2).status_code == 200
    body = {**body, "name": "orders_again"}
    loaded = test_client.post(shop["base"], json=body, headers=shop["h"])
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["no_rows"] == ROW_FLOOR + 300


@pytest.mark.e2e
@pytest.mark.parametrize("body,params", [(FULL, {}), (MERGE, {}), (MERGE, {"full": True})],
                         ids=["refresh", "increment", "rebuild"])
def test_a_refresh_over_the_row_limit_fails_and_keeps_the_previous_rows(test_client, shop, body, params):
    assert _set(test_client, shop, custom_table_max_rows=ROW_FLOOR).status_code == 200
    created = test_client.post(shop["base"], json=body, headers=shop["h"])
    assert created.status_code == 200, created.text
    url = f"{shop['base']}/{created.json()['id']}"

    _fill(shop["db_path"], shop["rows"] + 1, ROW_FLOOR + 300)
    over = test_client.post(f"{url}/refresh", params=params, headers=shop["h"])
    assert over.status_code == 400, over.text
    table = next(t for t in test_client.get(shop["base"], headers=shop["h"]).json()
                 if t["id"] == created.json()["id"])
    assert table["no_rows"] == shop["rows"], "the previous artifact is still the one served"
    assert table["last_refresh_status"] == "error"

    assert _set(test_client, shop, custom_table_max_rows=ROW_FLOOR * 2).status_code == 200
    raised = test_client.post(f"{url}/refresh", params=params, headers=shop["h"])
    assert raised.status_code == 200, raised.text
    assert raised.json()["no_rows"] == ROW_FLOOR + 300
