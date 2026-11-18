from app.ai.prompt_formatters import ForeignKey, Table, TableColumn
from app.data_sources.clients.postgresql_multi_catalog import TENANT_COLUMN, unify_catalogs


def col(name, dtype="text"):
    return TableColumn(name=name, dtype=dtype)


def table(name, cols, pks=(), fks=(), description=None):
    schema = name.split(".")[0]
    return Table(
        name=name,
        description=description,
        columns=[col(n, t) for n, t in cols],
        pks=[col(n, t) for n, t in pks],
        fks=list(fks),
        metadata_json={"schema": schema},
    )


def by_name(catalog):
    return {t.name: t for t in catalog.tables}


def test_tables_are_unified_across_tenants_with_tenant_column_first():
    catalog = unify_catalogs({
        "a": [table("public.orders", [("id", "integer"), ("total", "numeric")])],
        "b": [table("public.orders", [("id", "integer"), ("total", "numeric")])],
    })
    assert len(catalog.tables) == 1
    orders = catalog.tables[0]
    assert orders.name == "public.orders"
    assert orders.metadata_json["schema"] == "public"
    assert orders.metadata_json["tenants"] == ["a", "b"]
    assert orders.columns[0].name == TENANT_COLUMN
    assert [c.name for c in orders.columns] == ["tenant_db", "id", "total"]
    assert catalog.excluded == []


def test_column_missing_in_one_tenant_keeps_that_tenant():
    catalog = unify_catalogs({
        "a": [table("public.orders", [("id", "integer"), ("note", "text")])],
        "b": [table("public.orders", [("id", "integer")])],
    })
    orders = catalog.tables[0]
    assert orders.metadata_json["tenants"] == ["a", "b"]
    assert "note" in [c.name for c in orders.columns]


def test_type_conflict_excludes_only_the_minority_tenant_from_that_table():
    catalog = unify_catalogs({
        "a": [table("public.orders", [("x", "integer")])],
        "b": [table("public.orders", [("x", "integer")])],
        "c": [
            table("public.orders", [("x", "date")]),
            table("public.users", [("id", "integer")]),
        ],
    })
    tables = by_name(catalog)
    assert tables["public.orders"].metadata_json["tenants"] == ["a", "b"]
    assert tables["public.orders"].metadata_json["excluded_tenants"] == ["c"]
    assert "c" in tables["public.users"].metadata_json["tenants"]
    assert catalog.excluded == [
        {"table": "public.orders", "tenant": "c", "column": "x", "type": "date", "expected_type": "integer"}
    ]
    x = next(c for c in tables["public.orders"].columns if c.name == "x")
    assert x.dtype == "integer"


def test_type_tie_is_deterministic():
    forward = unify_catalogs({
        "a": [table("public.t", [("x", "integer")])],
        "b": [table("public.t", [("x", "text")])],
    })
    backward = unify_catalogs({
        "b": [table("public.t", [("x", "text")])],
        "a": [table("public.t", [("x", "integer")])],
    })
    assert forward == backward
    t = forward.tables[0]
    assert t.metadata_json["tenants"] == ["a"]
    assert t.metadata_json["excluded_tenants"] == ["b"]
    assert next(c for c in t.columns if c.name == "x").dtype == "integer"


def test_table_with_tenant_db_column_is_reported_not_unified():
    catalog = unify_catalogs({
        "a": [table("public.bad", [("tenant_db", "text")]), table("public.ok", [("id", "integer")])],
        "b": [table("public.bad", [("id", "integer")])],
    })
    assert [t.name for t in catalog.tables] == ["public.ok"]
    assert catalog.excluded == [{"table": "public.bad", "reason": "has tenant_db column"}]


def test_excluded_tenant_contributes_nothing_to_the_table():
    users = table("public.users", [("id", "integer")], pks=[("id", "integer")])
    odd = table(
        "public.orders",
        [("x", "date"), ("only_odd", "text")],
        pks=[("only_odd", "text")],
    )
    odd.fks = [ForeignKey(column=col("only_odd"), references_name="public.users", references_column=col("id"))]
    catalog = unify_catalogs({
        "a": [table("public.orders", [("x", "integer")]), users],
        "b": [table("public.orders", [("x", "integer")])],
        "c": [odd],
    })
    orders = by_name(catalog)["public.orders"]
    assert "only_odd" not in [c.name for c in orders.columns]
    assert [p.name for p in orders.pks] == []
    assert orders.fks == []


def test_foreign_keys_become_tenant_scoped_pairs():
    orders = table("public.orders", [("id", "integer"), ("user_id", "integer")])
    orders.fks = [ForeignKey(column=col("user_id", "integer"), references_name="public.users", references_column=col("id", "integer"))]
    catalog = unify_catalogs({
        "a": [orders, table("public.users", [("id", "integer")])],
        "b": [orders.model_copy(deep=True), table("public.users", [("id", "integer")])],
    })
    fks = by_name(catalog)["public.orders"].fks
    edges = [(f.column.name, f.references_name, f.references_column.name) for f in fks]
    assert edges.count(("user_id", "public.users", "id")) == 1
    assert edges.count(("tenant_db", "public.users", "tenant_db")) == 1
    assert len(edges) == 2


def test_tenant_db_pair_appears_once_per_referenced_table():
    t = table("public.orders", [("id", "integer"), ("user_id", "integer"), ("alt_user_id", "integer")])
    t.fks = [
        ForeignKey(column=col("user_id"), references_name="public.users", references_column=col("id")),
        ForeignKey(column=col("alt_user_id"), references_name="public.users", references_column=col("id")),
    ]
    catalog = unify_catalogs({"a": [t]})
    edges = [(f.column.name, f.references_name) for f in catalog.tables[0].fks]
    assert edges.count(("tenant_db", "public.users")) == 1
    assert len(edges) == 3


def test_pks_union_and_first_description_in_sorted_tenant_order():
    catalog = unify_catalogs({
        "b": [table("public.t", [("id", "integer"), ("k", "text")], pks=[("k", "text")], description="from b")],
        "a": [table("public.t", [("id", "integer")], pks=[("id", "integer")], description=None)],
    })
    t = catalog.tables[0]
    assert sorted(p.name for p in t.pks) == ["id", "k"]
    assert t.description == "from b"


def test_table_where_every_tenant_conflicts_is_reported_not_emitted():
    catalog = unify_catalogs({
        "a": [table("public.t", [("x", "integer"), ("y", "text")])],
        "b": [table("public.t", [("x", "text"), ("y", "integer")])],
    })
    assert catalog.tables == []
    assert {"table": "public.t", "reason": "no tenant matches the resolved column types"} in catalog.excluded
    assert any(e.get("column") == "x" for e in catalog.excluded)


def test_missing_dtype_does_not_crash_type_resolution():
    catalog = unify_catalogs({
        "a": [table("public.t", [("x", None)])],
        "b": [table("public.t", [("x", "integer")])],
    })
    assert all(isinstance(e, dict) for e in catalog.excluded)
    assert catalog.tables[0].metadata_json["tenants"]


def test_names_that_differ_only_by_case_are_reported_not_unified():
    catalog = unify_catalogs({
        "a": [table("public.users", [("id", "integer")]), table("public.orders", [("id", "integer")])],
        "b": [table("public.Users", [("id", "integer")]), table("Public.orders", [("id", "integer")])],
        "c": [table("public.items", [("id", "integer")])],
    })
    assert [t.name for t in catalog.tables] == ["public.items"]
    collided = {e["table"] for e in catalog.excluded if "case" in e.get("reason", "")}
    assert collided == {"public.users", "public.Users", "public.orders", "Public.orders"}


def test_tenant_column_is_reserved_in_any_case():
    catalog = unify_catalogs({
        "a": [table("public.bad", [("Tenant_DB", "text")]), table("public.ok", [("id", "integer")])],
    })
    assert [t.name for t in catalog.tables] == ["public.ok"]
    assert [e["table"] for e in catalog.excluded] == ["public.bad"]
