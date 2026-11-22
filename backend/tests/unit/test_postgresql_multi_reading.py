"""The multi-database Postgres connector checks a query as DuckDB itself reads it.

Its engine has every tenant database attached under one login, so the query
check is the only thing between an agent and every tenant's tables. The shared
checks read SQL with sqlglot; where sqlglot and DuckDB disagree about a piece
of text, a query could look harmless to them and read something else in the
engine. `check_query` asks DuckDB's parser what the query reads and decides on
that, so the connector no longer depends on sqlglot reading every quoting form
the way DuckDB does.
"""
import pytest

from app.ai.code_execution.table_scope import TableScope, TableScopeViolation, table_refs
from app.data_sources.clients.postgresql_multi_guard import check_query

VIEWS = {
    "public.orders": ["tenant_a", "tenant_b"],
    "public.customers": ["tenant_a", "tenant_b"],
    "public.salaries": ["tenant_a"],
}
# An agent given orders and customers, not salaries.
SCOPE = TableScope.from_names(["public.orders", "public.customers"], ["public.salaries"])


@pytest.mark.parametrize("sql,used", [
    ("SELECT tenant_db, count(*) FROM public.orders GROUP BY 1", ["public.orders"]),
    ("SELECT count(*) FROM public.orders;", ["public.orders"]),
    ("SELECT count(*) FROM public.orders; -- orders per tenant", ["public.orders"]),
    ("WITH o AS (SELECT * FROM public.orders) SELECT * FROM o", ["public.orders"]),
    ("SELECT o.id, c.name FROM public.orders o JOIN memory.public.customers c "
     "ON o.tenant_db = c.tenant_db AND o.customer_id = c.id", ["public.orders", "public.customers"]),
    ("SELECT * FROM public.orders o, LATERAL json_tree(o.status) t", ["public.orders"]),
    ("SELECT * FROM public.orders o, LATERAL unnest([1, 2]) u", ["public.orders"]),
    ("SELECT * FROM public.orders PIVOT (sum(total) FOR status IN ('open', 'paid'))", ["public.orders"]),
    ("SELECT note.from_json('{\"a\": \"VARCHAR\"}') FROM public.orders", ["public.orders"]),
    ("SELECT E'it\\'s' AS quoted FROM public.orders", ["public.orders"]),
])
def test_queries_over_the_agents_tables_pass_and_name_the_views_they_read(sql, used):
    assert check_query(sql, VIEWS, SCOPE) == used


# Text that sqlglot reads as harmless while DuckDB reads a table the agent was
# not given. This is the gap the connector used to rely on #1741 to close.
ESCAPE_STRING_READ = (
    "SELECT E'\\'' AS a, (SELECT sum(amount) FROM public.salaries) AS b FROM public.orders --'"
)


def test_sqlglot_and_duckdb_disagree_on_the_escape_string_case():
    """Pins the disagreement itself, so the test below cannot pass vacuously
    if a sqlglot upgrade starts reading the text like DuckDB does."""
    assert ("public", "salaries") not in [tuple(r) for r in table_refs(ESCAPE_STRING_READ, "duckdb")]


def test_a_table_hidden_from_sqlglot_by_an_escape_string_is_refused():
    with pytest.raises(TableScopeViolation, match="public.salaries"):
        check_query(ESCAPE_STRING_READ, VIEWS, SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM public.salaries",
    "SELECT * FROM public.orders WHERE customer_id IN (SELECT 1 FROM public.salaries)",
    "SELECT * FROM memory.public.salaries",
    "SELECT * FROM public.not_indexed",
    "SELECT * FROM orders",
])
def test_tables_outside_the_agents_selection_are_refused(sql):
    with pytest.raises(TableScopeViolation):
        check_query(sql, VIEWS, SCOPE)


def test_without_a_selection_every_indexed_view_is_readable():
    assert check_query("SELECT * FROM public.salaries", VIEWS, None) == ["public.salaries"]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM __t0001.public.salaries",
    "SELECT * FROM __t0001.salaries",
    "SELECT __t0001.public.some_fn() FROM public.orders",
])
def test_a_tenant_database_cannot_be_named_directly(sql):
    with pytest.raises(TableScopeViolation, match="Tenant databases"):
        check_query(sql, VIEWS, SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT 1; CREATE OR REPLACE VIEW public.orders AS SELECT * FROM public.salaries",
    "CREATE OR REPLACE VIEW public.orders AS SELECT * FROM public.salaries",
    "SELECT 1; SELECT 2",
    "DESCRIBE public.orders",
    "SHOW TABLES",
    "SUMMARIZE public.orders",
    "SET memory_limit = '1TB'",
    "SELECT * FROM (PIVOT public.orders ON status USING sum(total))",
])
def test_anything_but_one_select_is_refused(sql):
    with pytest.raises(TableScopeViolation):
        check_query(sql, VIEWS, SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM postgres_query('__t0001', 'SELECT * FROM salaries')",
    "SELECT * FROM public.orders o, LATERAL query_table('public.salaries') p",
    "SELECT * FROM public.orders o LEFT JOIN LATERAL query('SELECT * FROM public.salaries') p ON true",
    "SELECT * FROM public.orders o, LATERAL main.histogram('public.salaries', amount) h",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM duckdb_tables()",
    "SELECT current_setting('memory_limit')",
])
def test_functions_that_read_by_name_the_catalog_or_settings_are_refused(sql):
    with pytest.raises(TableScopeViolation):
        check_query(sql, VIEWS, SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT CAST(total AS __t0001.public.money_t) FROM public.orders",
    "SELECT json_transform(note, '{\"a\": \"__t0001.public.t\"}') FROM public.orders",
    "SELECT note.from_json('{\"a\": \"__t0001.public.t\"}') FROM public.orders",
    "SELECT json_transform(note, note) FROM public.orders",
])
def test_types_from_another_catalog_are_refused(sql):
    with pytest.raises(TableScopeViolation):
        check_query(sql, VIEWS, SCOPE)
