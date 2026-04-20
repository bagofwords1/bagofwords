from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
from typing import List, Dict, Optional
import requests
import pandas as pd
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape


SOAP_NS = "urn://oracle.bi.webservices/v12"
XMLA_ROWSET_NS = "urn:schemas-microsoft-com:xml-analysis:rowset"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"

_NS = {
    "soap": SOAP_ENV_NS,
    "sawsoap": SOAP_NS,
    "rs": XMLA_ROWSET_NS,
    "xsd": XSD_NS,
}


class OracleBIClient(DataSourceClient):
    """
    Oracle Business Intelligence client (OBIEE / OAS / Oracle Analytics Cloud).

    Uses the BI Web Services v12 SOAP API at /analytics-ws/saw.dll to:
      - authenticate via SAWSessionService.logon
      - list subject areas via MetadataService.getSubjectAreas
      - describe subject areas (tables + columns) via MetadataService.describeSubjectArea
      - execute Logical SQL via XmlViewService.executeXMLQuery

    Works identically across OBIEE 11g/12c, OAS, and OAC because all ship
    the same v12 WSDL at /analytics-ws/saw.dll/wsdl/v12.
    """

    def __init__(
        self,
        host: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: bool = True,
        timeout_sec: int = 60,
    ):
        self.host = (host or "").rstrip("/")
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.timeout_sec = timeout_sec

        self._session_id: Optional[str] = None
        self._http: Optional[requests.Session] = None

    # ------------------------------------------------------------------
    # Connection / auth
    # ------------------------------------------------------------------

    def connect(self):
        """Obtain a SOAP session ID via nQSessionService.logon."""
        if self._http and self._session_id:
            return

        if not self.host:
            raise RuntimeError("host is required")
        if not (self.username and self.password):
            raise RuntimeError("username and password are required")

        if self._http is None:
            self._http = requests.Session()
        body = (
            f"<v12:logon>"
            f"<v12:name>{xml_escape(self.username)}</v12:name>"
            f"<v12:password>{xml_escape(self.password)}</v12:password>"
            f"</v12:logon>"
        )
        resp_xml = self._soap_call("nQSessionService", body)
        sid_el = resp_xml.find(".//sawsoap:sessionID", _NS)
        if sid_el is None or not (sid_el.text or "").strip():
            raise RuntimeError("Oracle BI logon did not return a sessionID")
        self._session_id = sid_el.text.strip()

    def _logoff(self):
        if not (self._http and self._session_id):
            return
        try:
            body = f"<v12:logoff><v12:sessionID>{xml_escape(self._session_id)}</v12:sessionID></v12:logoff>"
            self._soap_call("nQSessionService", body)
        except Exception:
            pass
        finally:
            self._session_id = None

    def test_connection(self) -> Dict:
        try:
            self.connect()
        except Exception as e:
            return {"success": False, "message": f"Authentication failed: {e}"}

        try:
            names = self._list_subject_area_names()
        except Exception as e:
            return {
                "success": False,
                "connectivity": True,
                "message": f"Authenticated but failed to list subject areas: {e}",
            }

        msg = f"Connected to Oracle BI. Found {len(names)} subject area(s)."
        if not names:
            msg += " (Instance has no deployed semantic model — deploy an RPD or Semantic Model to enable queries.)"
        return {"success": True, "message": msg, "subject_areas": len(names)}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _list_subject_area_names(self) -> List[str]:
        """Return list of subject area names via MetadataService.getSubjectAreas."""
        self.connect()
        body = f"<v12:getSubjectAreas><v12:sessionID>{xml_escape(self._session_id)}</v12:sessionID></v12:getSubjectAreas>"
        root = self._soap_call("metadataService", body)
        names: List[str] = []
        for sa in root.findall(".//sawsoap:subjectArea", _NS):
            name_el = sa.find("sawsoap:name", _NS)
            if name_el is not None and (name_el.text or "").strip():
                names.append(name_el.text.strip())
        return names

    def _describe_subject_area(self, subject_area_name: str) -> Dict:
        """Describe one subject area with tables and columns."""
        self.connect()
        body = (
            f"<v12:describeSubjectArea>"
            f"<v12:subjectAreaName>{xml_escape(subject_area_name)}</v12:subjectAreaName>"
            f"<v12:detailsLevel>IncludeTablesAndColumns</v12:detailsLevel>"
            f"<v12:sessionID>{xml_escape(self._session_id)}</v12:sessionID>"
            f"</v12:describeSubjectArea>"
        )
        root = self._soap_call("metadataService", body)
        sa = root.find(".//sawsoap:subjectArea", _NS)
        if sa is None:
            return {}

        # Guard against the phantom-echo behaviour: when the subject area
        # does not exist the BI Server still echoes the name back with an
        # empty businessModel and no tables.
        business_model = (sa.findtext("sawsoap:businessModel", default="", namespaces=_NS) or "").strip()
        table_elements = sa.findall("sawsoap:tables", _NS)
        if not business_model and not table_elements:
            return {}

        display_name = (sa.findtext("sawsoap:displayName", default="", namespaces=_NS) or "").strip()
        description = (sa.findtext("sawsoap:description", default="", namespaces=_NS) or "").strip()

        tables: List[Dict] = []
        for tbl in table_elements:
            tbl_name = (tbl.findtext("sawsoap:name", default="", namespaces=_NS) or "").strip()
            tbl_display = (tbl.findtext("sawsoap:displayName", default="", namespaces=_NS) or "").strip()
            tbl_desc = (tbl.findtext("sawsoap:description", default="", namespaces=_NS) or "").strip()
            columns: List[Dict] = []
            for col in tbl.findall("sawsoap:columns", _NS):
                columns.append({
                    "name": (col.findtext("sawsoap:name", default="", namespaces=_NS) or "").strip(),
                    "display_name": (col.findtext("sawsoap:displayName", default="", namespaces=_NS) or "").strip(),
                    "data_type": (col.findtext("sawsoap:dataType", default="", namespaces=_NS) or "").strip() or "unknown",
                    "description": (col.findtext("sawsoap:description", default="", namespaces=_NS) or "").strip() or None,
                })
            tables.append({
                "name": tbl_name,
                "display_name": tbl_display or tbl_name,
                "description": tbl_desc or None,
                "columns": columns,
            })

        return {
            "name": (sa.findtext("sawsoap:name", default="", namespaces=_NS) or "").strip(),
            "display_name": display_name,
            "description": description or None,
            "business_model": business_model or None,
            "tables": tables,
        }

    def get_schemas(self) -> List[Table]:
        """Return one Table per presentation table across all subject areas."""
        tables: List[Table] = []
        for sa_name in self._list_subject_area_names():
            desc = self._describe_subject_area(sa_name)
            if not desc:
                continue
            sa_display = desc.get("display_name") or sa_name
            for t in desc.get("tables", []):
                table_display = t.get("display_name") or t.get("name") or ""
                full_name = f"{sa_display}/{table_display}".strip("/")
                cols = [
                    TableColumn(
                        name=c.get("display_name") or c.get("name") or "",
                        dtype=(c.get("data_type") or "unknown"),
                        description=c.get("description"),
                    )
                    for c in t.get("columns", [])
                    if (c.get("display_name") or c.get("name"))
                ]
                metadata_json = {
                    "oracle_bi": {
                        "subjectArea": sa_name,
                        "subjectAreaDisplayName": sa_display,
                        "tableName": t.get("name"),
                        "tableDisplayName": table_display,
                    }
                }
                tables.append(Table(
                    name=full_name,
                    description=t.get("description") or desc.get("description"),
                    columns=cols,
                    pks=[],
                    fks=[],
                    is_active=True,
                    metadata_json=metadata_json,
                ))
        return tables

    def get_schema(self, table_name: str) -> Table:
        """Resolve a single Table by name or by metadata identifiers."""
        all_tables = self.get_schemas()
        for tbl in all_tables:
            if tbl.name == table_name:
                return tbl
        for tbl in all_tables:
            meta = (tbl.metadata_json or {}).get("oracle_bi") or {}
            if meta.get("tableName") == table_name or meta.get("tableDisplayName") == table_name:
                return tbl
        for tbl in all_tables:
            meta = (tbl.metadata_json or {}).get("oracle_bi") or {}
            if meta.get("subjectArea") == table_name or meta.get("subjectAreaDisplayName") == table_name:
                return tbl
        raise RuntimeError(f"Table not found for '{table_name}'")

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(
        self,
        query: str,
        table_name: Optional[str] = None,
        max_rows: int = 10000,
    ) -> pd.DataFrame:
        """
        Execute a Logical SQL statement via XmlViewService.executeXMLQuery.

        Args:
            query: Logical SQL (uses presentation column names, e.g.
                   `SELECT "Products"."Product" FROM "A - Sample Sales"`).
            table_name: optional hint; not required because the subject area
                        is named directly in the Logical SQL.
            max_rows: maximum rows per page passed to the BI Server.
        """
        if not query or not query.strip():
            raise ValueError("Logical SQL query is required")

        self.connect()

        report_xml = (
            '<report xmlns="com.siebel.analytics.web/report/v1.1">'
            '<criteria xsi:type="sawx:expr" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<logicalSQL>{xml_escape(query)}</logicalSQL>"
            "</criteria>"
            "</report>"
        )
        body = (
            "<v12:executeXMLQuery>"
            "<v12:report><v12:reportXml>"
            f"<![CDATA[{report_xml}]]>"
            "</v12:reportXml></v12:report>"
            "<v12:outputFormat>SAWRowsetData</v12:outputFormat>"
            "<v12:executionOptions>"
            "<v12:async>false</v12:async>"
            f"<v12:maxRowsPerPage>{int(max_rows)}</v12:maxRowsPerPage>"
            "</v12:executionOptions>"
            f"<v12:sessionID>{xml_escape(self._session_id)}</v12:sessionID>"
            "</v12:executeXMLQuery>"
        )
        root = self._soap_call("xmlViewService", body)
        rowset_el = root.find(".//sawsoap:rowset", _NS)
        if rowset_el is None or not (rowset_el.text or "").strip():
            return pd.DataFrame()

        return self._parse_rowset(rowset_el.text)

    @staticmethod
    def _parse_rowset(rowset_xml: str) -> pd.DataFrame:
        """
        Parse a SAWRowsetData rowset XML string into a DataFrame.

        The BI Server returns an XMLA rowset with an inline xsd schema that
        defines the Row element and each column's type. Error payloads reuse
        the same <rowset> wrapper but contain free text with 'Error Codes:'
        instead of row elements — we detect and raise on those.
        """
        text = (rowset_xml or "").strip()
        if not text:
            return pd.DataFrame()

        try:
            rs_root = ET.fromstring(text)
        except ET.ParseError as e:
            raise RuntimeError(f"Failed to parse Oracle BI rowset: {e}")

        # Inline error: the BI Server stuffs "...Error Codes: XXXX..." as the
        # text content of the rowset when a Logical SQL fails, instead of
        # raising a SOAP fault.
        row_elements = rs_root.findall(f"{{{XMLA_ROWSET_NS}}}Row")
        if not row_elements and "Error Codes:" in text:
            err_text = (rs_root.text or "").strip() or text
            raise RuntimeError(f"Oracle BI query error: {err_text}")

        # Determine column ordering and XSD types from the inline schema.
        column_order: List[str] = []
        column_captions: Dict[str, str] = {}
        column_types: Dict[str, str] = {}
        for el in rs_root.iter(f"{{{XSD_NS}}}element"):
            name = el.get("name")
            if not name or name == "Row":
                continue
            column_order.append(name)
            xsd_type = el.get("type") or ""
            if xsd_type:
                column_types[name] = xsd_type.split(":", 1)[-1].lower()
            # Oracle exposes the user-facing caption via saw-sql:columnHeading
            for attr, val in el.attrib.items():
                if attr.endswith("columnHeading"):
                    column_captions[name] = val
                    break

        rows: List[Dict] = []
        for row_el in rs_root.findall(f"{{{XMLA_ROWSET_NS}}}Row"):
            row: Dict = {}
            for child in row_el:
                tag = child.tag.split("}", 1)[-1]
                row[tag] = child.text
            rows.append(row)

        if not rows:
            return pd.DataFrame(columns=column_order if column_order else None)

        df = pd.DataFrame(rows)
        if column_order:
            ordered = [c for c in column_order if c in df.columns]
            extra = [c for c in df.columns if c not in ordered]
            df = df[ordered + extra]

        for col, xsd_type in column_types.items():
            if col not in df.columns:
                continue
            if xsd_type in {"double", "float", "decimal"}:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            elif xsd_type in {"int", "integer", "long", "short", "byte",
                              "unsignedint", "unsignedlong", "unsignedshort"}:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
            elif xsd_type in {"datetime", "date"}:
                df[col] = pd.to_datetime(df[col], errors="coerce")
            elif xsd_type == "boolean":
                df[col] = df[col].map(lambda v: None if v is None else str(v).strip().lower() in {"true", "1"})

        if column_captions:
            df = df.rename(columns=column_captions)
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
            "Oracle BI Client: discover subject areas (MetadataService) and "
            "execute Logical SQL against the Oracle BI Server (XmlViewService). "
            "Works with OBIEE 11g/12c, Oracle Analytics Server, and Oracle "
            "Analytics Cloud."
        ) + self.system_prompt()

    def system_prompt(self) -> str:
        return """

## Oracle BI Logical SQL Guide

Execute Logical SQL queries against Oracle BI subject areas. The BI Server
translates Logical SQL into physical SQL against the underlying databases.

### Schema Structure

Each presentation table is exposed as a schema table named
`SubjectArea/PresentationTable`:
- `A - Sample Sales/Products`
- `A - Sample Sales/Base Facts`

The subject area quoting and table/column references live in
`metadata.oracle_bi.*` on each schema table. Pass the schema table name
(e.g., `A - Sample Sales/Products`) into `table_name` when you need to scope
a helper call, but the actual Logical SQL references the subject area
directly.

### Logical SQL Syntax

Logical SQL looks like SQL but references presentation columns qualified by
their presentation table, and the FROM clause names a subject area:

```sql
SELECT "Products"."Product", "Base Facts"."Revenue"
FROM "A - Sample Sales"
```

Double-quote any identifier that contains spaces, reserved words, or
mixed case — subject areas, tables, and columns almost always need quotes.

### Examples

```python
# Totals with grouping
df = client.execute_query('''
    SELECT "Products"."Product Category",
           SUM("Base Facts"."Revenue") AS "Revenue"
    FROM "A - Sample Sales"
    GROUP BY "Products"."Product Category"
    ORDER BY 2 DESC
    FETCH FIRST 10 ROWS ONLY
''')
```

```python
# Date filter (Logical SQL supports TIMESTAMPADD / CURRENT_DATE)
df = client.execute_query('''
    SELECT "Time"."Month",
           SUM("Base Facts"."Revenue") AS "Monthly Revenue"
    FROM "A - Sample Sales"
    WHERE "Time"."Date" >= TIMESTAMPADD(SQL_TSI_MONTH, -12, CURRENT_DATE)
    GROUP BY "Time"."Month"
    ORDER BY "Time"."Month"
''')
```

### Query Rules

- Always aggregate measures (SUM, COUNT, AVG); never SELECT raw measure
  columns without grouping.
- Use `FETCH FIRST N ROWS ONLY` for row limits; `LIMIT` is not supported.
- Double-quote identifiers with spaces or mixed case.
- Reference columns as `"Presentation Table"."Column"`; do NOT prefix with
  the subject area — the subject area goes in FROM.
- Joins happen implicitly via the semantic model; do not write JOIN clauses.
- Use BI Server SQL functions (`TIMESTAMPADD`, `DAYOFWEEK`, `FILTER`) rather
  than database-specific SQL.
"""

    # ------------------------------------------------------------------
    # SOAP transport
    # ------------------------------------------------------------------

    def _soap_call(self, service: str, body_xml: str) -> ET.Element:
        """
        POST a SOAP request to /analytics-ws/saw.dll?SoapImpl=<service> and
        return the parsed response root element. Raises on HTTP errors and
        SOAP faults.
        """
        if not self._http:
            self._http = requests.Session()

        url = f"{self.host}/analytics-ws/saw.dll?SoapImpl={service}"
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope '
            f'xmlns:soapenv="{SOAP_ENV_NS}" '
            f'xmlns:v12="{SOAP_NS}">'
            "<soapenv:Body>"
            f"{body_xml}"
            "</soapenv:Body>"
            "</soapenv:Envelope>"
        )
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""}
        resp = self._http.post(
            url,
            data=envelope.encode("utf-8"),
            headers=headers,
            timeout=self.timeout_sec,
            verify=self.verify_ssl,
        )
        if resp.status_code >= 300:
            raise RuntimeError(
                f"Oracle BI SOAP call failed: {service} HTTP {resp.status_code} {resp.text[:500]}"
            )

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise RuntimeError(f"Invalid SOAP response from {service}: {e}")

        fault = root.find(".//soap:Fault", _NS)
        if fault is not None:
            faultstring = (fault.findtext("faultstring") or "").strip()
            code = fault.find(".//{com.siebel.analytics.web/soap/error/v1}Code")
            code_text = (code.text.strip() if code is not None and code.text else "")
            msg = faultstring or "Oracle BI SOAP fault"
            if code_text:
                msg = f"{msg} [{code_text}]"
            raise RuntimeError(msg)

        return root
