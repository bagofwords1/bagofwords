"""An agent's table selection holds on SQL clients sqlglot has no dialect for.

Contract: on Db2, IBM i, Vertica, SQL Anywhere, SAP HANA and Pinot (guard
profile "nonparsing_sql"), while the selection disables a table, a query that
reads only enabled tables runs, and a query that reads a disabled table in any
position or spelling is refused before the client is called. So is a query
the checks cannot read the same way under every quoting rule the engine may
apply, a call of anything but a plain SQL function, a FROM form (APPLY or
LATERAL included) that reads a table named elsewhere, a catalog table, a name
that may resolve outside the catalog, and text that does not parse. With
nothing disabled, queries run as before.

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
    with pytest.raises(TableAccessError):
        _run(client, sql)
    assert sent == []


# Refused by the read-only guard before the selection is checked: a nested
# comment, which the engine and a plain reading end at different places,
# and a word the guard reads as a write.
READ_GUARD_FIRST = [
    *[(engine, "SELECT id FROM events /* a /* b */ UNION SELECT id FROM secret */") for engine in ENGINES],
    *[("vertica", f"SELECT {name}(id) AS n FROM events") for name in ("refresh", "REFRESH", "Refresh")],
]


@pytest.mark.parametrize("engine, sql", READ_GUARD_FIRST)
def test_the_read_only_guard_refuses_first(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(UnsafeSQLError):
        _run(client, sql)
    assert sent == []


# Text that one of the engine's quoting rules reads as a string and another
# as code: with a backslash escape (Vertica E'...', SQL Anywhere strings,
# Pinot E'...') the second line reads the disabled table; without one it is
# inside a string.
QUOTING_TRICKS = [
    ("vertica", "SELECT id FROM events WHERE note = 'a\\'' UNION SELECT id FROM secret --'"),
    ("sybase", "SELECT id FROM events WHERE note = 'a\\'' UNION SELECT id FROM secret --'"),
    ("pinot", "SELECT id FROM events WHERE note = E'\\'' UNION SELECT id FROM secret --'"),
    # A backtick quotes a name to Pinot's parser in one reading only.
    ("pinot", "SELECT id FROM events WHERE note = `a -- ` UNION SELECT id FROM secret"),
]


@pytest.mark.parametrize("engine, sql", QUOTING_TRICKS)
def test_text_the_engine_may_read_two_ways_is_refused(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError, match="reads differently under this source's quoting rules"):
        _run(client, sql)
    assert sent == []


# APPLY and LATERAL: over a subquery its tables are read as any other; over a
# name or a call the table it reads cannot be checked.
CATALOG_TABLES = {"db2": "SYSCAT.TABLES", "ibmi": "QSYS2.SYSTABLES", "vertica": "v_catalog.tables",
                  "sybase": "SYS.SYSTAB", "hana": "SYS.TABLES", "pinot": "information_schema.tables"}


def _apply_refused(engine):
    catalog, schema = CATALOG_TABLES[engine], ENGINES[engine]["schema"]
    return [
        f"SELECT e.id FROM events e CROSS APPLY {catalog}",
        "SELECT e.id FROM events e CROSS APPLY ledger",
        "SELECT e.id FROM events e CROSS APPLY fn_rows(e.id)",
        f"SELECT e.id FROM events e CROSS APPLY {schema}.fn_rows(1)",
        f"SELECT e.id FROM events e OUTER APPLY {catalog}",
        "SELECT e.id FROM events e OUTER APPLY ledger",
        "SELECT e.id FROM events e OUTER APPLY fn_rows(e.id)",
        f"SELECT e.id FROM events e OUTER APPLY {schema}.fn_rows(1)",
        "SELECT e.id FROM events e, LATERAL ledger",
        "SELECT e.id FROM events e CROSS JOIN LATERAL ledger",
    ]


@pytest.mark.parametrize("engine, sql", [(e, s) for e in ENGINES for s in _apply_refused(e)])
def test_apply_or_lateral_over_a_name_or_a_call_is_refused_before_dispatch(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError):
        _run(client, sql)
    assert sent == []


@pytest.mark.parametrize("engine, sql", [(e, s) for e in ENGINES for s in (
    "SELECT COUNT(*) AS n FROM events e CROSS APPLY (SELECT id FROM events f WHERE f.id = e.id) x",
    "SELECT COUNT(*) AS n FROM events e OUTER APPLY (SELECT id FROM events f WHERE f.id = e.id) x",
)])
def test_apply_over_a_subquery_of_enabled_tables_runs(engine, sql):
    client, sent = _client(engine)
    df, _, _ = _run(client, sql)
    assert df["n"].iloc[0] == 3
    assert sent == [sql]


# Calls: while a table is disabled only plain SQL functions may be called.
ALLOWED_CALLS = [
    "SELECT LOWER(note) AS l, UPPER(note) AS u, SUBSTR(note, 1, 2) AS s, TRIM(note) AS t, LENGTH(note) AS n "
    "FROM events",
    "SELECT COALESCE(note, 'x') AS c, NULLIF(id, 0) AS z, GREATEST(id, 1) AS g, LEAST(id, 2) AS n FROM events",
    "SELECT ROUND(AVG(id), 2) AS a, MIN(id) AS lo, MAX(id) AS hi, SUM(id) AS n, ABS(MOD(id, 3)) AS m "
    "FROM events",
    "SELECT id, RANK() OVER (ORDER BY created) AS r, LAG(id) OVER (ORDER BY created) AS n FROM events",
    "SELECT CONCAT(note, 'x') AS c, CEIL(id) AS c2, FLOOR(id) AS f, SQRT(ABS(id)) AS n FROM events",
    "SELECT COUNT(DISTINCT note) AS n, CAST(MAX(created) AS VARCHAR(20)) AS m FROM events",
]
ENGINE_DATE_CALLS = {
    "db2": "SELECT ADD_DAYS(created, 1) AS d, DAYS_BETWEEN(created, created) AS n FROM events",
    "ibmi": "SELECT ADD_MONTHS(created, 1) AS d, MONTHS_BETWEEN(created, created) AS n FROM events",
    "vertica": "SELECT DATE_TRUNC('month', created) AS d, DATEDIFF('day', created, created) AS n FROM events",
    "sybase": "SELECT DATEADD(day, 1, created) AS d, DATEDIFF(day, created, created) AS n FROM events",
    "hana": "SELECT ADD_DAYS(created, 1) AS d, DAYS_BETWEEN(created, created) AS n FROM events",
    "pinot": "SELECT dateTrunc('DAY', created) AS d, DATE_TRUNC('DAY', created) AS n FROM events",
}


@pytest.mark.parametrize("engine, sql", [(e, s) for e in ENGINES for s in ALLOWED_CALLS + [ENGINE_DATE_CALLS[e]]])
def test_plain_sql_functions_run_while_a_table_is_disabled(engine, sql):
    client, sent = _client(engine)
    df, _, _ = _run(client, sql)
    assert df["n"].iloc[0] == 3
    assert sent == [sql]


# Functions each engine documents as reading a table named in a string,
# running SQL held in a string, or acting on a table by name. None may be
# called while a table is disabled, however its name is spelled.
FUNCTIONS_THAT_READ_ELSEWHERE = {
    "db2": ["xmltable", "xmlquery", "xmlexists", "get_clob_from_file", "get_blob_from_file",
            "get_dbclob_from_file", "get_xml_file", "ifs_read", "ifs_read_utf8"],
    "vertica": ["analyze_statistics", "export_tables", "export_objects", "import_statistics", "drop_partitions",
                "copy_table", "move_partitions_to_table", "swap_partitions_between_tables", "partition_table",
                "purge_table", "get_compliance_status", "set_config_parameter", "clear_caches",
                "enable_local_segments", "disable_profiling", "compute_flextable_keys", "build_flextable_view",
                "materialize_flextable_columns", "restore_location", "merge_partitions", "infer_table_ddl",
                "evaluate_delete_performance", "do_tm_task", "start_refresh", "make_ahm_now",
                "alter_location_use", "close_session", "cancel_refresh", "interrupt_statement", "reload_spread",
                "rebalance_cluster", "save_query_profile", "mark_design_ksafe", "upgrade_license",
                "summarize_numcol", "detect_outliers", "one_hot_encoder_fit", "normalize_fit", "cross_validate",
                "purge", "refresh", "audit", "reorganize", "arima", "autoregressor", "bisecting_kmeans", "kmeans",
                "kprototypes", "linear_reg", "logistic_reg", "moving_average", "naive_bayes", "pls_reg",
                "poisson_reg", "rf_classifier", "rf_regressor", "svm_classifier", "svm_regressor",
                "xgb_classifier", "xgb_regressor", "iforest", "pca", "svd", "balance", "impute", "normalize",
                "corr_matrix", "chi_squared", "dump_table_partition_keys", "dump_partition_keys"],
    "sybase": ["openstring", "openxml", "read_client_file", "sa_table_page_usage", "sp_tables", "xp_read_file",
               "xp_cmdshell"],
    "hana": ["placeholder"],
    "pinot": ["lookup", "in_subquery", "in_partitioned_subquery", "insubquery", "look_up", "inpartitionedsubquery"],
}
FUNCTIONS_THAT_READ_ELSEWHERE["ibmi"] = FUNCTIONS_THAT_READ_ELSEWHERE["db2"]


def _spellings(name):
    variant = name.replace("_", "") if "_" in name else name[:2] + "_" + name[2:]
    return [name, name.upper(), name.title().replace("_", ""), f'"{name}"', variant, variant.upper()]


CALL_SPELLINGS = [(engine, spelled) for engine, names in FUNCTIONS_THAT_READ_ELSEWHERE.items()
                  for name in names for spelled in _spellings(name)
                  if (engine, f"SELECT {spelled}(id) AS n FROM events") not in READ_GUARD_FIRST]


@pytest.mark.parametrize("engine, spelled", CALL_SPELLINGS)
def test_a_function_that_reads_elsewhere_is_refused_in_every_spelling(engine, spelled):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError, match=spelled.strip('"').upper()):
        _run(client, f"SELECT {spelled}(id) AS n FROM events")
    assert sent == []


@pytest.mark.parametrize("engine, sql, name", [
    ("pinot", "SELECT insubquery(id, 'SELECT id FROM secret') AS n FROM events", "INSUBQUERY"),
    ("pinot", "SELECT look_up('secret', 'v', 'id', id) AS n FROM events", "LOOK_UP"),
    ("pinot", "SELECT LOOK_UP('secret', 'v', 'id', id) AS n FROM events", "LOOK_UP"),
    ("pinot", "SELECT \"lookUp\"('secret', 'v', 'id', id) AS n FROM events", "LOOKUP"),
    ("pinot", "SELECT IN_PARTITIONED_SUBQUERY(id, 'SELECT id FROM secret') AS n FROM events",
     "IN_PARTITIONED_SUBQUERY"),
    ("vertica", "SELECT DUMP_TABLE_PARTITION_KEYS('secret') AS n", "DUMP_TABLE_PARTITION_KEYS"),
    ("sybase", "SELECT [xp_read_file]('/etc/hosts') AS n FROM events", "XP_READ_FILE"),
    ("hana", "SELECT \"COUNT\"(id) AS n FROM events", "COUNT"),
    *[(e, "SELECT my_udf(id) AS n FROM events", "MY_UDF") for e in ENGINES],
])
def test_a_call_outside_the_plain_functions_is_refused_naming_it(engine, sql, name):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError, match=name):
        _run(client, sql)
    assert sent == []


# A catalog that stores two-part names: a name with more parts may be the
# same table in another database.
@pytest.mark.parametrize("engine", ["db2", "ibmi", "sybase", "hana"])
@pytest.mark.parametrize("database", ["otherdb", "remotedb"])
def test_a_name_with_more_parts_than_the_catalog_stores_is_refused(engine, database):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError, match="not enabled for this agent"):
        _run(client, f"SELECT COUNT(*) AS n FROM {database}.{ENGINES[engine]['schema']}.events")
    assert sent == []


def _vertica_in_schema(schema):
    client, sent = _client("vertica")
    client.schema = schema
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(active=["events", "users"], inactive=["secret"]))
    return client, sent


def test_vertica_refuses_an_unqualified_name_outside_the_public_schema():
    # Vertica reads a bare name through its search path, not the
    # connection's schema.
    client, sent = _vertica_in_schema("sales")
    with pytest.raises(TableAccessError, match="SALES.USERS"):
        _run(client, "SELECT COUNT(*) AS n FROM users")
    assert sent == []
    df, _, _ = _run(client, "SELECT COUNT(*) AS n FROM sales.users")
    assert sent == ["SELECT COUNT(*) AS n FROM sales.users"]
    df, _, _ = _run(client, "WITH u AS (SELECT * FROM sales.users) SELECT COUNT(*) AS n FROM u")
    assert len(sent) == 2


@pytest.mark.parametrize("schema", ["public", ""])
def test_vertica_keeps_unqualified_names_in_the_public_schema(schema):
    client, sent = _vertica_in_schema(schema)
    _run(client, "SELECT COUNT(*) AS n FROM users")
    assert sent == ["SELECT COUNT(*) AS n FROM users"]


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
    # A backtick opens a name in one Pinot reading; in the other the quote
    # inside it opens a string that never ends.
    ("pinot", "SELECT id FROM events WHERE note = `a' UNION SELECT id FROM secret --`", "could not be read as SQL"),
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
    # Nothing disabled: the selection is not checked on these clients, so a
    # query fares as it does on a client with no selection at all (the
    # read-only guard still applies to both).
    baseline, baseline_sent = _client(engine, disabled=False)
    delattr(baseline, TABLE_SCOPE_ATTR)
    client, sent = _client(engine, disabled=False)

    def outcome(c):
        try:
            _run(c, sql)
        except UnsafeSQLError as e:
            return type(e), str(e)
        return None

    assert outcome(client) == outcome(baseline)
    assert sent == baseline_sent
    assert sent in ([sql], [])


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
