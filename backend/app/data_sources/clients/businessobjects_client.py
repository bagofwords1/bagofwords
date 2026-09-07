from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
from typing import List, Dict, Optional, Any
import requests
import pandas as pd
import logging
import xml.etree.ElementTree as ET
from collections import Counter
from urllib.parse import quote


# BusinessObjects Semantic-Layer object kinds. Dimensions/attributes are
# groupable; measures aggregate. Filters/predefined conditions are not result
# columns, so they're skipped when building the schema.
_MEASURE_KINDS = {"measure"}
_DIMENSION_KINDS = {"dimension", "attribute", "detail"}


class BusinessObjectsClient(DataSourceClient):
    """SAP BusinessObjects (BOBJ) client over the RESTful Web Service SDK
    (``/biprws``).

    Discovers **universes** (the semantic layer, ``.unx``) via the Semantic
    Layer REST API and exposes each as one schema table whose columns are the
    universe's dimensions/attributes (``role=dimension``) and measures
    (``role=measure``). Queries run against a universe and BusinessObjects
    applies the universe's data/business security profiles + CMS object rights
    for the **logged-on named user**, so results respect per-user security.

    Auth is resolved by the connection layer and is one of, in priority order:

    * **pre-obtained logon token** (``logon_token``) — a per-user session token
      the platform already minted (delegated/OBO). Used verbatim; no logon.
    * **trusted authentication** (``shared_secret`` + ``trusted_user``) — the
      platform asserts an already-authenticated *named* user WITHOUT their
      password (CMC → Authentication → Enterprise → Trusted Authentication).
      This is the SSO-agnostic per-user path: it works whatever SSO (Kerberos,
      SAML, SAP) BusinessObjects itself is configured for.
    * **username/password** with an ``auth_type`` plugin (``secEnterprise`` |
      ``secLDAP`` | ``secWinAD`` | ``secSAPR3``) — resolves to a named CMS user.

    All calls carry the session token in the ``X-SAP-LogonToken`` header
    (double-quoted per the BI Platform contract).
    """

    # Auth-plugin identifiers accepted by /logon/long.
    VALID_AUTH_TYPES = {"secEnterprise", "secLDAP", "secWinAD", "secSAPR3"}

    def __init__(
        self,
        host: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        auth_type: str = "secEnterprise",
        trusted_user: Optional[str] = None,
        shared_secret: Optional[str] = None,
        logon_token: Optional[str] = None,
        base_path: str = "/biprws",
        page_size: int = 50,
        verify_ssl: bool = True,
        timeout_sec: int = 60,
    ):
        self.base_url = self._build_base_url(host, base_path)
        self.username = username
        self.password = password
        self.auth_type = (auth_type or "secEnterprise").strip() or "secEnterprise"
        self.trusted_user = (trusted_user or "").strip() or None
        self.shared_secret = shared_secret or None
        # A pre-minted per-user session token wins outright.
        self._logon_token: Optional[str] = (logon_token or "").strip() or None
        self.page_size = max(1, int(page_size or 50))
        self.verify_ssl = verify_ssl
        self.timeout_sec = timeout_sec

        self._http: Optional[requests.Session] = None
        # Whether _logon_token was supplied (don't logoff someone else's session).
        self._own_session = self._logon_token is None
        # Discovery cache — get_schemas() is a universe crawl.
        self._schemas_cache: Optional[List[Table]] = None

    @staticmethod
    def _build_base_url(host: str, base_path: str) -> str:
        """Normalize to ``https://host[:port]/biprws`` (append the base path
        only when the host doesn't already include it)."""
        h = (host or "").strip().rstrip("/")
        if not h:
            return h
        if not (h.startswith("http://") or h.startswith("https://")):
            h = f"https://{h}"
        bp = "/" + (base_path or "/biprws").strip().strip("/")
        if not h.endswith(bp) and bp.strip("/") not in h.split("://", 1)[-1]:
            h = h + bp
        return h

    # ------------------------------------------------------------------
    # Session / auth
    # ------------------------------------------------------------------

    def _session(self) -> requests.Session:
        if self._http is None:
            self._http = requests.Session()
            self._http.verify = self.verify_ssl
        return self._http

    def _logon(self) -> str:
        """Obtain (and cache) an ``X-SAP-LogonToken``.

        A pre-supplied token short-circuits. Otherwise trusted auth (shared
        secret asserting a named user) is tried when configured, else a standard
        username/password logon against the selected auth plugin.
        """
        if self._logon_token:
            return self._logon_token

        url = f"{self.base_url}/logon/long"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}

        if self.shared_secret:
            # Trusted authentication: the shared secret authenticates the calling
            # application; X-SAP-TRUSTED-USER names the user to impersonate. No
            # user password is sent. (Header carrier for the secret is
            # deployment-specific; X-SAP-TRUSTED-AUTH is the documented default.)
            user = self.trusted_user or self.username
            if not user:
                raise RuntimeError(
                    "Trusted authentication requires a user to impersonate "
                    "(trusted_user)."
                )
            headers["X-SAP-TRUSTED-USER"] = user
            headers["X-SAP-TRUSTED-AUTH"] = self.shared_secret
            resp = self._session().post(
                url, json={}, headers=headers, timeout=self.timeout_sec
            )
        else:
            if not (self.username and self.password):
                raise RuntimeError(
                    "username and password are required (or configure trusted "
                    "authentication / supply a logon token)."
                )
            if self.auth_type not in self.VALID_AUTH_TYPES:
                raise RuntimeError(
                    f"Unsupported auth_type '{self.auth_type}'. Use one of: "
                    f"{', '.join(sorted(self.VALID_AUTH_TYPES))}."
                )
            body = {
                "userName": self.username,
                "password": self.password,
                "auth": self.auth_type,
            }
            resp = self._session().post(
                url, json=body, headers=headers, timeout=self.timeout_sec
            )

        if resp.status_code >= 300:
            raise RuntimeError(
                f"Logon failed: HTTP {resp.status_code} {self._body_snippet(resp)}"
            )
        token = self._extract_token(resp)
        if not token:
            raise RuntimeError("Logon succeeded but no X-SAP-LogonToken was returned.")
        self._logon_token = token
        return token

    @staticmethod
    def _extract_token(resp) -> Optional[str]:
        """Read the token from the ``X-SAP-LogonToken`` header, falling back to
        a ``logonToken`` field in the JSON body. Strip any surrounding quotes so
        we control the (required) quoting on reuse."""
        token = None
        try:
            token = resp.headers.get("X-SAP-LogonToken")
        except Exception:
            token = None
        if not token:
            try:
                payload = resp.json() or {}
                token = payload.get("logonToken") or payload.get("logontoken")
            except Exception:
                token = None
        if token:
            token = token.strip().strip('"')
        return token or None

    def connect(self):
        """Prime the session token (surfaces auth errors early)."""
        self._logon()

    def _headers(self, accept: str = "application/json") -> Dict[str, str]:
        # The BI Platform contract requires the token value to be double-quoted.
        return {
            "X-SAP-LogonToken": f'"{self._logon()}"',
            "Accept": accept,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _body_snippet(resp) -> str:
        try:
            return (resp.text or "")[:300]
        except Exception:
            return ""

    def _get(self, path: str, **kwargs):
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        return self._session().get(url, headers=self._headers(), timeout=self.timeout_sec, **kwargs)

    def _post(self, path: str, json_body: Any = None, **kwargs):
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        return self._session().post(
            url, headers=self._headers(), json=json_body, timeout=self.timeout_sec, **kwargs
        )

    # ------------------------------------------------------------------
    # Discovery — universes
    # ------------------------------------------------------------------

    def get_schemas(self) -> List[Table]:
        return self.get_tables()

    def get_tables(self) -> List[Table]:
        """One BOW Table per universe; columns are the universe's dimensions/
        attributes (role=dimension) and measures (role=measure)."""
        if self._schemas_cache is not None:
            return self._schemas_cache

        tables: List[Table] = []
        for uni in self._list_universes():
            uid = str(uni.get("id") or "").strip()
            name = uni.get("name") or ""
            if not uid or not name:
                continue
            folder = uni.get("folderPath") or uni.get("path") or uni.get("folderName") or None
            columns = self._universe_columns(uid)
            tables.append(Table(
                name=name,
                description=folder,
                columns=columns,
                pks=[],
                fks=[],
                is_active=True,
                metadata_json={"businessobjects": {
                    "universe_id": uid,
                    "universe_name": name,
                    "folder": folder,
                    "type": uni.get("type") or None,
                }},
            ))
        self._schemas_cache = tables
        return tables

    def _list_universes(self) -> List[Dict]:
        """Enumerate universes via ``GET /sl/v1/universes`` with offset paging.

        SAP wraps collections as ``{"universes": {"universe": [...]}}`` and
        collapses a single element to a dict rather than a list — normalized
        here."""
        universes: List[Dict] = []
        offset = 0
        pages = 0
        while pages < 1000:
            resp = self._get(f"/sl/v1/universes?offset={offset}&limit={self.page_size}")
            if resp.status_code >= 300:
                raise RuntimeError(
                    f"Universe listing failed: HTTP {resp.status_code} {self._body_snippet(resp)}"
                )
            payload = resp.json() or {}
            batch = _as_list(_dig(payload, "universes", "universe"))
            for u in batch:
                if isinstance(u, dict):
                    universes.append(u)
            pages += 1
            if len(batch) < self.page_size:
                break
            offset += self.page_size
        return universes

    def _universe_columns(self, universe_id: str) -> List[TableColumn]:
        """Fetch the outline; never cache an incomplete schema after a failure."""
        resp = self._get(f"/sl/v1/universes/{universe_id}")
        self._check_response(resp, "Universe detail")
        payload = resp.json()

        root = payload.get("universe") if isinstance(payload, dict) else None
        root = root if isinstance(root, dict) else payload
        columns: List[TableColumn] = []
        seen = set()
        objects = list(_walk_universe_objects(root))
        counts = Counter(o.get("name") for o in objects)
        for obj in objects:
            oname = obj.get("name") or obj.get("id")
            identity = obj.get("id") or obj.get("path") or oname
            if not oname or identity in seen:
                continue
            kind = str(obj.get("@type") or obj.get("type") or obj.get("qualification") or "").lower()
            if kind in _MEASURE_KINDS:
                role, dtype = "measure", "measure"
            elif kind in _DIMENSION_KINDS:
                role, dtype = "dimension", _bo_dtype(obj.get("@dataType") or obj.get("dataType"))
            else:
                # Skip filters / conditions / folders that aren't result objects.
                continue
            seen.add(identity)
            if counts[oname] > 1:
                oname = f"{oname} [{identity}]"
            columns.append(TableColumn(
                name=oname,
                dtype=dtype,
                description="; ".join(filter(None, [obj.get("description"), f"Universe path: {obj['path']}" if obj.get("path") else None])) or None,
                metadata={"role": role, "object_id": obj.get("id"),
                          "path": obj.get("path"),
                          "data_type": obj.get("@dataType") or obj.get("dataType")},
            ))
        return columns

    def get_schema(self, table_name: str) -> Table:
        for t in self.get_schemas():
            if t.name == table_name:
                return t
        for t in self.get_schemas():
            meta = (t.metadata_json or {}).get("businessobjects") or {}
            if meta.get("universe_id") == table_name:
                return t
        raise RuntimeError(f"Universe not found for '{table_name}'")

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(
        self,
        query: Optional[str] = None,
        table_name: Optional[str] = None,
        select: Optional[str] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """Run a query against a universe and return the rows as a DataFrame.

        Following the framework's positional convention (like the Power BI /
        Datasphere clients), the primary form is
        ``execute_query("Obj1,Obj2,Measure", "Universe Name")`` where the first
        argument lists the universe **result objects** to return (comma
        separated, by name exactly as shown in the schema) and the second is the
        universe/table name. The named ``select`` kwarg is an alternative.

        BusinessObjects creates a query on the universe and returns a flattened
        result set; the query runs under the logged-on named user, so universe
        security profiles apply.
        """
        # Robustness: if the first positional arg is actually the universe name
        # (matches a known universe) and table_name is empty, treat it as the
        # target — the result objects then come from the `select` kwarg.
        if query and not table_name and self._resolve_universe(query):
            table_name = query
            query = None

        meta = self._resolve_universe(table_name)
        if not meta:
            raise ValueError(
                "execute_query needs a target universe: pass table_name exactly "
                "as shown in the schema."
            )

        result_objects = _split_objects(select or query)
        if not result_objects:
            raise ValueError(
                "No result objects specified — pass the universe object names to "
                "return (comma separated) as the first argument or via select=."
            )

        rows = self._run_universe_query(meta["universe_id"], result_objects, max_rows)
        if not rows:
            return pd.DataFrame(columns=result_objects)
        df = pd.DataFrame(rows)
        if max_rows is not None and max_rows > 0 and len(df) > max_rows:
            df = df.head(max_rows)
        return df

    def _resolve_universe(self, table_name: Optional[str]) -> Optional[Dict]:
        if not table_name:
            return None
        try:
            t = self.get_schema(table_name)
        except Exception:
            return None
        return (t.metadata_json or {}).get("businessobjects")

    def _run_universe_query(
        self, universe_id: str, result_objects: List[str], max_rows: Optional[int]
    ) -> List[Dict]:
        """Use the BO 4.2/4.3 XML query and OData result lifecycle."""
        table = self.get_schema(universe_id)
        by_name = {column.name: column for column in table.columns}
        query = ET.Element("query", {
            "xmlns": "http://www.sap.com/rws/sl/universe",
            "dataSourceType": (table.metadata_json["businessobjects"].get("type") or "unx"),
            "dataSourceId": universe_id,
        })
        spec = ET.SubElement(query, "querySpecification", {"version": "1.0"})
        results = ET.SubElement(ET.SubElement(spec, "queryData"), "resultObjects")
        if len(set(result_objects)) != len(result_objects):
            raise ValueError("Select each result object only once.")
        for name in result_objects:
            column = by_name.get(name)
            if column is None or not column.metadata.get("object_id"):
                raise ValueError(f"Unknown or unavailable result object: {name}")
            attrs = {"id": str(column.metadata["object_id"])}
            if column.metadata.get("path"):
                attrs["path"] = column.metadata["path"]
            ET.SubElement(results, "resultObject", attrs)
        headers = self._headers("application/xml")
        headers["Content-Type"] = "application/xml"
        response = self._session().post(
            f"{self.base_url}/sl/v1/queries", data=ET.tostring(query),
            headers=headers, timeout=self.timeout_sec,
        )
        self._check_response(response, "Query creation")
        root = ET.fromstring(response.text)
        query_id = next((e.text for e in root.iter() if e.tag.split("}")[-1] == "id"), None)
        if not query_id:
            raise RuntimeError("SAP did not return a query identifier.")
        path = f"{self.base_url}/sl/v1/queries/{quote(query_id, safe='')}"
        try:
            response = self._session().get(path + "/data.svc", headers=headers, timeout=self.timeout_sec)
            self._check_response(response, "Query execution (check required prompts/contexts)")
            service = ET.fromstring(response.text)
            flows = [e.attrib["href"] for e in service.iter()
                     if e.tag.split("}")[-1] == "collection" and "href" in e.attrib]
            if not flows:
                raise RuntimeError("SAP returned no result flow.")
            if len(flows) > 1:
                response = self._session().get(path + "/data.svc/$metadata", headers=headers, timeout=self.timeout_sec)
                self._check_response(response, "Result metadata")
                metadata = ET.fromstring(response.text)
                groups = []
                for entity in metadata.iter():
                    if entity.tag.split("}")[-1] != "EntityType":
                        continue
                    labels = []
                    for prop in entity:
                        if prop.tag.split("}")[-1] == "Property":
                            for annotation in prop:
                                if annotation.attrib.get("Term") == "sap.label":
                                    labels.extend(e.text for e in annotation if e.text)
                    if labels:
                        groups.append(", ".join(labels))
                raise ValueError(
                    "SAP returned separate result flows with different grains. "
                    "Query one compatible group at a time; do not join by OData Id. "
                    "Available result groups: " + "; ".join(groups)
                )
            flow = flows[0]
            if not flow or any(c in flow for c in "/:?\\"):
                raise RuntimeError("Invalid SAP result flow name.")
            response = self._session().get(path + "/data.svc/$metadata", headers=headers, timeout=self.timeout_sec)
            self._check_response(response, "Result metadata")
            metadata = ET.fromstring(response.text)
            entity_name = next((e.attrib.get("EntityType", "").split(".")[-1]
                                for e in metadata.iter() if e.tag.split("}")[-1] == "EntitySet"
                                and e.attrib.get("Name") == flow), None)
            entity = next((e for e in metadata.iter() if e.tag.split("}")[-1] == "EntityType"
                           and e.attrib.get("Name") == entity_name), None)
            if entity is None:
                raise RuntimeError("SAP result metadata is missing the flow entity type.")
            keys = {e.attrib["Name"] for e in entity.iter() if e.tag.split("}")[-1] == "PropertyRef"}
            # SAP exposes selected objects in query order, with an extra OData key.
            fields = [e.attrib["Name"] for e in entity if e.tag.split("}")[-1] == "Property"
                      and e.attrib["Name"] not in keys]
            if len(fields) != len(result_objects):
                raise RuntimeError("SAP result columns do not match selected objects.")
            response = self._session().get(
                path + "/data.svc/" + quote(flow, safe="") + "/$count",
                headers=self._headers("text/plain"), timeout=self.timeout_sec,
            )
            self._check_response(response, "Result row count")
            total = int(response.text.strip())
            if total < 0:
                raise RuntimeError("SAP returned an invalid row count.")
            rows = []
            limit = min(total, max_rows) if max_rows is not None and max_rows > 0 else total
            if limit == 0:
                return rows
            for _ in range(100000):
                size = min(self.page_size, limit - len(rows)) if limit else self.page_size
                response = self._session().get(
                    path + "/data.svc/" + quote(flow, safe=""), headers=self._headers(),
                    params={"$skip": len(rows), "$top": size}, timeout=self.timeout_sec,
                )
                self._check_response(response, "Result retrieval")
                payload = response.json()
                batch = payload.get("value")
                if batch is None and isinstance(payload.get("d"), dict):
                    batch = payload["d"].get("results")
                if not isinstance(batch, list):
                    raise RuntimeError("SAP returned an unsupported OData result shape.")
                if not batch:
                    raise RuntimeError("SAP ended the result before its reported row count.")
                for row in batch[:size]:
                    if not isinstance(row, dict) or any(field not in row for field in fields):
                        raise RuntimeError("SAP result row is missing selected fields.")
                    rows.append(dict(zip(result_objects, (row[field] for field in fields))))
                if limit and len(rows) >= limit:
                    return rows
            raise RuntimeError("SAP result exceeded the pagination safety limit.")
        finally:
            # Preserve the original query error if cleanup also fails.
            try:
                response = self._session().delete(path, headers=headers, timeout=self.timeout_sec)
                self._check_response(response, "Query cleanup")
            except Exception:
                logging.getLogger(__name__).warning("Could not delete temporary SAP query")

    @staticmethod
    def _check_response(response, operation: str):
        if response.status_code >= 300:
            raise RuntimeError(
                f"{operation} failed: HTTP {response.status_code} "
                f"{BusinessObjectsClient._body_snippet(response)}"
            )

    # ------------------------------------------------------------------
    # Connection test & prompt
    # ------------------------------------------------------------------

    def test_connection(self) -> Dict:
        try:
            self.connect()
        except Exception as e:
            return {"success": False, "message": f"Authentication failed: {e}"}
        try:
            universes = self._list_universes()
        except Exception as e:
            return {
                "success": False,
                "connectivity": True,
                "message": f"Logged on, but universe listing failed: {e}",
            }
        n = len(universes)
        msg = f"Connected to SAP BusinessObjects. Found {n} universe(s)."
        if n == 0:
            msg += (
                " (No universes visible — check the user's rights and that "
                "universes are published to the repository.)"
            )
        return {"success": True, "message": msg, "universes": n}

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    @property
    def description(self) -> str:
        return "SAP BusinessObjects universes (semantic layer via /biprws).\n\n" + self.system_prompt()

    def system_prompt(self) -> str:
        return """
## SAP BusinessObjects Query Guide (universes)

Each universe is one schema table. Columns are tagged `role=dimension`
(groupable attributes) or `role=measure` (aggregated values). You do NOT write
SQL — you pick the universe **result objects** to return and BusinessObjects
generates and runs the query, applying the universe's security for the
signed-in user.

### How to query — execute_query(objects, table_name)

`execute_query` takes a comma-separated list of result object names as the FIRST
argument and the universe name as the SECOND (like the Power BI client). Use the
object names exactly as shown in the schema.

```python
# Revenue by Country (Country is a dimension, Revenue a measure)
df = db_clients['businessobjects'].execute_query(
    "Country,Revenue",          # result objects (1st arg)
    "eFashion",                 # universe name (2nd arg)
)
```

### Rules
- FIRST arg = comma-separated result objects; SECOND arg = the universe name
  exactly as shown in the schema.
- Optional row cap is `max_rows=1000`, never `limit=`. Keep the exact
  universe name, including `.unx`, as table_name.
- A universe is a semantic catalog, not a flat physical table. Its objects
  may belong to incompatible fact tables. Inspect object paths/descriptions;
  begin with the few objects needed, not every sales/product/date field.
- For a product listing, query product dimensions only. For product sales,
  select product dimensions and a relevant measure; this returns aggregates
  at that grain, not raw transaction records.
- If SAP reports incompatible tables, remove unrelated object groups.
  If separate flows are reported, use the listed compatible result groups
  as separate queries AND separate output tables. Do not automatically merge
  them in pandas to work around the connector's rejection. Never join flows
  on their generated OData Id.
- A shared column name (SKU, store, date, etc.) does not establish a valid
  relationship. Combine groups only when source metadata or explicit business
  instructions establish the relationship, grain, and complete join keys.
  Then check key uniqueness and use pandas merge(validate=...) with the
  expected cardinality; verify unmatched keys and preservation of measures.
  Independently capped samples cannot establish complete matching coverage.
- Never use drop_duplicates(keep='first'/'last'), arbitrary aggregation, or
  fillna(0) to make incompatible groups join. Multiple labels/promotions may
  be legitimate distinct records. An unmatched promotion is unknown, not
  evidence that there was no promotion.
- For broad requests such as "show sales and promotions", start with a small
  coherent sales query and a separate promotions table. Explain their grains;
  do not invent a combined transaction-level dataset. If only one output table
  is possible, return one coherent group and disclose what it excludes.
- max_rows caps retrieval BEFORE any pandas sorting or filtering. Sorting a
  capped result and taking head(N) gives the top/latest N within that sample,
  not across the universe. Label it as a sample. For a global latest/top-N
  claim, retrieve the complete relevant result before sorting, or explain that
  the current query interface cannot establish it. Do not claim missing sampled
  rows or null joined values prove absence in the source.
- List the dimensions to group by plus the measures to return. Measures
  aggregate over the selected dimensions automatically.
- Prefer the universe's defined measures — do not recompute an aggregate the
  universe already exposes.
- Security (row/object restrictions) is enforced by BusinessObjects for the
  signed-in user; do not attempt to re-filter for security.
"""


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _dig(obj: Any, *keys: str) -> Any:
    """Nested dict get; returns None if any level is missing/non-dict."""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _as_list(value: Any) -> List:
    """SAP JSON collapses a single-element collection to the element itself;
    normalize to a list (None → [])."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _split_objects(spec: Optional[str]) -> List[str]:
    """Parse a comma-separated result-object list into trimmed names."""
    if not spec:
        return []
    return [s.strip() for s in str(spec).split(",") if s.strip()]


def _walk_universe_objects(node: Any):
    """Yield every object dict in a universe outline, descending nested
    folders/classes. Objects are dicts carrying a name plus a type/qualification;
    folders carry child lists under keys like ``item``/``items``/``objects``/
    ``folder``. Defensive against SAP's list-or-dict collapsing."""
    if isinstance(node, dict):
        # A leaf object typically has a name and a type/qualification.
        if node.get("name") and (node.get("@type") or node.get("type") or node.get("qualification")):
            yield node
        for key in ("item", "items", "object", "objects", "folder", "folders", "outline", "children"):
            if key in node:
                for child in _as_list(node.get(key)):
                    yield from _walk_universe_objects(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk_universe_objects(child)


def _bo_dtype(data_type: Optional[str]) -> str:
    """Map a BusinessObjects object dataType to a short dtype label."""
    dt = str(data_type or "").lower()
    if dt in ("numeric", "number", "double", "integer", "int", "long"):
        return "number"
    if dt in ("date", "datetime", "timestamp"):
        return "datetime"
    return "string"


# Compatibility aliases for dynamic resolvers.
SAPBusinessObjectsClient = BusinessObjectsClient
