"""Neo4j graph database: read-only Cypher, returned as DataFrames.

Every query runs in a READ session (the server refuses writes) after
``cypher_guard.check_cypher`` (which also refuses what a READ session allows:
``LOAD CSV``, ``USE``, admin commands, and any procedure or namespaced
function that isn't on its allow-list).

The catalog has one entry per node label, ``(:Person)``, and one per
relationship type, ``[:ACTED_IN]``, whose description names the patterns it
connects (``(:Person)-[:ACTED_IN]->(:Movie)``).

Auth follows the driver's options: username / password (native or LDAP), an
SSO bearer token, a service user that impersonates another Neo4j user, Kerberos
(BOW's own identity, or per-user constrained delegation as for SQL Server), or
none.
"""
from __future__ import annotations

import base64
import math
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import pandas as pd

from app.ai.prompt_formatters import ServiceFormatter, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.cypher_guard import check_cypher
from app.data_sources.clients.progress import discovery_items, discovery_progress

ALLOWED_SCHEMES = ("bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc")

# Default cap on the rows one query returns (admins can change it per
# connection): the client runs in the API process, so `MATCH (n) RETURN n`
# must not load a whole graph into memory.
MAX_ROWS = 10_000
MAX_ROWS_LIMIT = 100_000
# Budget for queries that don't come through the code executor (schema
# discovery, the connection test) and the default before apply_query_timeout.
DEFAULT_TIMEOUT_SECONDS = 60.0
SAMPLE_SIZE = 100


_SECURITY_RETRY_CODES = ("Neo.ClientError.Security.Unauthorized", "Neo.ClientError.Security.TokenExpired")


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(n, low), high)


def kerberos_token(service: str, host: str, ccache: str | None) -> tuple[str, int | None]:
    """(base64 GSS-API initial token for ``service@host``, its lifetime in
    seconds), from the credential cache the ticket manager chose: BOW's own
    identity, or a user's delegated credential (MIT krb5 does S4U2Proxy)."""
    from app.data_sources.kerberos import _import_gssapi, get_ticket_manager

    gssapi = _import_gssapi()
    with get_ticket_manager().activate(ccache):
        creds = gssapi.Credentials(usage="initiate")
        target = gssapi.Name(f"{service}@{host}", gssapi.NameType.hostbased_service)
        token = gssapi.SecurityContext(name=target, creds=creds, usage="initiate").step()
        lifetime = getattr(creds, "lifetime", None)
    return base64.b64encode(token).decode("ascii"), lifetime


def _kerberos_auth_manager(make_token):
    """An AuthManager that mints a Kerberos token when it has none or the last
    is near expiry, and once more when the server rejects it."""
    from neo4j import kerberos_auth
    from neo4j.auth_management import AuthManager

    class KerberosAuthManager(AuthManager):
        def __init__(self):
            self._lock = threading.Lock()
            self._auth = None
            self._renew_at = 0.0

        def get_auth(self):
            with self._lock:
                if self._auth is None or time.monotonic() >= self._renew_at:
                    token, lifetime = make_token()
                    self._auth = kerberos_auth(token)
                    seconds = lifetime if isinstance(lifetime, (int, float)) and lifetime > 0 else 600
                    self._renew_at = time.monotonic() + max(30.0, min(float(seconds), 3600.0) - 60.0)
                return self._auth

        def handle_security_exception(self, auth, error) -> bool:
            with self._lock:
                if auth is self._auth:
                    self._auth = None
            return getattr(error, "code", None) in _SECURITY_RETRY_CODES

    return KerberosAuthManager()

_TYPE_NAMES = {
    "String": "string", "Long": "integer", "Integer": "integer", "Double": "number", "Float": "number",
    "Boolean": "boolean", "Date": "date", "DateTime": "datetime", "LocalDateTime": "datetime",
    "Time": "time", "LocalTime": "time", "Duration": "duration", "Point": "point",
}


def validate_uri(uri: str) -> str:
    """The connection URI, or ValueError: an allowed scheme, a host, and no
    user info (the credentials live in the encrypted credentials)."""
    parts = urlsplit(str(uri or "").strip())
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"The URI must start with one of: {', '.join(s + '://' for s in ALLOWED_SCHEMES)}")
    if "@" in parts.netloc:
        raise ValueError("Put the username and password in the credentials, not in the URI.")
    if not parts.hostname:
        raise ValueError("The URI must include a host, e.g. neo4j+s://xxxx.databases.neo4j.io")
    return f"{parts.scheme.lower()}://{parts.netloc}"


def direct_uri(uri: str) -> str:
    """The URI the driver connects to: always a direct bolt connection to the
    host the admin entered. A neo4j:// (routing) URI would make the driver
    connect to whatever servers the server's routing table names, which no
    egress check sees; BOW reads one database and needs no routing."""
    scheme, rest = uri.split("://", 1)
    return f"{scheme.replace('neo4j', 'bolt', 1)}://{rest}"


def _type_name(property_types: Any) -> str:
    """'String', ['String'], ['Long', 'Double'], ['StringArray'] → one simple dtype."""
    names = [str(t) for t in (property_types if isinstance(property_types, list) else [property_types]) if t]
    if any(t.endswith("Array") for t in names):
        return "array"
    simple = {_TYPE_NAMES.get(t, "object") for t in names}
    if len(simple) == 1:
        return simple.pop()
    return "number" if simple and simple <= {"integer", "number"} else "object"


def to_plain(value: Any) -> Any:
    """A Cypher value as plain JSON-able Python: nodes and relationships as
    their properties plus ``_labels`` / ``_type``, paths as their parts,
    temporal values as ISO strings, points as coordinates."""
    from neo4j.graph import Node, Path, Relationship
    from neo4j.spatial import Point

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, Node):
        return {"_labels": sorted(value.labels), **{k: to_plain(v) for k, v in value.items()}}
    if isinstance(value, Relationship):
        return {"_type": value.type, **{k: to_plain(v) for k, v in value.items()}}
    if isinstance(value, Path):
        return {"nodes": [to_plain(n) for n in value.nodes],
                "relationships": [to_plain(r) for r in value.relationships]}
    if isinstance(value, Point):
        out = {"x": value.x, "y": value.y, "srid": value.srid}
        if len(value) > 2:
            out["z"] = value.z
        return out
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if hasattr(value, "iso_format"):  # neo4j.time Date / DateTime / Time / Duration
        return value.iso_format()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return str(value)


class Neo4jClient(DataSourceClient):
    # The code executor passes `params=` through instead of rendering them
    # into the text as SQL: Cypher binds them natively as $name.
    native_query_params = True

    relative_date_hint = (
        "Cypher: date() is today, datetime() is now; date() - duration('P7D') is a week ago, "
        "date.truncate('month', date()) starts this month."
    )

    def __init__(self, uri: str, database: str = "neo4j", username: str | None = None,
                 password: str | None = None, token: str | None = None,
                 impersonated_user: str | None = None,
                 use_kerberos: bool = False, kerberos_principal: str | None = None,
                 kerberos_impersonate: str | None = None, kerberos_expected_sid: str | None = None,
                 kerberos_service: str = "neo4j",
                 max_rows: int = MAX_ROWS, schema_sample_size: int = SAMPLE_SIZE,
                 allow_gds: bool = False):
        super().__init__()
        self.uri = validate_uri(uri)
        self.database = (database or "neo4j").strip()
        self.username = username
        self.password = password
        self.token = (token or "").strip() or None
        self.impersonated_user = (impersonated_user or "").strip() or None
        # Kerberos: BOW's own identity (default cache or kerberos_principal),
        # or per-user constrained delegation (kerberos_impersonate, set only
        # by the server from a verified directory identity).
        self.use_kerberos = bool(use_kerberos)
        self.kerberos_principal = (kerberos_principal or "").strip() or None
        self.kerberos_impersonate = (kerberos_impersonate or "").strip() or None
        self.kerberos_expected_sid = kerberos_expected_sid
        self.kerberos_service = (kerberos_service or "neo4j").strip() or "neo4j"
        if self.kerberos_impersonate and not self.use_kerberos:
            raise ValueError("Kerberos delegation needs use_kerberos.")
        self.max_rows = _bounded_int(max_rows, MAX_ROWS, 1, MAX_ROWS_LIMIT)
        self.schema_sample_size = _bounded_int(schema_sample_size, SAMPLE_SIZE, 1, 10_000)
        self.allow_gds = bool(allow_gds)
        self._query_budget = DEFAULT_TIMEOUT_SECONDS
        self._driver = None
        self._driver_lock = threading.Lock()

    # ── connection ──────────────────────────────────────────────────────────

    @property
    def host(self) -> str:
        return urlsplit(self.uri).hostname or ""

    def apply_query_timeout(self, seconds: int) -> None:
        """Adopt the connection's query timeout a little under the executor's,
        so the server ends a slow transaction itself (cancelling a timed-out
        query is SQLAlchemy-only in the executor)."""
        if isinstance(seconds, (int, float)) and seconds > 0:
            self._query_budget = float(max(5.0, seconds - max(3.0, seconds * 0.1)))

    @property
    def auth_method(self) -> str:
        if self.use_kerberos:
            return "kerberos"
        if self.token:
            return "bearer"
        return "basic" if self.username else "none"

    def _kerberos_ccache(self) -> str | None:
        from app.data_sources.kerberos import get_ticket_manager

        manager = get_ticket_manager()
        if self.kerberos_impersonate:
            if not self.kerberos_expected_sid:
                raise RuntimeError("Delegation requires a verified directory SID")
            return manager.delegated_ccache(self.kerberos_impersonate)
        return manager.service_ccache(self.kerberos_principal)

    def _auth(self):
        """What the driver signs in with. Kerberos tokens are short-lived and
        bound to one identity, so they come from an AuthManager that mints
        them on demand; one driver (and pool) per client keeps identities apart."""
        if self.use_kerberos:
            return _kerberos_auth_manager(
                lambda: kerberos_token(self.kerberos_service, self.host, self._kerberos_ccache()))
        if self.token:
            from neo4j import bearer_auth

            return bearer_auth(self.token)
        return (self.username, self.password or "") if self.username else None

    def _get_driver(self):
        """One driver per client: it pools connections and is thread-safe, so
        queries skip the TCP + TLS + auth handshake after the first."""
        from neo4j import GraphDatabase

        with self._driver_lock:
            if self._driver is None:
                self._driver = GraphDatabase.driver(direct_uri(self.uri), auth=self._auth(),
                                                    connection_timeout=15, max_connection_lifetime=300)
            return self._driver

    def close(self) -> None:
        with self._driver_lock:
            if self._driver is not None:
                self._driver.close()
                self._driver = None

    @contextmanager
    def connect(self) -> Generator:
        # One session per call: sessions are not thread-safe, and the
        # executor may run several queries at once.
        # impersonated_user: Neo4j runs the session as that user and checks
        # that user's roles (the signed-in service user needs IMPERSONATE).
        with self._get_driver().session(database=self.database, default_access_mode="READ",
                                        fetch_size=1000,
                                        impersonated_user=self.impersonated_user) as session:
            yield session

    def _read(self, cypher: str, params: dict | None = None, *, limit: int | None = None,
              timeout: float | None = None):
        """(columns, rows, truncated) for a read transaction."""
        from neo4j import unit_of_work

        limit = self.max_rows if limit is None else limit

        @unit_of_work(timeout=timeout or self._query_budget)
        def work(tx):
            result = tx.run(cypher, params or {})
            keys = list(result.keys())
            rows: list[list] = []
            truncated = False
            for record in result:
                if len(rows) >= limit:
                    truncated = True
                    break
                rows.append([to_plain(v) for v in record.values()])
            return keys, rows, truncated

        with self.connect() as session:
            return session.execute_read(work)

    # ── queries ─────────────────────────────────────────────────────────────

    def execute_query(self, query: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
        """Run read-only Cypher; parameters bind as $name."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("execute_query takes a Cypher string, e.g. 'MATCH (n:Person) RETURN n.name LIMIT 10'.")
        if params is not None and not isinstance(params, dict):
            raise ValueError("params must be a dict of $name values.")
        check_cypher(query, allow_gds=self.allow_gds)
        keys, rows, truncated = self._read(query, params)
        df = pd.DataFrame(rows, columns=keys)
        if truncated:
            df.attrs["bow_notice"] = (
                f"Neo4j: only the first {self.max_rows:,} rows were returned. "
                "Aggregate in Cypher (count, sum, collect) or add a LIMIT."
            )
        return df

    # ── schema ──────────────────────────────────────────────────────────────

    def _label_columns(self, session_rows: list[dict], key: str) -> dict[str, list[TableColumn]]:
        out: dict[str, dict[str, TableColumn]] = {}
        for row in session_rows:
            names = row.get(key)
            names = names if isinstance(names, list) else [str(names or "").strip(":`")]
            prop = row.get("propertyName")
            for name in names:
                cols = out.setdefault(name, {})
                if prop and prop not in cols:
                    cols[prop] = TableColumn(name=prop, dtype=_type_name(row.get("propertyTypes")))
        return {k: list(v.values()) for k, v in out.items()}

    def _schema_from_procedures(self) -> list[Table]:
        timeout = DEFAULT_TIMEOUT_SECONDS
        _, node_rows, _ = self._read(
            "CALL db.schema.nodeTypeProperties() YIELD nodeLabels, propertyName, propertyTypes "
            "RETURN nodeLabels, propertyName, propertyTypes", timeout=timeout)
        _, rel_rows, _ = self._read(
            "CALL db.schema.relTypeProperties() YIELD relType, propertyName, propertyTypes "
            "RETURN relType, propertyName, propertyTypes", timeout=timeout)
        viz = self._schema_patterns(timeout)
        nodes = self._label_columns(
            [dict(zip(("nodeLabels", "propertyName", "propertyTypes"), r, strict=True)) for r in node_rows], "nodeLabels")
        rels = self._label_columns(
            [dict(zip(("relType", "propertyName", "propertyTypes"), r, strict=True)) for r in rel_rows], "relType")
        patterns: dict[str, list[str]] = {}
        for from_labels, rel, to_labels in viz:
            for a in from_labels or ["?"]:
                for b in to_labels or ["?"]:
                    p = f"(:{a})-[:{rel}]->(:{b})"
                    if p not in patterns.setdefault(rel, []):
                        patterns[rel].append(p)
        return self._tables(nodes, rels, patterns)

    def _schema_patterns(self, timeout: float) -> list[tuple]:
        """(from_labels, type, to_labels) for each relationship in
        db.schema.visualization(). Its relationships are virtual: their end
        nodes are matched to the returned nodes by id in Python, since
        startNode()/labels() on them differs between server versions."""
        from neo4j import unit_of_work

        @unit_of_work(timeout=timeout)
        def work(tx):
            out = []
            for record in tx.run("CALL db.schema.visualization() YIELD nodes, relationships RETURN nodes, relationships"):
                labels = {n.element_id: sorted(n.labels) for n in record["nodes"]}
                for r in record["relationships"]:
                    start = labels.get(r.start_node.element_id) or sorted(r.start_node.labels)
                    end = labels.get(r.end_node.element_id) or sorted(r.end_node.labels)
                    out.append((start, r.type, end))
            return out

        with self.connect() as session:
            return session.execute_read(work)

    def _schema_from_samples(self) -> list[Table]:
        """When the schema procedures are restricted: labels, types and the
        keys of a sample of each."""
        _, labels, _ = self._read("CALL db.labels() YIELD label RETURN label", timeout=DEFAULT_TIMEOUT_SECONDS)
        _, types, _ = self._read("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType",
                                 timeout=DEFAULT_TIMEOUT_SECONDS)
        nodes: dict[str, list[TableColumn]] = {}
        for (label,) in discovery_items(labels, "labels", label=lambda r: r[0]):
            q = f"MATCH (n:`{label.replace('`', '``')}`) WITH n LIMIT {self.schema_sample_size} UNWIND keys(n) AS k RETURN DISTINCT k"
            _, keys, _ = self._read(q, timeout=DEFAULT_TIMEOUT_SECONDS)
            nodes[label] = [TableColumn(name=k, dtype="object") for (k,) in keys]
        rels: dict[str, list[TableColumn]] = {}
        for (rel,) in discovery_items(types, "relationship types", label=lambda r: r[0]):
            q = f"MATCH ()-[r:`{rel.replace('`', '``')}`]->() WITH r LIMIT {self.schema_sample_size} UNWIND keys(r) AS k RETURN DISTINCT k"
            _, keys, _ = self._read(q, timeout=DEFAULT_TIMEOUT_SECONDS)
            rels[rel] = [TableColumn(name=k, dtype="object") for (k,) in keys]
        return self._tables(nodes, rels, {})

    @staticmethod
    def _tables(nodes: dict[str, list[TableColumn]], rels: dict[str, list[TableColumn]],
                patterns: dict[str, list[str]]) -> list[Table]:
        tables = [
            Table(name=f"(:{label})", description=f"Nodes labelled {label}.", columns=cols, pks=[], fks=[],
                  metadata_json={"type": "node_label", "label": label})
            for label, cols in sorted(nodes.items())
        ]
        for rel, cols in sorted(rels.items()):
            shown = patterns.get(rel) or [f"()-[:{rel}]->()"]
            tables.append(Table(
                name=f"[:{rel}]", description="Relationship. Connects: " + ", ".join(shown),
                columns=cols, pks=[], fks=[],
                metadata_json={"type": "relationship_type", "relationship_type": rel, "patterns": shown},
            ))
        return tables

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> list[Table]:
        from neo4j.exceptions import ClientError

        try:
            return self._schema_from_procedures()
        except ClientError:
            return self._schema_from_samples()

    def get_schema(self, table_name: str) -> Table:
        for table in self.get_schemas():
            if table.name == table_name:
                return table
        raise ValueError(f"No node label or relationship type named {table_name!r}")

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    def test_connection(self):
        from app.errors.redact import redact_error

        try:
            _, rows, _ = self._read("CALL dbms.components() YIELD name, versions, edition "
                                    "RETURN name, versions, edition", timeout=30)
            name, versions, edition = rows[0] if rows else ("Neo4j", [], "")
            version = ", ".join(versions or [])
            who = f", as {self.impersonated_user}" if self.impersonated_user else ""
            return {"success": True,
                    "message": f"Connected to {name} {version} ({edition}), database {self.database}{who}".strip()}
        except Exception as e:
            secrets = [s for s in (self.password, self.token) if s]
            return {"success": False, "message": redact_error(e, secrets=secrets or None)}

    @property
    def _gds_rule(self) -> str:
        if not self.allow_gds:
            return ""
        return """
   Graph Data Science is allowed in stream / stats mode. Project, run, then drop the projection in its
   own execute_query calls, with a unique graph name:
     CALL gds.graph.project('g_pr_1', 'Person', 'KNOWS')
     CALL gds.pageRank.stream('g_pr_1') YIELD nodeId, score
       RETURN gds.util.asNode(nodeId).name AS name, score ORDER BY score DESC LIMIT 20
     CALL gds.graph.drop('g_pr_1')
   CALL gds.list() YIELD name lists the algorithms installed. write / mutate modes are refused."""

    @property
    def description(self) -> str:
        return f"""
Neo4j graph database at {self.host}, database {self.database}. Query it with read-only Cypher.

The schema lists node labels as (:Label) and relationship types as [:TYPE], each with its properties;
a relationship's description names the patterns it connects, e.g. (:Person)-[:ACTED_IN]->(:Movie).

RULES:
1. Read-only: MATCH / OPTIONAL MATCH / WITH / UNWIND / RETURN / ORDER BY / LIMIT, CALL {{ subquery }}.
   CREATE, MERGE, SET, DELETE, REMOVE, LOAD CSV, USE and SHOW are refused, and so is every procedure and
   namespaced function except the schema procedures and Cypher's own date / duration / point functions (no APOC).{self._gds_rule}
2. Return scalar properties with aliases (RETURN p.name AS name), not whole nodes, so the result is a table.
3. At most {self.max_rows:,} rows come back. Aggregate in Cypher (count, sum, avg, collect) and use LIMIT.
4. Pass values as parameters: execute_query(cypher, params={{"title": "..."}}) with $title in the query.
5. Use only labels, relationship types and properties that exist in the schema.

**Example - aggregate over a pattern:**
```python
df = client.execute_query('''
MATCH (p:Person)-[:ACTED_IN]->(m:Movie)
WHERE m.released >= $since
RETURN p.name AS actor, count(m) AS movies
ORDER BY movies DESC LIMIT 20''', params={{"since": 2000}})
```

**Example - two hops:**
```python
df = client.execute_query('''
MATCH (a:Person {{name: $name}})-[:ACTED_IN]->(m:Movie)<-[:DIRECTED]-(d:Person)
RETURN DISTINCT d.name AS director, m.title AS movie''', params={{"name": "Tom Hanks"}})
```
"""
