"""Legacy Presto entry.

Presto connections have always gone through the ``trino`` driver (the URI
scheme is ``trino://``), so this is the Trino client under its historical
name and defaults (plain HTTP on 8080, a single catalog/schema pair). Every
fix to TLS, roles, discovery or cancellation lands once, in `TrinoClient`.
"""

from app.data_sources.clients.trino_client import TrinoClient


class PrestoClient(TrinoClient):

    # Presto: '' only (sqlglot presto).
    sql_literal_style = "standard"

    relative_date_hint = TrinoClient.relative_date_hint.replace("(Trino)", "(Presto)")

    def __init__(self, host, port=8080, catalog=None, schema=None, user=None, password=None,
                 protocol="http", **kwargs):
        super().__init__(host, port=port, catalog=catalog, schema=schema, user=user,
                         password=password, protocol=protocol, **kwargs)

    @property
    def presto_uri(self):
        """Alias kept for the engine-pool dispose hook, which looks up the
        connection URI by attribute name per client family."""
        return self.trino_uri

    def test_connection(self):
        result = super().test_connection()
        if result.get("success"):
            result["message"] = "Successfully connected to Presto"
        return result

    @property
    def description(self):
        return super().description.replace("Trino database at", "Presto database at", 1)
