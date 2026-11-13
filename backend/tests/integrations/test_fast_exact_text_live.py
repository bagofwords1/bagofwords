"""A custom-table refresh sends the text the read-only guard checked, and
records that text on its run.

Contract, through `CustomQueryService.refresh` against a real Postgres 15
that logs every statement it receives:

* a windowed first load and the incremental refresh after it send each
  window's checked text: comments dropped, every literal in its standard
  form;
* a full (non-incremental) refresh sends the definition's checked text;
* each run's `rendered_query` is a text the server received.

Needs Docker. CI does not collect tests/integrations; these run on the test
harness. Without a Docker daemon they skip, or fail when CI is set.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio

from app.data_sources.fast import artifacts, incremental
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import KIND_BOW, ConnectionTable
from app.models.organization import Organization
from app.services import custom_table_runs
from app.services.custom_query_service import CustomQueryService

pytestmark = [pytest.mark.integration, pytest.mark.db, pytest.mark.asyncio]

BS = chr(92)
# A comment and an E-string the server would read as `it's`.
DEFINITION = "SELECT id, updated_at, E'it" + BS + "'s' AS lit /* note */ FROM items"
# The same definition as the guard sends it.
CHECKED_DEFINITION = "SELECT id, updated_at, 'it''s' AS lit   FROM items"


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def server():
    if not _docker_available():
        if os.environ.get("CI"):
            pytest.fail("Docker daemon not reachable in CI: the live custom-table tests must run")
        pytest.skip("Docker daemon not reachable")
    import psycopg2
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:15").with_command("postgres -c log_statement=all") as container:
        settings = {"host": container.get_container_host_ip(), "port": int(container.get_exposed_port(5432)),
                    "user": container.username, "password": container.password, "database": container.dbname,
                    "container": container}
        con = psycopg2.connect(host=settings["host"], port=settings["port"], user=settings["user"],
                               password=settings["password"], dbname=settings["database"])
        con.autocommit = True
        with con.cursor() as cur:
            cur.execute("CREATE TABLE items (id int PRIMARY KEY, updated_at timestamp)")
            cur.execute("INSERT INTO items SELECT g, timestamp '2026-01-01' + g * interval '1 hour' "
                        "FROM generate_series(1, 40) g")
        settings["admin"] = con
        try:
            yield settings
        finally:
            con.close()


def _received(server) -> list[str]:
    """The query texts the server received, as logged (a cursor's query
    without its DECLARE)."""
    logs = server["container"].get_wrapped_container().logs().decode()
    out = []
    for line in logs.splitlines():
        if "statement: " not in line:
            continue
        statement = line.split("statement: ", 1)[1]
        if statement.startswith("DECLARE ") and " FOR " in statement:
            statement = statement.split(" FOR ", 1)[1]
        out.append(statement)
    return out


class Lab:
    def __init__(self, server, connection_id: str):
        self.server, self.connection_id = server, connection_id
        self.service = CustomQueryService()
        self.table_id = None

    async def table(self, sql: str, mode: str, **config) -> None:
        settings = self.service.refresh_settings(mode, config, sql)
        async with async_session_maker() as db:
            row = ConnectionTable(connection_id=self.connection_id, name=f"copy_{mode}", kind=KIND_BOW,
                                  definition_sql=sql, columns=[], pks=[], fks=[],
                                  refresh_mode=settings.mode, refresh_config=settings.to_dict())
            db.add(row)
            await db.commit()
            self.table_id = row.id

    async def refresh(self) -> None:
        async with async_session_maker() as db:
            connection = await db.get(Connection, self.connection_id)
            table = await db.get(ConnectionTable, self.table_id)
            await self.service.refresh(db, connection, table)

    async def last_run(self) -> dict:
        async with async_session_maker() as db:
            listing = await custom_table_runs.list_runs(db, self.table_id, limit=1)
            return await custom_table_runs.get_run(db, self.table_id, listing["runs"][0]["id"])

    async def copy(self, sql: str) -> list:
        async with async_session_maker() as db:
            row = await db.get(ConnectionTable, self.table_id)
        con = artifacts.connect_encrypted(row.artifact_path, artifacts.decrypt_key(row.artifact_key_enc),
                                          read_only=True)
        try:
            return [tuple(r) for r in con.execute(sql).fetchall()]
        finally:
            con.close()


@pytest_asyncio.fixture
async def lab(server, tmp_path):
    with patch.object(artifacts, "_ARTIFACT_ROOT", tmp_path / "fast"):
        async with async_session_maker() as db:
            org = Organization(name=f"Live {uuid.uuid4().hex[:8]}")
            db.add(org)
            await db.flush()
            connection = Connection(name="Items (Postgres)", type="postgresql", organization_id=org.id,
                                    config={"host": server["host"], "port": server["port"],
                                            "database": server["database"]})
            connection.encrypt_credentials({"user": server["user"], "password": server["password"]})
            db.add(connection)
            await db.commit()
            connection_id = connection.id
        yield Lab(server, connection_id)


def _assert_checked(statements: list[str]) -> None:
    """Every received read of the definition is its checked form."""
    reads = [s for s in statements if " AS lit" in s]
    assert reads
    for text in reads:
        assert "note" not in text and "E'" not in text and "'it''s'" in text, text


async def test_a_windowed_load_and_the_increment_after_it_send_the_checked_text(lab, server):
    await lab.table(DEFINITION, incremental.MODE_MERGE, cursor_column="updated_at", unique_key=["id"])
    before = len(_received(server))
    with patch.object(incremental, "CHUNK_TARGET_ROWS", 10):
        await lab.refresh()
    first = await lab.last_run()
    assert first["status"] == "success" and first["chunks_total"] >= 3
    assert await lab.copy("SELECT count(*), min(lit), max(lit) FROM copy_merge") == [(40, "it's", "it's")]
    load = _received(server)[before:]
    _assert_checked(load)
    assert first["rendered_query"] in load

    server["admin"].cursor().execute(
        "INSERT INTO items SELECT g, now() AT TIME ZONE 'UTC' FROM generate_series(41, 45) g")
    before = len(_received(server))
    await lab.refresh()
    increment = await lab.last_run()
    assert increment["mode"] == "incremental" and increment["status"] == "success"
    assert await lab.copy("SELECT count(*) FROM copy_merge") == [(45,)]
    sent = _received(server)[before:]
    _assert_checked(sent)
    assert increment["rendered_query"] in sent


async def test_a_failed_increment_records_the_checked_text_it_was_sending(lab, server):
    from fastapi import HTTPException

    from app.data_sources.fast.sources import SqlAlchemySource

    await lab.table(DEFINITION, incremental.MODE_MERGE, cursor_column="updated_at", unique_key=["id"])
    await lab.refresh()
    server["admin"].cursor().execute(
        "INSERT INTO items SELECT g, now() AT TIME ZONE 'UTC' FROM generate_series(46, 48) g")

    sent: list = []
    with patch.object(SqlAlchemySource, "stream_batches", _failing_send(sent)), pytest.raises(HTTPException):
        await lab.refresh()
    run = await lab.last_run()
    assert (run["mode"], run["status"]) == ("incremental", "failed")
    _assert_checked([run["rendered_query"]])
    assert sent == [run["rendered_query"]]


def _failing_send(sent: list):
    """The source's send, failing once it has the text it was given."""
    def source_went_away(self, sql, batch_rows):
        sent.append(sql)
        raise ConnectionError("source went away")
    return source_went_away


async def test_a_full_refresh_that_fails_in_the_send_records_the_checked_text(lab, server):
    from fastapi import HTTPException

    from app.data_sources.fast.sources import SqlAlchemySource

    await lab.table(DEFINITION, incremental.MODE_FULL)
    sent: list = []
    with patch.object(SqlAlchemySource, "stream_batches", _failing_send(sent)), pytest.raises(HTTPException):
        await lab.refresh()
    run = await lab.last_run()
    assert run["status"] == "failed"
    assert sent == [CHECKED_DEFINITION]
    assert run["rendered_query"] == CHECKED_DEFINITION, "the text the source was given, never the definition"


# A definition with a comment that the guard refuses (it calls a function
# that reads a server file).
REFUSED_DEFINITION = "SELECT id, updated_at, pg_read_file('x') AS lit /* note */ FROM items"


async def test_a_refused_full_refresh_records_no_query_sent(lab, server):
    from fastapi import HTTPException

    await lab.table(REFUSED_DEFINITION, incremental.MODE_FULL)
    before = len(_received(server))
    with pytest.raises(HTTPException):
        await lab.refresh()
    run = await lab.last_run()
    assert run["status"] == "failed"
    assert run["rendered_query"] is None
    assert not any("pg_read_file" in s for s in _received(server)[before:])


async def test_a_refused_increment_records_no_query_sent(lab, server):
    from fastapi import HTTPException

    from app.models.connection_table import ConnectionTable as Row

    await lab.table(DEFINITION, incremental.MODE_MERGE, cursor_column="updated_at", unique_key=["id"])
    await lab.refresh()
    # The definition as stored before the guard refused it: the table keeps
    # its checkpoint, so the next refresh is an increment.
    async with async_session_maker() as db:
        row = await db.get(Row, lab.table_id)
        row.definition_sql = REFUSED_DEFINITION
        row.refresh_state = {**row.refresh_state, "definition_hash": incremental.definition_hash(
            REFUSED_DEFINITION, CustomQueryService.target_of(row), incremental.RefreshConfig.from_row(row))}
        await db.commit()
    before = len(_received(server))
    with pytest.raises(HTTPException):
        await lab.refresh()
    run = await lab.last_run()
    assert (run["mode"], run["status"]) == ("incremental", "failed")
    assert run["rendered_query"] is None
    assert not any("pg_read_file" in s for s in _received(server)[before:])


async def test_a_full_refresh_sends_and_records_the_checked_text(lab, server):
    await lab.table(DEFINITION, incremental.MODE_FULL)
    before = len(_received(server))
    await lab.refresh()
    run = await lab.last_run()
    assert run["status"] == "success"
    with server["admin"].cursor() as cur:
        cur.execute("SELECT count(*) FROM items")
        total = cur.fetchone()[0]
    assert await lab.copy("SELECT count(*), min(lit) FROM copy_full") == [(total, "it's")]
    sent = _received(server)[before:]
    _assert_checked(sent)
    assert run["rendered_query"] == CHECKED_DEFINITION
    assert CHECKED_DEFINITION in sent
