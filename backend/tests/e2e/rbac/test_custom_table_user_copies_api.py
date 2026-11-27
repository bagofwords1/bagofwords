"""Custom tables on a connection where each person signs in with their own
credentials, through the API.

What the connection's admin sees and may do there — the table is cached once
per person, so nothing is materialized on the table itself, nothing runs on a
clock, and each person's copy can be listed and removed — and what a member
without `manage_connection` may not.

How a copy is built, served, expired and cleaned up is exercised with real
files in tests/unit/test_custom_table_user_copies.py.
"""
import sqlite3
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def shop(tmp_path, test_client, create_user, login_user, whoami, create_connection):
    """An admin, custom tables switched on, and one connection per auth policy
    over the same SQLite file."""
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    db_path = tmp_path / f"shop_{uuid.uuid4().hex[:8]}.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT, seq INTEGER)")
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(i, "new", i) for i in range(1, 41)])
    con.commit()
    con.close()
    h = _headers(token, org_id)
    on = test_client.put("/api/organization/settings",
                         json={"config": {"enable_custom_queries": {"value": True}}}, headers=h)
    assert on.status_code == 200, on.text

    def connection(policy: str) -> dict:
        return create_connection(name=f"Shop {policy} {uuid.uuid4().hex[:6]}", type="sqlite",
                                 config={"database": str(db_path)}, auth_policy=policy,
                                 user_token=token, org_id=org_id)

    per_user, shared = connection("user_required"), connection("system_only")
    return {"token": token, "org_id": org_id, "h": h,
            "per_user": per_user, "shared": shared,
            "base": f"/api/connections/{per_user['id']}/custom-queries",
            "shared_base": f"/api/connections/{shared['id']}/custom-queries"}


def _create(test_client, shop, base=None, **over):
    body = {"name": f"orders_{uuid.uuid4().hex[:6]}", "definition_sql": "SELECT * FROM orders", **over}
    return test_client.post(base or shop["base"], json=body, headers=shop["h"])


@pytest.mark.e2e
def test_both_kinds_of_connection_offer_custom_tables(test_client, shop):
    for conn in (shop["per_user"], shop["shared"]):
        got = test_client.get(f"/api/connections/{conn['id']}", headers=shop["h"])
        assert got.status_code == 200, got.text
        assert got.json()["custom_queries_supported"] is True


@pytest.mark.e2e
def test_a_table_on_a_per_user_connection_is_defined_but_holds_no_shared_data(test_client, shop):
    created = _create(test_client, shop)
    assert created.status_code == 200, created.text
    table = created.json()
    assert table["per_user"] is True
    # Its shape is known (the agent's schema needs it) ...
    assert {c["name"] for c in table["columns"]} == {"id", "status", "seq"}
    assert table["last_refresh_status"] == "ok"
    # ... but no rows were materialized for everyone.
    assert table["artifact_bytes"] is None and table["no_rows"] == 0
    assert table["user_copies"] == {"copies": 0, "bytes": 0}

    listed = test_client.get(shop["base"], headers=shop["h"]).json()
    assert [(t["id"], t["per_user"]) for t in listed] == [(table["id"], True)]

    # The same query on the shared connection keeps behaving as it always has.
    shared = _create(test_client, shop, base=shop["shared_base"]).json()
    assert shared["per_user"] is False and shared["no_rows"] == 40 and shared["artifact_bytes"] > 0
    assert shared["user_copies"] is None


@pytest.mark.e2e
@pytest.mark.parametrize("mode, config", [
    ("merge", {"cursor_column": "seq", "unique_key": ["id"], "lookback_units": 0}),
    ("append", {"cursor_column": "seq"}),
])
def test_incremental_refresh_is_refused_where_every_person_holds_different_rows(test_client, shop, mode, config):
    refused = _create(test_client, shop, refresh_mode=mode, refresh_config=config)
    assert refused.status_code == 400, refused.text
    assert test_client.get(shop["base"], headers=shop["h"]).json() == [], "a refused table must not be left behind"
    # ... and an existing table cannot be switched to it either.
    table = _create(test_client, shop).json()
    switched = test_client.put(f"{shop['base']}/{table['id']}",
                               json={"refresh_mode": mode, "refresh_config": config}, headers=shop["h"])
    assert switched.status_code == 400, switched.text
    # The shared connection still accepts it.
    assert _create(test_client, shop, base=shop["shared_base"], refresh_mode=mode,
                   refresh_config=config).status_code == 200


@pytest.mark.e2e
def test_a_query_that_does_not_run_is_refused_when_the_table_is_saved(test_client, shop):
    broken = _create(test_client, shop, definition_sql="SELECT * FROM no_such_table")
    assert broken.status_code == 400, broken.text


@pytest.mark.e2e
def test_refresh_and_edit_keep_a_per_user_table_free_of_shared_data(test_client, shop):
    table = _create(test_client, shop).json()
    url = f"{shop['base']}/{table['id']}"
    refreshed = test_client.post(f"{url}/refresh", headers=shop["h"])
    assert refreshed.status_code == 200, refreshed.text
    edited = test_client.put(url, json={"definition_sql": "SELECT id, seq FROM orders"}, headers=shop["h"])
    assert edited.status_code == 200, edited.text
    for body in (refreshed.json(), edited.json()):
        assert body["per_user"] is True and body["artifact_bytes"] is None and body["no_rows"] == 0
    assert {c["name"] for c in edited.json()["columns"]} == {"id", "seq"}


@pytest.mark.e2e
def test_the_admin_can_list_and_remove_peoples_copies(test_client, shop):
    table = _create(test_client, shop).json()
    url = f"{shop['base']}/{table['id']}/user-copies"
    assert test_client.get(url, headers=shop["h"]).json() == []
    removed = test_client.delete(url, headers=shop["h"])
    assert removed.status_code == 200 and removed.json()["copies"] == 0
    # A table that is not on this connection is not reachable through it.
    other = _create(test_client, shop, base=shop["shared_base"]).json()
    assert test_client.get(f"{shop['base']}/{other['id']}/user-copies", headers=shop["h"]).status_code == 404
    assert test_client.get(f"{shop['base']}/{uuid.uuid4()}/user-copies", headers=shop["h"]).status_code == 404


@pytest.mark.e2e
def test_a_member_without_manage_connection_sees_and_removes_nobodys_copies(
        test_client, shop, invite_user_to_org):
    table = _create(test_client, shop).json()
    url = f"{shop['base']}/{table['id']}/user-copies"
    member = invite_user_to_org(org_id=shop["org_id"], admin_token=shop["token"])
    mh = _headers(member["token"], shop["org_id"])
    assert test_client.get(url, headers=mh).status_code == 403
    assert test_client.delete(url, headers=mh).status_code == 403
    assert test_client.delete(url, params={"copy_id": str(uuid.uuid4())}, headers=mh).status_code == 403
    # The admin still can.
    assert test_client.get(url, headers=shop["h"]).status_code == 200


@pytest.mark.e2e
def test_changing_how_people_sign_in_moves_the_tables_between_shared_and_per_person(
        test_client, shop, update_connection):
    shared = _create(test_client, shop, base=shop["shared_base"]).json()
    assert shared["artifact_bytes"] > 0 and shared["no_rows"] == 40
    conn_id = shop["shared"]["id"]

    # Shared -> per person: rows read with the shared credentials are dropped.
    update_connection(connection_id=conn_id, payload={"auth_policy": "user_required"},
                      user_token=shop["token"], org_id=shop["org_id"])
    listed = test_client.get(shop["shared_base"], headers=shop["h"]).json()
    assert [(t["per_user"], t["artifact_bytes"], t["no_rows"]) for t in listed] == [(True, None, 0)]

    # ... and back: the table is shared again and an admin can rebuild it.
    update_connection(connection_id=conn_id, payload={"auth_policy": "system_only"},
                      user_token=shop["token"], org_id=shop["org_id"])
    back = test_client.get(shop["shared_base"], headers=shop["h"]).json()[0]
    assert back["per_user"] is False and back["user_copies"] is None
    rebuilt = test_client.post(f"{shop['shared_base']}/{shared['id']}/refresh", headers=shop["h"])
    assert rebuilt.status_code == 200 and rebuilt.json()["no_rows"] == 40
