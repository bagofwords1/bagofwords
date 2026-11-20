"""`scripts/scan_postgres_schema_settings.py`: the pre-release scan.

Contract: it reads every saved `postgresql` connection's config from the app
database and reports each one whose schema setting the query-time rule
(`postgresql_client.checked_search_path`) refuses, one line per connection
with its id, its organization id and the kind of rule that failed. It never
prints the setting's value. Deleted connections are skipped; a config it
cannot decode is reported as `unreadable config`. The database comes from
the app's settings, never a command-line URL; before scanning, stderr names
its dialect and database name (a SQLite file's base name only), never the
host, user or password. Exit 0
when nothing is reported, 1 when something is, 2 with one error line on
stderr on any error, loading the app's own code included.

Runs on the test app database (SQLite by default). The connection rows are
written directly: the API refuses these settings now, and the scan exists
for rows saved before it did.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.db

TAB = chr(9)
BS = chr(92)
REFUSED = {
    "rvw_a;": "semicolon",
    "rvw_a --x": "comment",
    "rvw_a /* x */": "comment",
    "'a" + BS + "b'": "backslash",
    '"a' + TAB + 'b"': "control character",
    "a%b": "unquoted punctuation",
    "1.5": "unquoted punctuation",
    "public, -1": "unquoted punctuation",
}
ACCEPTED = ["public", "rvw_a, \"Sales\"", "'Sales'", "123", "\"$user\", public", None, ""]


def _scan_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "scan_postgres_schema_settings.py"
    spec = importlib.util.spec_from_file_location("scan_postgres_schema_settings", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _seed(settings, other_types=()):
    """One org per call; a Postgres connection per schema setting (half
    stored as a JSON string, as the services store it), plus connections of
    other types. Returns {setting: (connection id, organization id)}."""
    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.organization import Organization

    seeded = {}
    async with async_session_maker() as db:
        org = Organization(name=f"Scan Org {uuid.uuid4().hex[:8]}")
        db.add(org)
        await db.flush()
        for i, schema in enumerate(settings):
            config = {"host": "db.invalid", "port": 5432, "database": "d"}
            if schema is not None:
                config["schema"] = schema
            conn = Connection(organization_id=org.id, name=f"pg-{i}-{uuid.uuid4().hex[:6]}", type="postgresql",
                              config=json.dumps(config) if i % 2 else config, auth_policy="system_only")
            db.add(conn)
            await db.flush()
            seeded[schema] = (str(conn.id), str(org.id))
        for i, (conn_type, config) in enumerate(other_types):
            db.add(Connection(organization_id=org.id, name=f"other-{i}", type=conn_type, config=config,
                              auth_policy="system_only"))
        await db.commit()
    return seeded


def test_the_scan_reports_each_refused_setting_by_id_and_rule_without_its_value(capsys):
    refused = asyncio.run(_seed(list(REFUSED)))
    accepted = asyncio.run(_seed(ACCEPTED, other_types=[("sqlite", {"database": ":memory:", "schema": "x;y"})]))

    code = _scan_module().main([])
    out = capsys.readouterr().out

    assert code == 1
    lines = [line for line in out.splitlines() if "connection_id=" in line]
    assert len(lines) == len(REFUSED)
    for schema, (conn_id, org_id) in refused.items():
        [line] = [line for line in lines if f"connection_id={conn_id}" in line]
        assert f"organization_id={org_id}" in line
        assert f"problem={REFUSED[schema]}" in line
    for conn_id, _ in accepted.values():
        assert conn_id not in out
    for schema in REFUSED:
        for item in schema.split(","):
            # Shorter items (`-1`) can occur inside an id.
            if item.strip() != "public" and len(item.strip()) >= 3:
                assert item.strip() not in out
    assert "db.invalid" not in out


def test_the_scan_exits_zero_when_every_setting_passes(capsys):
    asyncio.run(_seed(ACCEPTED))
    code = _scan_module().main([])
    out = capsys.readouterr().out
    assert code == 0
    assert "connection_id=" not in out


async def _seed_row(config, *, deleted=False):
    """One Postgres connection with `config` as written to its column."""
    from datetime import datetime

    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.organization import Organization

    async with async_session_maker() as db:
        org = Organization(name=f"Scan Org {uuid.uuid4().hex[:8]}")
        db.add(org)
        await db.flush()
        conn = Connection(organization_id=org.id, name=f"pg-{uuid.uuid4().hex[:6]}", type="postgresql",
                          config=config, auth_policy="system_only",
                          deleted_at=datetime.utcnow() if deleted else None)
        db.add(conn)
        await db.commit()
        return str(conn.id), str(org.id)


def test_the_scan_skips_a_deleted_connection(capsys):
    asyncio.run(_seed_row({"host": "db.invalid", "database": "d", "schema": "rvw_a;"}, deleted=True))
    code = _scan_module().main([])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "connection_id=" not in out


def test_the_scan_reports_a_config_it_cannot_read(capsys):
    conn_id, org_id = asyncio.run(_seed_row("{not json"))
    code = _scan_module().main([])
    out = capsys.readouterr().out
    assert code == 1
    assert f"connection_id={conn_id} organization_id={org_id} problem=unreadable config" in out
    assert "not json" not in out


def _app_db(path, rows):
    """A bare app database file with a `connections` table holding `rows`
    of (id, organization id, config text, deleted_at)."""
    import sqlite3

    db = sqlite3.connect(path)
    db.execute("CREATE TABLE connections (id TEXT, organization_id TEXT, type TEXT, config TEXT, deleted_at TEXT)")
    db.executemany("INSERT INTO connections VALUES (?, ?, 'postgresql', ?, ?)", rows)
    db.commit()
    db.close()


def test_the_scan_reads_the_app_database_the_settings_name(tmp_path, monkeypatch, capsys):
    """The app's own settings choose the database (`BOW_DATABASE_URL`, or
    `TEST_DATABASE_URL` under test); the URL is never a command-line flag,
    which would show its password in the process list."""
    path = tmp_path / "app.db"
    _app_db(str(path), [("c-env", "o-env", json.dumps({"schema": "a%b"}), None)])
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{path}")
    module = _scan_module()
    code = module.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert "connection_id=c-env organization_id=o-env problem=unquoted punctuation" in out

    with pytest.raises(SystemExit):
        module.main(["--help"])
    assert "--database-url" not in capsys.readouterr().out


def test_the_scan_exits_two_with_one_line_when_the_database_is_unreachable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{tmp_path}/missing-dir/app.db")
    code = _scan_module().main([])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    # The banner, then the one error line.
    assert captured.err.splitlines() == ["scanning sqlite database app.db", "scan failed: OperationalError"]


def test_the_sqlite_banner_names_only_the_files_base_name(tmp_path, monkeypatch, capsys):
    path = tmp_path / "private-dir" / "bow-app.db"
    path.parent.mkdir()
    _app_db(str(path), [])
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{path}")
    code = _scan_module().main([])
    err = capsys.readouterr().err
    assert code == 0
    assert err.splitlines() == ["scanning sqlite database bow-app.db"]
    assert "private-dir" not in err


def test_the_banner_names_the_dialect_and_database_but_no_credentials(monkeypatch, capsys):
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://scan_user:Pw-s3cret-9@db.invalid:5432/app_db")
    code = _scan_module().main([])
    err = capsys.readouterr().err
    assert code == 2
    assert err.splitlines()[0] == "scanning postgresql database app_db"
    assert len(err.splitlines()) == 2
    for secret in ("scan_user", "Pw-s3cret-9", "db.invalid", "5432"):
        assert secret not in err


def test_the_scan_exits_two_with_one_line_when_the_app_does_not_import(monkeypatch, capsys):
    """Any failure, even loading the app's own code, is one line and exit 2."""
    monkeypatch.setitem(__import__("sys").modules, "app.data_sources.clients.postgresql_client", None)
    try:
        code = _scan_module().main([])
    except ImportError as e:
        code = f"crashed: {type(e).__name__}"
    err = capsys.readouterr().err
    assert code == 2
    assert err.splitlines() == ["scan failed: ModuleNotFoundError"]


def test_the_scan_exits_two_with_one_line_when_the_settings_fail(monkeypatch, capsys):
    def broken():
        raise RuntimeError("settings hold postgresql://u:Pw-s3cret-9@h/db")

    monkeypatch.setattr("app.settings.database.create_database_engine", broken)
    code = _scan_module().main([])
    err = capsys.readouterr().err
    assert code == 2
    assert err.splitlines() == ["scan failed: RuntimeError"]
    assert "Pw-s3cret-9" not in err
