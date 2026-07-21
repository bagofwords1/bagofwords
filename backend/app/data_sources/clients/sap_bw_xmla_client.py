from app.data_sources.clients.xmla_base import XmlaClient
from typing import Optional
from urllib.parse import urlencode


class SapBwXmlaClient(XmlaClient):
    """SAP BW / BW4HANA client over the XML for Analysis (XMLA) interface.

    BW ships a standard **XMLA** provider as a SOAP-over-HTTP ICF service at
    ``/sap/bw/xml/soap/xmla`` that wraps the OLAP processor. It is the *same*
    SOAP contract every other XMLA source speaks (SSAS, Infor OLAP), so all the
    transport, Discover (catalogs/cubes/measures/hierarchies) and Execute (MDX)
    machinery is inherited unchanged from ``XmlaClient``. Only the endpoint
    construction and the BW-flavored MDX guidance are BW-specific.

    Why XMLA over HTTP (not RFC): the OLAP **BAPIs** (``RSR_OLAP*``) require the
    proprietary, non-redistributable NetWeaver RFC SDK. The XMLA web service is
    plain HTTP/SOAP — no SDK — and, unlike the BW-OData/EasyQuery path, has no
    "one structure per query" limitation.

    Per-user security: BW **analysis authorizations** (RSECADMIN) are enforced
    only when the query runs under the end user's SAP identity. This client
    authenticates with Basic auth using the *per-user* SAP credentials the
    connection layer resolves (auth_policy ``user_required``), so each user sees
    only the data their analysis authorizations permit. (SSO2 ticket / Kerberos
    propagation are future auth variants; Basic-per-user is the baseline.)

    The queryable "objects" are BW **InfoProviders / BEx queries** exposed as
    XMLA cubes; each becomes one schema table named ``Catalog/Cube`` whose
    columns are the cube's characteristics (dimensions) and key figures
    (measures).
    """

    META_KEY = "sap_bw"
    PRODUCT_NAME = "SAP BW (XMLA)"
    EMPTY_NOTE = (
        "No InfoProviders/queries visible to this user — check the user's "
        "analysis authorizations and that queries are released for external access."
    )
    QUERY_REQUIRED_MSG = "An MDX query is required"
    PATH_HINT = "For SAP BW the path is usually /sap/bw/xml/soap/xmla."

    # Default ICF path of the BW XMLA web service.
    DEFAULT_XMLA_PATH = "/sap/bw/xml/soap/xmla"

    def __init__(
        self,
        host: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        xmla_path: Optional[str] = None,
        sap_client: Optional[str] = None,
        sap_language: Optional[str] = None,
        catalog: Optional[str] = None,
        verify_ssl: bool = True,
        timeout_sec: int = 60,
    ):
        """
        Args:
            host: Base server URL or full XMLA endpoint URL. A bare host
                (``bw.example.com:44300``) or origin (``https://bw…:44300``) has
                the XMLA path appended; a URL that already contains the XMLA path
                is used verbatim.
            xmla_path: Override the ICF path (default ``/sap/bw/xml/soap/xmla``).
            sap_client: Optional SAP client/mandant (e.g. ``100``) — appended as
                ``sap-client=`` on the endpoint when set.
            sap_language: Optional logon language (e.g. ``EN``) — appended as
                ``sap-language=`` when set.
        """
        path = "/" + (xmla_path or self.DEFAULT_XMLA_PATH).strip().strip("/")
        endpoint = self._build_endpoint(host, path, sap_client, sap_language)
        super().__init__(
            host=endpoint,
            username=username,
            password=password,
            catalog=catalog,
            verify_ssl=verify_ssl,
            timeout_sec=timeout_sec,
        )
        self.sap_client = (sap_client or "").strip() or None
        self.sap_language = (sap_language or "").strip() or None

    @staticmethod
    def _build_endpoint(host: str, path: str, sap_client: Optional[str],
                        sap_language: Optional[str]) -> str:
        """Normalize ``host`` to a full XMLA endpoint URL (https by default),
        appending the XMLA path unless one is already present, plus optional
        ``sap-client`` / ``sap-language`` query parameters."""
        h = (host or "").strip().rstrip("/")
        if not h:
            return h
        if not (h.startswith("http://") or h.startswith("https://")):
            h = f"https://{h}"
        # Preserve an explicit endpoint the caller already fully specified.
        origin, sep, existing_qs = h.partition("?")
        if path.rstrip("/") not in origin:
            origin = origin + path
        params = {}
        if sap_client and (sap_client or "").strip():
            params["sap-client"] = str(sap_client).strip()
        if sap_language and (sap_language or "").strip():
            params["sap-language"] = str(sap_language).strip()
        qs = existing_qs
        if params:
            extra = urlencode(params)
            qs = f"{existing_qs}&{extra}" if existing_qs else extra
        return f"{origin}?{qs}" if qs else origin

    @property
    def description(self) -> str:
        return (
            "SAP BW (XMLA) client: discover InfoProviders/BEx queries as cubes "
            "(XMLA Discover) and execute MDX against the BW OLAP processor (XMLA "
            "Execute) over the /sap/bw/xml/soap/xmla web service."
        ) + self.system_prompt()

    def system_prompt(self) -> str:
        return """

## SAP BW (XMLA / MDX) Query Guide

Query SAP BW InfoProviders and BEx queries over XMLA using **MDX**. There is no
SQL and no DAX — BW's OLAP processor parses MDX and enforces the user's
**analysis authorizations** server-side, so results already respect row-level
security.

### Schema Structure

Each InfoProvider/query is exposed as a schema table named `Catalog/Cube`:
- Columns with `dtype="dimension"` are **characteristics** (groupable
  attributes); `dtype="measure"` are **key figures** (aggregated values).
- Each column's query identifier is in `metadata.unique_name` — reference
  members/measures by that unique name in MDX.

### How to Execute Queries

**Signature**: `execute_query(mdx, table_name)` — pass the MDX statement first
and the `Catalog/Cube` schema table name second (used to resolve the catalog).

```python
df = db_clients['sap_bw'].execute_query(
    '''
    SELECT { [Measures].[4GBQ8ZW...] } ON COLUMNS,
           NON EMPTY { [0D_NW_C01__ZCOUNTRY].[LEVEL01].MEMBERS } ON ROWS
    FROM [0D_NW_C01/0D_NW_C01_Q001]
    ''',
    "0D_NW_C01/0D_NW_C01_Q001"
)
```

### Rules
- FROM names the cube/query (BW cube unique names are bracketed, e.g.
  `FROM [InfoProvider/Query]`); put key figures on one axis and characteristic
  members on the other; use `NON EMPTY` to drop empty tuples.
- Reference characteristics and key figures by their `metadata.unique_name`
  exactly as shown in the schema — BW's technical names are not guessable.
- Prefer a released BEx query's cube over a raw InfoProvider when available; it
  carries the intended restricted/calculated key figures.
- Analysis authorizations are applied automatically for the signed-in user — do
  not attempt to re-filter for security in MDX.
"""


# Compatibility alias for any dynamic resolver expecting 'SAPBwXmlaClient'.
SAPBwXmlaClient = SapBwXmlaClient
