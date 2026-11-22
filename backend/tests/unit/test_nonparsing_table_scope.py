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
from app.ai.code_execution.table_scope import (
    TABLE_SCOPE_ATTR,
    TableScope,
    TableScopeViolation,
    enforce_table_scope,
)

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


# A word the engine may read as a table's alias where the parse reads the
# start of a clause: what follows it is still checked as a source.
def _after_a_clause_word(engine):
    return [
        f"SELECT * FROM events limit, {CATALOG_TABLES[engine]}",
        "SELECT * FROM events limit, ledger",
        "SELECT * FROM (SELECT * FROM events limit, ledger) t",
        "SELECT * FROM events AS limit, ledger",
        "SELECT * FROM events AS limit(a), ledger",
        "SELECT * FROM events limit(a), ledger",
        "SELECT * FROM events offset, ledger",
        "SELECT * FROM events fetch, ledger",
        "SELECT * FROM events limit JOIN ledger ON 1 = 1",
        "SELECT * FROM events limit JOIN events b ON 1 = 1, ledger",
        "SELECT * FROM events e JOIN events f ON e.limit = f.id, ledger",
        "SELECT * FROM events limit ledger",
    ]


@pytest.mark.parametrize("engine, sql", [(e, s) for e in ENGINES for s in _after_a_clause_word(e)])
def test_a_source_after_a_clause_word_is_refused_before_dispatch(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError):
        _run(client, sql)
    assert sent == []


@pytest.mark.parametrize("engine, sql", [(e, s) for e in ENGINES for s in (
    "SELECT COUNT(*) AS n FROM events LIMIT 10",
    "SELECT COUNT(*) AS n FROM events LIMIT 10 OFFSET 5",
    "SELECT COUNT(*) AS n FROM events ORDER BY 1 FETCH FIRST 5 ROWS ONLY",
)])
def test_a_row_limit_that_is_a_number_runs(engine, sql):
    client, sent = _client(engine)
    df, _, _ = _run(client, sql)
    assert df["n"].iloc[0] == 3
    assert sent == [sql]


# After a table in FROM, with any keyword of the parser where an alias may
# stand, a name that follows as a source is never left unchecked. A query
# may run only with the bare name right after the keyword, where it is no
# source to any engine: the alias itself, a condition, a sampling method or
# one that reads the keyword, and a second alias (an error) to one that reads
# the keyword as the alias. The same with the keyword as the alias of a
# joined table, before the join's USING or ON.
_NOT_A_SOURCE_AFTER = frozenset({"as", "where", "having", "qualify", "tablesample"})
_AFTER_A_KEYWORD = ("{k}, ledger", "{k} JOIN ledger ON 1 = 1", "{k} NATURAL JOIN ledger", "{k} CROSS APPLY ledger",
                    "{k} (a), ledger", "AS {k}, ledger", "AS {k}(a), ledger", "{k} ledger",
                    *("a JOIN events {k} " + condition + source
                      for condition in ("USING (id)", "ON a.id = 1")
                      for source in (", ledger", " JOIN ledger ON 1 = 1", " ledger")))


def _parser_keywords():
    from sqlglot import Dialect

    words = set()
    for dialect in (None, "postgres", "tsql"):
        tokenizer = Dialect.get_or_raise(dialect).tokenizer_class
        words |= {k.lower() for k in tokenizer.KEYWORDS if k.replace("_", "a").isalnum() and k[0].isalpha()}
    return sorted(words)


@pytest.mark.parametrize("engine", list(ENGINES))
def test_no_keyword_in_alias_position_leaves_a_source_unchecked(engine):
    client, _ = _client(engine)
    keywords = _parser_keywords()
    assert len(keywords) > 250 and {"limit", "offset", "fetch", "window", "qualify"} <= set(keywords)
    ran = []
    for keyword in keywords:
        for form in _AFTER_A_KEYWORD:
            try:
                enforce_table_scope(client, "SELECT * FROM events " + form.format(k=keyword))
            except TableScopeViolation:
                continue
            ran.append((keyword, form))
    assert [(k, f) for k, f in ran if f != "{k} ledger" or k not in _NOT_A_SOURCE_AFTER] == []


# WINDOW: an engine in which the word is no keyword reads it as the alias of
# the table before it, and a name after its USING or ON as one more source.
# While a table is disabled the word is refused outside strings and quoted
# names.
def _after_window(engine):
    return [
        "SELECT * FROM events a JOIN events window USING (id), ledger",
        f"SELECT * FROM events a JOIN events window USING (id), {CATALOG_TABLES[engine]}",
        "SELECT * FROM events a JOIN events window USING (id), \"SECRET \"",
        "SELECT * FROM (SELECT * FROM events a JOIN events window USING (id), ledger) t",
        "SELECT * FROM events a JOIN events AS window USING (id), ledger",
        "SELECT * FROM events window x (id), ledger",
        "SELECT * FROM events a JOIN events WINDOW USING (id) JOIN ledger ON 1 = 1",
        "SELECT * FROM events WINDOW w AS (PARTITION BY id), ledger",
        "SELECT SUM(id) OVER w AS n FROM events WINDOW w AS (ORDER BY id)",
        "SELECT e.window AS n FROM events e",
    ]


@pytest.mark.parametrize("engine, sql", [(e, s) for e in ENGINES for s in _after_window(e)])
def test_the_word_window_is_refused_before_dispatch(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError):
        _run(client, sql)
    assert sent == []


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_refusal_of_window_names_the_word(engine):
    client, _ = _client(engine)
    with pytest.raises(TableAccessError, match="WINDOW is not allowed"):
        _run(client, "SELECT SUM(id) OVER w AS n FROM events WINDOW w AS (ORDER BY id)")


@pytest.mark.parametrize("engine", list(ENGINES))
def test_window_in_a_string_or_a_quoted_name_still_runs(engine):
    client, sent = _client(engine)
    sql = "SELECT COUNT(*) AS n, MAX(id) AS \"window\" FROM events WHERE note = 'window'"
    df, _, _ = _run(client, sql)
    assert df["n"].iloc[0] == 3
    assert sent == [sql]


# A source after a FOR clause, and a name with an empty part (`a..b`, which
# an engine may read in another database).
def _after_for_or_with_an_empty_part(engine):
    schema = ENGINES[engine]["schema"]
    return [
        "SELECT * FROM events FOR SYSTEM_TIME AS OF '2020-01-01', ledger",
        "SELECT * FROM events FOR XML AUTO, ledger",
        "SELECT * FROM events NEXT VALUE FOR seq, ledger",
        "SELECT * FROM events e FOR SYSTEM_TIME AS OF '2020-01-01' JOIN ledger ON 1 = 1",
        "SELECT * FROM events for, ledger",
        f"SELECT COUNT(*) AS n FROM {schema}..events",
        "SELECT COUNT(*) AS n FROM otherdb..events",
        "SELECT COUNT(*) AS n FROM ..events",
        f"SELECT COUNT(*) AS n FROM events e JOIN {schema}..events f ON e.id = f.id",
        "SELECT e..id AS n FROM events e",
    ]


@pytest.mark.parametrize("engine, sql", [(e, s) for e in ENGINES for s in _after_for_or_with_an_empty_part(e)])
def test_a_source_after_a_for_clause_or_a_name_with_an_empty_part_is_refused(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError):
        _run(client, sql)
    assert sent == []


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_refusal_of_an_empty_name_part_names_its_reason(engine):
    client, _ = _client(engine)
    with pytest.raises(TableAccessError, match="two dots in a row"):
        _run(client, f"SELECT COUNT(*) AS n FROM {ENGINES[engine]['schema']}..events")


# Blanks in a quoted name. Db2 and IBM i leave trailing blanks out when they
# compare a quoted name, so there the name is compared without them. Any
# other blank at an end of a quoted name is refused.
@pytest.mark.parametrize("engine", list(ENGINES))
@pytest.mark.parametrize("sql", [
    "SELECT * FROM \"SECRET \"",
    "SELECT * FROM \"secret   \"",
    "SELECT \"SECRET \" AS n FROM events",
    "SELECT id AS \"Secret \" FROM events",
    "SELECT COUNT(*) AS n FROM events e JOIN \"SECRET \" s ON e.id = s.id",
    "SELECT COUNT(*) AS n FROM events e, \"SECRET \" s",
    "WITH \"SECRET \" AS (SELECT id FROM events) SELECT COUNT(*) AS n FROM \"SECRET \"",
    "SELECT COUNT(*) AS n FROM events WHERE id IN (SELECT id FROM \"SECRET \")",
    "SELECT * FROM \" SECRET\"",
    "SELECT \" secret\" AS n FROM events",
    "SELECT COUNT(*) AS n FROM \" events\"",
    "SELECT COUNT(*) AS n FROM \" events \"",
    "SELECT COUNT(*) AS n FROM \"ledger \"",
])
def test_a_quoted_name_with_blanks_at_an_end_gives_a_disabled_table_no_pass(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError):
        _run(client, sql)
    assert sent == []


@pytest.mark.parametrize("engine", ["db2", "ibmi"])
@pytest.mark.parametrize("sql", [
    "SELECT \"SECRET \" AS n FROM events",
    "SELECT * FROM \"SECRET \"",
])
def test_a_disabled_name_with_trailing_blanks_is_named_without_them(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError, match=r"SECRET is (the name of a table that is )?not enabled"):
        _run(client, sql)
    assert sent == []


@pytest.mark.parametrize("engine", ["db2", "ibmi"])
@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) AS n FROM \"EVENTS \"",
    "SELECT COUNT(*) AS n FROM app.\"EVENTS  \" e",
    "SELECT COUNT(*) AS \"n \" FROM events",
])
def test_an_enabled_name_with_trailing_blanks_runs_as_written(engine, sql):
    client, sent = _client(engine)
    df, _, _ = _run(client, sql)
    assert df["n"].iloc[0] == 3
    assert sent == [sql]


@pytest.mark.parametrize("engine", ["vertica", "sybase", "hana", "pinot"])
@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) AS n FROM \"events \"",
    "SELECT COUNT(*) AS \"n \" FROM events",
])
def test_a_quoted_name_with_trailing_blanks_is_refused_where_they_may_count(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError, match="blank"):
        _run(client, sql)
    assert sent == []


# A string where a FROM clause names a table is refused as any other source
# that is not a table.
@pytest.mark.parametrize("engine", list(ENGINES))
@pytest.mark.parametrize("sql", [
    "SELECT * FROM 'x'",
    "SELECT * FROM events e, 'x'",
    "SELECT * FROM events e JOIN 'secret' s ON 1 = 1",
    "SELECT * FROM ''",
])
def test_a_string_where_a_table_is_named_is_refused(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError, match="Could not verify which tables this query reads"):
        _run(client, sql)
    assert sent == []


# A catalog that stores bare names (Vertica, Pinot): a stored name holding
# dots is read as `schema.table` at its first dot too, so no later part of
# it is a table's name on its own.
def _bare_catalog_client(engine):
    client, sent = _client(engine)
    schema = ENGINES[engine]["schema"]
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(
        active=["events", f"{schema}.sap.T1"], inactive=["secret", f"{schema}.hr.PAY"]))
    return client, sent


@pytest.mark.parametrize("engine", ["vertica", "pinot"])
@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) AS n FROM t1",
    "SELECT COUNT(*) AS n FROM sap.t1",
    "SELECT COUNT(*) AS n FROM {schema}.t1",
    "SELECT COUNT(*) AS n FROM {schema}.sap.t1",
    "SELECT COUNT(*) AS n FROM otherdb.{schema}.events",
    "SELECT COUNT(*) AS n FROM pay",
    "SELECT COUNT(*) AS n FROM {schema}.pay",
    "SELECT COUNT(*) AS n FROM events e JOIN t1 t ON e.id = t.id",
])
def test_a_later_part_of_a_stored_name_is_no_table_where_the_catalog_stores_bare_names(engine, sql):
    client, sent = _bare_catalog_client(engine)
    with pytest.raises(TableAccessError):
        _run(client, sql.format(schema=ENGINES[engine]["schema"]))
    assert sent == []


@pytest.mark.parametrize("engine", ["vertica", "pinot"])
@pytest.mark.parametrize("sql", ["SELECT COUNT(*) AS n FROM events", "SELECT COUNT(*) AS n FROM {schema}.events"])
def test_a_bare_stored_name_still_runs_next_to_a_stored_name_holding_dots(engine, sql):
    client, sent = _bare_catalog_client(engine)
    sql = sql.format(schema=ENGINES[engine]["schema"])
    df, _, _ = _run(client, sql)
    assert df["n"].iloc[0] == 3
    assert sent == [sql]


# A catalog that stores `schema.table`: a table's name may hold dots. The
# stored name is read at its first dot only, and a query names such a table
# with the name quoted.
DOTTED = ["db2", "ibmi", "sybase", "hana"]


def _dotted_client(engine):
    client, sent = _client(engine)
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(
        active=["APP.EVENTS", "APP.sap.T1"], inactive=["APP.SECRET", "APP.hr.PAY"]))
    return client, sent


@pytest.mark.parametrize("engine", DOTTED)
@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) AS n FROM otherdb.app.events",
    "SELECT COUNT(*) AS n FROM sap.t1",
    "SELECT COUNT(*) AS n FROM app.sap.t1",
    "SELECT COUNT(*) AS n FROM t1",
    "SELECT COUNT(*) AS n FROM \"sap\".\"T1\"",
    "SELECT COUNT(*) AS n FROM app.\"hr.PAY\"",
    "SELECT COUNT(*) AS n FROM \"hr.PAY\"",
    "SELECT COUNT(*) AS n FROM hr.pay",
    "SELECT COUNT(*) AS n FROM other.\"sap.T1\"",
    "SELECT COUNT(*) AS n FROM \"app.events\"",
])
def test_a_name_that_differs_from_the_stored_schema_and_table_is_refused(engine, sql):
    client, sent = _dotted_client(engine)
    with pytest.raises(TableAccessError):
        _run(client, sql)
    assert sent == []


@pytest.mark.parametrize("engine", DOTTED)
@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) AS n FROM app.\"sap.T1\"",
    "SELECT COUNT(*) AS n FROM \"sap.T1\"",
    "SELECT COUNT(*) AS n FROM app.events e JOIN app.\"sap.T1\" t ON e.id = t.id",
])
def test_a_table_whose_name_holds_a_dot_is_read_by_its_quoted_name(engine, sql):
    client, sent = _dotted_client(engine)
    df, _, _ = _run(client, sql)
    assert df["n"].iloc[0] == 3
    assert sent == [sql]


@pytest.mark.parametrize("engine", ["vertica", "pinot"])
def test_a_quoted_name_holding_a_dot_stays_refused_where_the_catalog_stores_bare_names(engine):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError):
        _run(client, "SELECT COUNT(*) AS n FROM \"default.events\"")
    assert sent == []


# A word before a parenthesis is a function unless the parenthesis opens
# syntax after it; being a keyword gives a call no pass.
@pytest.mark.parametrize("engine", list(ENGINES))
@pytest.mark.parametrize("word", ["offset", "minus", "group", "like", "any", "some", "all", "exists", "top",
                                  "limit", "union", "having", "apply", "lateral", "filter"])
def test_a_keyword_called_as_a_function_is_refused_naming_it(engine, word):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError, match=rf"{word.upper()}\(\) is not allowed"):
        _run(client, f"SELECT {word}(id) AS n FROM events")
    assert sent == []


@pytest.mark.parametrize("engine, sql", [(e, s) for e in ENGINES for s in (
    "SELECT COUNT(*) AS n FROM events WHERE id IN (SELECT id FROM events)",
    "SELECT COUNT(*) AS n FROM events WHERE id IN (1, 2, 3)",
    "SELECT COUNT(*) AS n FROM events WHERE id = ANY (SELECT id FROM events)",
    "SELECT COUNT(*) AS n FROM events WHERE id > ALL (SELECT id FROM events)",
    "SELECT COUNT(*) AS n FROM events e WHERE EXISTS (SELECT 1 FROM events f WHERE f.id = e.id)",
    "SELECT SUM(id) OVER (PARTITION BY note ORDER BY created) AS n FROM events",
    "SELECT COUNT(*) AS n FROM events WHERE (id > 1 AND (note = 'a' OR NOT (id = 2)))",
)])
def test_a_parenthesis_that_opens_syntax_still_runs(engine, sql):
    client, sent = _client(engine)
    df, _, _ = _run(client, sql)
    assert df["n"].iloc[0] == 3
    assert sent == [sql]


@pytest.mark.parametrize("engine", list(ENGINES))
@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) AS n FROM events WHERE id = ANY (SELECT id FROM secret)",
    "SELECT COUNT(*) AS n FROM events e WHERE EXISTS (SELECT 1 FROM ledger f WHERE f.id = e.id)",
])
def test_a_subquery_after_a_keyword_is_still_checked(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError, match="not enabled"):
        _run(client, sql)
    assert sent == []


# More plain functions, per engine: each reads only its arguments there.
_DB2_CALLS = ["year", "month", "day", "hour", "minute", "second", "quarter", "week", "dayofweek", "date", "days",
              "now", "to_char", "to_date", "to_timestamp", "to_number", "int", "integer", "double", "ifnull",
              "nvl", "decode", "value", "listagg", "median", "percentile_cont", "cume_dist", "percent_rank",
              "sign", "exp", "ln", "log", "trunc", "rand", "instr", "locate", "initcap", "repeat", "lcase",
              "ucase", "digits", "regexp_like", "regexp_replace"]
MORE_CALLS = {
    "db2": _DB2_CALLS,
    "ibmi": _DB2_CALLS,
    "vertica": ["year", "month", "day", "hour", "minute", "second", "quarter", "week", "dayofweek",
                "date", "now", "getdate", "to_char", "to_date", "to_timestamp", "to_number", "ifnull", "nvl",
                "isnull", "zeroifnull", "decode", "listagg", "median", "percentile_cont", "cume_dist",
                "percent_rank", "sign", "exp", "ln", "log", "trunc", "instr", "initcap", "repeat", "regexp_like",
                "regexp_replace"],
    "sybase": ["year", "month", "day", "hour", "minute", "second", "quarter", "datepart", "date", "days",
               "dateformat", "now", "getdate", "to_char", "ifnull", "isnull", "list", "median", "percentile_cont",
               "cume_dist", "percent_rank", "sign", "exp", "log", "rand", "locate", "charindex", "reverse",
               "repeat", "lcase", "ucase"],
    "hana": ["year", "month", "hour", "minute", "second", "quarter", "week", "now", "to_date", "to_timestamp",
             "to_varchar", "to_decimal", "ifnull", "string_agg", "median", "percentile_cont", "cume_dist",
             "percent_rank", "sign", "exp", "ln", "log", "rand", "locate", "initcap", "lcase", "ucase"],
    "pinot": ["year", "month", "day", "hour", "minute", "second", "quarter", "week", "dayofweek", "now", "ago",
              "datetimeconvert", "todatetime", "jsonextractscalar", "text_match", "distinctcount",
              "distinctcounthll", "percentile", "percentiletdigest", "sign", "exp", "ln", "reverse",
              "regexp_like", "regexp_replace"],
}
_CALL_FORMS = {
    "now": "SELECT NOW() AS n FROM events",
    "getdate": "SELECT GETDATE() AS n FROM events",
    "rand": "SELECT RAND() AS n FROM events",
    "listagg": "SELECT LISTAGG(note, '-') WITHIN GROUP (ORDER BY id) AS n FROM events",
    "percentile_cont": "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY id) AS n FROM events",
    "cume_dist": "SELECT CUME_DIST() OVER (ORDER BY id) AS n FROM events",
    "percent_rank": "SELECT PERCENT_RANK() OVER (ORDER BY id) AS n FROM events",
    "ago": "SELECT COUNT(*) AS n FROM events WHERE created > ago('PT1H')",
    "text_match": "SELECT COUNT(*) AS n FROM events WHERE TEXT_MATCH(note, 'a')",
    "jsonextractscalar": "SELECT JSONEXTRACTSCALAR(note, 'a', 'STRING') AS n FROM events",
    "datetimeconvert": "SELECT DATETIMECONVERT(created, 'a', 'b', 'c') AS n FROM events",
    "charindex": "SELECT CHARINDEX('a', note) AS n FROM events",
    "datepart": "SELECT DATEPART(day, created) AS n FROM events",
    "decode": "SELECT DECODE(note, 'a', 1, 0) AS n FROM events",
    "instr": "SELECT INSTR(note, 'a') AS n FROM events",
    "locate": "SELECT LOCATE('a', note) AS n FROM events",
    "regexp_like": "SELECT COUNT(*) AS n FROM events WHERE REGEXP_LIKE(note, 'a')",
    "regexp_replace": "SELECT REGEXP_REPLACE(note, 'a', 'b') AS n FROM events",
    "repeat": "SELECT REPEAT(note, 2) AS n FROM events",
    "to_char": "SELECT TO_CHAR(created, 'YYYY') AS n FROM events",
}


@pytest.mark.parametrize("engine, name", [(e, name) for e, names in MORE_CALLS.items() for name in names])
def test_each_more_plain_function_runs_while_a_table_is_disabled(engine, name):
    client, sent = _client(engine)
    sql = _CALL_FORMS.get(name, f"SELECT {name.upper()}(note) AS n FROM events")
    df, _, _ = _run(client, sql)
    assert df["n"].iloc[0] == 3
    assert sent == [sql]


@pytest.mark.parametrize("engine, name", [
    ("hana", "decode"), ("hana", "day"), ("hana", "to_char"), ("pinot", "nvl"), ("pinot", "to_date"),
    ("pinot", "rollup"), ("pinot", "cube"), ("sybase", "nvl"), ("sybase", "to_date"), ("sybase", "week"),
    ("vertica", "days"), ("vertica", "int"), ("vertica", "date_part"), ("vertica", "rand"),
    ("db2", "datepart"), ("ibmi", "getdate"),
    *[(e, name) for e in ENGINES for name in ("len", "approx_count_distinct", "sets")],
])
def test_a_function_an_engine_is_not_known_to_have_stays_refused(engine, name):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError, match=name.upper()):
        _run(client, f"SELECT {name}(id) AS n FROM events")
    assert sent == []


# More syntax that names no table.
GROUPING = [
    "SELECT COUNT(*) AS n FROM events GROUP BY ROLLUP(note, id)",
    "SELECT COUNT(*) AS n FROM events GROUP BY CUBE(note, id)",
    "SELECT COUNT(*) AS n FROM events GROUP BY GROUPING SETS ((note), (id), ())",
]
COLUMN_LISTS = [
    "WITH x(a) AS (SELECT id FROM events) SELECT COUNT(*) AS n FROM x",
    "WITH x(a, b) AS (SELECT id, note FROM events), y(c) AS (SELECT a FROM x) SELECT COUNT(*) AS n FROM y",
    "SELECT COUNT(*) AS n FROM (SELECT id FROM events) AS t(a)",
    "SELECT CAST(id AS NUMBER(10, 2)) AS n FROM events",
    "SELECT CAST(id AS DECFLOAT(16)) AS n FROM events",
]
PARENTHESES = [
    "SELECT COUNT(*) AS n FROM ((SELECT id FROM events)) t",
    "SELECT COUNT(*) AS n FROM (events a JOIN events b ON a.id = b.id)",
    "SELECT COUNT(*) AS n FROM (events a JOIN events b ON a.id = b.id) JOIN events c ON c.id = a.id",
    "SELECT COUNT(*) AS n FROM (events)",
]
DB2_SYNTAX = [
    "SELECT COUNT(*) AS n FROM events WITH UR",
    "SELECT COUNT(*) AS n FROM events FOR READ ONLY",
    "SELECT COUNT(*) AS n FROM events FOR FETCH ONLY",
    "SELECT COUNT(*) AS n FROM events FETCH FIRST 5 ROWS ONLY FOR READ ONLY WITH UR",
    "SELECT CURRENT DATE AS n FROM events",
    "SELECT COUNT(*) AS n FROM events WHERE created > CURRENT TIMESTAMP - 7 DAYS",
    "SELECT COUNT(*) AS n FROM events WHERE created > CURRENT DATE - 1 MONTH + 2 HOURS",
    "WITH r(k) AS (SELECT id FROM events UNION ALL SELECT k + 1 FROM r WHERE k < 5) SELECT COUNT(*) AS n FROM r",
    "SELECT COUNT(*) AS n FROM events WHERE note IS DISTINCT FROM 'a'",
    "SELECT COUNT(*) AS n FROM events WHERE note IS NOT DISTINCT FROM created",
]
MORE_SYNTAX = (
    [(e, s) for e in ENGINES if e != "pinot" for s in GROUPING]
    + [(e, s) for e in ENGINES for s in COLUMN_LISTS + PARENTHESES]
    + [(e, s) for e in ("db2", "ibmi") for s in DB2_SYNTAX]
    + [("pinot", s) for s in DB2_SYNTAX[-2:]]
)


@pytest.mark.parametrize("engine, sql", MORE_SYNTAX)
def test_more_syntax_that_names_no_table_runs_while_a_table_is_disabled(engine, sql):
    client, sent = _client(engine)
    df, _, _ = _run(client, sql)
    assert df["n"].iloc[0] == 3
    assert sent == [sql]


@pytest.mark.parametrize("engine, sql", [
    *[(e, s) for e in ENGINES for s in (
        "WITH x(a) AS (SELECT id FROM secret) SELECT COUNT(*) AS n FROM x",
        "WITH x(a) AS (SELECT id FROM ledger) SELECT COUNT(*) AS n FROM x",
        "SELECT COUNT(*) AS n FROM (SELECT id FROM ledger) AS t(a)",
        "SELECT COUNT(*) AS n FROM ((SELECT id FROM ledger)) t",
        "SELECT COUNT(*) AS n FROM (events a JOIN ledger b ON a.id = b.id)",
        "SELECT COUNT(*) AS n FROM (events a JOIN events b ON a.id = b.id), ledger",
        "SELECT COUNT(*) AS n FROM (ledger)",
        "SELECT COUNT(*) AS n FROM (events a, ledger b)",
        "SELECT COUNT(*) AS n FROM (fn_rows(1))",
        "SELECT my_udf(a) AS n FROM events",
        "SELECT 1 AS n, my_udf(a) AS (b) FROM events",
        "SELECT id AS my_udf(a) FROM ledger",
    )],
    *[(e, s) for e in ("db2", "ibmi") for s in (
        "SELECT COUNT(*) AS n FROM ledger WITH UR",
        "SELECT COUNT(*) AS n FROM events FOR READ ONLY, ledger",
        "SELECT COUNT(*) AS n FROM events WITH UR, ledger",
        "SELECT COUNT(*) AS n FROM events WHERE created > CURRENT DATE - 7 DAYS, ledger",
        "WITH r(k) AS (SELECT id FROM r UNION ALL SELECT id FROM events) SELECT COUNT(*) AS n FROM r",
        "WITH r(k) AS (SELECT id FROM events UNION ALL SELECT id FROM ledger) SELECT COUNT(*) AS n FROM r",
        "SELECT is DISTINCT FROM ledger",
        "SELECT 1 AS n, note IS DISTINCT FROM ledger l JOIN events ON 1 = 1",
        "SELECT 1 AS n, 1 IS DISTINCT FROM ledger",
        "SELECT ALL is DISTINCT FROM ledger",
        "SELECT note, is NOT DISTINCT FROM ledger",
    )],
    *[(e, "SELECT COUNT(*) AS n FROM events WHERE note IS DISTINCT FROM 'a'") for e in ("vertica", "sybase", "hana")],
    ("hana", "WITH r(k) AS (SELECT id FROM events UNION ALL SELECT k + 1 FROM r WHERE k < 5) SELECT COUNT(*) AS n "
             "FROM r"),
    ("hana", "SELECT COUNT(*) AS n FROM events WITH UR"),
])
def test_more_syntax_gives_no_table_a_pass(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError):
        _run(client, sql)
    assert sent == []


# One-row tables of the engine: only by their exact names.
@pytest.mark.parametrize("engine, sql", [
    ("hana", "SELECT 3 AS n FROM DUMMY"),
    ("hana", "SELECT COUNT(*) AS n FROM events, dummy"),
    ("db2", "SELECT 3 AS n FROM SYSIBM.SYSDUMMY1"),
    ("ibmi", "SELECT CURRENT DATE AS n FROM sysibm.sysdummy1"),
])
def test_the_one_row_table_of_the_engine_is_a_source(engine, sql):
    client, sent = _client(engine)
    df, _, _ = _run(client, sql)
    assert df["n"].iloc[0] == 3
    assert sent == [sql]


@pytest.mark.parametrize("engine, sql", [
    ("hana", "SELECT 3 AS n FROM app.dummy"),
    ("hana", "SELECT 3 AS n FROM sys.dummy"),
    ("hana", "SELECT 3 AS n FROM other.sys.dummy"),
    ("hana", "SELECT 3 AS n FROM sysibm.sysdummy1"),
    ("db2", "SELECT 3 AS n FROM sysdummy1"),
    ("db2", "SELECT 3 AS n FROM app.sysdummy1"),
    ("db2", "SELECT 3 AS n FROM other.sysibm.sysdummy1"),
    ("db2", "SELECT 3 AS n FROM sysibm.systables"),
    ("db2", "SELECT 3 AS n FROM dummy"),
    *[(e, "SELECT 3 AS n FROM dummy") for e in ("vertica", "sybase", "pinot")],
    *[(e, "SELECT 3 AS n FROM sysibm.sysdummy1") for e in ("vertica", "sybase", "pinot")],
])
def test_only_the_exact_name_of_the_one_row_table_is_a_source(engine, sql):
    client, sent = _client(engine)
    with pytest.raises(TableAccessError):
        _run(client, sql)
    assert sent == []


@pytest.mark.parametrize("engine, table, sql", [
    ("hana", "APP.DUMMY", "SELECT 3 AS n FROM dummy"),
    ("db2", "SYSIBM.SYSDUMMY1", "SELECT 3 AS n FROM sysibm.sysdummy1"),
])
def test_a_one_row_table_name_the_selection_disables_stays_refused(engine, table, sql):
    client, sent = _client(engine)
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(active=["APP.EVENTS"], inactive=[table]))
    with pytest.raises(TableAccessError):
        _run(client, sql)
    assert sent == []
