"""Unified mode indexes one table per name across the matching Glue databases.

Only boto3's Glue client and awswrangler are faked (shared with the athena
client tests); unification itself runs for real.
"""

import pytest

from app.data_sources.clients import aws_athena_client as athena_module
from app.data_sources.clients.aws_athena_client import AwsAthenaClient
from tests.unit.test_aws_athena_client import FakeGlue, _glue_error, make_client, names, wrangler  # noqa: F401

DATABASES = {
    "acme_db": ["assets", "users"],
    "globex_db": ["assets", "alerts"],
}


def unified_client(database, glue, **kwargs):
    return make_client(database, glue, unify_tables=True, **kwargs)


def by_name(tables):
    return {t.name: t for t in tables}


def test_unified_index_has_one_table_per_name_with_every_tenant(wrangler):
    client = unified_client("*_db", FakeGlue(DATABASES))
    tables = by_name(client.get_tables())
    assert sorted(tables) == ["alerts", "assets", "users"]
    assert tables["assets"].metadata_json["tenants"] == ["acme_db", "globex_db"]
    assert tables["users"].metadata_json["tenants"] == ["acme_db"]
    assert "tenant_db" in [c.name for c in tables["assets"].columns]
    assert client.index_stats() == {}


def test_unreadable_database_is_left_out_and_reported(wrangler):
    glue = FakeGlue({**DATABASES, "broken_db": _glue_error("AccessDeniedException")})
    client = unified_client("*_db", glue)
    tables = by_name(client.get_tables())
    assert all("broken_db" not in t.metadata_json["tenants"] for t in tables.values())
    assert sorted(tables) == ["alerts", "assets", "users"]
    stats = client.index_stats()
    assert stats["unreadable_datasets"] == [{"name": "broken_db", "reason": "AccessDeniedException"}]
    assert client.unindexed_table_prefixes() == []


def test_type_conflict_leaves_a_tenant_out_of_one_table_and_is_reported(wrangler):
    glue = FakeGlue(
        {"a_db": ["orders"], "b_db": ["orders"], "c_db": ["orders"]},
        columns={
            ("a_db", "orders"): [{"Name": "total", "Type": "int"}],
            ("b_db", "orders"): [{"Name": "total", "Type": "int"}],
            ("c_db", "orders"): [{"Name": "total", "Type": "boolean"}],
        },
    )
    client = unified_client("*_db", glue)
    tables = by_name(client.get_tables())
    assert tables["orders"].metadata_json["tenants"] == ["a_db", "b_db"]
    assert client.index_stats()["unreadable_datasets"] == [
        {"name": "orders (c_db)", "reason": "column type conflict on total"}
    ]
    assert client.index_stats()["unreadable_dataset_count"] == 1


def test_table_without_a_matching_tenant_is_reported_by_table_name(wrangler):
    glue = FakeGlue(
        {"a_db": ["orders"], "b_db": ["orders"]},
        columns={
            ("a_db", "orders"): [{"Name": "tenant_db", "Type": "varchar"}],
            ("b_db", "orders"): [{"Name": "tenant_db", "Type": "varchar"}],
        },
    )
    client = unified_client("*_db", glue)
    assert client.get_tables() == []
    assert client.index_stats()["unreadable_datasets"] == [
        {"name": "orders", "reason": "has tenant_db column"}
    ]


def test_exclude_pattern_applies_before_unifying(wrangler):
    client = unified_client("*_db", FakeGlue(DATABASES), table_exclude_pattern="^assets$")
    assert names(client.get_tables()) == ["alerts", "users"]


def test_count_tables_counts_unified_names(wrangler):
    client = unified_client("*_db", FakeGlue(DATABASES))
    assert client.count_tables() == 3
    assert len(client.get_tables()) == 3


def test_unified_mode_prunes_nothing_by_prefix(wrangler):
    glue = FakeGlue({**DATABASES, "broken_db": _glue_error("AccessDeniedException")})
    client = unified_client("*_db", glue)
    client.get_tables()
    assert client.unindexed_table_prefixes() == []


def test_unified_mode_has_no_exact_table_scope_catalog(wrangler):
    assert unified_client("*_db", FakeGlue(DATABASES)).exact_table_scope_catalogs is None
    assert unified_client("acme_db", FakeGlue(DATABASES)).exact_table_scope_catalogs is None


def test_single_database_with_unify_still_gets_tenant_db(wrangler):
    client = unified_client("acme_db", FakeGlue(DATABASES))
    tables = by_name(client.get_tables())
    assert sorted(tables) == ["assets", "users"]
    assert all(t.metadata_json["tenants"] == ["acme_db"] for t in tables.values())
    assert all("tenant_db" in [c.name for c in t.columns] for t in tables.values())


@pytest.mark.parametrize("unify_tables", [False])
def test_per_database_mode_output_is_unchanged(wrangler, unify_tables):
    client = make_client("*_db", FakeGlue(DATABASES), unify_tables=unify_tables)
    tables = client.get_tables()
    assert names(tables) == ["acme_db.assets", "acme_db.users", "globex_db.alerts", "globex_db.assets"]
    assert all(t.metadata_json == {"database": t.name.split(".")[0]} for t in tables)
    assert client.unindexed_table_prefixes() == []


def indexed_client(glue, database="*_db"):
    client = unified_client(database, glue)
    client.attach_table_metadata(
        [{"name": t.name, "metadata_json": t.metadata_json} for t in client.get_tables()]
    )
    return client


def test_query_sends_the_union_to_athena_with_no_default_database(wrangler):
    import sqlglot

    client = indexed_client(FakeGlue(DATABASES))
    client.execute_query("SELECT id FROM assets")
    call = wrangler.calls[-1]
    assert call["database"] is None
    union = sqlglot.parse_one(call["sql"], read="athena").find(sqlglot.exp.Union)
    sources = sorted(t.db for t in union.find_all(sqlglot.exp.Table))
    assert sources == ["acme_db", "globex_db"]


def test_query_before_indexing_asks_to_index_first(wrangler):
    client = unified_client("*_db", FakeGlue(DATABASES))
    with pytest.raises(ValueError, match="Index this connection first"):
        client.execute_query("SELECT * FROM assets")
    assert wrangler.calls == []


@pytest.mark.parametrize("sql", [
    "SELECT * FROM acme_db.assets",
    "SELECT * FROM awsdatacatalog.acme_db.assets",
    "SELECT * FROM missing_table",
    "SELECT FROM FROM (",
    "SELECT * FROM assets TABLESAMPLE BERNOULLI (10)",
])
def test_unusable_queries_are_refused_before_athena(wrangler, sql):
    client = indexed_client(FakeGlue(DATABASES))
    with pytest.raises(ValueError):
        client.execute_query(sql)
    assert wrangler.calls == []


def test_qualified_table_is_refused_in_unified_mode(wrangler):
    client = indexed_client(FakeGlue(DATABASES))
    with pytest.raises(ValueError, match="names a tenant database directly"):
        client.execute_query("SELECT * FROM acme_db.assets")
    assert wrangler.calls == []


def test_single_database_unified_client_also_sends_no_default_database(wrangler):
    client = indexed_client(FakeGlue(DATABASES), database="acme_db")
    client.execute_query("SELECT * FROM users")
    assert wrangler.calls[-1]["database"] is None


def test_attach_replaces_the_learned_set_and_ignores_entries_without_tenants(wrangler):
    client = indexed_client(FakeGlue(DATABASES))
    client.attach_table_metadata([
        {"name": "users", "metadata_json": {"tenants": ["acme_db"], "column_types": {"id": "int"}}},
        {"name": "legacy", "metadata_json": {"database": "acme_db"}},
        {"name": "bare", "metadata_json": None},
    ])
    assert sorted(client._unified_tables) == ["users"]


def test_description_explains_tenant_db_and_joins_and_names_left_out_tenants(wrangler):
    glue = FakeGlue(
        {
            "a_db": ["orders"], "b_db": ["orders"], "c_db": ["orders"],
            "broken_db": _glue_error("AccessDeniedException"),
        },
        columns={
            ("a_db", "orders"): [{"Name": "total", "Type": "int"}],
            ("b_db", "orders"): [{"Name": "total", "Type": "int"}],
            ("c_db", "orders"): [{"Name": "total", "Type": "boolean"}],
        },
    )
    client = indexed_client(glue)
    client._query_left_out = {"orders": {"b_db": "table vanished"}}
    text = client.description
    assert "tenant_db" in text
    assert "a.tenant_db = b.tenant_db" in text
    assert "c_db" in text
    assert "broken_db" in text
    assert "b_db" in text.split("Tenants left out of single tables")[1]
    assert text.count("Tell the user results") == 2


def test_description_of_clean_unified_connection_names_nobody(wrangler):
    text = indexed_client(FakeGlue(DATABASES)).description
    assert "Not readable" not in text
    assert "left out" not in text


def test_per_database_mode_queries_are_unchanged(wrangler):
    client = make_client("*_db", FakeGlue(DATABASES))
    client.execute_query("SELECT * FROM acme_db.assets")
    assert wrangler.calls[-1]["sql"] == "SELECT * FROM acme_db.assets"
    assert wrangler.calls[-1]["database"] is None
    with pytest.raises(ValueError):
        client.execute_query("SELECT * FROM assets")


def test_agent_scope_allows_selected_unified_table_and_refuses_unselected(wrangler):
    from app.ai.code_execution.table_scope import TableScope, TableScopeViolation, check_table_scope

    client = indexed_client(FakeGlue(DATABASES))
    scope = TableScope.from_names(["assets"], ["alerts", "users"])
    check_table_scope("SELECT * FROM assets", "athena", scope, client.exact_table_scope_catalogs)
    with pytest.raises(TableScopeViolation):
        check_table_scope("SELECT * FROM alerts", "athena", scope, client.exact_table_scope_catalogs)


def stored_rows(client):
    return [{"name": t.name, "metadata_json": t.metadata_json} for t in client.get_tables()]


def test_fresh_client_names_unreadable_tenants_from_stored_metadata_alone(wrangler):
    glue = FakeGlue({**DATABASES, "broken_db": _glue_error("AccessDeniedException")})
    rows = stored_rows(unified_client("*_db", glue))
    fresh = unified_client("*_db", FakeGlue(DATABASES))
    fresh.attach_table_metadata(rows)
    text = fresh.description
    assert "broken_db" in text
    assert "Tell the user results exclude them" in text


def test_fresh_client_names_excluded_tenants_from_stored_metadata_alone(wrangler):
    glue = FakeGlue(
        {"a_db": ["orders"], "b_db": ["orders"], "c_db": ["orders"]},
        columns={
            ("a_db", "orders"): [{"Name": "total", "Type": "int"}],
            ("b_db", "orders"): [{"Name": "total", "Type": "int"}],
            ("c_db", "orders"): [{"Name": "total", "Type": "boolean"}],
        },
    )
    rows = stored_rows(unified_client("*_db", glue))
    fresh = unified_client("*_db", FakeGlue({}))
    fresh.attach_table_metadata(rows)
    assert "orders leaves out c_db" in fresh.description


def query_failed(text):
    from awswrangler.exceptions import QueryFailed

    return QueryFailed(text)


def branch_tenants(call):
    import sqlglot

    return sorted({t.db for t in sqlglot.parse_one(call["sql"], read="athena").find_all(sqlglot.exp.Table)})


OWNER_COLUMNS = {
    ("acme_db", "assets"): [{"Name": "id", "Type": "int"}, {"Name": "owner", "Type": "varchar"}],
    ("globex_db", "assets"): [{"Name": "id", "Type": "int"}, {"Name": "owner", "Type": "varchar"}],
}


def test_missing_tenant_table_is_dropped_and_the_query_retried(wrangler):
    client = indexed_client(FakeGlue(DATABASES))
    wrangler.outcomes = [
        query_failed("TABLE_NOT_FOUND: line 1:15: Table 'awsdatacatalog.globex_db.assets' does not exist")
    ]
    df = client.execute_query("SELECT id FROM assets")
    assert len(df) == 1
    assert len(wrangler.calls) == 2
    assert branch_tenants(wrangler.calls[0]) == ["acme_db", "globex_db"]
    assert branch_tenants(wrangler.calls[1]) == ["acme_db"]
    assert "assets leaves out globex_db" in client.description


def test_table_name_match_is_case_insensitive_and_ignores_tables_not_in_the_query(wrangler):
    client = indexed_client(FakeGlue(DATABASES))
    wrangler.outcomes = [
        query_failed("TABLE_NOT_FOUND: line 1:15: Table 'AwsDataCatalog.GLOBEX_DB.Assets' does not exist"),
        query_failed("TABLE_NOT_FOUND: line 1:15: Table 'awsdatacatalog.globex_db.alerts' does not exist"),
    ]
    with pytest.raises(Exception, match="Query execution failed"):
        client.execute_query("SELECT id FROM assets")
    assert len(wrangler.calls) == 2
    assert client._query_left_out == {"assets": {"globex_db": "table vanished"}}


@pytest.mark.parametrize("message", [
    "COLUMN_NOT_FOUND: line 1:8: Column 'owner' cannot be resolved",
    "COLUMN_NOT_FOUND: line 1:8: Column 'owner' cannot be resolved or requester is not authorized "
    "to access requested resources",
])
def test_missing_column_is_traced_to_the_tenant_through_glue(wrangler, message):
    glue = FakeGlue(DATABASES, columns=dict(OWNER_COLUMNS))
    client = indexed_client(glue)
    glue.columns[("globex_db", "assets")] = [{"Name": "id", "Type": "int"}]
    wrangler.outcomes = [query_failed(message)]
    client.execute_query("SELECT owner FROM assets")
    assert branch_tenants(wrangler.calls[1]) == ["acme_db"]
    assert client._query_left_out == {"assets": {"globex_db": "column missing"}}


def test_column_missing_everywhere_is_the_users_error_and_drops_nothing(wrangler):
    glue = FakeGlue(DATABASES, columns=dict(OWNER_COLUMNS))
    client = indexed_client(glue)
    glue.columns = {k: [{"Name": "id", "Type": "int"}] for k in OWNER_COLUMNS}
    wrangler.outcomes = [query_failed("COLUMN_NOT_FOUND: line 1:8: Column 'owner' cannot be resolved")]
    with pytest.raises(Exception, match="Query execution failed"):
        client.execute_query("SELECT owner FROM assets")
    assert len(wrangler.calls) == 1
    assert client._query_left_out == {}


def test_glue_failure_while_diagnosing_drops_nothing(wrangler):
    glue = FakeGlue({**DATABASES, "globex_db": _glue_error("AccessDeniedException")}, columns=dict(OWNER_COLUMNS))
    client = indexed_client(FakeGlue(DATABASES, columns=dict(OWNER_COLUMNS)))
    client.glue_client = glue
    wrangler.outcomes = [query_failed("COLUMN_NOT_FOUND: line 1:8: Column 'owner' cannot be resolved")]
    with pytest.raises(Exception, match="Query execution failed"):
        client.execute_query("SELECT owner FROM assets")
    assert client._query_left_out == {}


@pytest.mark.parametrize("message", [
    "Permission denied on S3 path: s3://lake/globex/assets/dt=1/part-0.parquet",
    "HIVE_CANNOT_OPEN_SPLIT: Error opening Hive split s3://lake/globex/assets/part-0.parquet: "
    "Access Denied (Service: Amazon S3; Status Code: 403)",
])
def test_s3_access_denied_is_traced_by_table_location(wrangler, message):
    glue = FakeGlue(
        DATABASES,
        locations={("acme_db", "assets"): "s3://lake/acme/assets", ("globex_db", "assets"): "s3://lake/globex/assets/"},
    )
    client = indexed_client(glue)
    wrangler.outcomes = [query_failed(message)]
    client.execute_query("SELECT id FROM assets")
    assert branch_tenants(wrangler.calls[1]) == ["acme_db"]
    assert client._query_left_out == {"assets": {"globex_db": "access denied"}}


def test_glue_access_denied_on_a_table_arn_drops_that_tenant(wrangler):
    client = indexed_client(FakeGlue(DATABASES))
    wrangler.outcomes = [query_failed(
        "AccessDeniedException: User is not authorized to perform glue:GetTable on resource: "
        "arn:aws:glue:eu-west-1:123456789012:table/globex_db/assets"
    )]
    client.execute_query("SELECT id FROM assets")
    assert branch_tenants(wrangler.calls[1]) == ["acme_db"]
    assert client._query_left_out == {"assets": {"globex_db": "access denied"}}


def test_unrecognized_error_is_raised_with_a_reindex_hint_and_drops_nothing(wrangler):
    client = indexed_client(FakeGlue(DATABASES))
    wrangler.outcomes = [query_failed("SYNTAX_ERROR: line 1:8: mismatched input")]
    with pytest.raises(Exception, match="Query execution failed: .*SYNTAX_ERROR.*re-index"):
        client.execute_query("SELECT id FROM assets")
    assert len(wrangler.calls) == 1
    assert client._query_left_out == {}


def test_retry_stops_at_the_drop_cap_and_names_every_dropped_tenant(wrangler):
    cap = athena_module.MAX_DROPPED_TENANTS_PER_QUERY
    tenants = [f"t{i:02d}_db" for i in range(cap + 2)]
    client = indexed_client(FakeGlue({t: ["assets"] for t in tenants}))
    wrangler.outcomes = [
        query_failed(f"TABLE_NOT_FOUND: line 1:15: Table 'awsdatacatalog.{t}.assets' does not exist")
        for t in tenants
    ]
    with pytest.raises(Exception) as raised:
        client.execute_query("SELECT id FROM assets")
    message = str(raised.value)
    assert len(wrangler.calls) == cap + 1
    for tenant in tenants[:cap]:
        assert tenant in message
    assert f"Table 'awsdatacatalog.{tenants[cap]}.assets' does not exist" in message


def test_dropped_tenants_stay_out_for_the_client_lifetime(wrangler):
    client = indexed_client(FakeGlue(DATABASES))
    wrangler.outcomes = [
        query_failed("TABLE_NOT_FOUND: line 1:15: Table 'awsdatacatalog.globex_db.assets' does not exist")
    ]
    client.execute_query("SELECT id FROM assets")
    client.execute_query("SELECT id FROM assets")
    assert len(wrangler.calls) == 3
    assert branch_tenants(wrangler.calls[2]) == ["acme_db"]


def test_per_database_mode_failure_is_raised_without_diagnosis(wrangler):
    client = make_client("*_db", FakeGlue(DATABASES))
    wrangler.outcomes = [query_failed("TABLE_NOT_FOUND: Table 'awsdatacatalog.globex_db.assets' does not exist")]
    with pytest.raises(Exception, match="Query execution failed") as raised:
        client.execute_query("SELECT * FROM globex_db.assets")
    assert "re-index" not in str(raised.value)
    assert len(wrangler.calls) == 1


def test_column_blame_ignores_tables_whose_branches_did_not_project_the_column(wrangler):
    glue = FakeGlue(
        {"acme_db": ["assets", "other"], "globex_db": ["assets", "other"]},
        columns={
            ("acme_db", "assets"): [{"Name": "id", "Type": "int"}, {"Name": "owner", "Type": "varchar"}],
            ("globex_db", "assets"): [{"Name": "id", "Type": "int"}, {"Name": "owner", "Type": "varchar"}],
            ("acme_db", "other"): [{"Name": "id", "Type": "int"}, {"Name": "owner", "Type": "varchar"}],
            ("globex_db", "other"): [{"Name": "id", "Type": "int"}, {"Name": "owner", "Type": "varchar"}],
        },
    )
    client = indexed_client(glue)
    glue.columns[("acme_db", "assets")] = [{"Name": "id", "Type": "int"}]
    glue.columns[("globex_db", "other")] = [{"Name": "id", "Type": "int"}]
    wrangler.outcomes = [query_failed("COLUMN_NOT_FOUND: line 1:8: Column 'owner' cannot be resolved")]
    client.execute_query(
        "SELECT o.owner FROM assets a JOIN other o ON a.tenant_db = o.tenant_db AND a.id = o.id"
    )
    assert client._query_left_out == {"other": {"globex_db": "column missing"}}


def test_overlapping_table_locations_blame_only_the_longest_prefix(wrangler):
    glue = FakeGlue(
        DATABASES,
        locations={("acme_db", "assets"): "s3://lake", ("globex_db", "assets"): "s3://lake/globex/assets"},
    )
    client = indexed_client(glue)
    wrangler.outcomes = [query_failed("Permission denied on S3 path: s3://lake/globex/assets/part-0.parquet")]
    client.execute_query("SELECT id FROM assets")
    assert client._query_left_out == {"assets": {"globex_db": "access denied"}}


def test_s3_path_matches_on_segment_boundaries(wrangler):
    glue = FakeGlue(
        DATABASES,
        locations={("acme_db", "assets"): "s3://lake/acme", ("globex_db", "assets"): "s3://lake/globex"},
    )
    client = indexed_client(glue)
    wrangler.outcomes = [query_failed("Permission denied on S3 path: s3://lake/globex2/assets/part-0.parquet")]
    with pytest.raises(Exception, match="re-index"):
        client.execute_query("SELECT id FROM assets")
    assert client._query_left_out == {}


def test_ambiguous_or_partially_unreadable_locations_drop_nothing(wrangler):
    same = FakeGlue(
        DATABASES,
        locations={("acme_db", "assets"): "s3://lake/shared", ("globex_db", "assets"): "s3://lake/shared/"},
    )
    client = indexed_client(same)
    wrangler.outcomes = [query_failed("Permission denied on S3 path: s3://lake/shared/part-0.parquet")]
    with pytest.raises(Exception, match="re-index"):
        client.execute_query("SELECT id FROM assets")
    assert client._query_left_out == {}

    partial = FakeGlue(
        {**DATABASES, "globex_db": _glue_error("AccessDeniedException")},
        locations={("acme_db", "assets"): "s3://lake"},
    )
    client = indexed_client(FakeGlue(DATABASES))
    client.glue_client = partial
    wrangler.outcomes = [query_failed("Permission denied on S3 path: s3://lake/globex/assets/part-0.parquet")]
    with pytest.raises(Exception, match="re-index"):
        client.execute_query("SELECT id FROM assets")
    assert client._query_left_out == {}


def test_connection_test_succeeds_before_any_unified_table_is_indexed(wrangler):
    client = unified_client("acme_db, globex_db", FakeGlue(DATABASES))
    assert client.test_connection()["success"] is True
    assert wrangler.calls[-1]["sql"] == "SELECT 1"


INJECTION_PROBE = "x) AS c, (SELECT secret FROM other_db.creds) AS d, CAST(NULL AS x"


def test_unsupported_glue_column_type_leaves_the_tenant_out_and_is_never_sent(wrangler):
    glue = FakeGlue(
        {"a_db": ["orders"], "b_db": ["orders"], "c_db": ["orders"]},
        columns={
            ("a_db", "orders"): [{"Name": "id", "Type": "int"}],
            ("b_db", "orders"): [{"Name": "id", "Type": "int"}, {"Name": "c", "Type": "varchar"}],
            ("c_db", "orders"): [{"Name": "id", "Type": "int"}, {"Name": "c", "Type": INJECTION_PROBE}],
        },
    )
    client = indexed_client(glue)
    assert {"name": "orders (c_db)", "reason": "unsupported column type on c"} in client.index_stats()[
        "unreadable_datasets"
    ]
    client.execute_query("SELECT c FROM orders")
    assert "secret" not in wrangler.calls[-1]["sql"]
    assert branch_tenants(wrangler.calls[-1]) == ["a_db", "b_db"]


def test_stored_metadata_with_an_unparseable_type_is_refused_before_athena(wrangler):
    client = unified_client("*_db", FakeGlue(DATABASES))
    client.attach_table_metadata([{
        "name": "orders",
        "metadata_json": {
            "tenants": ["acme_db", "globex_db"],
            "column_types": {"id": "int", "c": INJECTION_PROBE},
            "tenant_missing": {"globex_db": ["c"]},
        },
    }])
    with pytest.raises(ValueError):
        client.execute_query("SELECT c FROM orders")
    assert wrangler.calls == []


@pytest.mark.parametrize("sql", [
    "SHOW TABLES IN acme_db",
    "SHOW COLUMNS FROM acme_db.assets",
    "SHOW CREATE TABLE assets",
    "MSCK REPAIR TABLE assets",
    "EXPLAIN SELECT * FROM acme_db.assets",
    "UNLOAD (SELECT * FROM assets) TO 's3://bucket/out/' WITH (format = 'PARQUET')",
    "DESCRIBE assets",
    "SELECT id FROM assets; SELECT id FROM users",
    "INSERT INTO assets SELECT * FROM users",
    "CREATE TABLE copied AS SELECT * FROM assets",
    "DROP TABLE assets",
    "VALUES (1)",
])
def test_unified_mode_sends_only_a_single_query_statement(wrangler, sql):
    client = indexed_client(FakeGlue(DATABASES))
    with pytest.raises(ValueError) as raised:
        client.execute_query(sql)
    assert wrangler.calls == []
    assert "<database>.<table>" not in str(raised.value)


@pytest.mark.parametrize("sql", [
    "SELECT id FROM assets;",
    "WITH recent AS (SELECT id FROM assets) SELECT * FROM recent",
    "SELECT id FROM assets UNION ALL SELECT id FROM users",
    "(SELECT id FROM assets)",
])
def test_unified_mode_runs_any_single_query_shape(wrangler, sql):
    client = indexed_client(FakeGlue(DATABASES))
    client.execute_query(sql)
    assert len(wrangler.calls) == 1


def test_unreadable_sql_is_refused_with_unified_guidance(wrangler):
    client = indexed_client(FakeGlue(DATABASES))
    with pytest.raises(ValueError) as raised:
        client.execute_query("SELECT FROM FROM (")
    assert "<database>.<table>" not in str(raised.value)
    assert "unified" in str(raised.value)
    assert wrangler.calls == []


NARROWED = {
    "acme_db": ["assets", "users"],
    "globex_db": ["assets", "alerts", "orders"],
    "initech_db": ["orders"],
    "umbrella_db": ["orders"],
    "broken_db": _glue_error("AccessDeniedException"),
}
NARROWED_COLUMNS = {
    ("globex_db", "orders"): [{"Name": "total", "Type": "boolean"}],
    ("initech_db", "orders"): [{"Name": "total", "Type": "int"}],
    ("umbrella_db", "orders"): [{"Name": "total", "Type": "int"}],
}


@pytest.mark.parametrize("database", ["*_db, !globex_db, !broken_db", "ACME_DB, INITECH_DB, UMBRELLA_DB"])
def test_stored_tenants_outside_the_database_setting_are_not_queried_or_named(wrangler, database):
    rows = stored_rows(unified_client("*_db", FakeGlue(NARROWED, columns=NARROWED_COLUMNS)))
    client = unified_client(database, FakeGlue(NARROWED, columns=NARROWED_COLUMNS))
    client.attach_table_metadata(rows)

    client.execute_query("SELECT id FROM assets")
    assert branch_tenants(wrangler.calls[-1]) == ["acme_db"]
    client.execute_query("SELECT total FROM orders")
    assert branch_tenants(wrangler.calls[-1]) == ["initech_db", "umbrella_db"]
    calls = len(wrangler.calls)
    with pytest.raises(ValueError):
        client.execute_query("SELECT * FROM alerts")
    assert len(wrangler.calls) == calls
    assert "globex_db" not in client.description
    assert "broken_db" not in client.description


def test_single_database_unified_client_reads_only_its_database_from_stored_rows(wrangler):
    rows = stored_rows(unified_client("*_db", FakeGlue(NARROWED, columns=NARROWED_COLUMNS)))
    client = unified_client("globex_db", FakeGlue(NARROWED, columns=NARROWED_COLUMNS))
    client.attach_table_metadata(rows)
    client.execute_query("SELECT * FROM assets")
    assert branch_tenants(wrangler.calls[-1]) == ["globex_db"]
    with pytest.raises(ValueError):
        client.execute_query("SELECT * FROM users")
    assert "acme_db" not in client.description


THREE_TENANTS = {t: ["assets", "users"] for t in ("acme_db", "globex_db", "initech_db")}


@pytest.mark.parametrize("schema", ["globex_db", "GLOBEX_DB", "awsdatacatalog.globex_db"])
def test_missing_tenant_database_is_dropped_from_every_table_and_retried(wrangler, schema):
    client = indexed_client(FakeGlue(THREE_TENANTS))
    wrangler.outcomes = [query_failed(f"SCHEMA_NOT_FOUND: line 1:15: Schema '{schema}' does not exist")]
    client.execute_query("SELECT a.id FROM assets a JOIN users u ON a.tenant_db = u.tenant_db AND a.id = u.id")
    assert len(wrangler.calls) == 2
    assert branch_tenants(wrangler.calls[1]) == ["acme_db", "initech_db"]
    assert client._query_left_out == {
        "assets": {"globex_db": "database missing"},
        "users": {"globex_db": "database missing"},
    }


def test_one_missing_database_counts_once_against_the_drop_cap(wrangler):
    names_ = [f"t{i}" for i in range(athena_module.MAX_DROPPED_TENANTS_PER_QUERY + 1)]
    client = indexed_client(FakeGlue({tenant: list(names_) for tenant in ("acme_db", "globex_db")}))
    wrangler.outcomes = [query_failed("SCHEMA_NOT_FOUND: line 1:15: Schema 'globex_db' does not exist")]
    sql = " UNION ALL ".join(f"SELECT tenant_db, id FROM {n}" for n in names_)
    client.execute_query(sql)
    assert len(wrangler.calls) == 2
    assert branch_tenants(wrangler.calls[1]) == ["acme_db"]
