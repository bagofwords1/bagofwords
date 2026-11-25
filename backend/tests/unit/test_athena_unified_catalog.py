import pytest

from app.ai.prompt_formatters import Table, TableColumn
from app.data_sources.clients.athena_unified_catalog import (
    TENANT_CASTS_KEY,
    TENANT_MISSING_KEY,
    unify_glue_catalogs,
)
from app.data_sources.clients.postgresql_multi_catalog import COLUMN_TYPES_KEY, TENANT_COLUMN


def table(name, cols):
    return Table(
        name=name,
        columns=[TableColumn(name=n, dtype=t) for n, t in cols],
        pks=[],
        fks=[],
        metadata_json={},
    )


def column_types(catalog, name="orders"):
    unified = next(t for t in catalog.tables if t.name == name)
    return {c.name: c.dtype for c in unified.columns}


@pytest.mark.parametrize(
    "dtypes, advertised",
    [
        (["tinyint", "int", "bigint"], "bigint"),
        (["smallint", "integer"], "integer"),
        (["float", "double"], "double"),
        (["real", "double"], "double"),
    ],
)
def test_integer_and_float_families_widen(dtypes, advertised):
    per_tenant = {f"t{i}": [table("orders", [("n", d)])] for i, d in enumerate(dtypes)}
    catalog = unify_glue_catalogs(per_tenant)
    assert column_types(catalog)["n"] == advertised
    assert catalog.excluded == []


def test_decimals_widen_to_cover_every_tenant():
    catalog = unify_glue_catalogs({
        "a": [table("orders", [("amt", "decimal(10,2)")])],
        "b": [table("orders", [("amt", "decimal(12,4)")])],
    })
    assert column_types(catalog)["amt"] == "decimal(12,4)"
    wider = unify_glue_catalogs({
        "a": [table("orders", [("amt", "decimal(10,2)")])],
        "b": [table("orders", [("amt", "decimal(8,6)")])],
    })
    assert column_types(wider)["amt"] == "decimal(14,6)"


def test_decimals_wider_than_38_digits_are_advertised_as_double():
    fits = unify_glue_catalogs({
        "a": [table("orders", [("amt", "decimal(38,0)")])],
        "b": [table("orders", [("amt", "decimal(38,10)")])],
    })
    assert column_types(fits)["amt"] == "double"
    wide = unify_glue_catalogs({
        "a": [table("orders", [("amt", "decimal(39,2)")])],
        "b": [table("orders", [("amt", "double")])],
    })
    assert column_types(wide)["amt"] == "double"


def test_string_and_varchar_are_one_type_without_casts():
    catalog = unify_glue_catalogs({
        "a": [table("orders", [("name", "string")])],
        "b": [table("orders", [("name", "varchar")])],
    })
    assert column_types(catalog)["name"] == "varchar"
    assert catalog.tables[0].metadata_json[TENANT_CASTS_KEY] == {}


def test_widening_records_a_cast_only_for_the_narrower_tenant():
    catalog = unify_glue_catalogs({
        "a": [table("orders", [("n", "int")])],
        "b": [table("orders", [("n", "bigint")])],
    })
    assert catalog.tables[0].metadata_json[TENANT_CASTS_KEY] == {"a": {"n": "int"}}


def test_missing_columns_are_recorded_per_tenant():
    catalog = unify_glue_catalogs({
        "a": [table("orders", [("id", "bigint"), ("note", "string")])],
        "b": [table("orders", [("id", "bigint")])],
    })
    metadata = catalog.tables[0].metadata_json
    assert metadata[TENANT_MISSING_KEY] == {"b": ["note"]}
    assert metadata["tenants"] == ["a", "b"]


def test_complex_types_unify_only_when_identical():
    catalog = unify_glue_catalogs({
        "a": [table("orders", [("tags", "array<string>")])],
        "b": [table("orders", [("tags", "array<string>")])],
        "c": [table("orders", [("tags", "array<int>")])],
    })
    metadata = catalog.tables[0].metadata_json
    assert metadata["tenants"] == ["a", "b"]
    assert metadata["excluded_tenants"] == ["c"]
    assert column_types(catalog)["tags"] == "array<string>"


def test_family_conflict_leaves_the_minority_tenant_out():
    catalog = unify_glue_catalogs({
        "a": [table("orders", [("v", "string")])],
        "b": [table("orders", [("v", "string")])],
        "c": [table("orders", [("v", "bigint")])],
    })
    metadata = catalog.tables[0].metadata_json
    assert metadata["excluded_tenants"] == ["c"]
    assert [e["tenant"] for e in catalog.excluded if "tenant" in e] == ["c"]


def test_tenant_db_column_is_first_and_varchar():
    catalog = unify_glue_catalogs({"a": [table("orders", [("id", "bigint")])]})
    first = catalog.tables[0].columns[0]
    assert (first.name, first.dtype) == (TENANT_COLUMN, "varchar")


def test_metadata_keeps_existing_keys_and_drops_schema():
    catalog = unify_glue_catalogs({"a": [table("orders", [("id", "bigint")])]})
    metadata = catalog.tables[0].metadata_json
    assert "schema" not in metadata
    assert metadata["tenants"] == ["a"]
    assert metadata["excluded_tenants"] == []
    assert metadata[COLUMN_TYPES_KEY] == {"id": "bigint"}
    assert metadata[TENANT_CASTS_KEY] == {}
    assert metadata[TENANT_MISSING_KEY] == {}


INJECTION_PROBE = "x) AS c, (SELECT secret FROM other_db.creds) AS d, CAST(NULL AS x"

UNSAFE_GLUE_TYPES = [
    "decimal((select secret from other_db.creds),2)",
    "int) AS c, (SELECT secret FROM other_db.creds) AS d, CAST(NULL AS int",
    "struct<user-agent:string>",
    "struct<select:string,z:int>",
    "struct<not:int>",
    "struct<a.b:int>",
    "array<struct<user-agent:string,id:int>>",
    "struct<id:int,name:string,date:date,user-agent:string,f5:int,f6:int,f7:int,f8:int,f9:int,f10:int>",
    "array",
    "map",
    "array<int>[1,2]",
    "decimal(1.5,2)",
]

SUPPORTED_GLUE_TYPES = {
    "int": "INTEGER",
    "bigint": "BIGINT",
    "double": "DOUBLE",
    "decimal(12,4)": "DECIMAL(12, 4)",
    "string": "VARCHAR",
    "varchar(10)": "VARCHAR(10)",
    "char(3)": "CHAR(3)",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "array<string>": "ARRAY(VARCHAR)",
    "map<string,int>": "MAP(VARCHAR, INTEGER)",
    "struct<a:int,b:string>": "ROW(a INTEGER, b VARCHAR)",
    "array<struct<a:int,b:string>>": "ARRAY(ROW(a INTEGER, b VARCHAR))",
    "struct<f1:int,f2:string,f3:bigint,f4:double,f5:date,f6:boolean,f7:int,f8:string,f9:timestamp,f10:int>": (
        "ROW(f1 INTEGER, f2 VARCHAR, f3 BIGINT, f4 DOUBLE, f5 DATE, f6 BOOLEAN, "
        "f7 INTEGER, f8 VARCHAR, f9 TIMESTAMP, f10 INTEGER)"
    ),
}


@pytest.mark.parametrize(
    "bad_type", [INJECTION_PROBE, "uniontype<int,string>", "struct<`a b`:int>", "no such type", *UNSAFE_GLUE_TYPES]
)
def test_tenant_with_an_unsupported_column_type_is_left_out_with_that_reason(bad_type):
    catalog = unify_glue_catalogs({
        "a": [table("orders", [("id", "bigint"), ("c", "varchar")])],
        "b": [table("orders", [("id", "bigint")])],
        "c": [table("orders", [("id", "bigint"), ("c", bad_type)])],
    })
    (unified,) = catalog.tables
    metadata = unified.metadata_json
    assert metadata["tenants"] == ["a", "b"]
    assert "c" in metadata["excluded_tenants"]
    assert [
        (e["tenant"], e["column"], e["reason"]) for e in catalog.excluded if e.get("tenant") == "c"
    ] == [("c", "c", "unsupported column type")]
    assert bad_type not in repr(metadata)
    assert bad_type not in [col.dtype for col in unified.columns]


@pytest.mark.parametrize("glue_type", SUPPORTED_GLUE_TYPES)
def test_tenant_with_a_plain_athena_type_is_kept(glue_type):
    catalog = unify_glue_catalogs({
        "a": [table("orders", [("id", "bigint"), ("c", glue_type)])],
        "b": [table("orders", [("id", "bigint")])],
    })
    (unified,) = catalog.tables
    assert unified.metadata_json["tenants"] == ["a", "b"]
    assert catalog.excluded == []
    assert "c" in column_types(catalog)


def test_table_whose_every_tenant_has_an_unsupported_type_is_not_unified():
    catalog = unify_glue_catalogs({
        "a": [table("orders", [("c", INJECTION_PROBE)]), table("users", [("id", "int")])],
        "b": [table("orders", [("c", INJECTION_PROBE)])],
    })
    assert [t.name for t in catalog.tables] == ["users"]
    assert sorted(e["tenant"] for e in catalog.excluded if e["table"] == "orders") == ["a", "b"]
