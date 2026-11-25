"""A file-based connection may only be pointed at data, not at the server itself.

Contract, through the connection API:
* Creating or repointing a sqlite / duckdb / csv connection at one of the app's
  own files or a system location is refused, and nothing is saved.
* A connection over an ordinary data file is created and tested as before.
* Generated demo data is only reachable by the organization it belongs to.
* A database file that does not exist is an ordinary failed connection test
  that names the file, and the file is not created.
"""
import sqlite3
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3]
CHINOOK = BACKEND / "tests" / "config" / "chinook.sqlite"

PROTECTED = [
    ("sqlite", {"database": "/etc/passwd"}),
    ("sqlite", {"database": str(BACKEND / "db" / "app.db")}),
    ("duckdb", {"database": "/proc/self/environ"}),
    ("duckdb", {"uris": str(BACKEND / "uploads" / "**" / "*.csv")}),
    ("csv", {"file_paths": str(BACKEND / "uploads" / "files" / "*.csv")}),
    ("csv", {"file_paths": "/etc/*.csv"}),
]


@pytest.fixture
def admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    return {"token": token, "org": whoami(token)["organizations"][0]["id"]}


def _headers(account):
    return {"Authorization": f"Bearer {account['token']}", "X-Organization-Id": str(account["org"])}


def _create(test_client, account, conn_type, config):
    return test_client.post(
        "/api/connections",
        json={"name": f"files-{uuid.uuid4().hex[:6]}", "type": conn_type, "config": config,
              "credentials": {}, "auth_policy": "system_only"},
        headers=_headers(account),
    )


@pytest.mark.parametrize("conn_type,config", PROTECTED)
def test_connection_cannot_be_created_over_a_protected_path(test_client, admin, get_connections, conn_type, config):
    resp = _create(test_client, admin, conn_type, config)
    assert resp.status_code == 400, resp.text
    assert get_connections(user_token=admin["token"], org_id=admin["org"]) == []


def test_connection_over_a_data_file_still_works(test_client, admin, tmp_path, test_connection_connectivity):
    db = tmp_path / "shop.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL)")
    con.commit()
    con.close()
    for config in ({"database": str(db)}, {"database": str(CHINOOK)}):
        resp = _create(test_client, admin, "sqlite", config)
        assert resp.status_code == 200, resp.text
        result = test_connection_connectivity(
            connection_id=resp.json()["id"], user_token=admin["token"], org_id=admin["org"]
        )
        assert result["success"] is True, result


def test_connection_cannot_be_repointed_at_a_protected_path(test_client, admin, get_connection):
    created = _create(test_client, admin, "sqlite", {"database": str(CHINOOK)})
    assert created.status_code == 200, created.text
    conn_id = created.json()["id"]
    resp = test_client.put(
        f"/api/connections/{conn_id}", json={"config": {"database": "/etc/passwd"}}, headers=_headers(admin)
    )
    assert resp.status_code == 400, resp.text
    kept = get_connection(connection_id=conn_id, user_token=admin["token"], org_id=admin["org"])
    assert kept["config"]["database"] == str(CHINOOK)


def test_generated_demo_data_of_another_organization_is_refused(test_client, admin):
    other_org = str(uuid.uuid4())
    theirs = _create(test_client, admin, "sqlite", {"database": f"uploads/demo_data/{other_org}/sales.sqlite"})
    assert theirs.status_code == 400, theirs.text
    assert "protected" in theirs.json()["detail"]
    # The caller's own folder passes the policy; what is left is that this
    # particular file does not exist.
    mine = _create(test_client, admin, "sqlite", {"database": f"uploads/demo_data/{admin['org']}/sales.sqlite"})
    assert "protected" not in mine.text
    assert "not found" in mine.text


def test_missing_database_file_is_a_failed_test_not_a_server_error(test_client, admin, tmp_path):
    missing = tmp_path / "gone.sqlite"
    created = _create(test_client, admin, "sqlite", {"database": str(missing)})
    assert created.status_code < 500, created.text
    # The API shortens server paths in error text to the file name.
    assert "database file not found" in created.text and missing.name in created.text
    assert not missing.exists()

    # A saved connection whose file later disappears: the test endpoint reports it.
    db = tmp_path / "shop.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    saved = _create(test_client, admin, "sqlite", {"database": str(db)})
    assert saved.status_code == 200, saved.text
    db.unlink()
    resp = test_client.post(f"/api/connections/{saved.json()['id']}/test", headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is False
    assert "database file not found" in resp.json()["message"]
    assert not db.exists()


def test_organization_with_assigned_directories_cannot_connect_outside_them(test_client, admin, tmp_path, monkeypatch):
    from app.settings.config import settings

    mine, theirs = tmp_path / "deptA", tmp_path / "deptB"
    for share in (mine, theirs):
        share.mkdir()
        (share / "notes.txt").write_text("hello")
        con = sqlite3.connect(share / "shop.sqlite")
        con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()

    # Without an assignment both shares are usable, as today.
    assert _create(test_client, admin, "network_dir", {"root_path": str(theirs)}).status_code == 200

    monkeypatch.setattr(settings.bow_config.security, "organization_file_roots", {admin["org"]: [str(mine)]})
    for conn_type, key, name in (("network_dir", "root_path", ""), ("sqlite", "database", "shop.sqlite")):
        refused = _create(test_client, admin, conn_type, {key: str(theirs / name)})
        assert refused.status_code == 400, refused.text
        allowed = _create(test_client, admin, conn_type, {key: str(mine / name)})
        assert allowed.status_code == 200, allowed.text

    # Another organization's assignment does not bind this one.
    monkeypatch.setattr(settings.bow_config.security, "organization_file_roots", {str(uuid.uuid4()): [str(mine)]})
    assert _create(test_client, admin, "sqlite", {"database": str(theirs / "shop.sqlite")}).status_code == 200
