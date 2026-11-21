"""An agent's table selection holds on SQL clients sqlglot has no dialect for.

Contract: on Db2, IBM i, Vertica, SQL Anywhere, SAP HANA and Pinot (guard
profile "nonparsing_sql"), while the selection disables a table, a query that
reads only enabled tables runs, and a query that reads a disabled table in any
position or spelling is refused before the client is called. So is a query
the checks cannot read the same way under every quoting rule the engine may
apply, a function or FROM form that reads a table named elsewhere, a catalog
table, and text that does not parse. With nothing disabled, queries run as
before.

Through the code-execution wrapper, as an agent's generated code calls the
client; the client's transport is replaced and every statement it is sent is
recorded.
"""

import importlib

import pandas as pd
import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor, TableAccessError, UnsafeSQLError
from app.ai.code_execution.table_scope import TABLE_SCOPE_ATTR, TableScope

# Per client: how its catalog stores the two tables, the schema a qualified
# reference names, the client attributes the connection sets, one idiom of
# the engine, and one table function or catalog table.
ENGINES = {
    "db2": dict(module="db2_client", name="Db2Client", active="APP.EVENTS", inactive="APP.SECRET",
                schema="app", attrs={}, idioms=["SELECT id FROM events FETCH FIRST 5 ROWS ONLY"],
                sources=["SELECT * FROM SYSCAT.TABLES",
                         "SELECT * FROM TABLE(MON_GET_TABLE('', '', -1)) AS t",
                         "SELECT * FROM FINAL TABLE (SELECT id FROM events)",
                         "SELECT XMLQUERY('$d' PASSING id AS \"d\") FROM events"]),
    "ibmi": dict(module="ibmi_client", name="IbmiClient", active="APP.EVENTS", inactive="APP.SECRET",
                 schema="app", attrs={}, idioms=["SELECT id FROM app.events FETCH FIRST 5 ROWS ONLY"],
                 sources=["SELECT * FROM QSYS2.SYSTABLES",
                          "SELECT * FROM XMLTABLE('$d/r' PASSING x COLUMNS a INT PATH 'a') AS t",
                          "SELECT id FROM app/secret"]),
    "vertica": dict(module="vertica_client", name="VerticaClient", active="events", inactive="secret",
                    schema="public", attrs={"schema": "public"},
                    idioms=["SELECT created::date AS d FROM events LIMIT 5"],
                    sources=["SELECT * FROM v_catalog.tables",
                             "SELECT EXPORT_TABLES('', 'secret')",
                             "SELECT * FROM other.events"]),
    "sybase": dict(module="sybase_client", name="SybaseClient", active="dbo.events", inactive="dbo.secret",
                   schema="dbo", attrs={}, idioms=["SELECT TOP 5 id FROM [dbo].[events]"],
                   sources=["SELECT * FROM SYS.SYSTAB",
                            "SELECT * FROM OPENSTRING(VALUE 'a') WITH (a INT) AS t",
                            "SELECT * FROM sa_table_page_usage()"]),
    "hana": dict(module="sap_hana_client", name="SapHanaClient", active="APP.EVENTS", inactive="APP.SECRET",
                 schema="app", attrs={}, idioms=["SELECT TOP 5 id FROM events", "SELECT id FROM events LIMIT 5"],
                 sources=["SELECT * FROM SYS.TABLES",
                          "SELECT * FROM \"app\".\"cv\"(PLACEHOLDER.\"$$p$$\" => 'x')",
                          "SELECT * FROM app.cv (PLACEHOLDER.\"p\" => 'x')"]),
    "pinot": dict(module="pinot_client", name="PinotClient", active="events", inactive="secret",
                  schema="default", attrs={}, idioms=["SELECT id FROM events LIMIT 5"],
                  sources=["SELECT lookUp('secret', 'v', 'id', id) FROM events",
                           "SELECT * FROM other.events"]),
}


def _client(engine, *, disabled=True):
    spec = ENGINES[engine]
    parent = getattr(importlib.import_module(f"app.data_sources.clients.{spec['module']}"), spec["name"])
    sent = []

    def execute_query(self, sql):
        sent.append(sql)
        return pd.DataFrame({"n": [3]})

    client = object.__new__(type("Recording" + spec["name"], (parent,), {"execute_query": execute_query}))
    for key, value in spec["attrs"].items():
        setattr(client, key, value)
    active, inactive = [spec["active"]], [spec["inactive"]]
    if not disabled:
        active, inactive = active + inactive, []
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(active=active, inactive=inactive))
    return client, sent


def _run(client, sql):
    code = (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return ds_clients['src'].execute_query({sql!r})\n"
    )
    return StreamingCodeExecutor().execute_code(code=code, ds_clients={"src": client}, excel_files=[])


def _allowed(engine):
    schema = ENGINES[engine]["schema"]
    return [
        "SELECT COUNT(*) AS n FROM events",
        f"SELECT COUNT(*) AS n FROM {schema}.events",
        "WITH x AS (SELECT * FROM events) SELECT COUNT(*) AS n FROM x",
        "SELECT COUNT(*) AS n FROM events WHERE note = 'secret'",
        "-- secret is off limits\nSELECT COUNT(*) AS n FROM events /* not secret */",
        "SELECT EXTRACT(YEAR FROM created) AS y, COUNT(*) AS n FROM events GROUP BY EXTRACT(YEAR FROM created)",
        "SELECT a.id, COUNT(*) AS n FROM events a LEFT JOIN events b ON a.id = b.id GROUP BY a.id ORDER BY 2 DESC",
        "SELECT t.id FROM (SELECT id FROM events WHERE id > 1) t, events u WHERE t.id = u.id",
        "SELECT id, ROW_NUMBER() OVER (PARTITION BY note ORDER BY created) AS r FROM events",
        "SELECT CASE WHEN id > 1 THEN 'big' ELSE 'small' END AS size, CAST(id AS DECIMAL(10, 2)) AS v "
        "FROM events e WHERE EXISTS (SELECT 1 FROM events f WHERE f.id = e.id)",
        *ENGINES[engine]["idioms"],
    ]


def _refused(engine):
    schema = ENGINES[engine]["schema"]
    return [
        "SELECT * FROM secret",
        "SELECT e.id FROM events e JOIN secret s ON e.id = s.id",
        "SELECT e.id FROM events e, secret s WHERE e.id = s.id",
        "SELECT id FROM events WHERE id IN (SELECT id FROM secret)",
        "SELECT (SELECT MAX(id) FROM secret) AS m FROM events",
        "WITH x AS (SELECT * FROM secret) SELECT * FROM x",
        "SELECT id FROM events UNION SELECT id FROM secret",
        "SELECT * FROM SECRET",
        "SELECT * FROM \"secret\"",
        f"SELECT * FROM {schema}.secret",
        # A nested comment, which the engine and a plain reading end at
        # different places.
        "SELECT id FROM events /* a /* b */ UNION SELECT id FROM secret */",
        "SELECT FROM WHERE events",
        *ENGINES[engine]["sources"],
    ]


CASES_ALLOWED = [(engine, sql) for engine in ENGINES for sql in _allowed(engine)]
CASES_REFUSED = [(engine, sql) for engine in ENGINES for sql in _refused(engine)]


@pytest.mark.parametrize("engine, sql", CASES_ALLOWED)
def test_a_query_on_enabled_tables_runs_while_another_table_is_disabled(engine, sql):
    client, sent = _client(engine)
    df, _, _ = _run(client, sql)
    assert df["n"].iloc[0] == 3
    assert sent == [sql]


@pytest.mark.parametrize("engine, sql", CASES_REFUSED)
def test_a_read_of_a_disabled_table_is_refused_before_dispatch(engine, sql):
    client, sent = _client(engine)
    with pytest.raises((TableAccessError, UnsafeSQLError)):
        _run(client, sql)
    assert sent == []


# Text that one of the engine's quoting rules reads as a string and another
# as code: with a backslash escape (Vertica E'...', SQL Anywhere strings) the
# second line reads the disabled table; without one it is inside a string.
QUOTING_TRICKS = [
    ("vertica", "SELECT id FROM events WHERE note = 'a\\'' UNION SELECT id FROM secret --'"),
    ("sybase", "SELECT id FROM events WHERE note = 'a\\'' UNION SELECT id FROM secret --'"),
    # A backtick quotes a name to Pinot's parser in one reading only.
    ("pinot", "SELECT id FROM events WHERE note = `a' UNION SELECT id FROM secret --`"),
]


@pytest.mark.parametrize("engine, sql", QUOTING_TRICKS)
def test_text_the_engine_may_read_two_ways_is_refused(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError, match="(?i)read"):
        _run(client, sql)
    assert sent == []


# Names the token cross-check refuses where the parse alone reads no table:
# a function that reads a table named in a string, and the disabled table's
# name used as anything else.
CROSS_CHECKED = [
    ("pinot", "SELECT lookUp('secret', 'v', 'id', id) AS n FROM events"),
    ("vertica", "SELECT EXPORT_TABLES('', 'secret') AS n"),
    ("db2", "SELECT XMLQUERY('$d') AS n FROM events"),
    ("hana", "SELECT secret AS n FROM events"),
    ("sybase", "SELECT \"Secret\" AS n FROM events"),
    ("ibmi", "SELECT id AS secret FROM events"),
]


@pytest.mark.parametrize("engine, sql", CROSS_CHECKED)
def test_the_token_cross_check_refuses_what_the_parse_reads_no_table_in(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError):
        _run(client, sql)
    assert sent == []


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_refusal_names_the_disabled_table(engine):
    client, _ = _client(engine)
    with pytest.raises(TableAccessError, match="SECRET is not enabled for this agent"):
        _run(client, "SELECT e.id FROM events e JOIN secret s ON e.id = s.id")


@pytest.mark.parametrize("engine, sql, reason", [
    ("ibmi", "SELECT id FROM app/events", "system naming"),
    ("sybase", "SELECT * FROM sa_table_page_usage()", "not allowed as a query source"),
    ("db2", "SELECT * FROM FINAL TABLE (SELECT id FROM events)", "TABLE is not allowed"),
    ("hana", "SELECT * FROM app.cv (PLACEHOLDER.\"p\" => 'x')", "not allowed"),
    ("vertica", "SELECT id FROM events WHERE id = @x", "'@' outside a string"),
    ("db2", "SELECT secret FROM events", "SECRET is the name of a table that is not enabled"),
])
def test_a_refusal_names_its_reason(engine, sql, reason):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError, match=reason):
        _run(client, sql)
    assert sent == []


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_refusal_names_a_table_outside_the_selection(engine):
    client, _ = _client(engine)
    with pytest.raises(TableAccessError, match="LEDGER is not enabled for this agent"):
        _run(client, "SELECT COUNT(*) AS n FROM ledger")


@pytest.mark.parametrize("engine, sql", [(e, s) for e in ENGINES for s in _allowed(e) + _refused(e)
                                         if "FROM WHERE" not in s and "/*" not in s])
def test_with_nothing_disabled_queries_run_as_before(engine, sql):
    # Nothing disabled: the selection is not checked on these clients, as
    # before (the read-only guard still is).
    client, sent = _client(engine, disabled=False)
    try:
        _run(client, sql)
    except UnsafeSQLError:
        assert sent == []
        return
    assert sent == [sql]


@pytest.mark.parametrize("engine", list(ENGINES))
def test_names_that_differ_only_in_case_with_different_states_refuse_every_query(engine):
    # The engine may hold both (a quoted name keeps its case on Db2 and
    # HANA); the selection compares names lowered, so it cannot tell them
    # apart and fails closed for the connection.
    spec = ENGINES[engine]
    client, sent = _client(engine)
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(
        active=[spec["active"]], inactive=[spec["active"].swapcase(), spec["inactive"]]))
    with pytest.raises(TableAccessError, match="cannot be checked for this connection type"):
        _run(client, "SELECT COUNT(*) AS n FROM events")
    assert sent == []
