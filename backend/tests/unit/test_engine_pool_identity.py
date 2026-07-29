"""Pooled engines must never be shared across identities.

Engines are now cached and reused instead of being created and disposed per
query. That is only safe if the pool key distinguishes *who is connecting* —
otherwise, on a `user_required` connection, user B can be handed a live pooled
connection still authenticated as user A.

The key is (uri, connect_args, key_extra), so for every pooled client the
identity has to end up in one of those three. These tests assert that per
client, because the failure is silent: everything works, the wrong person's
rows come back.

The SQL Server client already documents this hazard in `sql_server_uri` — under
integrated auth the identity comes from KRB5CCNAME at connect time and not from
the connection string, so it binds `APP=BagOfWords-<principal>` to give each
principal its own bucket.
"""

import pytest

from app.data_sources.engine_pool import _key


def _pool_key(uri, connect_args=None, key_extra=None):
    return _key(uri, connect_args, key_extra)


# --------------------------------------------------------------------------
# Per-client: two identities, same target → different pool keys
# --------------------------------------------------------------------------

def test_postgres_identity_is_in_the_uri():
    from app.data_sources.clients.postgresql_client import PostgresqlClient

    a = PostgresqlClient(host="h", port=5432, database="d", user="alice", password="pw1")
    b = PostgresqlClient(host="h", port=5432, database="d", user="bob", password="pw2")
    assert _pool_key(a.pg_uri) != _pool_key(b.pg_uri)


def test_postgres_password_rotation_changes_the_key():
    """A rotated password must not keep serving from a pool authenticated with
    the old one."""
    from app.data_sources.clients.postgresql_client import PostgresqlClient

    old = PostgresqlClient(host="h", port=5432, database="d", user="svc", password="old")
    new = PostgresqlClient(host="h", port=5432, database="d", user="svc", password="new")
    assert _pool_key(old.pg_uri) != _pool_key(new.pg_uri)


def test_mysql_identity_is_in_the_uri():
    from app.data_sources.clients.mysql_client import MysqlClient

    a = MysqlClient(host="h", port=3306, database="d", user="alice", password="pw1")
    b = MysqlClient(host="h", port=3306, database="d", user="bob", password="pw2")
    assert _pool_key(a.mysql_uri) != _pool_key(b.mysql_uri)


def test_mariadb_identity_is_in_the_uri():
    from app.data_sources.clients.mariadb_client import MariadbClient

    a = MariadbClient(host="h", port=3306, database="d", user="alice", password="pw1")
    b = MariadbClient(host="h", port=3306, database="d", user="bob", password="pw2")
    assert _pool_key(a.mariadb_uri) != _pool_key(b.mariadb_uri)


def test_oracle_identity_is_in_the_uri():
    from app.data_sources.clients.oracledb_client import OracledbClient

    a = OracledbClient(host="h", port=1521, service_name="s", user="alice", password="pw1")
    b = OracledbClient(host="h", port=1521, service_name="s", user="bob", password="pw2")
    assert _pool_key(a.oracle_uri) != _pool_key(b.oracle_uri)


def test_trino_identity_is_in_the_uri():
    """Trino commonly authenticates with a bare user and no password — the user
    alone still has to separate the pools."""
    from app.data_sources.clients.trino_client import TrinoClient

    a = TrinoClient(host="h", port=8080, catalog="c", schema="s", user="alice")
    b = TrinoClient(host="h", port=8080, catalog="c", schema="s", user="bob")
    assert _pool_key(a.trino_uri) != _pool_key(b.trino_uri)


def test_mssql_sql_auth_identity_is_in_the_uri():
    from app.data_sources.clients.mssql_client import MSSQLClient

    a = MSSQLClient(host="h", port=1433, database="d", user="alice", password="pw1")
    b = MSSQLClient(host="h", port=1433, database="d", user="bob", password="pw2")
    assert _pool_key(a.sql_server_uri) != _pool_key(b.sql_server_uri)


def test_mssql_kerberos_identity_separates_pools():
    """Under integrated auth the connection string is otherwise identical for
    every user; the impersonated principal must still split the pool."""
    from app.data_sources.clients.mssql_client import MSSQLClient

    a = MSSQLClient(host="h", port=1433, database="d", use_kerberos=True,
                    kerberos_principal="svc@REALM", kerberos_impersonate="alice@REALM")
    b = MSSQLClient(host="h", port=1433, database="d", use_kerberos=True,
                    kerberos_principal="svc@REALM", kerberos_impersonate="bob@REALM")
    assert _pool_key(a.sql_server_uri) != _pool_key(b.sql_server_uri)


# --------------------------------------------------------------------------
# The registry itself
# --------------------------------------------------------------------------

def test_key_extra_separates_otherwise_identical_uris():
    """The escape hatch for connectors whose identity is not in the URI."""
    assert _pool_key("x://h/d", None, "ccache-alice") != _pool_key("x://h/d", None, "ccache-bob")


def test_same_identity_reuses_one_key():
    """Pooling has to actually pool — identical identity must collide."""
    assert _pool_key("x://u:p@h/d") == _pool_key("x://u:p@h/d")


def test_connect_args_participate_in_the_key():
    assert _pool_key("x://h/d", {"ssl": "require"}) != _pool_key("x://h/d", {"ssl": "disable"})


def test_engines_are_not_disposed_per_query():
    """Regression guard: the whole point is that connect() stops tearing the
    engine down. A stray dispose() in a client silently restores the old
    connect-per-query behaviour and the 3-statement handshake with it."""
    import inspect

    from app.data_sources.clients import (
        mariadb_client, mssql_client, mysql_client, oracledb_client,
        postgresql_client, presto_client, snowflake_client, trino_client,
    )

    for mod in (postgresql_client, mysql_client, mariadb_client, oracledb_client,
                trino_client, presto_client, mssql_client, snowflake_client):
        src = inspect.getsource(mod)
        # Allow the word inside comments; reject an actual call.
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "engine.dispose()" not in stripped, (
                f"{mod.__name__} disposes its engine per query — pooling is defeated"
            )
