"""Priority ERP (Priority Software) connector — OData v4 over the REST API.

Works against **both** Priority Cloud and on-premise installations: the OData
API ships with the on-prem application server (hosted under IIS), so the
protocol surface is identical and only the service root and TLS posture differ.

Design notes
------------
* **Forms are tables.** A Priority form is exposed as an OData entity set; each
  form column is a property. `$metadata` returns the whole tenant schema in one
  request, so a full catalog crawl is a single (large) HTTP call rather than
  Power BI's per-dataset fan-out. Priority warns that generating it "can take a
  while", hence the generous timeout and the per-entity `GetMetadataFor`
  fast-path for incremental refresh.

* **Titles come from annotations, not properties.** `<Property Name= Type=>`
  carries no human label. The business vocabulary lives in
  `<Annotation Term="Priority.OData.Description" String="Customer Number"/>`,
  which is what makes the catalog readable — parsing only `<Property>` would
  yield a catalog of bare column names. Titles follow the environment language
  (a Hebrew tenant returns Hebrew titles), so form *names* are always used as
  identifiers and titles only ever as descriptions.

* **Auth** (per prioritysoftware.github.io/restapi/authenticate/):
  PAT sends `Basic base64("<PAT>:PAT")` — token as username, literal "PAT" as
  password. Basic uses a dedicated API user. Per-user OAuth passes a Bearer
  token and is on-premise only.

* **Rate limits are a Cloud concern**: 100 calls/min/user, 15 concurrent, 3-min
  request ceiling, 429 on excess. On-prem has no documented ceiling, so the
  limiter is configurable rather than hardcoded.
"""
from __future__ import annotations

import base64
import re
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from typing import Dict, Generator, List, Optional
from urllib.parse import quote, urlsplit

import pandas as pd
import requests

from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ForeignKey, ServiceFormatter

# EDMX namespaces. Priority emits OData v4 CSDL.
_NS = {
    "edmx": "http://docs.oasis-open.org/odata/ns/edmx",
    "edm": "http://docs.oasis-open.org/odata/ns/edm",
}

# Annotation terms Priority adds on top of plain CSDL.
_TERM_DESCRIPTION = "Priority.OData.Description"   # the form column TITLE
_TERM_MANDATORY = "Priority.OData.Mandatory"       # v25.1+

# A curated starter set of the forms almost every Priority tenant has. Used when
# neither `forms` nor `discover_all` is set: a tenant can carry thousands of
# forms, and on a fresh connection there is no usage data to rank them by, so
# an unfiltered catalog buries the useful ones. Everything remains discoverable
# via `discover_all`.
_DEFAULT_FORMS = [
    "ORDERS", "ORDERITEMS", "CUSTOMERS", "PART", "AINVOICES", "AINVOICEITEMS",
    "PORDERS", "PORDERITEMS", "SUPPLIERS", "LOGPART", "DOCUMENTS", "FNCITEMS",
    "PRIT", "CUSTNOTES", "PARTBALANCE",
]

# Priority's per-request ceiling is 3 minutes; a full $metadata generation is
# the request most likely to approach it.
_METADATA_TIMEOUT = 190
_QUERY_TIMEOUT = 120

# Detects a SQL statement handed to `execute_query`. Priority form names are
# bare identifiers, so a leading SELECT/WITH can only be a dialect mix-up.
_SQL_PREFIX_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class _RateLimiter:
    """Token-bucket over a sliding 60s window.

    Priority Cloud enforces 100 calls/minute/user and answers 429 beyond it.
    On-premise installs have no documented ceiling, so `per_minute <= 0`
    disables throttling entirely rather than pacing a server that doesn't ask
    for it.
    """

    def __init__(self, per_minute: int):
        self.per_minute = int(per_minute or 0)
        self._times: List[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self.per_minute <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < 60.0]
                if len(self._times) < self.per_minute:
                    self._times.append(now)
                    return
                sleep_for = 60.0 - (now - self._times[0]) + 0.01
            time.sleep(max(sleep_for, 0.01))


class PriorityErpClient(DataSourceClient):
    """Query Priority ERP forms over the OData REST API."""

    def __init__(
        self,
        service_root: str,
        pat: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        access_token: Optional[str] = None,
        forms: Optional[str] = None,
        discover_all: bool = False,
        verify_ssl: bool = True,
        max_calls_per_minute: int = 100,
    ):
        self.service_root = (service_root or "").rstrip("/")
        self.pat = pat
        self.username = username
        self.password = password
        # Per-user delegated OAuth (on-prem / External ID). When present the
        # client authenticates with Bearer so Priority applies the signed-in
        # user's own permissions. Populated from user_connection_credentials;
        # construct_client passes the token fields the signature accepts.
        self.access_token = access_token
        self.forms = [f.strip().upper() for f in forms.split(",") if f.strip()] if forms else None
        self.discover_all = bool(discover_all)
        self.verify_ssl = bool(verify_ssl)
        self._limiter = _RateLimiter(max_calls_per_minute)
        self._schemas_cache: Optional[List[Table]] = None

    # ── connection ──────────────────────────────────────────────────────────

    def _auth_headers(self) -> Dict[str, str]:
        """Build the Authorization header for whichever mode is configured.

        PAT is checked first: it is the recommended server-to-server mode and
        Priority's docs are explicit that the token goes in the *username*
        position with the literal password "PAT".
        """
        if self.access_token:
            return {"Authorization": f"Bearer {self.access_token}"}
        if self.pat:
            raw = f"{self.pat}:PAT".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        if self.username or self.password:
            raw = f"{self.username or ''}:{self.password or ''}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        raise RuntimeError(
            "Priority ERP client has no credentials: provide a Personal Access "
            "Token, an API username/password, or sign in with Priority."
        )

    @contextmanager
    def connect(self) -> Generator[requests.Session, None, None]:
        if not self.service_root:
            raise RuntimeError("Priority ERP client requires a service_root URL.")
        session = requests.Session()
        session.verify = self.verify_ssl
        session.headers.update({"Accept": "application/json"})
        session.headers.update(self._auth_headers())
        try:
            yield session
        finally:
            session.close()

    def _get(self, session: requests.Session, path: str, params: Optional[dict] = None,
             timeout: int = _QUERY_TIMEOUT, accept: str = "application/json") -> requests.Response:
        self._limiter.acquire()
        url = f"{self.service_root}/{path.lstrip('/')}" if path else self.service_root
        resp = session.get(url, params=params, timeout=timeout, headers={"Accept": accept})
        if resp.status_code == 429:
            raise RuntimeError(
                "Priority rate limit hit (HTTP 429). Priority Cloud allows 100 calls/minute "
                "per user; lower Max Calls Per Minute or retry shortly."
            )
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Priority authentication failed (HTTP {resp.status_code}). Check the token/user "
                "has an API licence, and note Basic auth is rejected while External ID is enabled."
            )
        if resp.status_code == 404:
            raise RuntimeError(
                f"Priority returned 404 for '{path}'. Check the form name exists in this company "
                "(form names are case-sensitive in the URL) and that the service root is correct."
            )
        if resp.status_code == 501 or "$apply" in (url or ""):
            # Priority documents no $apply. The agent reaches for aggregation
            # naturally, so name the limitation and the workaround rather than
            # surfacing a bare "501 Not Implemented".
            raise RuntimeError(
                "Priority's OData API does not support $apply (server-side aggregation). "
                "Select the rows you need with $filter/$select and aggregate in code instead."
            )
        resp.raise_for_status()
        return resp

    # ── metadata parsing ────────────────────────────────────────────────────

    @staticmethod
    def _annotations(node: ET.Element) -> Dict[str, str]:
        """Collect `<Annotation Term=... String=|Bool=>` children into a dict.

        Priority carries the human-readable column title here rather than on the
        `<Property>` element itself, so this is where the catalog gets its
        business vocabulary.
        """
        out: Dict[str, str] = {}
        for ann in node.findall("edm:Annotation", _NS):
            term = ann.get("Term") or ""
            val = ann.get("String")
            if val is None:
                val = ann.get("Bool")
            if term and val is not None:
                out[term] = val
        return out

    def _parse_metadata(self, xml_text: str) -> Dict[str, Table]:
        """Turn an EDMX document into `{form_name: Table}`."""
        root = ET.fromstring(xml_text)
        tables: Dict[str, Table] = {}

        for entity in root.iter():
            if not entity.tag.endswith("}EntityType"):
                continue
            form = entity.get("Name")
            if not form:
                continue

            key_names = [
                pr.get("Name")
                for kr in entity.findall("edm:Key", _NS)
                for pr in kr.findall("edm:PropertyRef", _NS)
                if pr.get("Name")
            ]

            columns: List[TableColumn] = []
            mandatory: List[str] = []
            by_name: Dict[str, TableColumn] = {}
            for prop in entity.findall("edm:Property", _NS):
                name = prop.get("Name")
                if not name:
                    continue
                ann = self._annotations(prop)
                title = ann.get(_TERM_DESCRIPTION)
                is_mandatory = str(ann.get(_TERM_MANDATORY, "")).lower() == "true"
                if is_mandatory:
                    mandatory.append(name)
                meta: Dict[str, object] = {}
                if is_mandatory:
                    meta["mandatory"] = True
                if prop.get("MaxLength"):
                    meta["max_length"] = prop.get("MaxLength")
                col = TableColumn(
                    name=name,
                    dtype=prop.get("Type") or "Edm.String",
                    # The form column TITLE — what turns CUSTNAME into "Customer
                    # Number" for the agent. Language follows the environment.
                    description=title,
                    metadata=meta or None,
                )
                columns.append(col)
                by_name[name] = col

            # Navigation properties are Priority subforms (and related forms).
            # Modelled as foreign keys so the agent can see join paths.
            subforms: List[str] = []
            fks: List[ForeignKey] = []
            for nav in entity.findall("edm:NavigationProperty", _NS):
                nav_name = nav.get("Name")
                if not nav_name:
                    continue
                subforms.append(nav_name)
                target = (nav.get("Type") or "").replace("Collection(", "").rstrip(")")
                target = target.rsplit(".", 1)[-1] if target else nav_name
                anchor = by_name.get(key_names[0]) if key_names else (columns[0] if columns else None)
                if anchor is not None:
                    fks.append(ForeignKey(
                        column=anchor,
                        references_name=target,
                        references_column=anchor,
                    ))

            tables[form] = Table(
                name=form,
                description=self._annotations(entity).get(_TERM_DESCRIPTION),
                columns=columns,
                pks=[by_name[k] for k in key_names if k in by_name],
                fks=fks,
                metadata_json={"priority_erp": {
                    "form": form,
                    "subforms": subforms,
                    "mandatory": mandatory,
                    "company": self._company(),
                }},
            )
        return tables

    def _company(self) -> str:
        """The Priority company is the last segment of the service root."""
        return (urlsplit(self.service_root).path.rstrip("/").rsplit("/", 1) or [""])[-1]

    # ── DataSourceClient contract ───────────────────────────────────────────

    def test_connection(self) -> Dict:
        """Validate credentials and that the OData service answers.

        Failures are classified by which layer replied, mirroring the Power BI
        probe: an auth rejection is a credential problem, while any well-formed
        OData response proves auth, routing and read access all work.
        """
        if not self.service_root:
            return {"success": False, "message": "Service root URL is required."}
        if "/odata/" not in self.service_root.lower():
            return {
                "success": False,
                "message": (
                    "Service root does not look like a Priority OData URL — expected "
                    "https://<host>/odata/Priority/<tabula>.ini/<company>"
                ),
            }
        try:
            self._auth_headers()
        except RuntimeError as e:
            return {"success": False, "message": str(e)}

        try:
            with self.connect() as session:
                # A single-entity metadata request is the cheapest proof of life:
                # it exercises auth and routing without generating the whole
                # tenant schema (which Priority warns can be slow).
                resp = self._get(session, "$metadata", accept="application/xml",
                                 timeout=_METADATA_TIMEOUT)
                text = resp.text or ""
                if "EntityType" not in text:
                    return {
                        "success": False,
                        "message": "Connected, but $metadata returned no entity types. "
                                   "Check the company name in the service root.",
                    }
                forms = len(re.findall(r"<EntityType\b", text))
                return {
                    "success": True,
                    "message": f"Connected to Priority. {forms} form(s) visible in $metadata.",
                }
        except Exception as e:
            return {"success": False, "message": f"Connection failed: {e}"}

    def get_schemas(self, force_refresh: bool = False,
                    prior_tables: Optional[Dict[str, Dict]] = None) -> List[Table]:
        """Build one `Table` per Priority form from `$metadata`.

        `prior_tables` enables incremental discovery: forms already indexed are
        rebuilt from the stored definition and only new ones are re-read, which
        matters because full `$metadata` is a heavy request.
        """
        if self._schemas_cache is not None and not force_refresh:
            return self._schemas_cache

        with self.connect() as session:
            resp = self._get(session, "$metadata", accept="application/xml",
                             timeout=_METADATA_TIMEOUT)
            parsed = self._parse_metadata(resp.text or "")

        wanted = self._select_forms(parsed.keys())
        tables = [parsed[name] for name in wanted if name in parsed]

        # Reuse prior definitions for forms whose schema we already have, so an
        # interactive reload doesn't discard curated catalog state.
        if prior_tables:
            for t in tables:
                prior = prior_tables.get(t.name)
                if prior and prior.get("columns") and not t.columns:
                    t.columns = prior["columns"]

        self._schemas_cache = tables
        return tables

    def _select_forms(self, available) -> List[str]:
        available = list(available)
        if self.forms:
            wanted = {f.upper() for f in self.forms}
            return [f for f in available if f.upper() in wanted]
        if self.discover_all:
            return available
        curated = {f.upper() for f in _DEFAULT_FORMS}
        picked = [f for f in available if f.upper() in curated]
        # A tenant that shares none of the curated names (heavily customised, or
        # non-English) would otherwise index nothing at all.
        return picked or available

    def get_schema(self, table_name: str) -> Table:
        for t in self.get_schemas():
            if t.name.lower() == (table_name or "").lower():
                return t
        # Not in the selected set — fetch just this form (v25.0+) rather than
        # regenerating the whole tenant schema.
        with self.connect() as session:
            resp = self._get(
                session,
                f"GetMetadataFor(entity='{quote(table_name, safe='')}')",
                accept="application/xml",
            )
            parsed = self._parse_metadata(resp.text or "")
        for name, table in parsed.items():
            if name.lower() == (table_name or "").lower():
                return table
        raise ValueError(f"Unknown Priority form: {table_name}")

    def execute_query(
        self,
        query: str,
        table_name: Optional[str] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """Run an OData query against a form and return a DataFrame.

        `query` is either a bare form name ("ORDERS") or a form with OData
        query options ("ORDERS?$filter=CUSTNAME eq '1011'&$top=10"). Priority
        supports $filter/$select/$expand/$orderby/$top/$skip and its own
        $since; it does NOT document $apply, so aggregation is not pushed down.
        """
        target = (query or table_name or "").strip()
        if not target:
            raise ValueError("A Priority form name (optionally with OData query options) is required.")
        target = target.lstrip("/")

        # A SQL string would otherwise be percent-encoded into the URL path and
        # come back as an opaque HTTP 400. Naming the mismatch gives the codegen
        # retry loop something it can actually act on.
        if _SQL_PREFIX_RE.match(target):
            raise ValueError(
                "Priority is queried with OData, not SQL. Pass a form name with OData query "
                "options instead of a SELECT statement — e.g. "
                "\"CUSTOMERS?$select=CUSTNAME,CDES&$top=10\" or "
                "\"ORDERS?$filter=CUSTNAME eq '1011'&$orderby=CURDATE desc\". "
                "Supported options: $filter, $select, $expand, $orderby, $top, $skip, $since."
            )

        path, _, query_string = target.partition("?")
        params: Dict[str, str] = {}
        if max_rows and "$top" not in query_string:
            params["$top"] = str(int(max_rows))

        with self.connect() as session:
            url_path = f"{path}?{query_string}" if query_string else path
            resp = self._get(session, url_path, params=params or None)
            payload = resp.json()

        rows = payload.get("value")
        if rows is None:
            # A single-entity read returns the entity itself, not a collection.
            rows = [] if not isinstance(payload, dict) else [
                {k: v for k, v in payload.items() if not k.startswith("@odata")}
            ]
        df = pd.DataFrame(rows)
        # Subforms arrive as nested lists of dicts; leaving them raw makes the
        # frame unusable in downstream code, so summarise to a count.
        for col in df.columns:
            if df[col].apply(lambda v: isinstance(v, (list, dict))).any():
                df[col] = df[col].apply(
                    lambda v: len(v) if isinstance(v, list) else (v if not isinstance(v, dict) else str(v))
                )
        if max_rows:
            df = df.head(int(max_rows))
        return df

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    @property
    def description(self) -> str:
        # `description` is the ONLY channel that carries per-connector query
        # guidance into the code-generation prompt (it is read as-is in
        # build_codegen_context and rendered under <connection_clients>).
        # Without the guide appended, the coder falls back to the prompt's
        # SQL-shaped defaults and emits SELECT statements, which this client
        # then percent-encodes into the OData URL path.
        company = self._company() or "unknown company"
        text = f"Priority ERP (OData) — company '{company}' at {self.service_root}"
        return text + "\n\n" + self.system_prompt()

    def system_prompt(self) -> str:
        return """
## Priority ERP Query Guide

Query Priority ERP forms over the OData v4 REST API. Each schema table is a
Priority **form** (ORDERS, CUSTOMERS, PART …) and each column is a form column.

**This is NOT a SQL data source.** `execute_query` takes an OData URL fragment,
not SQL — the string is appended to the service root as a URL path. Any
`SELECT ... FROM ...` is percent-encoded into the URL and fails with HTTP 400.
`execute_query` takes exactly one positional argument; there is no
`query_params`, no bind parameters, and no second argument.

### How to Execute Queries

`execute_query(query)` where `query` is a form name plus optional OData query
options:

```
ORDERS
ORDERS?$filter=CUSTNAME eq '1011'&$top=50
ORDERS?$select=ORDNAME,CUSTNAME,CURDATE&$orderby=CURDATE desc
ORDERS?$expand=ORDERITEMS_SUBFORM($select=PARTNAME,PRICE)
```

**Always push `$select` and `$top` into the query.** A bare form name
(`execute_query("CUSTOMERS")`) downloads every row of every column before any
pandas filtering runs — on a real tenant that is minutes of transfer for ten
rows. Ask the server for what you need:

```
CUSTOMERS?$select=CUSTNAME,CDES,PHONE,EMAIL&$top=10
```

Never fetch a whole form and then `.head(n)` it in pandas.

### Supported query options

`$filter`, `$select`, `$expand`, `$orderby`, `$top`, `$skip`, and Priority's
`$since` (records changed since a UTC timestamp, e.g. `$since=2026-01-01T00:00:00Z`).

**There is no `$apply`** — Priority does not support server-side aggregation.
To aggregate, select the rows you need with `$filter`/`$select` and aggregate
in code. Keep `$top` tight: every row crosses the network.

### Important details

- String literals use single quotes: `$filter=CUSTNAME eq '1011'`.
- Decimals always use a period, regardless of locale.
- Dates are DateTimeOffset: `$filter=CURDATE ge 2026-01-01T00:00:00Z`.
- **Calculated columns are not stored** and cannot be used in `$filter` or
  `$orderby` — read them from the returned rows instead.
- Subforms are navigation properties (`ORDERITEMS_SUBFORM`) reached via
  `$expand`; with composite keys, `$select` must include all parent key fields.
- Column *titles* are in each column's description — use them to interpret
  cryptic names like CUSTNAME, and always query by the column **name**.
- Priority Cloud allows 100 calls/minute; prefer one well-filtered query over
  many small ones.
"""
