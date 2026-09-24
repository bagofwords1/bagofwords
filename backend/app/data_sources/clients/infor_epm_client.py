import re
import time
from typing import Any
from xml.etree.ElementTree import Element

import pandas as pd
import requests
from defusedxml import ElementTree as ET

from app.ai.prompt_formatters import ServiceFormatter, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import ProgressCallback, discovery_items, discovery_progress

# Payload conventions shared with the BOW_* Application Engine processes.
# The processes are plain BI# functions that return a string; these markers
# are how they signal failure / truncation inside that string.
BOW_ERROR_PREFIX = "BOW_ERROR:"
BOW_TRUNCATED_MARKER = "BOW_TRUNCATED"
# ``BOW_ExecuteMdx`` reports an empty cell set as an error string; for the
# agent that is an empty result, not a failure.
_NO_CELLS_MARKER = "MDX returned no cells"

# ODBO dimension type codes as reported in the Alea ``ODBOType`` property.
_ODBO_MEASURE_TYPE = "2"
_ODBO_TIME_TYPE = "1"

_STATUS_DONE = {"completed", "complete", "succeeded", "success", "finished", "done", "ok"}
_STATUS_FAILED = {"failed", "error", "faulted", "cancelled", "canceled", "aborted"}
_RESULT_KEYS = ("result", "Result", "methodResult", "MethodResult", "value", "Value", "returnValue", "ReturnValue", "data", "Data")
_TASK_KEYS = ("taskId", "TaskId", "taskID", "id", "Id", "task_id")
_STATUS_KEYS = ("status", "Status", "state", "State")

_BRACKET_RE = re.compile(r"\[([^\]]*)\]")
_XML_PROLOG_RE = re.compile(r"<\?xml[^>]*\?>")


class InforEpmError(RuntimeError):
    """A failure reported by the Application Engine or by a BOW_* process."""


class InforEpmHttpError(InforEpmError):
    def __init__(self, status_code: int, body_snippet: str = ""):
        self.status_code = status_code
        self.body_snippet = body_snippet or ""
        super().__init__(f"Application Engine call failed: HTTP {status_code} {self.body_snippet}")


class InforEpmClient(DataSourceClient):
    """
    Infor EPM connector over the Application Engine REST API.

    Infor d/EPM farms secured with Infor Federation Services accept only IFS /
    OAuth2 identities at the perimeter, while the OLAP XMLA provider itself
    authenticates with Basic credentials only — so an external client cannot
    run MDX against OLAP directly. This connector routes everything through
    Application Engine processes instead: three BI# processes (``BOW_GetCubeList``,
    ``BOW_GetCubeSchema``, ``BOW_ExecuteMdx``) run inside the farm on a named
    OLAP connection and are published as REST endpoints behind the ION API
    Gateway, where a client-credentials token is all the caller needs.

    Process contract (all return one string):

    - ``BOW_GetCubeList()``           -> cube names, one per line
    - ``BOW_GetCubeSchema(CubeName)`` -> ``<BOWSchema>`` XML: one ``<BOWSection>``
      per dimension wrapping the raw Alea ``Dimension GetProperties`` response
      plus a ``<BOWElements>`` sample
    - ``BOW_ExecuteMdx(mdxQuery, rowLimit)`` -> one cell per line:
      ``coord1<TAB>...<TAB>coordN<TAB>STR|NUM<TAB>value``, optional
      ``BOW_TRUNCATED`` trailer
    - any process may return ``BOW_ERROR: <message>``

    Each cube is exposed as one schema table named ``Database/Cube``. Its
    dimensions become ``dimension`` columns; the elements of the cube's
    measures dimension (Infor OLAP has no separate measure objects — measures
    are elements of one designated dimension) become ``measure`` columns.
    """

    META_KEY = "infor_epm"
    PRODUCT_NAME = "Infor EPM"

    def __init__(
        self,
        api_url: str,
        olap_database: str,
        max_rows: int = 5000,
        timeout_sec: int = 120,
        verify_ssl: bool = True,
        async_mode: bool = True,
        poll_interval_sec: float = 1.0,
        cube_list_process: str = "BOW_GetCubeList",
        cube_schema_process: str = "BOW_GetCubeSchema",
        mdx_process: str = "BOW_ExecuteMdx",
        measure_dimension_pattern: str = "measure",
        gateway_token_url: str | None = None,
        gateway_client_id: str | None = None,
        gateway_client_secret: str | None = None,
        gateway_scope: str | None = None,
        bearer_token: str | None = None,
    ):
        self.api_url = (api_url or "").strip().rstrip("/")
        self.olap_database = (olap_database or "").strip()
        self.max_rows = int(max_rows) if max_rows else 5000
        self.timeout_sec = int(timeout_sec) if timeout_sec else 120
        self.verify_ssl = bool(verify_ssl)
        self.async_mode = bool(async_mode)
        self.poll_interval_sec = max(float(poll_interval_sec or 1.0), 0.05)
        self.cube_list_process = (cube_list_process or "BOW_GetCubeList").strip()
        self.cube_schema_process = (cube_schema_process or "BOW_GetCubeSchema").strip()
        self.mdx_process = (mdx_process or "BOW_ExecuteMdx").strip()
        self.measure_dimension_pattern = (measure_dimension_pattern or "").strip().lower()
        self.gateway_token_url = (gateway_token_url or "").strip() or None
        self.gateway_client_id = gateway_client_id
        self.gateway_client_secret = gateway_client_secret
        self.gateway_scope = (gateway_scope or "").strip() or None
        self.bearer_token = (bearer_token or "").strip() or None

        self._http: requests.Session | None = None
        self._token_expires_at = 0.0
        self._schema_cache: list[Table] | None = None

    # ------------------------------------------------------------------
    # Connection / auth
    # ------------------------------------------------------------------

    def connect(self):
        if self._http is not None:
            return
        if not self.api_url:
            raise RuntimeError("api_url is required")
        if not self.olap_database:
            raise RuntimeError("olap_database is required")
        if not self.bearer_token:
            if not self.gateway_token_url:
                raise RuntimeError("gateway_token_url is required (or a bearer_token)")
            if not (self.gateway_client_id and self.gateway_client_secret):
                raise RuntimeError("gateway_client_id and gateway_client_secret are required")
        self._http = requests.Session()
        self._ensure_token()

    def _ensure_token(self):
        if self.bearer_token:
            self._http.headers["Authorization"] = f"Bearer {self.bearer_token}"
            return
        if time.monotonic() < self._token_expires_at:
            return
        data = {
            "grant_type": "client_credentials",
            "client_id": self.gateway_client_id,
            "client_secret": self.gateway_client_secret,
        }
        if self.gateway_scope:
            data["scope"] = self.gateway_scope
        response = requests.post(
            self.gateway_token_url, data=data, timeout=self.timeout_sec, verify=self.verify_ssl
        )
        if response.status_code >= 300:
            raise InforEpmHttpError(response.status_code, "ION API Gateway token exchange failed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise InforEpmError("ION API Gateway token exchange returned invalid JSON") from exc
        token = payload.get("access_token")
        if not token:
            raise InforEpmError("ION API Gateway token exchange returned no access_token")
        try:
            expires_in = max(int(payload.get("expires_in", 300)), 1)
        except (TypeError, ValueError):
            expires_in = 300
        self._token_expires_at = time.monotonic() + expires_in - min(30, expires_in // 5)
        self._http.headers["Authorization"] = f"Bearer {token}"

    def test_connection(self) -> dict:
        try:
            self.connect()
            cubes = self._list_cubes()
        except Exception as e:
            return self._classify_failure(e)
        msg = f"Connected to {self.PRODUCT_NAME}. Found {len(cubes)} cube(s) in {self.olap_database}."
        if not cubes:
            msg += " (No queryable cubes returned — check the OLAP database name and the user's application access.)"
        return {"success": True, "message": msg, "cubes": len(cubes)}

    def _classify_failure(self, e: Exception) -> dict:
        if isinstance(e, InforEpmHttpError):
            if e.status_code in (401, 403):
                msg = (
                    "The ION API Gateway rejected the request (HTTP "
                    f"{e.status_code}) — check the client credentials and that the "
                    "authorized app may call the DEPM Application Engine API."
                )
            elif e.status_code == 404:
                msg = (
                    "Endpoint reached, but nothing serves the process at this URL "
                    "(HTTP 404) — verify the Application Engine API URL and the "
                    "process names, and that the DEPM API was refreshed after publishing."
                )
            else:
                msg = f"Application Engine call failed: HTTP {e.status_code}"
            return {"success": False, "message": msg}
        if isinstance(e, requests.exceptions.SSLError):
            return {"success": False, "message": f"TLS error talking to the gateway: {e}"}
        if isinstance(e, requests.exceptions.ConnectionError):
            return {"success": False, "message": f"Could not reach the Application Engine API: {e}"}
        if isinstance(e, requests.exceptions.Timeout):
            return {"success": False, "message": "The Application Engine API did not respond in time — check the timeout."}
        if isinstance(e, RuntimeError) and not isinstance(e, InforEpmError):
            return {"success": False, "message": f"Configuration error: {e}"}
        return {"success": False, "connectivity": True, "message": f"Reached the gateway but the process failed: {e}"}

    # ------------------------------------------------------------------
    # Application Engine transport
    # ------------------------------------------------------------------

    def _process_url(self, process: str, suffix: str = "") -> str:
        return f"{self.api_url}/{process}{suffix}"

    def _post(self, url: str, payload: dict[str, Any]) -> requests.Response:
        self._ensure_token()
        response = self._http.post(
            url, json=payload, timeout=self.timeout_sec, verify=self.verify_ssl,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        if response.status_code == 401 and not self.bearer_token:
            self._token_expires_at = 0.0
            self._ensure_token()
            response = self._http.post(
                url, json=payload, timeout=self.timeout_sec, verify=self.verify_ssl,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        if response.status_code >= 300:
            raise InforEpmHttpError(response.status_code, (response.text or "")[:200])
        return response

    @staticmethod
    def _decode_body(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def _first_key(body: dict, keys) -> Any:
        for k in keys:
            if k in body and body[k] is not None:
                return body[k]
        return None

    @classmethod
    def _raise_engine_error(cls, body: Any) -> None:
        # The engine wraps uncaught process failures (and unknown task ids)
        # as ``{"error": "..."}`` with HTTP 200 — on the submit, the poll,
        # and the sync response alike.
        if isinstance(body, dict):
            err = cls._first_key(body, ("error", "Error", "errorMessage", "ErrorMessage"))
            if err:
                raise InforEpmError(str(err).strip())

    def _extract_result(self, body: Any) -> str:
        """Pull the process' string return value out of an engine response.

        The engine wraps uncaught process failures as ``{"error": "..."}``; a
        finished call carries the return value under a result-ish key (or is
        the bare string itself). ``BOW_ERROR:`` strings are the processes'
        own failure signal and are raised the same way.
        """
        if isinstance(body, dict):
            self._raise_engine_error(body)
            value = self._first_key(body, _RESULT_KEYS)
            if value is None:
                raise InforEpmError(f"Application Engine response carried no result (keys: {sorted(body)})")
            text = value if isinstance(value, str) else str(value)
        elif isinstance(body, str):
            text = body
        else:
            text = str(body)
        stripped = text.strip()
        if stripped.startswith(BOW_ERROR_PREFIX):
            raise InforEpmError(stripped[len(BOW_ERROR_PREFIX):].strip())
        return text

    def _run_process(self, process: str, params: dict[str, Any]) -> str:
        """Execute a published Application Engine process and return its string result."""
        self.connect()
        if not self.async_mode:
            response = self._post(self._process_url(process), params)
            return self._extract_result(self._decode_body(response))

        response = self._post(self._process_url(process, "/async"), params)
        body = self._decode_body(response)
        self._raise_engine_error(body)
        task_id = self._first_key(body, _TASK_KEYS) if isinstance(body, dict) else body
        if not task_id:
            raise InforEpmError(f"Async call to {process} returned no task id")
        deadline = time.monotonic() + self.timeout_sec
        result_url = self._process_url("getasyncresult")
        while True:
            poll = self._decode_body(self._post(result_url, {"taskId": task_id}))
            if isinstance(poll, dict):
                self._raise_engine_error(poll)
                status = str(self._first_key(poll, _STATUS_KEYS) or "").strip().lower()
                if status in _STATUS_FAILED:
                    raise InforEpmError(f"Process {process} failed (status {status})")
                has_result = self._first_key(poll, _RESULT_KEYS) is not None
                if status in _STATUS_DONE or (not status and has_result):
                    return self._extract_result(poll)
            else:
                return self._extract_result(poll)
            if time.monotonic() >= deadline:
                raise InforEpmError(f"Process {process} did not finish within {self.timeout_sec}s")
            time.sleep(self.poll_interval_sec)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _list_cubes(self) -> list[str]:
        text = self._run_process(self.cube_list_process, {"OLAPName": self.olap_database})
        cubes: list[str] = []
        for raw in re.split(r"[\r\n;]+", text):
            name = raw.strip()
            if not name or name.startswith("#"):
                continue
            if name not in cubes:
                cubes.append(name)
        return cubes

    def _cube_schema(self, cube: str) -> dict:
        text = self._run_process(
            self.cube_schema_process, {"OLAPName": self.olap_database, "CubeName": cube}
        )
        return parse_bow_schema(text)

    def _is_measure_dimension(self, dim: dict) -> bool:
        if dim.get("odbo_type") == _ODBO_MEASURE_TYPE:
            return True
        pattern = self.measure_dimension_pattern
        return bool(pattern) and pattern in (dim.get("name") or "").lower()

    @discovery_progress
    def get_schemas(self, progress_callback: ProgressCallback | None = None) -> list[Table]:
        self.connect()
        tables: list[Table] = []
        for cube in discovery_items(self._list_cubes(), "cubes", label=str):
            schema = self._cube_schema(cube)
            tables.append(self._build_table(cube, schema))
        self._schema_cache = tables
        return tables

    def _build_table(self, cube: str, schema: dict) -> Table:
        dims = schema.get("dimensions") or []
        measure_dims = [d for d in dims if d.get("odbo_type") == _ODBO_MEASURE_TYPE]
        if not measure_dims:
            measure_dims = [d for d in dims if self._is_measure_dimension(d)]
        measure_dim = measure_dims[0] if measure_dims else None

        columns: list[TableColumn] = []
        for d in dims:
            name = d["name"]
            is_measure_dim = measure_dim is not None and d is measure_dim
            columns.append(TableColumn(
                name=name,
                dtype="dimension",
                description=d.get("description"),
                metadata={
                    "role": "dimension",
                    "unique_name": f"[{name}]",
                    "position": d.get("position"),
                    "odbo_type": d.get("odbo_type"),
                    "is_time_dimension": d.get("odbo_type") == _ODBO_TIME_TYPE,
                    "is_measure_dimension": is_measure_dim,
                    "default_hierarchy": d.get("default_hierarchy"),
                    "hierarchies": d.get("hierarchies") or [],
                    "element_count": d.get("element_count"),
                    "sample_elements": d.get("elements") or [],
                    "sample_unique_names": [f"[{name}].[{e}]" for e in (d.get("elements") or [])],
                },
            ))
            if is_measure_dim:
                for elem in d.get("elements") or []:
                    columns.append(TableColumn(
                        name=elem,
                        dtype="measure",
                        metadata={
                            "role": "measure",
                            "unique_name": f"[{name}].[{elem}]",
                            "dimension": f"[{name}]",
                        },
                    ))

        meta = {
            "catalog": self.olap_database,
            "cube": cube,
            "cubeUniqueName": f"[{cube}]",
            "dimension_order": [d["name"] for d in dims],
            "measure_dimension": measure_dim["name"] if measure_dim else None,
        }
        cube_info = schema.get("cube") or {}
        return Table(
            name=f"{self.olap_database}/{cube}",
            description=cube_info.get("description"),
            columns=columns,
            pks=[],
            fks=[],
            is_active=True,
            metadata_json={self.META_KEY: meta},
        )

    def get_schema(self, table_name: str) -> Table:
        tables = self._schema_cache or self.get_schemas()
        for tbl in tables:
            if tbl.name == table_name:
                return tbl
        for tbl in tables:
            meta = (tbl.metadata_json or {}).get(self.META_KEY) or {}
            if meta.get("cube") == table_name:
                return tbl
        raise RuntimeError(f"Table not found for '{table_name}'")

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(
        self,
        query: str,
        table_name: str | None = None,
        max_rows: int | None = None,
    ) -> pd.DataFrame:
        """Run an MDX statement through ``BOW_ExecuteMdx`` and return a DataFrame."""
        if not query or not query.strip():
            raise ValueError("MDX query is required")
        limit = int(max_rows) if max_rows and int(max_rows) > 0 else self.max_rows
        params = {"OLAPName": self.olap_database, "mdxQuery": query.strip(), "rowLimit": limit}
        try:
            text = self._run_process(self.mdx_process, params)
        except InforEpmError as exc:
            if _NO_CELLS_MARKER.lower() in str(exc).lower():
                return pd.DataFrame()
            raise
        df, truncated = parse_mdx_cells(text)
        if max_rows is not None and max_rows > 0 and len(df) > max_rows:
            df = df.head(max_rows)
        df.attrs["truncated"] = truncated
        return df

    # ------------------------------------------------------------------
    # Prompt / description
    # ------------------------------------------------------------------

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    @property
    def description(self) -> str:
        return (
            "Infor EPM Client: discover cubes and execute MDX against Infor d/EPM "
            "OLAP through Application Engine processes published on the ION API "
            "Gateway (IFS/OAuth2 authentication)."
        ) + self.system_prompt()

    def system_prompt(self) -> str:
        return """

## Infor EPM MDX Guide

Queries are MDX SELECT statements executed inside the Infor EPM farm. The
result is one row per cell: one column per axis coordinate (named after the
dimension) plus a `value` column.

### Schema Structure

Each cube is a schema table named `Database/Cube` (e.g. `DEMO_OLAP/Sales`).
- Columns with `dtype="dimension"` are the cube's dimensions. Their
  `metadata.unique_name` is the MDX name (e.g. `[Region]`), and
  `metadata.sample_elements` lists member names you can use as
  `[Region].[North]`. `metadata.hierarchies` lists hierarchy/level names.
- Infor OLAP has no separate measures: one dimension acts as the measures
  dimension (`metadata.is_measure_dimension = true`, e.g. `[SalesMeasures]`).
  Its elements are exposed as `dtype="measure"` columns whose
  `metadata.unique_name` (e.g. `[SalesMeasures].[Revenue]`) goes on an axis.
- The cube name for the FROM clause is `metadata.infor_epm.cubeUniqueName`.

### How to Execute Queries

**Signature**: `execute_query(mdx_query, table_name)` — pass the
`Database/Cube` table name as the second argument.

```python
df = db_clients['infor_epm'].execute_query(
    '''
    SELECT { [SalesMeasures].[Revenue] } ON COLUMNS,
           NON EMPTY { [Region].Members } ON ROWS
    FROM [Sales]
    ''',
    "DEMO_OLAP/Sales"
)
```

The DataFrame has one column per dimension that appears on an axis (member
name as the value) plus `value`. Dimensions not on an axis default to their
top-level (total) element.

### MDX Rules

- Always name the cube in brackets: `FROM [Sales]`.
- Put measure elements on one axis and dimension members on the other.
- Reference members by `[Dimension].[Element]`; use `[Dimension].Members`
  for all elements and `[Dimension].[Element].Children` for children.
- Use `NON EMPTY` on the large axis and keep queries narrow — every extra
  dimension multiplies the cell count, and results are capped by `rowLimit`
  (the DataFrame carries `df.attrs["truncated"]`).
- Use `CROSSJOIN(...)` to combine dimensions on one axis; a `WHERE (...)`
  slicer fixes dimensions that are not on an axis.
- This is MDX, not SQL: no `SELECT *`, no `GROUP BY` — aggregation comes
  from consolidated elements (e.g. `[Region].[All]`).
"""


# ----------------------------------------------------------------------
# Payload parsers (module-level so tests and the mock server can share them)
# ----------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_local(el: Element, name: str) -> Element | None:
    for child in el.iter():
        if _local(child.tag) == name:
            return child
    return None


def _findall_local(el: Element, name: str) -> list[Element]:
    return [child for child in el.iter() if _local(child.tag) == name]


def parse_bow_schema(text: str) -> dict:
    """Parse the ``<BOWSchema>`` document returned by ``BOW_GetCubeSchema``.

    The document wraps raw Alea ``GetProperties`` responses per section; each
    embedded ``Alea:Document`` may carry its own XML prolog, which is illegal
    mid-document — strip them before parsing. Alea error nodes inside a
    section (e.g. an unsupported request) are ignored rather than fatal.
    """
    cleaned = _XML_PROLOG_RE.sub("", text or "").strip()
    if not cleaned:
        return {"cube": {}, "dimensions": []}
    try:
        root = ET.fromstring(cleaned)
    except Exception as exc:
        raise InforEpmError(f"Could not parse BOWSchema payload: {exc}") from exc
    if _local(root.tag) != "BOWSchema":
        raise InforEpmError(f"Unexpected schema payload root <{_local(root.tag)}>")

    result: dict[str, Any] = {
        "cube": {"name": root.get("cube"), "database": root.get("db")},
        "dimensions": [],
    }
    for section in root:
        if _local(section.tag) != "BOWSection":
            continue
        kind = (section.get("kind") or "").lower()
        if kind == "cube":
            result["cube"].update(_parse_alea_properties(section))
            continue
        if kind != "dimension":
            continue
        dim: dict[str, Any] = {
            "name": section.get("name"),
            "position": _to_int(section.get("position")),
            "description": None,
            "odbo_type": None,
            "default_hierarchy": None,
            "hierarchies": [],
            "elements": [],
            "element_count": None,
        }
        dim.update(_parse_alea_properties(section))
        elements = _find_local(section, "BOWElements")
        if elements is not None:
            dim["element_count"] = _to_int(elements.get("count"))
            dim["elements"] = [
                (e.text or "").strip() for e in elements if _local(e.tag) == "BOWElement" and (e.text or "").strip()
            ]
        result["dimensions"].append(dim)
    result["dimensions"].sort(key=lambda d: (d.get("position") is None, d.get("position") or 0))
    return result


def _parse_alea_properties(section: Element) -> dict[str, Any]:
    """Extract description / ODBO type / hierarchies from an Alea GetProperties
    response embedded in a BOWSection. Tolerant of missing pieces."""
    out: dict[str, Any] = {}
    props = _find_local(section, "Properties")
    if props is None:
        return out
    desc = None
    for child in props:
        if _local(child.tag) == "Description" and (child.text or "").strip():
            desc = child.text.strip()
            break
    if desc is None:
        d = _find_local(props, "Description")
        if d is not None and (d.text or "").strip():
            desc = d.text.strip()
    if desc:
        out["description"] = desc
    odbo = _find_local(props, "ODBOType")
    if odbo is not None and odbo.get("Code"):
        out["odbo_type"] = odbo.get("Code")
    default_h = _find_local(props, "DefaultHierarchy")
    if default_h is not None and default_h.get("Name"):
        out["default_hierarchy"] = default_h.get("Name")
    hierarchies = []
    for h in _findall_local(props, "Hierarchy"):
        levels = [lvl.get("Name") for lvl in _findall_local(h, "Level") if lvl.get("Name")]
        hierarchies.append({"name": h.get("Name"), "levels": levels})
    if hierarchies:
        out["hierarchies"] = hierarchies
    return out


def _to_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _coord_parts(coord: str):
    """``[Region].[Region].[North]`` -> (``Region``, ``North``). A bare token
    without brackets is returned as-is for both."""
    parts = _BRACKET_RE.findall(coord)
    if not parts:
        token = coord.strip()
        return token, token
    return parts[0], parts[-1]


def parse_mdx_cells(text: str):
    """Parse ``BOW_ExecuteMdx`` output into (DataFrame, truncated).

    Each line is ``coord…<TAB>TYPE<TAB>value``. Coordinates are member unique
    names; the column is named after the first bracket segment (the
    dimension) and the cell value is the last segment (the member). Values
    typed ``NUM`` become floats, ``STR`` stay strings.
    """
    rows: list[dict[str, Any]] = []
    truncated = False
    dim_columns: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if line.strip() == BOW_TRUNCATED_MARKER:
            truncated = True
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            # Malformed/decorative line — a process still notifying banners.
            continue
        coords, vtype, value = parts[:-2], parts[-2].strip().upper(), parts[-1]
        row: dict[str, Any] = {}
        for idx, coord in enumerate(coords):
            dim, member = _coord_parts(coord)
            col = dim or f"coord_{idx + 1}"
            base, n = col, 2
            while col in row:
                col = f"{base}_{n}"
                n += 1
            row[col] = member
            if col not in dim_columns:
                dim_columns.append(col)
        if vtype == "NUM":
            try:
                row["value"] = float(value)
            except ValueError:
                row["value"] = value
        else:
            row["value"] = value
        rows.append(row)
    if not rows:
        return pd.DataFrame(), truncated
    df = pd.DataFrame(rows, columns=dim_columns + ["value"])
    if all(isinstance(v, float) for v in df["value"]):
        df["value"] = pd.to_numeric(df["value"])
    return df, truncated
