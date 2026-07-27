from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
from typing import List, Dict, Optional, Any
import requests
import pandas as pd


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
        """Fetch a universe's outline (``GET /sl/v1/universes/{id}``) and flatten
        its objects into TableColumns. Best-effort: a universe whose detail can't
        be read still yields a table (no columns) rather than failing discovery."""
        try:
            resp = self._get(f"/sl/v1/universes/{universe_id}")
            if resp.status_code >= 300:
                return []
            payload = resp.json() or {}
        except Exception:
            return []

        root = payload.get("universe") if isinstance(payload, dict) else None
        root = root if isinstance(root, dict) else payload
        columns: List[TableColumn] = []
        seen = set()
        for obj in _walk_universe_objects(root):
            oname = obj.get("name") or obj.get("id")
            if not oname or oname in seen:
                continue
            kind = str(obj.get("@type") or obj.get("type") or obj.get("qualification") or "").lower()
            if kind in _MEASURE_KINDS:
                role, dtype = "measure", "measure"
            elif kind in _DIMENSION_KINDS:
                role, dtype = "dimension", _bo_dtype(obj.get("dataType") or obj.get("type"))
            else:
                # Skip filters / conditions / folders that aren't result objects.
                continue
            seen.add(oname)
            columns.append(TableColumn(
                name=oname,
                dtype=dtype,
                description=obj.get("description") or None,
                metadata={"role": role, "object_id": obj.get("id"),
                          "data_type": obj.get("dataType")},
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
        """Create a query on the universe and return its rows.

        Uses the Semantic Layer query resource: POST a query specification
        referencing the universe and its result objects, then read the flattened
        result set. The response's ``rows``/``columns`` (or ``dataset``) is
        normalized into a list of ``{objectName: value}`` dicts.

        NOTE: the exact SL query JSON is BusinessObjects-version-specific; this
        implements the documented 4.x shape and is exercised by mocked tests
        (Loop A). Confirm against a live tenant before production (Loop B).
        """
        spec = {
            "queryData": {
                "universe": {"id": universe_id},
                "resultObjects": [{"name": name} for name in result_objects],
            }
        }
        resp = self._post("/sl/v1/queries", json_body=spec)
        if resp.status_code >= 300:
            raise RuntimeError(
                f"Query creation failed: HTTP {resp.status_code} {self._body_snippet(resp)}"
            )
        payload = resp.json() or {}
        return self._normalize_result_rows(payload, result_objects)

    @staticmethod
    def _normalize_result_rows(payload: Dict, result_objects: List[str]) -> List[Dict]:
        """Normalize a SL result payload into row dicts.

        Handles the common shapes: a columnar ``{"columns": [...], "rows":
        [[...], ...]}`` block, or an already-tabular ``{"rows": [{...}, ...]}``.
        """
        data = payload
        for key in ("queryData", "dataProvider", "result", "dataset"):
            if isinstance(data, dict) and isinstance(data.get(key), dict):
                data = data[key]
        rows = data.get("rows") if isinstance(data, dict) else None
        if not rows:
            return []
        first = rows[0]
        # Already tabular: a dict of {objectName: value} (no positional cells).
        if isinstance(first, dict) and "cells" not in first:
            return rows
        # Columnar: rows are positional cell lists (or {"cells": [...]}); map each
        # cell to its column name.
        columns = data.get("columns") or result_objects
        col_names = [
            (c.get("name") if isinstance(c, dict) else c) for c in columns
        ]
        out: List[Dict] = []
        for r in rows:
            cells = r.get("cells") if isinstance(r, dict) else r
            out.append({col_names[i] if i < len(col_names) else f"col{i}": v
                        for i, v in enumerate(cells or [])})
        return out

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
