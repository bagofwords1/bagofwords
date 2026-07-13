import time
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape as xml_escape

import requests

from app.data_sources.clients.xmla_base import XMLA_NS, XmlaClient, XmlaHttpError

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
        secured: bool = False,
        worker_url_base: Optional[str] = None,
        gateway_token_url: Optional[str] = None,
        gateway_client_id: Optional[str] = None,
        gateway_client_secret: Optional[str] = None,
        gateway_scope: Optional[str] = None,
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
        self.secured = bool(secured)
        self.worker_url_base = (worker_url_base or "").rstrip("/") or None
        self.gateway_token_url = (gateway_token_url or "").strip() or None
        self.gateway_client_id = gateway_client_id
        self.gateway_client_secret = gateway_client_secret
        self.gateway_scope = (gateway_scope or "").strip() or None
        self._gateway_token_expires_at = 0.0
        # Original manager endpoint and the worker URL resolved from it.
        self.manager_url: Optional[str] = self.host if self.manager_discovery else None
        self.resolved_worker_url: Optional[str] = None

    # ------------------------------------------------------------------
    # Manager discovery (documented connection flow)
    # ------------------------------------------------------------------

    def connect(self):
        if self.gateway_token_url:
            self._connect_gateway()
        else:
            super().connect()
        if self.manager_discovery and self.resolved_worker_url is None:
            self._resolve_worker_url()

    def _connect_gateway(self):
        if not self.host:
            raise RuntimeError("host is required")
        if not (self.username and self.password):
            raise RuntimeError("username and password are required")
        if not (self.gateway_client_id and self.gateway_client_secret):
            raise RuntimeError(
                "gateway_client_id and gateway_client_secret are required "
                "when gateway_token_url is configured"
            )
        if self._http is None:
            self._http = requests.Session()
        self._ensure_gateway_token()

    def _ensure_gateway_token(self):
        if time.monotonic() < self._gateway_token_expires_at:
            return
        token_data = {
            "grant_type": "client_credentials",
            "client_id": self.gateway_client_id,
            "client_secret": self.gateway_client_secret,
        }
        if self.gateway_scope:
            token_data["scope"] = self.gateway_scope
        response = requests.post(
            self.gateway_token_url,
            data=token_data,
            timeout=self.timeout_sec,
            verify=self.verify_ssl,
        )
        if response.status_code >= 300:
            raise RuntimeError(
                f"ION API Gateway token exchange failed (HTTP {response.status_code})"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("ION API Gateway token exchange returned invalid JSON") from exc
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("ION API Gateway token exchange returned no access_token")
        try:
            expires_in = max(int(payload.get("expires_in", 300)), 1)
        except (TypeError, ValueError):
            expires_in = 300
        refresh_skew = min(30, expires_in // 5)
        self._gateway_token_expires_at = time.monotonic() + expires_in - refresh_skew
        self._http.headers["Authorization"] = f"Bearer {token}"

    def _soap_call(
        self,
        method: str,
        body_xml: str,
        url: Optional[str] = None,
        header_xml: str = "",
    ):
        if self.gateway_token_url:
            self._ensure_gateway_token()
        try:
            return super()._soap_call(method, body_xml, url=url, header_xml=header_xml)
        except XmlaHttpError as exc:
            if not self.gateway_token_url or exc.status_code != 401:
                raise
            self._gateway_token_expires_at = 0.0
            self._ensure_gateway_token()
            return super()._soap_call(method, body_xml, url=url, header_xml=header_xml)

    def _resolve_worker_url(self):
        """Ask the OLAP Service Manager for the databases it hosts and point
        ``self.host`` at the chosen database's worker URL."""
        rows = self._discover_datasources()
        if not rows:
            raise RuntimeError(
                "Manager discovery returned no databases (DISCOVER_DATASOURCES "
                "response was empty) — check Tenant, Catalog, Secured, and user access."
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
        restrictions = []
        if self.catalog:
            restrictions.append(f"<Databasename>{xml_escape(self.catalog)}</Databasename>")
        restrictions.append(f"<Secured>{str(self.secured).lower()}</Secured>")
        restriction_xml = "".join(restrictions)
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

    def _property_list_xml(
        self,
        catalog: Optional[str],
        fmt: str = "Tabular",
        content: Optional[str] = None,
    ) -> str:
        """Build the application-level context required by Infor workers.

        Infor authenticates XMLA database-worker requests from PropertyList;
        HTTP Basic alone reaches the endpoint but leaves ``userName`` empty.
        """
        parts = []
        if catalog:
            parts.append(f"<Catalog>{xml_escape(catalog)}</Catalog>")
        if self.tenant:
            parts.append(f"<Tenant>{xml_escape(self.tenant)}</Tenant>")
        if self.username:
            parts.append(f"<UserName>{xml_escape(self.username)}</UserName>")
        if self.password:
            parts.append(f"<Password>{xml_escape(self.password)}</Password>")
        parts.append(f"<Format>{xml_escape(fmt)}</Format>")
        if content:
            parts.append(f"<Content>{xml_escape(content)}</Content>")
        return "".join(parts)

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
        if self.worker_url_base:
            base = urlsplit(self.worker_url_base)
            if not base.scheme or not base.netloc:
                raise RuntimeError("worker_url_base must be an absolute URL")
            path = f"{base.path.rstrip('/')}/{returned.path.lstrip('/')}"
            return urlunsplit(
                (base.scheme, base.netloc, path, returned.query, returned.fragment)
            )
        if not configured.hostname or not returned.hostname:
            return url
        netloc = configured.hostname
        if returned.port:
            netloc += f":{returned.port}"
        return urlunsplit((returned.scheme, netloc, returned.path, returned.query, returned.fragment))

    def test_connection(self) -> Dict:
        result = super().test_connection()
        if result.get("success") and self.catalog:
            try:
                cubes = self._list_cubes(self.catalog)
            except Exception as exc:
                return self._classify_failure(exc)
            result["cubes"] = len(cubes)
            result["message"] += f" Found {len(cubes)} cube(s) in {self.catalog}."
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
