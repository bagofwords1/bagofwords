"""Table selection across Snowflake databases.

Contract: for a connection whose tables live in a default database (stored
SCHEMA.TABLE) and possibly other databases (stored DATABASE.SCHEMA.TABLE), a
two-part name means "in the default database", wherever it appears. So:

  * every enabled table can be read by its full or its short name, and enabled
    tables of different databases can be joined in one query;
  * a table in some other database never passes for an enabled one just
    because it shares the schema and table name;
  * an inactive table stays refused however it is spelled.

Connections with several databases enforce this. Single-database connections
(all existing ones) still allow the old lenient match for now, and log it, so
saved queries keep running while the log shows whether anyone relies on it.
"""

import logging

import pytest

from app.ai.code_execution import table_scope
from app.ai.code_execution.table_scope import TableScope, TableScopeViolation, check_table_scope


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def scope_log():
    """Warnings from the table-scope check, captured on its own logger (other
    tests may turn off propagation, which would blind `caplog`)."""
    handler = _Records()
    logger = table_scope.logger
    old_level, old_disabled = logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.disabled = False
    yield handler.records
    logger.removeHandler(handler)
    logger.setLevel(old_level)
    logger.disabled = old_disabled

MULTI = TableScope.from_names(
    active=["DB_A.SALES.ORDERS", "DB_B.CRM.CUSTOMERS"],
    inactive=["DB_A.SALES.SECRETS", "DB_B.SALES.ORDERS"],
    default_database="DB_A",
    enforce_databases=True,
)
# Today's stored names on a one-database connection.
SINGLE = TableScope.from_names(
    active=["SALES.ORDERS"],
    inactive=["SALES.SECRETS"],
    default_database="DB_A",
    enforce_databases=False,
)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM DB_A.SALES.ORDERS",
    "SELECT * FROM db_a.sales.orders",
    "SELECT * FROM SALES.ORDERS",
    "SELECT * FROM CUSTOMERS",
    "SELECT * FROM DB_B.CRM.CUSTOMERS",
    "SELECT c.NAME, SUM(o.ID) FROM DB_A.SALES.ORDERS o JOIN DB_B.CRM.CUSTOMERS c ON c.ID = o.ID GROUP BY 1",
    "SELECT * FROM SALES.ORDERS o JOIN DB_B.CRM.CUSTOMERS c ON c.ID = o.ID",
])
def test_enabled_tables_of_every_database_can_be_read_and_joined(sql):
    check_table_scope(sql, "snowflake", MULTI)


@pytest.mark.parametrize("sql", [
    # Same schema.table as an enabled table, other database: inactive here.
    "SELECT * FROM DB_B.SALES.ORDERS",
    # A database that is not part of the connection at all.
    "SELECT * FROM DB_X.SALES.ORDERS",
    "SELECT * FROM DB_X.CRM.CUSTOMERS",
    # CRM.CUSTOMERS is enabled in DB_B, not in the default database.
    "SELECT * FROM CRM.CUSTOMERS",
    # Unqualified, and a disabled table in another database has that name too:
    # ambiguous, so refused rather than guessed.
    "SELECT * FROM ORDERS",
    # Inactive, every spelling.
    "SELECT * FROM DB_A.SALES.SECRETS",
    "SELECT * FROM SALES.SECRETS",
    "SELECT * FROM DB_A.SALES.ORDERS o JOIN DB_A.SALES.SECRETS s ON s.ID = o.ID",
])
def test_other_databases_never_pass_for_an_enabled_table(sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "snowflake", MULTI)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM SALES.ORDERS",
    "SELECT * FROM DB_A.SALES.ORDERS",
    "SELECT * FROM ORDERS",
])
def test_single_database_connections_read_their_tables_by_any_name(sql, scope_log):
    check_table_scope(sql, "snowflake", SINGLE)
    assert not scope_log


@pytest.mark.parametrize("other", ["DB_X", "ANALYTICS"])
def test_single_database_connections_log_but_allow_the_old_cross_database_match(other, scope_log):
    check_table_scope(f"SELECT * FROM {other}.SALES.ORDERS", "snowflake", SINGLE)
    assert any(other in r.getMessage() for r in scope_log)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM SALES.SECRETS",
    "SELECT * FROM DB_A.SALES.SECRETS",
    "SELECT * FROM DB_X.SALES.SECRETS",
])
def test_single_database_connections_still_refuse_inactive_tables(sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "snowflake", SINGLE)


def test_without_a_default_database_matching_is_unchanged():
    scope = TableScope.from_names(active=["sales.orders"], inactive=[])
    # The pre-existing lenient match for connectors that name no database.
    check_table_scope("SELECT * FROM warehouse.sales.orders", "postgres", scope)
