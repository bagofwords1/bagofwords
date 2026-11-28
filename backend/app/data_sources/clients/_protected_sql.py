"""Live row and column policy entry points for SQLAlchemy-backed sources."""

import pandas as pd
from app.data_sources.clients._pg_session import prepare_pg_session, verify_pg_session_after
from app.data_sources.policy_history import record_protected_execution

from app.data_sources.fabric_company_rls import protect_query


class ProtectedSql:
    _company_reader = None

    @property
    def company_filtered(self):
        return self._company_reader is not None

    def bind_company_reader(self, policy, employee, identity=None):
        if self.company_filtered:
            raise ValueError("Reader already bound")
        self._company_reader = policy, employee, identity

    def connect(self, *args, **kwargs):
        if self.company_filtered:
            raise ValueError("Raw connections are unavailable to protected readers")
        return self._policy_session(*args, **kwargs)

    def _read_session(self):
        if self.policy_dialect == "postgres":
            return self._policy_session(read_only=True)
        return self._policy_session()

    def _execute_protected(self, conn, statement, bindings):
        if self.policy_dialect != "postgres":
            return conn.exec_driver_sql(statement, bindings)
        prepare_pg_session(conn, statement)
        try:
            return conn.exec_driver_sql(statement, bindings)
        finally:
            verify_pg_session_after(conn)

    def has_company_grant(self, rule):
        from app.data_sources.fabric_company_rls import protect_query
        from app.data_sources.fast.rls import resolve_source

        policy, employee, identity = self._company_reader
        if rule.row_attribute:
            return bool(resolve_source(rule.row_attribute, identity))
        statement, bindings = protect_query(
            f"SELECT * FROM {rule.table}",
            policy,
            employee,
            identity=identity,
            dialect=getattr(self, "policy_dialect", "fabric"),
            grant_probe=True,
        )
        with self._read_session() as conn:
            return bool(self._execute_protected(conn, statement, bindings).scalar_one())

    @record_protected_execution
    def execute_company_query(self, sql, policy, employee, identity=None, *, preview_columns=None):
        if self.company_filtered and self._company_reader != (policy, employee, identity):
            raise ValueError("Reader mismatch")
        from app.data_sources.policy_history import record_policy

        record_policy(policy)
        try:
            text, bindings = protect_query(sql, policy, employee, identity=identity, dialect=self.policy_dialect, preview_columns=preview_columns)
            with self._read_session() as conn:
                record_policy(policy, text)
                result = self._execute_protected(conn, text, bindings)
                columns = [field[0] for field in result.cursor.description]
                return pd.DataFrame.from_records(result.fetchall(), columns=columns)
        except Exception:
            raise ValueError("Protected query unavailable") from None

    def validate_company_policy(self, policy):
        self._validated_policy_catalog(policy)

    def _validated_policy_catalog(self, policy):
        from sqlalchemy import inspect

        with self._read_session() as conn:
            catalog = inspect(conn)
            schemas = {}
            base_tables = {}
            for name in [r.table for r in policy.tables] + ([policy.grants_table] if policy.grants_table else []):
                schema, table = name.split(".")
                if self.policy_dialect == "oracle":
                    rows = conn.exec_driver_sql(
                        "SELECT c.column_name, c.data_type FROM all_tab_columns c "
                        "JOIN all_tables t ON t.owner=c.owner AND t.table_name=c.table_name "
                        "WHERE c.owner=:owner AND c.table_name=:name ORDER BY c.column_id",
                        {"owner": schema, "name": table},
                    )
                    schemas[name] = dict(rows.fetchall())
                    if not schemas[name]:
                        raise ValueError("A protected source must be a base table")
                    continue
                if schema not in base_tables:
                    base_tables[schema] = set(catalog.get_table_names(schema=schema))
                if table not in base_tables[schema]:
                    raise ValueError("A protected source must be a base table")
                schemas[name] = {c["name"]: c["type"] for c in catalog.get_columns(table, schema=schema)}
            for rule in policy.tables:
                fields = schemas[rule.table]
                if rule.company_column not in fields or set(rule.columns or {}).difference(fields):
                    raise ValueError("Invalid protected columns")
                if not rule.row_attribute:
                    grants = schemas[policy.grants_table]
                    if not all(
                        c in grants for c in (policy.employee_column, policy.source_column, policy.company_column) if c
                    ):
                        raise ValueError("Invalid entitlement columns")
                    if str(grants[policy.company_column]) != str(fields[rule.company_column]):
                        raise ValueError("Incompatible company keys")
            return schemas
