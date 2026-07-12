from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
from typing import List, Dict, Optional
import re
import requests
import pandas as pd
from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element
from xml.sax.saxutils import escape as xml_escape


# Standard XML for Analysis (XMLA) namespaces. Every XMLA provider — SSAS,
# Infor d/EPM OLAP, Mondrian, icCube — speaks the same SOAP contract, so these
# are fixed.
SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
XMLA_NS = "urn:schemas-microsoft-com:xml-analysis"
XMLA_ROWSET_NS = "urn:schemas-microsoft-com:xml-analysis:rowset"

_NS = {
    "soap": SOAP_ENV_NS,
    "xmla": XMLA_NS,
    "rs": XMLA_ROWSET_NS,
}

# MDSCHEMA_DIMENSIONS exposes the synthetic "Measures" dimension as type 2;
# we surface measures separately (via MDSCHEMA_MEASURES) so we skip it here.
_MEASURE_DIMENSION_TYPE = "2"


class XmlaHttpError(RuntimeError):
    """Non-2xx HTTP response from the XMLA endpoint. Carries enough context
    for test_connection to say *what kind* of failure this is (wrong path vs
    rejected credentials vs server error) instead of one generic message."""

    def __init__(self, method: str, status_code: int, server_header: str = "", body_snippet: str = ""):
        self.method = method
        self.status_code = status_code
        self.server_header = server_header or ""
        self.body_snippet = body_snippet or ""
        super().__init__(f"XMLA {method} failed: HTTP {status_code} {self.body_snippet}")


class XmlaClient(DataSourceClient):
    """
    Shared base for XML for Analysis (XMLA) data source clients.

    Implements the common XMLA SOAP machinery over HTTP with Basic auth:

      - discover catalogs (databases)      via Discover DBSCHEMA_CATALOGS
      - discover cubes per catalog         via Discover MDSCHEMA_CUBES
      - discover measures / hierarchies    via Discover MDSCHEMA_MEASURES
                                               and MDSCHEMA_HIERARCHIES
      - run a statement (MDX/DAX/DMV)      via Execute (Format=Tabular)

    Each cube/model is exposed as one schema table named ``Catalog/Cube`` whose
    columns are the cube's hierarchies (dimensions) and measures. The unique
    names needed to author queries are carried in each column's ``metadata``.

    Subclasses set the class attributes below and provide ``description`` /
    ``system_prompt``; they may override ``_catalog_context`` to attach
    provider-specific metadata (e.g. model type) to every cube in a catalog.
    """

    # Top-level key under each Table.metadata_json (e.g. "infor_olap").
    META_KEY: str = "xmla"
    # Human-facing product name used in messages.
    PRODUCT_NAME: str = "XMLA source"
    # Appended to the test_connection message when no catalogs are visible.
    EMPTY_NOTE: str = "No databases visible to this user — check permissions."
    # ValueError message when execute_query is called with an empty statement.
    QUERY_REQUIRED_MSG: str = "A query is required"
    # Provider-specific hint appended when the endpoint answers HTTP 404 —
    # i.e. the host is right but the URL path is not an XMLA service.
    PATH_HINT: str = ""

    def __init__(
        self,
        host: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        catalog: Optional[str] = None,
        verify_ssl: bool = True,
        timeout_sec: int = 60,
    ):
        # host is the full XMLA endpoint URL.
        self.host = (host or "").rstrip("/")
        self.username = username
        self.password = password
        # Optional single catalog to scope discovery to.
        self.catalog = (catalog or "").strip() or None
        self.verify_ssl = verify_ssl
        self.timeout_sec = timeout_sec

        self._http: Optional[requests.Session] = None

    # ------------------------------------------------------------------
    # Connection / auth
    # ------------------------------------------------------------------

    def connect(self):
        """Prepare a Basic-auth HTTP session. XMLA is stateless — there is no
        logon round-trip, the credentials ride on every request."""
        if self._http is not None:
            return
        if not self.host:
            raise RuntimeError("host is required")
        if not (self.username and self.password):
            raise RuntimeError("username and password are required")

        session = requests.Session()
        session.auth = (self.username, self.password)
        self._http = session

    def test_connection(self) -> Dict:
        # connect() may do network I/O in subclasses (e.g. manager discovery),
        # so both phases share the same failure classification.
        try:
            self.connect()
            catalogs = self._list_catalogs()
        except Exception as e:
            return self._classify_failure(e)

        msg = f"Connected to {self.PRODUCT_NAME}. Found {len(catalogs)} catalog(s)."
        if not catalogs:
            msg += f" ({self.EMPTY_NOTE})"
        return {"success": True, "message": msg, "catalogs": len(catalogs)}

    def _classify_failure(self, e: Exception) -> Dict:
        """Turn a connection/discovery exception into an actionable message.
        The distinctions matter operationally: DNS vs wrong-path vs rejected
        credentials each point at a different owner (network, endpoint URL,
        account) — collapsing them into one sentence hides the next step."""
        if isinstance(e, XmlaHttpError):
            if e.status_code == 404:
                msg = (
                    "Endpoint reached, but nothing serves XMLA at this URL path "
                    "(HTTP 404) — verify the path portion of the endpoint URL."
                )
                # Windows HTTP.sys answers unregistered paths itself; its
                # signature server header means "right machine, wrong path".
                if self.PATH_HINT and e.server_header.lower().startswith("microsoft-httpapi"):
                    msg += f" {self.PATH_HINT}"
                return {"success": False, "connectivity": True, "message": msg}
            if e.status_code in (401, 403):
                return {
                    "success": False,
                    "connectivity": True,
                    "message": (
                        f"Endpoint found, but the credentials or authentication "
                        f"scheme were rejected (HTTP {e.status_code})."
                    ),
                }
            return {
                "success": False,
                "connectivity": True,
                "message": f"XMLA endpoint returned HTTP {e.status_code}: {e.body_snippet[:300]}",
            }
        if isinstance(e, requests.exceptions.ConnectionError):
            detail = str(e)
            if "NameResolutionError" in detail or "Temporary failure in name resolution" in detail:
                msg = (
                    "Could not resolve the endpoint hostname (DNS). Use an IP "
                    "address or a resolvable FQDN in the endpoint URL."
                )
            else:
                msg = f"Could not connect to the endpoint (refused or reset): {detail[:300]}"
            return {"success": False, "connectivity": False, "message": msg}
        if isinstance(e, requests.exceptions.Timeout):
            return {
                "success": False,
                "connectivity": False,
                "message": "Connection to the endpoint timed out — check firewall rules and the port.",
            }
        if isinstance(e, requests.exceptions.RequestException):
            return {"success": False, "connectivity": False, "message": f"HTTP request failed: {e}"}
        # connect()'s local validation ("host is required", "username and
        # password are required") never touched the network.
        if isinstance(e, RuntimeError) and "required" in str(e) and self._http is None:
            return {"success": False, "message": f"Configuration error: {e}"}
        return {
            "success": False,
            "connectivity": True,
            "message": f"Reached the XMLA endpoint but discovery failed: {e}",
        }

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _list_catalogs(self) -> List[str]:
        """Return catalog (database) names. Honors the configured catalog scope."""
        if self.catalog:
            return [self.catalog]
        rows = self._discover("DBSCHEMA_CATALOGS")
        return [c for c in (r.get("CATALOG_NAME") for r in rows) if c]

    def _list_cubes(self, catalog: str) -> List[Dict]:
        """Return cube descriptors for a catalog (skips dimension-only rows)."""
        rows = self._discover(
            "MDSCHEMA_CUBES",
            restrictions={"CATALOG_NAME": catalog},
            catalog=catalog,
        )
        cubes: List[Dict] = []
        for r in rows:
            name = r.get("CUBE_NAME")
            if not name:
                continue
            # CUBE_TYPE 'CUBE' is a queryable cube; 'DIMENSION' rows are skipped.
            if (r.get("CUBE_TYPE") or "CUBE").upper() != "CUBE":
                continue
            cubes.append({
                "name": name,
                "caption": r.get("CUBE_CAPTION") or name,
                "description": r.get("DESCRIPTION") or None,
            })
        return cubes

    def _list_measures(self, catalog: str, cube: str) -> List[Dict]:
        rows = self._discover(
            "MDSCHEMA_MEASURES",
            restrictions={"CATALOG_NAME": catalog, "CUBE_NAME": cube},
            catalog=catalog,
        )
        measures: List[Dict] = []
        for r in rows:
            name = r.get("MEASURE_NAME")
            if not name:
                continue
            measures.append({
                "name": name,
                "caption": r.get("MEASURE_CAPTION") or name,
                "unique_name": r.get("MEASURE_UNIQUE_NAME") or f"[Measures].[{name}]",
                "data_type": r.get("DATA_TYPE") or None,
                "description": r.get("DESCRIPTION") or None,
            })
        return measures

    def _list_hierarchies(self, catalog: str, cube: str) -> List[Dict]:
        rows = self._discover(
            "MDSCHEMA_HIERARCHIES",
            restrictions={"CATALOG_NAME": catalog, "CUBE_NAME": cube},
            catalog=catalog,
        )
        hierarchies: List[Dict] = []
        for r in rows:
            # Skip the Measures dimension's hierarchy — measures are surfaced
            # separately so they don't show up twice.
            if (r.get("DIMENSION_TYPE") or "").strip() == _MEASURE_DIMENSION_TYPE:
                continue
            unique_name = r.get("HIERARCHY_UNIQUE_NAME")
            name = r.get("HIERARCHY_NAME") or r.get("HIERARCHY_CAPTION")
            if not (unique_name or name):
                continue
            hierarchies.append({
                "name": name or unique_name,
                "caption": r.get("HIERARCHY_CAPTION") or name or unique_name,
                "unique_name": unique_name or f"[{name}]",
                "dimension_unique_name": r.get("DIMENSION_UNIQUE_NAME") or None,
                "description": r.get("DESCRIPTION") or None,
            })
        return hierarchies

    def _catalog_context(self, catalog: str) -> Dict:
        """Hook for provider-specific per-catalog metadata merged into every
        cube's metadata (e.g. Tabular vs Multidimensional). Default: none."""
        return {}

    def get_schemas(self) -> List[Table]:
        """Return one Table per cube across all (scoped) catalogs."""
        tables: List[Table] = []
        for catalog in self._list_catalogs():
            ctx = self._catalog_context(catalog)
            for cube in self._list_cubes(catalog):
                cube_name = cube["name"]
                columns: List[TableColumn] = []

                for h in self._list_hierarchies(catalog, cube_name):
                    columns.append(TableColumn(
                        name=h["caption"],
                        dtype="dimension",
                        description=h.get("description"),
                        metadata={
                            "role": "dimension",
                            "unique_name": h["unique_name"],
                            "dimension": h.get("dimension_unique_name"),
                        },
                    ))

                for m in self._list_measures(catalog, cube_name):
                    columns.append(TableColumn(
                        name=m["caption"],
                        dtype="measure",
                        description=m.get("description"),
                        metadata={
                            "role": "measure",
                            "unique_name": m["unique_name"],
                            "data_type": m.get("data_type"),
                        },
                    ))

                meta = {
                    "catalog": catalog,
                    "cube": cube_name,
                    "cubeUniqueName": f"[{cube_name}]",
                }
                meta.update(ctx)

                tables.append(Table(
                    name=f"{catalog}/{cube_name}",
                    description=cube.get("description"),
                    columns=columns,
                    pks=[],
                    fks=[],
                    is_active=True,
                    metadata_json={self.META_KEY: meta},
                ))
        return tables

    def get_schema(self, table_name: str) -> Table:
        """Resolve a single Table by name or by metadata identifiers."""
        all_tables = self.get_schemas()
        for tbl in all_tables:
            if tbl.name == table_name:
                return tbl
        for tbl in all_tables:
            meta = (tbl.metadata_json or {}).get(self.META_KEY) or {}
            if meta.get("cube") == table_name:
                return tbl
        for tbl in all_tables:
            meta = (tbl.metadata_json or {}).get(self.META_KEY) or {}
            if meta.get("catalog") == table_name:
                return tbl
        raise RuntimeError(f"Table not found for '{table_name}'")

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(
        self,
        query: str,
        table_name: Optional[str] = None,
        catalog: Optional[str] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """Execute a statement via XMLA Execute and return a DataFrame."""
        if not query or not query.strip():
            raise ValueError(self.QUERY_REQUIRED_MSG)
        self.connect()
        target_catalog = self._resolve_catalog(table_name, catalog)
        rows = self._execute_statement(query, target_catalog)
        return self._rows_to_df(rows, max_rows)

    def _resolve_catalog(self, table_name: Optional[str], catalog: Optional[str]) -> Optional[str]:
        target = catalog or self.catalog
        # Schema tables are named "Catalog/Cube" (get_schemas), so the catalog
        # is the first path segment — resolving it locally avoids re-running
        # full discovery (several XMLA round trips) on every query.
        if not target and table_name and "/" in table_name:
            target = table_name.split("/", 1)[0]
        if not target and table_name:
            try:
                meta = (self.get_schema(table_name).metadata_json or {}).get(self.META_KEY) or {}
                target = meta.get("catalog")
            except Exception:
                pass
        return target

    @staticmethod
    def _rows_to_df(rows: List[Dict], max_rows: Optional[int]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        # XMLA encodes special chars in tabular column tags (e.g. brackets and
        # spaces) as _xHHHH_ — decode them back to the caption form.
        df.columns = [_decode_xmla_name(c) for c in df.columns]
        if max_rows is not None and max_rows > 0 and len(df) > max_rows:
            df = df.head(max_rows)
        return df

    # ------------------------------------------------------------------
    # Prompt / description
    # ------------------------------------------------------------------

    def prompt_schema(self) -> str:
        schemas = self.get_schemas()
        return ServiceFormatter(schemas).table_str

    def system_prompt(self) -> str:
        return ""

    # ------------------------------------------------------------------
    # XMLA transport
    # ------------------------------------------------------------------

    def _discover(
        self,
        request_type: str,
        restrictions: Optional[Dict[str, str]] = None,
        catalog: Optional[str] = None,
    ) -> List[Dict]:
        """Issue an XMLA Discover and return its rowset as a list of dicts."""
        self.connect()

        restriction_xml = "".join(
            f"<{k}>{xml_escape(v)}</{k}>" for k, v in (restrictions or {}).items() if v
        )
        properties_xml = self._property_list_xml(catalog if catalog is not None else self.catalog)
        body = (
            f'<Discover xmlns="{XMLA_NS}">'
            f"<RequestType>{request_type}</RequestType>"
            f"<Restrictions><RestrictionList>{restriction_xml}</RestrictionList></Restrictions>"
            f"<Properties><PropertyList>{properties_xml}</PropertyList></Properties>"
            "</Discover>"
        )
        root = self._soap_call("Discover", body)
        return self._parse_rowset(root)

    def _execute_statement(self, statement: str, catalog: Optional[str]) -> List[Dict]:
        """Issue an XMLA Execute (Format=Tabular) and return flattened rows.

        The same Execute command carries MDX, DAX, or DMV statements — the
        server parses the language.
        """
        self.connect()

        properties_xml = self._property_list_xml(catalog, fmt="Tabular", content="SchemaData")
        body = (
            f'<Execute xmlns="{XMLA_NS}">'
            f"<Command><Statement>{xml_escape(statement)}</Statement></Command>"
            f"<Properties><PropertyList>{properties_xml}</PropertyList></Properties>"
            "</Execute>"
        )
        root = self._soap_call("Execute", body)
        return self._parse_rowset(root)

    @staticmethod
    def _property_list_xml(catalog: Optional[str], fmt: str = "Tabular", content: Optional[str] = None) -> str:
        parts = [f"<Format>{fmt}</Format>"]
        if content:
            parts.append(f"<Content>{content}</Content>")
        if catalog:
            parts.append(f"<Catalog>{xml_escape(catalog)}</Catalog>")
        return "".join(parts)

    def _soap_call(
        self,
        method: str,
        body_xml: str,
        url: Optional[str] = None,
        header_xml: str = "",
    ) -> Element:
        """
        POST an XMLA SOAP request (Discover/Execute) and return the parsed
        response root. Raises on HTTP errors, SOAP faults, and inline XMLA
        error messages. ``url`` overrides the target (used for manager
        discovery); ``header_xml`` is injected as the SOAP Header body.
        """
        if not self._http:
            self.connect()

        header = f"<soap:Header>{header_xml}</soap:Header>" if header_xml else ""
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<soap:Envelope xmlns:soap="{SOAP_ENV_NS}">'
            f"{header}"
            "<soap:Body>"
            f"{body_xml}"
            "</soap:Body>"
            "</soap:Envelope>"
        )
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{XMLA_NS}:{method}"',
        }
        resp = self._http.post(
            url or self.host,
            data=envelope.encode("utf-8"),
            headers=headers,
            timeout=self.timeout_sec,
            verify=self.verify_ssl,
        )
        if resp.status_code >= 300:
            raise XmlaHttpError(
                f"{self.PRODUCT_NAME} {method}",
                resp.status_code,
                resp.headers.get("Server", ""),
                resp.text[:500],
            )

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise RuntimeError(f"Invalid XMLA response from {method}: {e}")

        fault = root.find(".//soap:Fault", _NS)
        if fault is not None:
            faultstring = (fault.findtext("faultstring") or "").strip()
            raise RuntimeError(faultstring or f"{self.PRODUCT_NAME} SOAP fault on {method}")

        self._raise_on_xmla_error(root)
        return root

    def _raise_on_xmla_error(self, root: Element):
        """XMLA reports query/discovery errors as <Messages><Error/> inside the
        rowset root rather than as a SOAP fault."""
        for err in root.iter(f"{{{XMLA_ROWSET_NS}}}Error"):
            desc = err.get("Description") or err.get("ErrorCode") or "XMLA error"
            raise RuntimeError(f"{self.PRODUCT_NAME} query error: {desc}")

    @staticmethod
    def _parse_rowset(root: Element) -> List[Dict]:
        """Extract <row> elements (rowset namespace) into a list of dicts."""
        rows: List[Dict] = []
        for row_el in root.iter(f"{{{XMLA_ROWSET_NS}}}row"):
            row: Dict = {}
            for child in row_el:
                tag = child.tag.split("}", 1)[-1]
                row[tag] = child.text
            rows.append(row)
        return rows


def _decode_xmla_name(name: str) -> str:
    """Decode XMLA _xHHHH_ escapes used in tabular column element names."""
    if not name or "_x" not in name:
        return name
    return re.sub(
        r"_x([0-9A-Fa-f]{4})_",
        lambda m: chr(int(m.group(1), 16)),
        name,
    )
