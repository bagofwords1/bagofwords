import pytest
import sqlglot
from sqlglot import exp

from app.data_sources.clients.athena_unified_catalog import (
    TENANT_CASTS_KEY,
    TENANT_MISSING_KEY,
    TENANTS_KEY,
)
from app.data_sources.clients.athena_union_rewrite import (
    MAX_ATHENA_QUERY_BYTES,
    UnifiedQueryTooLarge,
    UnifiedTable,
    UnknownUnifiedTable,
    UnsupportedColumnType,
    rewrite_unified,
)
from app.data_sources.clients.postgresql_multi_catalog import COLUMN_TYPES_KEY

CATALOG = "AwsDataCatalog"


def _table(name, columns, tenants, casts=None, missing=None):
    return UnifiedTable(
        name=name,
        columns=tuple(columns),
        tenants=tuple(tenants),
        casts=casts or {},
        missing={t: frozenset(c) for t, c in (missing or {}).items()},
    )


ASSETS = _table(
    "assets",
    [("id", "bigint"), ("hostname", "varchar"), ("risk", "double"), ("owner", "varchar")],
    ["acme_db", "globex_db", "initech_db"],
    casts={"globex_db": {"id": "int"}},
    missing={"initech_db": ["owner"]},
)
FINDINGS = _table(
    "findings",
    [("asset_id", "bigint"), ("severity", "varchar")],
    ["acme_db", "globex_db"],
)
TABLES = {"assets": ASSETS, "findings": FINDINGS}


def _parse(sql):
    return sqlglot.parse_one(sql, read="athena")


def _unions(tree, alias):
    found = [
        s for s in tree.find_all(exp.Subquery)
        if s.alias_or_name.lower() == alias.lower() and isinstance(s.this, (exp.Union, exp.Select))
    ]
    assert found, f"no rewritten subquery aliased {alias}"
    return found


def _branches(subquery):
    node = subquery.this
    if isinstance(node, exp.Select):
        return [node]
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, exp.Union):
            assert not n.args.get("distinct"), "branches must be joined by UNION ALL"
            stack.extend([n.expression, n.this])
        else:
            out.append(n)
    return out


def _tenant_of(branch):
    first = branch.expressions[0]
    assert isinstance(first, exp.Alias) and first.alias == "tenant_db"
    assert isinstance(first.this, exp.Literal) and first.this.is_string
    return first.this.this


def _source(branch):
    t = branch.args["from"].this
    return (t.catalog, t.db, t.name)


def _projections(branch):
    return {p.alias_or_name: p for p in branch.expressions[1:]}


def _tenants(subquery):
    return [_tenant_of(b) for b in _branches(subquery)]


def test_reference_becomes_aliased_union_with_tenant_literals():
    out = rewrite_unified("SELECT hostname FROM assets", TABLES, CATALOG)
    tree = _parse(out.sql)
    (sub,) = _unions(tree, "assets")
    branches = _branches(sub)
    assert sorted(_tenant_of(b) for b in branches) == ["acme_db", "globex_db", "initech_db"]
    for b in branches:
        assert _source(b) == (CATALOG.lower(), _tenant_of(b), "assets")
    assert set(out.branches["assets"]) == {"acme_db", "globex_db", "initech_db"}
    assert all(t.db for t in tree.find_all(exp.Table))


def test_emitted_identifiers_are_quoted():
    out = rewrite_unified("SELECT hostname FROM assets", TABLES, CATALOG)
    (sub,) = _unions(_parse(out.sql), "assets")
    assert sub.args["alias"].this.quoted
    for b in _branches(sub):
        t = b.args["from"].this
        assert all(part.quoted for part in (t.this, t.args["db"], t.args["catalog"]))
        assert b.expressions[0].args["alias"].quoted


def test_agent_alias_is_kept():
    out = rewrite_unified("SELECT a.hostname FROM assets a WHERE a.risk > 5", TABLES, CATALOG)
    tree = _parse(out.sql)
    (sub,) = _unions(tree, "a")
    assert len(_branches(sub)) == 3
    assert not [s for s in tree.find_all(exp.Subquery) if s.alias_or_name == "assets"]


def test_only_referenced_columns_are_selected():
    out = rewrite_unified(
        "SELECT a.hostname FROM assets a WHERE risk > 5 ORDER BY a.hostname", TABLES, CATALOG
    )
    (sub,) = _unions(_parse(out.sql), "a")
    for b in _branches(sub):
        assert [p.alias_or_name for p in b.expressions] == ["tenant_db", "hostname", "risk"]


def test_tenant_db_is_projected_even_when_unused():
    out = rewrite_unified("SELECT COUNT(*) FROM assets", TABLES, CATALOG)
    (sub,) = _unions(_parse(out.sql), "assets")
    for b in _branches(sub):
        assert [p.alias_or_name for p in b.expressions] == ["tenant_db"]


@pytest.mark.parametrize("sql,alias", [
    ("SELECT * FROM assets", "assets"),
    ("SELECT a.*, f.severity FROM assets a JOIN findings f ON f.asset_id = a.id", "a"),
])
def test_star_and_qualified_star_select_every_column(sql, alias):
    out = rewrite_unified(sql, TABLES, CATALOG)
    (sub,) = _unions(_parse(out.sql), alias)
    for b in _branches(sub):
        assert [p.alias_or_name for p in b.expressions] == [
            "tenant_db", "id", "hostname", "risk", "owner"
        ]


def test_qualified_star_of_another_source_does_not_widen_this_one():
    out = rewrite_unified(
        "SELECT f.*, a.hostname FROM assets a JOIN findings f ON f.asset_id = a.id", TABLES, CATALOG
    )
    (sub,) = _unions(_parse(out.sql), "a")
    for b in _branches(sub):
        assert [p.alias_or_name for p in b.expressions] == ["tenant_db", "id", "hostname"]


def test_alias_column_list_keeps_the_unified_column_order():
    out = rewrite_unified(
        "SELECT t, h FROM assets AS x(t, i, h, r, o) WHERE t = 'acme_db'", TABLES, CATALOG
    )
    (sub,) = _unions(_parse(out.sql), "x")
    assert [c.name for c in sub.args["alias"].columns] == ["t", "i", "h", "r", "o"]
    assert sorted(_tenants(sub)) == ["acme_db", "globex_db", "initech_db"]
    for b in _branches(sub):
        assert [p.alias_or_name for p in b.expressions] == [
            "tenant_db", "id", "hostname", "risk", "owner"
        ]


def test_missing_column_is_typed_null():
    out = rewrite_unified("SELECT owner FROM assets", TABLES, CATALOG)
    (sub,) = _unions(_parse(out.sql), "assets")
    by_tenant = {_tenant_of(b): _projections(b) for b in _branches(sub)}
    owner = by_tenant["initech_db"]["owner"].this
    assert isinstance(owner, exp.Cast)
    assert isinstance(owner.this, exp.Null)
    assert owner.to.is_type("varchar")
    assert isinstance(by_tenant["acme_db"]["owner"], exp.Column)


def test_cast_only_where_recorded():
    out = rewrite_unified("SELECT id, risk FROM assets", TABLES, CATALOG)
    (sub,) = _unions(_parse(out.sql), "assets")
    by_tenant = {_tenant_of(b): _projections(b) for b in _branches(sub)}
    cast = by_tenant["globex_db"]["id"].this
    assert isinstance(cast, exp.Cast) and cast.to.is_type("bigint")
    assert isinstance(cast.this, exp.Column) and cast.this.name == "id"
    assert by_tenant["globex_db"]["id"].alias == "id"
    for tenant in ("acme_db", "initech_db"):
        assert not list(by_tenant[tenant]["id"].find_all(exp.Cast))
    assert not any(list(p["risk"].find_all(exp.Cast)) for p in by_tenant.values())


def test_bounded_varchar_tenant_is_cast_to_the_advertised_varchar():
    table = _table("users", [("email", "varchar")], ["a_db", "b_db"], casts={"b_db": {"email": "varchar(64)"}})
    out = rewrite_unified("SELECT email FROM users", {"users": table}, CATALOG)
    (sub,) = _unions(_parse(out.sql), "users")
    by_tenant = {_tenant_of(b): _projections(b) for b in _branches(sub)}
    cast = by_tenant["b_db"]["email"].this
    assert isinstance(cast, exp.Cast) and cast.to.is_type("varchar")
    assert not cast.to.expressions


@pytest.mark.parametrize("sql,alias,kept", [
    ("SELECT a.hostname FROM assets a WHERE a.tenant_db = 'globex_db'", "a", ["globex_db"]),
    ("SELECT a.hostname FROM assets a WHERE 'globex_db' = a.tenant_db AND a.risk > 1", "a", ["globex_db"]),
    ("SELECT hostname FROM assets WHERE tenant_db IN ('acme_db', 'initech_db')", "assets",
     ["acme_db", "initech_db"]),
    ("SELECT hostname FROM assets WHERE tenant_db = 'acme_db'", "assets", ["acme_db"]),
    ("SELECT a.hostname FROM assets a WHERE (a.tenant_db IN ('acme_db', 'globex_db') AND a.tenant_db = 'globex_db')",
     "a", ["globex_db"]),
    ("SELECT f.severity FROM findings f JOIN assets a ON a.id = f.asset_id AND a.tenant_db = 'acme_db'",
     "a", ["acme_db"]),
    ("SELECT f.severity FROM findings f LEFT JOIN assets a ON a.tenant_db IN ('initech_db') AND a.id = f.asset_id",
     "a", ["initech_db"]),
])
def test_tenant_literal_filter_prunes_branches(sql, alias, kept):
    out = rewrite_unified(sql, TABLES, CATALOG)
    (sub,) = _unions(_parse(out.sql), alias)
    assert sorted(_tenants(sub)) == kept
    assert sorted(out.branches["assets"]) == kept


@pytest.mark.parametrize("sql,alias", [
    ("SELECT a.hostname FROM assets a WHERE a.tenant_db = 'acme_db' OR a.risk > 1", "a"),
    ("SELECT a.hostname FROM assets a WHERE a.tenant_db = lower('ACME_DB')", "a"),
    ("SELECT a.hostname FROM assets a WHERE a.tenant_db LIKE 'acme%'", "a"),
    ("SELECT a.hostname FROM assets a WHERE NOT a.tenant_db IN ('acme_db')", "a"),
    ("SELECT a.hostname FROM assets a JOIN findings f ON f.asset_id = a.id WHERE tenant_db = 'acme_db'", "a"),
    ("SELECT a.hostname FROM findings f RIGHT JOIN assets a ON a.tenant_db = 'acme_db'", "a"),
    ("SELECT a.hostname FROM findings f FULL JOIN assets a ON a.tenant_db = 'acme_db'", "a"),
    ("SELECT a.hostname FROM assets a LEFT JOIN findings f ON a.tenant_db = 'acme_db'", "a"),
    ("SELECT a.hostname FROM assets a WHERE a.id IN (SELECT 1 WHERE a.tenant_db = 'acme_db')", "a"),
])
def test_or_and_non_literal_filters_keep_every_branch(sql, alias):
    out = rewrite_unified(sql, TABLES, CATALOG)
    (sub,) = _unions(_parse(out.sql), alias)
    assert sorted(_tenants(sub)) == ["acme_db", "globex_db", "initech_db"]


@pytest.mark.parametrize("sql", [
    "SELECT f.severity FROM findings f WHERE EXISTS "
    "(SELECT 1 FROM assets AS x(t, i, h, r, o) WHERE tenant_db = 'acme_db' AND i = f.asset_id)",
    "SELECT h FROM assets AS x(t, i, h, r, o) WHERE tenant_db IN ('globex_db')",
])
def test_column_list_alias_hides_tenant_db_from_pruning(sql):
    out = rewrite_unified(sql, TABLES, CATALOG)
    (sub,) = _unions(_parse(out.sql), "x")
    assert sorted(_tenants(sub)) == ["acme_db", "globex_db", "initech_db"]


def test_filter_that_matches_no_tenant_returns_typed_empty_relation():
    out = rewrite_unified(
        "SELECT a.hostname, a.risk FROM assets a WHERE a.tenant_db = 'nobody_db'", TABLES, CATALOG
    )
    tree = _parse(out.sql)
    (sub,) = _unions(tree, "a")
    (empty,) = _branches(sub)
    assert empty.args.get("from") is None
    assert empty.args.get("where") is not None
    cols = _projections(empty)
    tenant = empty.expressions[0]
    assert tenant.alias == "tenant_db" and isinstance(tenant.this, exp.Cast)
    assert isinstance(tenant.this.this, exp.Null) and tenant.this.to.is_type("varchar")
    assert cols["hostname"].this.to.is_type("varchar")
    assert cols["risk"].this.to.is_type("double")
    assert out.branches["assets"] == ()
    assert not [t for t in tree.find_all(exp.Table) if t.name == "assets"]


def test_self_join_rewrites_each_reference_with_its_own_alias():
    out = rewrite_unified(
        "SELECT a.hostname, b.owner FROM assets a JOIN assets b "
        "ON a.tenant_db = b.tenant_db AND a.id = b.id AND b.tenant_db = 'globex_db'",
        TABLES, CATALOG,
    )
    tree = _parse(out.sql)
    (a,) = _unions(tree, "a")
    (b,) = _unions(tree, "b")
    assert sorted(_tenants(a)) == ["acme_db", "globex_db", "initech_db"]
    assert _tenants(b) == ["globex_db"]
    assert [p.alias_or_name for p in _branches(a)[0].expressions] == ["tenant_db", "id", "hostname"]
    assert [p.alias_or_name for p in _branches(b)[0].expressions] == ["tenant_db", "id", "owner"]
    assert sorted(out.branches["assets"]) == ["acme_db", "globex_db", "initech_db"]


def test_cte_with_a_unified_name_shadows_the_table():
    out = rewrite_unified(
        "WITH assets AS (SELECT * FROM findings) SELECT * FROM assets", TABLES, CATALOG
    )
    tree = _parse(out.sql)
    final_from = tree.args["from"].this
    assert isinstance(final_from, exp.Table) and final_from.name == "assets" and not final_from.db
    (sub,) = _unions(tree, "findings")
    assert len(_branches(sub)) == 2
    assert "assets" not in out.branches


def test_cte_body_reading_the_unified_table_is_rewritten():
    out = rewrite_unified(
        "WITH hot AS (SELECT id FROM assets WHERE risk > 9) SELECT COUNT(*) FROM hot", TABLES, CATALOG
    )
    (sub,) = _unions(_parse(out.sql), "assets")
    assert len(_branches(sub)) == 3


def test_table_names_resolve_case_insensitively():
    out = rewrite_unified("SELECT Assets.hostname FROM Assets", TABLES, CATALOG)
    tree = _parse(out.sql)
    (sub,) = _unions(tree, "Assets")
    for b in _branches(sub):
        assert _source(b)[2] == "assets"
        assert "hostname" in _projections(b)
    assert "assets" in out.branches


def test_left_out_tenants_get_no_branch():
    out = rewrite_unified(
        "SELECT hostname FROM assets", TABLES, CATALOG, left_out={"assets": frozenset({"globex_db"})}
    )
    (sub,) = _unions(_parse(out.sql), "assets")
    assert sorted(_tenants(sub)) == ["acme_db", "initech_db"]
    assert sorted(out.branches["assets"]) == ["acme_db", "initech_db"]


def test_tenant_name_with_a_quote_stays_a_literal():
    table = _table("t", [("x", "bigint")], ["o'brien_db"])
    out = rewrite_unified("SELECT x FROM t WHERE tenant_db = 'o''brien_db'", {"t": table}, CATALOG)
    (sub,) = _unions(_parse(out.sql), "t")
    (branch,) = _branches(sub)
    assert _tenant_of(branch) == "o'brien_db"
    assert _source(branch)[1] == "o'brien_db"


@pytest.mark.parametrize("sql", [
    "SELECT * FROM machines",
    "SELECT * FROM assets JOIN machines ON true",
    "SELECT * FROM assets WHERE id IN (SELECT id FROM machines)",
])
def test_unknown_bare_table_is_refused(sql):
    with pytest.raises(UnknownUnifiedTable):
        rewrite_unified(sql, TABLES, CATALOG)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM acme_db.assets",
    "SELECT * FROM awsdatacatalog.acme_db.assets",
    "SELECT * FROM assets a JOIN globex_db.findings f ON true",
])
def test_qualified_reference_is_refused(sql):
    with pytest.raises(UnknownUnifiedTable):
        rewrite_unified(sql, TABLES, CATALOG)


@pytest.mark.parametrize("sql", [
    "SELECT hostname FROM assets TABLESAMPLE BERNOULLI (10)",
    "SELECT hostname FROM assets FOR TIMESTAMP AS OF TIMESTAMP '2026-01-01 00:00:00'",
])
def test_table_modifiers_on_a_unified_table_are_refused_not_dropped(sql):
    with pytest.raises(ValueError):
        rewrite_unified(sql, TABLES, CATALOG)


def test_oversized_query_is_refused_before_sending():
    wide = _table(
        "device_configurations",
        [(f"security_configuration_setting_{i:03d}", "varchar") for i in range(400)],
        [f"tenant_{i:02d}_db" for i in range(40)],
    )
    with pytest.raises(UnifiedQueryTooLarge) as err:
        rewrite_unified(
            "SELECT * FROM device_configurations", {"device_configurations": wide}, CATALOG
        )
    message = str(err.value)
    assert "device_configurations" in message
    assert "40" in message


def test_query_under_the_limit_is_not_refused():
    wide = _table(
        "device_configurations",
        [(f"security_configuration_setting_{i:03d}", "varchar") for i in range(400)],
        [f"tenant_{i:02d}_db" for i in range(40)],
    )
    out = rewrite_unified(
        "SELECT security_configuration_setting_001 FROM device_configurations", {"device_configurations": wide}, CATALOG
    )
    assert len(out.sql.encode("utf-8")) <= MAX_ATHENA_QUERY_BYTES


def test_from_metadata_keeps_column_order_and_tenant_details():
    table = UnifiedTable.from_metadata("assets", {
        TENANTS_KEY: ["acme_db", "globex_db"],
        COLUMN_TYPES_KEY: {"zeta": "bigint", "alpha": "varchar", "mid": "double"},
        TENANT_CASTS_KEY: {"globex_db": {"zeta": "int"}},
        TENANT_MISSING_KEY: {"acme_db": ["mid"]},
    })
    assert table.name == "assets"
    assert table.columns == (("zeta", "bigint"), ("alpha", "varchar"), ("mid", "double"))
    assert table.tenants == ("acme_db", "globex_db")
    assert table.casts["globex_db"]["zeta"] == "int"
    assert table.missing["acme_db"] == frozenset({"mid"})


@pytest.mark.parametrize("metadata", [
    {},
    {TENANTS_KEY: [], COLUMN_TYPES_KEY: {"id": "bigint"}},
    {"schema": "public", COLUMN_TYPES_KEY: {"id": "bigint"}},
])
def test_from_metadata_ignores_tables_without_tenants(metadata):
    assert UnifiedTable.from_metadata("assets", metadata) is None


INJECTION_PROBE = "x) AS c, (SELECT secret FROM other_db.creds) AS d, CAST(NULL AS x"


@pytest.mark.parametrize("sql", ["SELECT c FROM weird", "SELECT * FROM weird WHERE tenant_db = 'nobody'"])
def test_unparseable_column_type_is_refused_never_emitted(sql):
    weird = _table("weird", [("c", INJECTION_PROBE)], ["acme_db", "globex_db"], missing={"globex_db": ["c"]})
    with pytest.raises(UnsupportedColumnType):
        rewrite_unified(sql, {"weird": weird}, CATALOG)
