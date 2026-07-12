from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape as xml_escape

from app.data_sources.clients.xmla_base import XmlaClient, XMLA_NS

# Analysis-Services-dialect SOAP header used by the documented Infor
# DISCOVER_DATASOURCES example (Discover requests, OLAP XMLA Provider guide).
_AS_ENGINE_NS = "http://schemas.microsoft.com/analysisservices/2003/engine/2"
_VERSION_HEADER = f'<Version Sequence="200" xmlns="{_AS_ENGINE_NS}" />'


class InforOlapClient(XmlaClient):
    """
    Infor d/EPM OLAP client (formerly Infor BI / MIS Alea OLAP).

    Talks to the Infor OLAP XMLA Provider — the supported entry point for
    on-premise d/EPM (native connections were removed; XMLA is mandatory).
    All XMLA transport, discovery, and query execution live in ``XmlaClient``;
    this subclass supplies Infor-specific labels, the MDX prompt, and the
    documented Service-Manager connection flow:

    Per "Connecting to the XMLA Provider" (OLAP XMLA Provider guide), clients
    first send DISCOVER_DATASOURCES to the OLAP Service Manager
    (``http(s)://host:port/BI/APP/SOAP/OLAPDB``); the response lists each
    database with the URL of its Database Worker, which serves all subsequent
    requests. With ``manager_discovery`` enabled this client performs that
    bootstrap on connect. Farms advertise internal hostnames in worker URLs,
    so by default the configured host is substituted back in
    (``rewrite_worker_host``).

    Each cube is exposed as one schema table named ``Catalog/Cube`` whose
    columns are the cube's hierarchies (dimensions) and measures. The unique
    names needed to author MDX are carried in each column's ``metadata``.
    """

    META_KEY = "infor_olap"
    PRODUCT_NAME = "Infor OLAP"
    EMPTY_NOTE = "No OLAP databases visible to this user — check application access."
    QUERY_REQUIRED_MSG = "MDX query is required"
    PATH_HINT = (
        "If this is an Infor EPM farm, the documented entry point is "
        "http(s)://<server>:<manager_port>/BI/APP/SOAP/OLAPDB (OLAP Service "
        "Manager) — point the URL there and enable manager auto-discovery."
    )

    def __init__(
        self,
        host: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        catalog: Optional[str] = None,
        verify_ssl: bool = True,
        timeout_sec: int = 60,
        manager_discovery: bool = False,
        rewrite_worker_host: bool = True,
        tenant: str = "single",
    ):
        super().__init__(
            host=host,
            username=username,
            password=password,
            catalog=catalog,
            verify_ssl=verify_ssl,
            timeout_sec=timeout_sec,
        )
        self.manager_discovery = bool(manager_discovery)
        self.rewrite_worker_host = bool(rewrite_worker_host)
        self.tenant = (tenant or "").strip()
        # Original manager endpoint and the worker URL resolved from it.
        self.manager_url: Optional[str] = self.host if self.manager_discovery else None
        self.resolved_worker_url: Optional[str] = None

    # ------------------------------------------------------------------
    # Manager discovery (documented connection flow)
    # ------------------------------------------------------------------

    def connect(self):
        super().connect()
        if self.manager_discovery and self.resolved_worker_url is None:
            self._resolve_worker_url()

    def _resolve_worker_url(self):
        """Ask the OLAP Service Manager for the databases it hosts and point
        ``self.host`` at the chosen database's worker URL."""
        rows = self._discover_datasources()
        if not rows:
            raise RuntimeError(
                "Manager discovery returned no databases (DISCOVER_DATASOURCES "
                "response was empty) — check permissions for this user."
            )
        row = self._pick_datasource(rows)
        url = row.get("URL") or row.get("Url") or row.get("url")
        if not url:
            fields = sorted({k for r in rows for k in r})
            raise RuntimeError(
                f"Manager discovery response had no URL column (fields: {fields})."
            )
        self.resolved_worker_url = self._rewrite_host(url) if self.rewrite_worker_host else url
        self.host = self.resolved_worker_url.rstrip("/")

    def _discover_datasources(self) -> List[Dict]:
        restriction_xml = ""
        if self.catalog:
            restriction_xml = f"<Databasename>{xml_escape(self.catalog)}</Databasename>"
        tenant_xml = f"<Tenant>{xml_escape(self.tenant)}</Tenant>" if self.tenant else ""
        body = (
            f'<Discover xmlns="{XMLA_NS}">'
            "<RequestType>DISCOVER_DATASOURCES</RequestType>"
            f"<Restrictions><RestrictionList>{restriction_xml}</RestrictionList></Restrictions>"
            f"<Properties><PropertyList>{tenant_xml}<Content>SchemaData</Content></PropertyList></Properties>"
            "</Discover>"
        )
        root = self._soap_call(
            "Discover", body, url=self.manager_url, header_xml=_VERSION_HEADER
        )
        # Parse rows leniently (any namespace): the manager's rowset namespace
        # is not guaranteed to match the worker's.
        rows: List[Dict] = []
        for el in root.iter():
            if el.tag.split("}", 1)[-1] != "row":
                continue
            row = {child.tag.split("}", 1)[-1]: child.text for child in el}
            rows.append(row)
        return rows

    def _pick_datasource(self, rows: List[Dict]) -> Dict:
        names = [r.get("DataSourceName") or r.get("Databasename") or "" for r in rows]
        if self.catalog:
            for r, name in zip(rows, names):
                if name == self.catalog:
                    return r
            # Database names are case-sensitive; fall back with a warning-free
            # case-insensitive match rather than failing on casing alone.
            for r, name in zip(rows, names):
                if name.lower() == self.catalog.lower():
                    return r
            raise RuntimeError(
                f"Database '{self.catalog}' not found on the manager. "
                f"Available: {', '.join(n for n in names if n) or '(unnamed)'}"
            )
        if len(rows) == 1:
            return rows[0]
        raise RuntimeError(
            "Multiple databases found on the manager — set Catalog to one of: "
            + ", ".join(n for n in names if n)
        )

    def _rewrite_host(self, url: str) -> str:
        """Swap the hostname in a discovered worker URL for the configured
        one. Farms advertise internal machine names that rarely resolve from
        outside; the worker is reached at the same address the manager was."""
        configured = urlsplit(self.manager_url or self.host)
        returned = urlsplit(url)
        if not configured.hostname or not returned.hostname:
            return url
        netloc = configured.hostname
        if returned.port:
            netloc += f":{returned.port}"
        return urlunsplit((returned.scheme, netloc, returned.path, returned.query, returned.fragment))

    def test_connection(self) -> Dict:
        result = super().test_connection()
        if result.get("success") and self.resolved_worker_url:
            result["message"] += f" Worker URL: {self.resolved_worker_url}"
            result["worker_url"] = self.resolved_worker_url
        return result

    @property
    def description(self) -> str:
        return (
            "Infor OLAP Client: discover cubes (XMLA Discover) and execute MDX "
            "against the Infor d/EPM OLAP semantic layer (XMLA Execute). Works "
            "with the on-premise Infor OLAP XMLA Provider."
        ) + self.system_prompt()

    def system_prompt(self) -> str:
        return """

## Infor OLAP MDX Guide

Execute MDX queries against Infor d/EPM OLAP cubes. The OLAP server resolves
MDX against the multidimensional semantic model (dimensions, hierarchies, and
measures).

### Schema Structure

Each cube is exposed as a schema table named `Catalog/Cube`:
- `Finance/GL` - the GL cube in the Finance catalog
- `Sales/Revenue` - the Revenue cube in the Sales catalog

Each column is either a dimension hierarchy (`dtype="dimension"`) or a measure
(`dtype="measure"`). The MDX `unique_name` for every column lives in its
`metadata.unique_name` (e.g. `[Time].[Calendar]`, `[Measures].[Sales Amount]`),
and the cube's unique name is in `metadata.infor_olap.cubeUniqueName`.

### How to Execute Queries

**Signature**: `execute_query(mdx_query, table_name)` — pass the `Catalog/Cube`
schema table name as the second argument so the catalog can be resolved.

```python
df = db_clients['infor_olap'].execute_query(
    '''
    SELECT
      { [Measures].[Sales Amount] } ON COLUMNS,
      NON EMPTY { [Product].[Category].Members } ON ROWS
    FROM [Revenue]
    ''',
    "Sales/Revenue"
)
```

### MDX Rules

- The FROM clause names the cube in brackets: `FROM [Revenue]`.
- Put measures on one axis (usually COLUMNS) and dimension members on the
  other (usually ROWS).
- Reference members by their unique name from `metadata.unique_name`, e.g.
  `[Product].[Category].Members`, `[Time].[2025]`.
- Use `NON EMPTY` to drop empty rows and `CROSSJOIN(...)` to combine
  dimensions on one axis.
- Use MDX functions (`Members`, `Children`, `Descendants`, `Filter`,
  `Order`, `TopCount`) rather than SQL syntax — this is MDX, not SQL.
"""
