"""Incremental custom tables and their run history, through the API.

What an admin does in the modal: create a table that refreshes incrementally,
watch it follow the source, read its history, force a full rebuild — and what
a member without `manage_connection` may not see.
"""
import sqlite3
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def shop(tmp_path, test_client, create_user, login_user, whoami, create_connection):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    db_path = tmp_path / f"shop_{uuid.uuid4().hex[:8]}.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT, seq INTEGER)")
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(i, "new", i) for i in range(1, 41)])
    con.commit()
    con.close()
    conn = create_connection(name=f"Shop {uuid.uuid4().hex[:6]}", type="sqlite",
                             config={"database": str(db_path)}, user_token=token, org_id=org_id)
    h = _headers(token, org_id)
    on = test_client.put("/api/organization/settings",
                         json={"config": {"enable_custom_queries": {"value": True}}}, headers=h)
    assert on.status_code == 200, on.text
    return {"token": token, "org_id": org_id, "h": h, "db_path": db_path,
            "base": f"/api/connections/{conn['id']}/custom-queries"}


def _change(shop, *statements):
    con = sqlite3.connect(shop["db_path"])
    for statement in statements:
        con.execute(statement)
    con.commit()
    con.close()


def _create(test_client, shop, **over):
    body = {"name": "orders_copy", "definition_sql": "SELECT * FROM orders", "refresh_mode": "merge",
            "refresh_config": {"cursor_column": "seq", "unique_key": ["id"], "lookback_units": 0}, **over}
    return test_client.post(shop["base"], json=body, headers=shop["h"])


@pytest.mark.e2e
def test_an_incremental_table_follows_the_source_and_keeps_its_history(test_client, shop):
    created = _create(test_client, shop)
    assert created.status_code == 200, created.text
    table = created.json()
    assert (table["refresh_mode"], table["no_rows"], table["last_refresh_status"]) == ("merge", 40, "ok")
    assert table["checkpoint"] == {"kind": "number", "value": "40"}
    assert table["refresh_config"]["unique_key"] == ["id"]
    # A new table gets the row-drop guard without asking for it.
    assert table["refresh_config"]["row_drop_guard_pct"] == 50
    assert not any(c["name"].startswith("_bow_") for c in table["columns"])
    url = f"{shop['base']}/{table['id']}"

    _change(shop, "INSERT INTO orders VALUES (41, 'new', 41)",
            "UPDATE orders SET status = 'paid', seq = 42 WHERE id = 3",
            "DELETE FROM orders WHERE id = 10")
    refreshed = test_client.post(f"{url}/refresh", headers=shop["h"])
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["no_rows"] == 41, "an increment adds the new row and cannot see the deletion"
    assert refreshed.json()["checkpoint"]["value"] == "42"

    rebuilt = test_client.post(f"{url}/refresh", params={"full": True}, headers=shop["h"])
    assert rebuilt.status_code == 200 and rebuilt.json()["no_rows"] == 40

    listing = test_client.get(f"{url}/runs", headers=shop["h"]).json()
    summary = [(r["trigger"], r["mode"], r["status"], r["rows_read"]) for r in listing["runs"]]
    assert summary == [("manual", "full", "success", 40), ("manual", "incremental", "success", 3),
                       ("create", "full", "success", 40)]
    assert listing["runs"][1]["rows_inserted"] == 1 and listing["runs"][1]["rows_updated"] == 1
    assert listing["runs"][0]["triggered_by"]["id"]

    detail = test_client.get(f"{url}/runs/{listing['runs'][1]['id']}", headers=shop["h"])
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert ">= 40" in body["rendered_query"] and body["window_to"]["value"] == "42"
    assert test_client.get(f"{url}/runs/{uuid.uuid4()}", headers=shop["h"]).status_code == 404

    page = test_client.get(f"{url}/runs", params={"limit": 2}, headers=shop["h"]).json()
    assert len(page["runs"]) == 2 and page["has_more"]
    older = test_client.get(f"{url}/runs", params={"limit": 2, "before": page["next_before"]},
                            headers=shop["h"]).json()
    assert [r["trigger"] for r in older["runs"]] == ["create"] and not older["has_more"]


@pytest.mark.e2e
@pytest.mark.parametrize("over,problem", [
    ({"refresh_config": {"cursor_column": "seq"}}, "key"),
    ({"refresh_mode": "append", "refresh_config": {}}, "cursor"),
    ({"refresh_mode": "sideways"}, "mode"),
    ({"definition_sql": "SELECT * FROM orders WHERE seq >= {{since}}"}, "cursor type"),
])
def test_settings_that_cannot_work_are_refused_before_anything_is_created(test_client, shop, over, problem):
    res = _create(test_client, shop, **over)
    assert res.status_code == 400, res.text
    assert problem in res.json()["detail"].lower()
    assert test_client.get(shop["base"], headers=shop["h"]).json() == []


@pytest.mark.e2e
def test_a_query_that_places_its_own_filter_can_be_previewed_and_saved(test_client, shop):
    sql = "SELECT * FROM orders WHERE seq >= {{since}}"
    config = {"cursor_column": "seq", "cursor_kind": "number", "unique_key": ["id"]}
    preview = test_client.post(f"{shop['base']}/preview", headers=shop["h"],
                               json={"definition_sql": sql, "refresh_config": config})
    assert preview.status_code == 200, preview.text
    assert len(preview.json()["rows"]) == 40
    created = _create(test_client, shop, definition_sql=sql, refresh_config=config)
    assert created.status_code == 200, created.text
    assert created.json()["no_rows"] == 40


@pytest.mark.e2e
def test_changing_settings_keeps_the_ones_not_sent_and_rebuilds_only_when_rows_could_change(test_client, shop):
    table = _create(test_client, shop).json()
    url = f"{shop['base']}/{table['id']}"

    tuned = test_client.put(url, headers=shop["h"], json={"refresh_config": {"lookback_units": 5}})
    assert tuned.status_code == 200, tuned.text
    config = tuned.json()["refresh_config"]
    assert (config["lookback_units"], config["cursor_column"], config["unique_key"]) == (5, "seq", ["id"])
    assert len(test_client.get(f"{url}/runs", headers=shop["h"]).json()["runs"]) == 1

    switched = test_client.put(url, headers=shop["h"], json={"refresh_mode": "full"})
    assert switched.status_code == 200, switched.text
    assert switched.json()["refresh_mode"] == "full" and switched.json()["checkpoint"] is None
    runs = test_client.get(f"{url}/runs", headers=shop["h"]).json()["runs"]
    assert (runs[0]["trigger"], runs[0]["mode"]) == ("definition", "full")


@pytest.mark.e2e
def test_run_history_needs_manage_connection(test_client, shop, invite_user_to_org):
    table = _create(test_client, shop).json()
    url = f"{shop['base']}/{table['id']}"
    run_id = test_client.get(f"{url}/runs", headers=shop["h"]).json()["runs"][0]["id"]
    member = invite_user_to_org(org_id=shop["org_id"], admin_token=shop["token"])
    mh = _headers(member["token"], shop["org_id"])
    for path in (f"{url}/runs", f"{url}/runs/{run_id}"):
        assert test_client.get(path, headers=mh).status_code == 403
    assert test_client.post(f"{url}/refresh", params={"full": True}, headers=mh).status_code == 403
