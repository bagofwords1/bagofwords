"""How a `postgresql_multi` tenant failure is named in logs, index stats and agent text.

Every attach error DuckDB raises echoes the attach string, which carries
`options='-c statement_timeout=...'`, and libpq names the server, port and
user. The messages below are what DuckDB 1.5 and psycopg2 return against a
real Postgres 15; the category must come from the cause, never from the
echoed options, and must name neither the server nor the login.
"""

import duckdb
import pytest

from app.data_sources.clients.postgresql_multi_engine import failure_category

HOST = "db-17.internal.example"
USER = "bow_reader"
_ATTACH = (
    'IO Error: Unable to connect to Postgres at "dbname=tenant_x sslmode=disable '
    "options='-c statement_timeout=300000'\": "
)
_SERVER = f'connection to server at "{HOST}" (10.1.2.3), port 6543 failed: '

CASES = [
    (duckdb.IOException(_ATTACH + _SERVER + "FATAL:  sorry, too many clients already"), "no free connection"),
    (duckdb.IOException(
        _ATTACH + _SERVER + "FATAL:  remaining connection slots are reserved for non-replication superuser connections"
    ), "no free connection"),
    (duckdb.IOException(_ATTACH + _SERVER + f'FATAL:  password authentication failed for user "{USER}"'),
     "authentication"),
    (duckdb.IOException(_ATTACH + _SERVER + 'FATAL:  permission denied for database "tenant_x"'), "permission"),
    (duckdb.IOException(_ATTACH + _SERVER + 'FATAL:  database "tenant_x" does not exist'), "does not exist"),
    (duckdb.IOException(
        _ATTACH + _SERVER + "Connection refused\n\tIs the server running on that host and accepting TCP/IP connections?"
    ), "connection failed"),
    (duckdb.IOException(
        _ATTACH + f'could not translate host name "{HOST}" to address: nodename nor servname provided, or not known'
    ), "connection failed"),
    (duckdb.IOException(_ATTACH + _SERVER + "timeout expired"), "timed out"),
    (duckdb.IOException("IO Error: Failed to fetch header for COPY: ERROR:  canceling statement due to statement timeout"),
     "timed out"),
    (duckdb.Error(
        'Failed to prepare COPY "COPY (SELECT "id" FROM "public"."secret" ) TO STDOUT (FORMAT "binary");": '
        "ERROR:  permission denied for table secret"
    ), "permission"),
    (duckdb.Error(
        'Failed to prepare COPY "COPY (SELECT "id" FROM "public"."t" ) TO STDOUT (FORMAT "binary");": '
        'ERROR:  relation "public.t" does not exist'
    ), "does not exist"),
    (f'(psycopg2.OperationalError) connection to server at "{HOST}" (10.1.2.3), port 6543 failed: '
     'FATAL:  permission denied for database "tenant_x"', "permission"),
]


@pytest.mark.parametrize(("error", "expected"), CASES)
def test_failure_is_named_by_its_cause(error, expected):
    category = failure_category(error)

    assert expected in category
    assert HOST not in category
    assert USER not in category
    assert "6543" not in category


def test_attach_failure_of_unknown_cause_is_a_connection_failure():
    category = failure_category(duckdb.IOException(_ATTACH + _SERVER + "FATAL:  something new"))

    assert "connection failed" in category


def test_unrecognised_failure_names_only_the_error_type():
    category = failure_category(
        duckdb.IOException(f"IO Error: Failed to fetch header for COPY from {HOST}: ERROR:  out of shared memory")
    )

    assert category == "IOException"
