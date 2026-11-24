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
