from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
from typing import List, Dict, Optional
import re
import requests
import pandas as pd
from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element
from xml.sax.saxutils import escape as xml_escape


# Standard XML for Analysis (XMLA) namespaces. Infor d/EPM OLAP exposes the
# same XMLA SOAP contract as SSAS/Mondrian/icCube, so these are fixed.
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


class InforOlapClient(DataSourceClient):
    """
    Infor d/EPM OLAP client (formerly Infor BI / MIS Alea OLAP).

    Talks to the Infor OLAP XMLA Provider — the supported entry point for
    on-premise d/EPM (native connections were removed; XMLA is mandatory).
    It uses the standard XMLA SOAP contract over HTTP with Basic auth to:

      - discover catalogs (databases)          via Discover DBSCHEMA_CATALOGS
      - discover cubes per catalog             via Discover MDSCHEMA_CUBES
      - discover measures / hierarchies        via Discover MDSCHEMA_MEASURES
                                                   and MDSCHEMA_HIERARCHIES
      - run MDX against the semantic layer      via Execute (Format=Tabular)

    Each cube is exposed as one schema table named ``Catalog/Cube`` whose
    columns are the cube's hierarchies (dimensions) and measures. The unique
    names needed to author MDX are carried in each column's ``metadata``.
    """

    def __init__(
        self,
        host: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        catalog: Optional[str] = None,
        verify_ssl: bool = True,
        timeout_sec: int = 60,
    ):
        # host is the full XMLA endpoint URL, e.g.
        # http://epm-server/<web_app>/<service_host_instance>
        self.host = (host or "").rstrip("/")
        self.username = username
        self.password = password
        # Optional single catalog (d/EPM application) to scope discovery to.
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
        try:
            self.connect()
        except Exception as e:
            return {"success": False, "message": f"Authentication failed: {e}"}

        try:
            catalogs = self._list_catalogs()
        except Exception as e:
            return {
                "success": False,
                "connectivity": True,
                "message": f"Reached the XMLA endpoint but discovery failed: {e}",
            }

        msg = f"Connected to Infor OLAP. Found {len(catalogs)} catalog(s)."
        if not catalogs:
            msg += " (No OLAP databases visible to this user — check application access.)"
        return {"success": True, "message": msg, "catalogs": len(catalogs)}

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

    def get_schemas(self) -> List[Table]:
        """Return one Table per cube across all (scoped) catalogs."""
        tables: List[Table] = []
        for catalog in self._list_catalogs():
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

                tables.append(Table(
                    name=f"{catalog}/{cube_name}",
                    description=cube.get("description"),
                    columns=columns,
                    pks=[],
                    fks=[],
                    is_active=True,
                    metadata_json={
                        "infor_olap": {
                            "catalog": catalog,
                            "cube": cube_name,
                            "cubeUniqueName": f"[{cube_name}]",
                        }
                    },
                ))
        return tables

    def get_schema(self, table_name: str) -> Table:
        """Resolve a single Table by name or by metadata identifiers."""
        all_tables = self.get_schemas()
        for tbl in all_tables:
            if tbl.name == table_name:
                return tbl
        for tbl in all_tables:
            meta = (tbl.metadata_json or {}).get("infor_olap") or {}
            if meta.get("cube") == table_name:
                return tbl
        for tbl in all_tables:
            meta = (tbl.metadata_json or {}).get("infor_olap") or {}
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
        """
        Execute an MDX statement via XMLA Execute and return a DataFrame.

        Args:
            query: an MDX SELECT statement.
            table_name: optional ``Catalog/Cube`` hint used to resolve the
                catalog the cube lives in (needed for the XMLA Catalog property).
            catalog: explicit catalog override (takes precedence).
            max_rows: optional client-side row cap.
        """
        if not query or not query.strip():
            raise ValueError("MDX query is required")

        self.connect()

        target_catalog = catalog or self.catalog
        if not target_catalog and table_name:
            try:
                meta = (self.get_schema(table_name).metadata_json or {}).get("infor_olap") or {}
                target_catalog = meta.get("catalog")
            except Exception:
                pass

        rows = self._execute_mdx(query, target_catalog)
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        # XMLA encodes special chars in tabular column tags (e.g. brackets and
        # spaces) as _xHHHH_ — decode them back to the MDX caption form.
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

    def _execute_mdx(self, mdx: str, catalog: Optional[str]) -> List[Dict]:
        """Issue an XMLA Execute (Format=Tabular) and return flattened rows."""
        self.connect()

        properties_xml = self._property_list_xml(catalog, fmt="Tabular", content="SchemaData")
        body = (
            f'<Execute xmlns="{XMLA_NS}">'
            f"<Command><Statement>{xml_escape(mdx)}</Statement></Command>"
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

    def _soap_call(self, method: str, body_xml: str) -> Element:
        """
        POST an XMLA SOAP request (Discover/Execute) and return the parsed
        response root. Raises on HTTP errors, SOAP faults, and inline XMLA
        error messages.
        """
        if not self._http:
            self.connect()

        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<soap:Envelope xmlns:soap="{SOAP_ENV_NS}">'
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
            self.host,
            data=envelope.encode("utf-8"),
            headers=headers,
            timeout=self.timeout_sec,
            verify=self.verify_ssl,
        )
        if resp.status_code >= 300:
            raise RuntimeError(
                f"Infor OLAP XMLA {method} failed: HTTP {resp.status_code} {resp.text[:500]}"
            )

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise RuntimeError(f"Invalid XMLA response from {method}: {e}")

        fault = root.find(".//soap:Fault", _NS)
        if fault is not None:
            faultstring = (fault.findtext("faultstring") or "").strip()
            raise RuntimeError(faultstring or f"Infor OLAP SOAP fault on {method}")

        self._raise_on_xmla_error(root)
        return root

    @staticmethod
    def _raise_on_xmla_error(root: Element):
        """XMLA reports query/discovery errors as <Messages><Error/> inside the
        rowset root rather than as a SOAP fault."""
        for err in root.iter(f"{{{XMLA_ROWSET_NS}}}Error"):
            desc = err.get("Description") or err.get("ErrorCode") or "Infor OLAP XMLA error"
            raise RuntimeError(f"Infor OLAP query error: {desc}")

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
