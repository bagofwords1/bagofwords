from app.ai.prompt_formatters import Table, TableColumn
from app.data_sources.clients.postgresql_multi_catalog import (
    TENANT_COLUMN,
    TypeRules,
    unify_catalogs,
)


def tbl(cols):
    return Table(
        name="s.t",
        columns=[TableColumn(name=n, dtype=d) for n, d in cols],
        pks=[],
        fks=[],
        metadata_json={"schema": "s"},
    )


def test_custom_rules_decide_conflicts_and_advertised_types():
    rules = TypeRules(
        family=lambda dtype: (dtype or "")[:1],
        advertised=lambda family, spellings: max(spellings),
        tenant_column_type="varchar",
    )
    catalog = unify_catalogs(
        {
            "a": [tbl([("x", "int"), ("y", "str")])],
            "b": [tbl([("x", "iota"), ("y", "sym")])],
            "c": [tbl([("x", "text")])],
        },
        rules,
    )
    unified = catalog.tables[0]
    columns = {c.name: c.dtype for c in unified.columns}
    assert unified.metadata_json["tenants"] == ["a", "b"]
    assert unified.metadata_json["excluded_tenants"] == ["c"]
    assert columns["x"] == "iota"
    assert columns[TENANT_COLUMN] == rules.tenant_column_type
