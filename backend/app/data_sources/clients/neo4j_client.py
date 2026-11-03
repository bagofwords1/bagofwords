"""Neo4j graph database: read-only Cypher, returned as DataFrames.

Every query runs in a READ session (the server refuses writes) after
``cypher_guard.check_cypher`` (which also refuses what a READ session allows:
``LOAD CSV``, ``USE``, admin commands, and any procedure or namespaced
function that isn't on its allow-list).

The catalog has one entry per node label, ``(:Person)``, and one per
relationship type, ``[:ACTED_IN]``, whose description names the patterns it
connects (``(:Person)-[:ACTED_IN]->(:Movie)``).
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional
from urllib.parse import urlsplit

import pandas as pd

from app.ai.prompt_formatters import ServiceFormatter, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.cypher_guard import check_cypher
from app.data_sources.clients.progress import discovery_items, discovery_progress

ALLOWED_SCHEMES = ("bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc")
ROUTING_SCHEMES = ("neo4j", "neo4j+s", "neo4j+ssc")

# Hard cap on the rows one query returns: the client runs in the API process,
# so `MATCH (n) RETURN n` must not load a whole graph into memory.
MAX_ROWS = 10_000
# Budget for queries that don't come through the code executor (schema
# discovery, the connection test) and the default before apply_query_timeout.
DEFAULT_TIMEOUT_SECONDS = 60.0
SAMPLE_SIZE = 100


def _max_rows() -> int:
    return MAX_ROWS

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

    def __init__(self, uri: str, database: str = "neo4j", username: Optional[str] = None,
                 password: Optional[str] = None):
        super().__init__()
        self.uri = validate_uri(uri)
        self.database = (database or "neo4j").strip()
        self.username = username
        self.password = password
        self._query_budget = DEFAULT_TIMEOUT_SECONDS
        self._routing_checked = False

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

    def _check_routing_table(self, driver) -> None:
        """A routing URI (neo4j://) makes the driver connect to whatever servers
        the routing table lists. Check each of them like the URI's own host
        before any query reaches them."""
        if self._routing_checked or urlsplit(self.uri).scheme not in ROUTING_SCHEMES:
            return
        from app.core.egress import check_host
        from neo4j import GraphDatabase

        direct = self.uri.replace("neo4j", "bolt", 1)
        with GraphDatabase.driver(direct, auth=self._auth(), connection_timeout=15) as probe:
            with probe.session(database=self.database, default_access_mode="READ") as session:
                rows = session.run("CALL dbms.routing.getRoutingTable({}, $db)", db=self.database).data()
        for row in rows:
            for server in row.get("servers") or []:
                for address in server.get("addresses") or []:
                    check_host(address.rsplit(":", 1)[0].strip("[]"), require_resolution=False)
        self._routing_checked = True

    def _auth(self):
        return (self.username, self.password or "") if self.username else None

    @contextmanager
    def connect(self) -> Generator:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(self.uri, auth=self._auth(), connection_timeout=15,
                                      max_connection_lifetime=300)
        try:
            self._check_routing_table(driver)
            # One session per call: sessions are not thread-safe, and the
            # executor may run several queries at once.
            with driver.session(database=self.database, default_access_mode="READ",
                                fetch_size=1000) as session:
                yield session
        finally:
            driver.close()

    def _read(self, cypher: str, params: Optional[dict] = None, *, limit: Optional[int] = None,
              timeout: Optional[float] = None):
        """(columns, rows, truncated) for a read transaction."""
        from neo4j import unit_of_work

        limit = MAX_ROWS if limit is None else limit

        @unit_of_work(timeout=timeout or self._query_budget)
        def work(tx):
            result = tx.run(cypher, params or {})
            keys = list(result.keys())
            rows: List[list] = []
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

    def execute_query(self, query: str, params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
        """Run read-only Cypher; parameters bind as $name."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("execute_query takes a Cypher string, e.g. 'MATCH (n:Person) RETURN n.name LIMIT 10'.")
        if params is not None and not isinstance(params, dict):
            raise ValueError("params must be a dict of $name values.")
        check_cypher(query)
        keys, rows, truncated = self._read(query, params)
        df = pd.DataFrame(rows, columns=keys)
        if truncated:
            df.attrs["bow_notice"] = (
                f"Neo4j: only the first {_max_rows():,} rows were returned. "
                "Aggregate in Cypher (count, sum, collect) or add a LIMIT."
            )
        return df

    # ── schema ──────────────────────────────────────────────────────────────

    def _label_columns(self, session_rows: List[dict], key: str) -> Dict[str, List[TableColumn]]:
        out: Dict[str, Dict[str, TableColumn]] = {}
        for row in session_rows:
            names = row.get(key)
            names = names if isinstance(names, list) else [str(names or "").strip(":`")]
            prop = row.get("propertyName")
            for name in names:
                cols = out.setdefault(name, {})
                if prop and prop not in cols:
                    cols[prop] = TableColumn(name=prop, dtype=_type_name(row.get("propertyTypes")))
        return {k: list(v.values()) for k, v in out.items()}

    def _schema_from_procedures(self) -> List[Table]:
        timeout = DEFAULT_TIMEOUT_SECONDS
        _, node_rows, _ = self._read(
            "CALL db.schema.nodeTypeProperties() YIELD nodeLabels, propertyName, propertyTypes "
            "RETURN nodeLabels, propertyName, propertyTypes", timeout=timeout)
        _, rel_rows, _ = self._read(
            "CALL db.schema.relTypeProperties() YIELD relType, propertyName, propertyTypes "
            "RETURN relType, propertyName, propertyTypes", timeout=timeout)
        viz = self._schema_patterns(timeout)
        nodes = self._label_columns([dict(zip(("nodeLabels", "propertyName", "propertyTypes"), r)) for r in node_rows],
                                    "nodeLabels")
        rels = self._label_columns([dict(zip(("relType", "propertyName", "propertyTypes"), r)) for r in rel_rows],
                                   "relType")
        patterns: Dict[str, List[str]] = {}
        for from_labels, rel, to_labels in viz:
            for a in from_labels or ["?"]:
                for b in to_labels or ["?"]:
                    p = f"(:{a})-[:{rel}]->(:{b})"
                    if p not in patterns.setdefault(rel, []):
                        patterns[rel].append(p)
        return self._tables(nodes, rels, patterns)

    def _schema_patterns(self, timeout: float) -> List[tuple]:
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

    def _schema_from_samples(self) -> List[Table]:
        """When the schema procedures are restricted: labels, types and the
        keys of a sample of each."""
        _, labels, _ = self._read("CALL db.labels() YIELD label RETURN label", timeout=DEFAULT_TIMEOUT_SECONDS)
        _, types, _ = self._read("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType",
                                 timeout=DEFAULT_TIMEOUT_SECONDS)
        nodes: Dict[str, List[TableColumn]] = {}
        for (label,) in discovery_items(labels, "labels", label=lambda r: r[0]):
            q = f"MATCH (n:`{label.replace('`', '``')}`) WITH n LIMIT {SAMPLE_SIZE} UNWIND keys(n) AS k RETURN DISTINCT k"
            _, keys, _ = self._read(q, timeout=DEFAULT_TIMEOUT_SECONDS)
            nodes[label] = [TableColumn(name=k, dtype="object") for (k,) in keys]
        rels: Dict[str, List[TableColumn]] = {}
        for (rel,) in types:
            q = f"MATCH ()-[r:`{rel.replace('`', '``')}`]->() WITH r LIMIT {SAMPLE_SIZE} UNWIND keys(r) AS k RETURN DISTINCT k"
            _, keys, _ = self._read(q, timeout=DEFAULT_TIMEOUT_SECONDS)
            rels[rel] = [TableColumn(name=k, dtype="object") for (k,) in keys]
        return self._tables(nodes, rels, {})

    @staticmethod
    def _tables(nodes: Dict[str, List[TableColumn]], rels: Dict[str, List[TableColumn]],
                patterns: Dict[str, List[str]]) -> List[Table]:
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
    def get_schemas(self, progress_callback=None) -> List[Table]:
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
            return {"success": True,
                    "message": f"Connected to {name} {version} ({edition}), database {self.database}".strip()}
        except Exception as e:
            return {"success": False, "message": redact_error(e, secrets=[self.password] if self.password else None)}

    @property
    def description(self) -> str:
        return f"""
Neo4j graph database at {self.host}, database {self.database}. Query it with read-only Cypher.

The schema lists node labels as (:Label) and relationship types as [:TYPE], each with its properties;
a relationship's description names the patterns it connects, e.g. (:Person)-[:ACTED_IN]->(:Movie).

RULES:
1. Read-only: MATCH / OPTIONAL MATCH / WITH / UNWIND / RETURN / ORDER BY / LIMIT, CALL {{ subquery }}.
   CREATE, MERGE, SET, DELETE, REMOVE, LOAD CSV, USE and SHOW are refused, and so is every procedure and
   namespaced function except the schema procedures and Cypher's own date / duration / point functions (no APOC).
2. Return scalar properties with aliases (RETURN p.name AS name), not whole nodes, so the result is a table.
3. At most {_max_rows():,} rows come back. Aggregate in Cypher (count, sum, avg, collect) and use LIMIT.
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
