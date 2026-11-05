"""Sample rows from a connection's introspected tables.

Contract (GET /api/connections/{id}/tables/{table_id}/preview):
  * a connection manager gets the table's real columns and rows, bounded by
    ``row_limit``, with the query that ran — the bound is written into that
    query, so running it anywhere returns the same rows; ``base_sql`` is the
    same query unbounded, the starting point for a custom table;
  * the query is built server-side from the catalog row, so any table name
    the source allows — spaces, quotes, keywords — previews correctly;
  * only ``manage_connection`` may read rows this way: a plain member gets 403;
  * a table id resolves only within its own connection.
"""
import sqlite3
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _make_sqlite_db(path, *, big_rows: int):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, balance REAL)")
    con.executemany(
        "INSERT INTO customers (id, name, balance) VALUES (?, ?, ?)",
        [(1, "Ada", 10.5), (2, "Grace", None), (3, "Linus", 0.0)],
    )
    # A reserved word is only reachable quoted: `SELECT * FROM order` is a
    # syntax error. (The SQLite indexer catalogs identifier-shaped names only;
    # quote escaping per dialect is covered in unit/test_table_preview_quoting.)
    con.execute('CREATE TABLE "order" (sku TEXT, qty INTEGER)')
    con.execute('INSERT INTO "order" VALUES (\'A-1\', 4)')
    con.execute("CREATE TABLE events (n INTEGER)")
    con.executemany("INSERT INTO events (n) VALUES (?)", [(i,) for i in range(big_rows)])
    con.commit()
    con.close()


@pytest.fixture
def sqlite_connection(tmp_path, create_user, login_user, whoami, create_connection,
                      wait_for_connection_indexing, get_connection_tables):
    def _make(*, big_rows: int = 120):
        admin = create_user()
        token = login_user(admin["email"], admin["password"])
        org_id = whoami(token)["organizations"][0]["id"]
        db_path = tmp_path / f"preview_{uuid.uuid4().hex[:8]}.sqlite"
        _make_sqlite_db(str(db_path), big_rows=big_rows)
        conn = create_connection(
            name=f"Preview {uuid.uuid4().hex[:6]}",
            type="sqlite",
            config={"database": str(db_path)},
            user_token=token,
            org_id=org_id,
        )
        wait_for_connection_indexing(connection_id=conn["id"], user_token=token, org_id=org_id)
        tables = get_connection_tables(connection_id=conn["id"], user_token=token, org_id=org_id)
        by_name = {t["name"]: t for t in tables}
        return {"token": token, "org_id": org_id, "admin": admin, "conn": conn,
                "tables": by_name, "db_path": db_path}
    return _make


def _preview(test_client, ctx, table_id, *, token=None, conn_id=None):
    return test_client.get(
        f"/api/connections/{conn_id or ctx['conn']['id']}/tables/{table_id}/preview",
        headers=_headers(token or ctx["token"], ctx["org_id"]),
    )


@pytest.mark.e2e
def test_manager_sees_real_columns_and_rows(test_client, sqlite_connection):
    ctx = sqlite_connection()
    table = ctx["tables"]["customers"]
    assert table["preview_supported"] is True

    res = _preview(test_client, ctx, table["id"])
    assert res.status_code == 200, res.text
    body = res.json()

    assert [c["name"] for c in body["columns"]] == ["id", "name", "balance"]
    rows = sorted(body["rows"], key=lambda r: r[0])
    assert rows == [[1, "Ada", 10.5], [2, "Grace", None], [3, "Linus", 0.0]]
    assert body["truncated"] is False
    assert body["table_id"] == table["id"]
    assert "customers" in body["sql"]


@pytest.mark.e2e
@pytest.mark.parametrize("big_rows", [51, 300])
def test_rows_are_bounded_by_row_limit(test_client, sqlite_connection, big_rows):
    ctx = sqlite_connection(big_rows=big_rows)
    res = _preview(test_client, ctx, ctx["tables"]["events"]["id"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["rows"]) == body["row_limit"] < big_rows
    assert body["truncated"] is True

    # The bound lives in the query itself, not only in how rows are fetched:
    # run as written, `sql` returns row_limit rows and `base_sql` all of them.
    con = sqlite3.connect(str(ctx["db_path"]))
    try:
        assert len(con.execute(body["sql"]).fetchall()) == body["row_limit"]
        assert len(con.execute(body["base_sql"]).fetchall()) == big_rows
    finally:
        con.close()


@pytest.mark.e2e
def test_names_that_need_quoting_still_preview(test_client, sqlite_connection):
    ctx = sqlite_connection()
    name = "order"
    assert name in ctx["tables"], list(ctx["tables"])
    res = _preview(test_client, ctx, ctx["tables"][name]["id"])
    assert res.status_code == 200, res.text
    assert res.json()["rows"] == [["A-1", 4]]


@pytest.mark.e2e
def test_member_without_manage_connection_is_refused(
    test_client, sqlite_connection, invite_user_to_org,
):
    ctx = sqlite_connection()
    member = invite_user_to_org(org_id=ctx["org_id"], admin_token=ctx["token"])

    res = _preview(test_client, ctx, ctx["tables"]["customers"]["id"], token=member["token"])
    assert res.status_code == 403, res.text


@pytest.mark.e2e
def test_table_id_resolves_only_within_its_connection(
    test_client, sqlite_connection, create_connection, tmp_path,
):
    ctx = sqlite_connection()

    unknown = _preview(test_client, ctx, str(uuid.uuid4()))
    assert unknown.status_code == 404
    assert unknown.json().get("error_code") == "connection.table_not_found"

    # Same admin, a second connection: its id must not reach the first's table.
    other_db = tmp_path / f"other_{uuid.uuid4().hex[:8]}.sqlite"
    _make_sqlite_db(str(other_db), big_rows=1)
    other = create_connection(
        name=f"Other {uuid.uuid4().hex[:6]}",
        type="sqlite",
        config={"database": str(other_db)},
        user_token=ctx["token"],
        org_id=ctx["org_id"],
    )
    crossed = _preview(test_client, ctx, ctx["tables"]["customers"]["id"], conn_id=other["id"])
    assert crossed.status_code == 404


@pytest.mark.e2e
def test_source_failure_is_a_typed_400_without_internals(test_client, sqlite_connection):
    """The source's own reason reaches the manager; library internals do not."""
    ctx = sqlite_connection()
    # The table is in the catalog but gone at the source.
    con = sqlite3.connect(str(ctx["db_path"]))
    con.execute("DROP TABLE customers")
    con.commit()
    con.close()

    res = _preview(test_client, ctx, ctx["tables"]["customers"]["id"])
    assert res.status_code == 400, res.text
    body = res.json()
    assert body["error_code"] == "connection.table_preview_failed"
    assert "customers" in body["params"]["reason"]
    for marker in ("Traceback", "sqlalchemy", "site-packages", "[SQL:"):
        assert marker not in res.text, marker


@pytest.mark.e2e
def test_edited_query_from_the_preview_runs_read_only(
    test_client, sqlite_connection, invite_user_to_org,
):
    """The preview's editor runs an admin's edit of the table query through the
    custom-table preview route: a narrowed SELECT returns its rows, the result
    stays bounded, a write is refused and leaves the source untouched, and a
    member without manage_connection cannot run edited SQL at all."""
    ctx = sqlite_connection(big_rows=250)
    h = _headers(ctx["token"], ctx["org_id"])
    on = test_client.put(
        "/api/organization/settings",
        json={"config": {"enable_custom_queries": {"value": True}}},
        headers=h,
    )
    assert on.status_code == 200, on.text

    table = _preview(test_client, ctx, ctx["tables"]["customers"]["id"]).json()
    edited = table["base_sql"] + " WHERE balance > 1"
    url = f"/api/connections/{ctx['conn']['id']}/custom-queries/preview"

    res = test_client.post(url, json={"definition_sql": edited}, headers=h)
    assert res.status_code == 200, res.text
    assert res.json()["rows"] == [[1, "Ada", 10.5]]

    # An edit that widens the query is still bounded by the route's row limit.
    wide = test_client.post(url, json={"definition_sql": "SELECT n FROM events"}, headers=h)
    assert wide.status_code == 200, wide.text
    assert len(wide.json()["rows"]) == wide.json()["row_limit"] < 250

    member = invite_user_to_org(org_id=ctx["org_id"], admin_token=ctx["token"])
    refused = test_client.post(
        url, json={"definition_sql": edited},
        headers=_headers(member["token"], ctx["org_id"]),
    )
    assert refused.status_code == 403, refused.text

    write =test_client.post(url, json={"definition_sql": "DELETE FROM customers"}, headers=h)
    assert write.status_code >= 400, write.text
    con = sqlite3.connect(str(ctx["db_path"]))
    assert con.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 3
    con.close()
