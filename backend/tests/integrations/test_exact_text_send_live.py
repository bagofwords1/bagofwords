"""The text the checks approved is the text the server receives.

Contract: an agent query on Postgres or MySQL reaches the server exactly as
the read-only guard returned it. Nothing rewrites it on the way: no
`:name` bind markers, no `%` handling, so literals holding them come back
unchanged, and the server's own record of the statement equals the text the
client was handed (on Postgres, after the one message's read-only prefix
line).

Needs Docker. CI does not collect tests/integrations; these run on the test
harness. Without a Docker daemon they skip, or fail when CI is set.
"""

from __future__ import annotations

import os
import sys

import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.code_execution.sql_guard import ensure_read_only
from app.data_sources.clients.mysql_client import MysqlClient
from app.data_sources.clients.postgresql_client import PostgresqlClient

pytestmark = pytest.mark.integration


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


def _require_docker() -> None:
    if not _docker_available():
        if os.environ.get("CI"):
            pytest.fail("Docker daemon not reachable in CI: the live exact-text tests must run")
        pytest.skip("Docker daemon not reachable")


@pytest.fixture(scope="module")
def postgres():
    _require_docker()
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:15") as container:
        yield {"host": container.get_container_host_ip(), "port": int(container.get_exposed_port(5432)),
               "user": container.username, "password": container.password, "database": container.dbname}


@pytest.fixture(scope="module")
def mysql():
    _require_docker()
    from testcontainers.mysql import MySqlContainer

    with MySqlContainer("mysql:8") as container:
        yield {"host": container.get_container_host_ip(), "port": int(container.get_exposed_port(3306)),
               "user": container.username, "password": container.password, "database": container.dbname}


class RecordingPostgresqlClient(PostgresqlClient):
    def execute_query(self, sql):
        self.sent.append(sql)
        return super().execute_query(sql)


class RecordingMysqlClient(MysqlClient):
    def execute_query(self, sql):
        self.sent.append(sql)
        return super().execute_query(sql)


@pytest.fixture(params=["inprocess", "subprocess"])
def mode(request, monkeypatch):
    if request.param == "subprocess" and not sys.platform.startswith("linux"):
        pytest.skip("sandbox child needs Linux")
    monkeypatch.setenv("BOW_CODE_SANDBOX", request.param)
    if request.param == "inprocess":
        # In-process mode is for local development only; CI runs with
        # ENVIRONMENT=production, so the test names the environment itself.
        monkeypatch.setenv("ENVIRONMENT", "development")
    return request.param


def _run(client, sql: str):
    """Run `sql` through the agent wrapper; (rows, captured queries)."""
    code = (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return ds_clients['db'].execute_query({sql!r})\n"
    )
    df, _log, queries = StreamingCodeExecutor().execute_code(code=code, ds_clients={"db": client}, excel_files=[])
    return df.to_dict("records"), queries


def test_postgres_receives_the_checked_text_with_its_literals_unchanged(mode, postgres):
    client = RecordingPostgresqlClient(**postgres)
    client.sent = []
    sql = "SELECT current_query() AS q, ':x' AS a, '%' AS b, 'a%%b' AS c /* note */"
    checked = ensure_read_only(client, sql)
    rows, queries = _run(client, sql)
    # The server's record is the one message: the read-only prefix on its
    # own line, then the checked text byte for byte.
    assert rows == [{"q": "BEGIN READ ONLY;\n" + checked, "a": ":x", "b": "%", "c": "a%%b"}]
    assert client.sent == queries == [checked]


def test_mysql_receives_the_checked_text_with_its_literals_unchanged(mode, mysql):
    client = RecordingMysqlClient(**mysql)
    client.sent = []
    sql = ("SELECT ':x' AS a, '%' AS b, 'a%%b' AS c, "
           "(SELECT INFO FROM information_schema.PROCESSLIST WHERE ID = CONNECTION_ID()) AS q")
    checked = ensure_read_only(client, sql)
    rows, queries = _run(client, sql)
    assert rows == [{"a": ":x", "b": "%", "c": "a%%b", "q": checked}]
    assert client.sent == queries == [checked]
