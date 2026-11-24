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
