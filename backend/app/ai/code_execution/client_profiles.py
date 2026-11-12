"""Which data-source clients the read-only guard knows, identified by type.

One table, keyed by the defining class's `(__module__, __qualname__)`. A
client is looked up by walking its MRO, so a subclass inherits its parent's
entry and the most specific registered class wins (`PrestoClient` before
`TrinoClient`). Keys are strings, so this module imports no driver.

Never decide anything about a client from `type(client).__name__`: a subclass
with another name would slip past the check. The `family` string is for
display and for the identity-bind grammar choice only.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GuardProfile:
    """How the wrapper checks statements for one client family.

    kind: "sql" (parsed with `dialect`), "nonparsing_sql" (SQL without a
    parser dialect), "spl" (Splunk), "cypher" (Neo4j).
    extra_kwargs: keyword arguments the client may receive besides the
    statement; any other keyword is refused.
    accepts_dict: the statement may be a dict envelope (checked by the client
    at its dispatch point).
    """

    family: str
    kind: str
    dialect: str | None
    extra_kwargs: frozenset[str] = field(default_factory=frozenset)
    accepts_dict: bool = False


@dataclass(frozen=True)
class ClientEntry:
    family: str
    profile: GuardProfile | None
    renderer: str | None = None


_CLIENTS = "app.data_sources.clients"


def _sql(module: str, name: str, dialect: str, extra_kwargs: frozenset[str] = frozenset()):
    return (f"{_CLIENTS}.{module}", name), ClientEntry(
        family=name, profile=GuardProfile(family=name, kind="sql", dialect=dialect, extra_kwargs=extra_kwargs),
    )


def _entry(module: str, name: str, kind: str, **profile_kwargs):
    return (f"{_CLIENTS}.{module}", name), ClientEntry(
        family=name, profile=GuardProfile(family=name, kind=kind, dialect=None, **profile_kwargs),
    )


_CLIENT_TABLE: dict[tuple[str, str], ClientEntry] = dict([
    _sql("postgresql_client", "PostgresqlClient", "postgres"),
    _sql("aws_redshift_client", "AwsRedshiftClient", "redshift"),
    _sql("mysql_client", "MysqlClient", "mysql"),
    _sql("mariadb_client", "MariadbClient", "mysql"),
    _sql("mssql_client", "MSSQLClient", "tsql"),
    _sql("ms_fabric_client", "MsFabricClient", "fabric"),
    _sql("oracledb_client", "OracledbClient", "oracle"),
    _sql("sqlite_client", "SqliteClient", "sqlite"),
    _sql("snowflake_client", "SnowflakeClient", "snowflake"),
    _sql("bigquery_client", "BigqueryClient", "bigquery",
         frozenset({"maximum_bytes_billed", "use_query_cache"})),
    _sql("aws_athena_client", "AwsAthenaClient", "athena"),
    _sql("presto_client", "PrestoClient", "presto"),
    _sql("trino_client", "TrinoClient", "trino"),
    _sql("clickhouse_client", "ClickhouseClient", "clickhouse"),
    _sql("databricks_sql_client", "DatabricksSqlClient", "databricks"),
    _sql("spark_connect_client", "SparkConnectClient", "spark"),
    _sql("duckdb_client", "DuckDBClient", "duckdb"),
    _sql("teradata_client", "TeradataClient", "teradata"),
    _sql("druid_client", "DruidClient", "druid"),
    _entry("db2_client", "Db2Client", "nonparsing_sql"),
    _entry("ibmi_client", "IbmiClient", "nonparsing_sql"),
    _entry("splunk_client", "SplunkClient", "spl", accepts_dict=True),
    _entry("neo4j_client", "Neo4jClient", "cypher", extra_kwargs=frozenset({"params"})),
])


def client_entry(client) -> ClientEntry | None:
    """The entry of the most specific registered class in `client`'s MRO."""
    for cls in type(client).__mro__:
        entry = _CLIENT_TABLE.get((cls.__module__, cls.__qualname__))
        if entry is not None:
            return entry
    return None


def guard_profile(client) -> GuardProfile | None:
    entry = client_entry(client)
    return entry.profile if entry is not None else None


def family_name(client) -> str:
    """Display / identity-bind name: the registered family, else the class name."""
    entry = client_entry(client)
    return entry.family if entry is not None else type(client).__name__
