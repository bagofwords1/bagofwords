"""Shared Qlik Engine (QIX) plumbing.

The Engine API is the same JSON-RPC 2.0-over-WebSocket protocol on Qlik Cloud and
on Qlik Sense Enterprise on Windows; only the URL, the TLS setup and the auth
headers differ. Cloud sends ``Authorization: Bearer <token>`` to
``wss://tenant/app/{id}``; on-prem presents a client certificate to
``wss://host:4747/app/{id}`` and names the acting user in ``X-Qlik-User``.

Everything that is genuinely common lives here — the session/RPC loop, the
hypercube builder, the result-matrix decoder, and the two catalog mappers — so a
protocol-level fix lands once for both clients rather than twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session / RPC
# ---------------------------------------------------------------------------


class QixSession:
    """One Engine WebSocket, used as an async context manager.

    ``async with QixSession(url, headers=…, ssl_context=…) as qix:``
        ``result = await qix.rpc(-1, "OpenDoc", [app_id])``

    Handles are the Engine's object addressing scheme: ``-1`` is the global
    object, ``OpenDoc`` returns a document handle, and each session object
    (hypercube, measure list, …) returns its own.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: Optional[Sequence[Tuple[str, str]]] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        timeout_sec: int = 30,
    ):
        self.url = url
        self.headers = list(headers or [])
        self.ssl_context = ssl_context
        self.timeout_sec = timeout_sec
        self._cm = None
        self._ws = None
        self._next_id = 1

    async def __aenter__(self) -> "QixSession":
        import websockets

        # `additional_headers`, NOT `extra_headers`: the legacy asyncio client
        # was removed in websockets 15 and the parameter renamed in 14. Passing
        # the old name does not raise a clean error — it falls through
        # `**kwargs` into `loop.create_connection()` and dies there with an
        # unrelated-looking TypeError.
        self._cm = websockets.connect(
            self.url,
            additional_headers=self.headers,
            ssl=self.ssl_context,
            open_timeout=self.timeout_sec,
            close_timeout=5,
        )
        self._ws = await self._cm.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._cm is not None:
            return await self._cm.__aexit__(exc_type, exc, tb)
        return False

    async def rpc(self, handle: int, method: str, params: Any) -> Dict[str, Any]:
        """Send one JSON-RPC request and return its ``result``.

        Reads until the reply carrying our own ``id`` arrives. The Engine
        interleaves unsolicited notifications on the same socket — ``OnConnected``
        on open, plus ``OnAuthenticationInformation`` and per-object change
        events — so treating the next frame as the answer desynchronises the
        session and every later call reads the previous call's reply.
        """
        request_id = self._next_id
        self._next_id += 1
        await self._ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "handle": handle,
            "method": method,
            "params": params,
        }))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=self.timeout_sec)
            msg = json.loads(raw)
            if msg.get("id") != request_id:
                continue  # notification or a stale frame — keep reading
            if "error" in msg:
                err = msg["error"] or {}
                raise RuntimeError(f"Qlik QIX error on {method}: {err.get('message') or err}")
            return msg.get("result") or {}


def build_ssl_context(
    *,
    verify_ssl: bool = True,
    ca_file: Optional[str] = None,
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    key_password: Optional[str] = None,
) -> ssl.SSLContext:
    """Build the TLS context for an Engine WebSocket or a REST call.

    ``cert_file``/``key_file`` add the client certificate on-prem needs (mutual
    TLS). ``ca_file`` verifies the server against Qlik's own root (``root.pem``);
    without it, ``verify_ssl=False`` is the only way to reach a site using
    self-signed service certificates, which is the default QSEoW install.
    """
    ctx = ssl.create_default_context(cafile=ca_file)
    if not verify_ssl and ca_file is None:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if cert_file:
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file, password=key_password or None)
    return ctx


# ---------------------------------------------------------------------------
# Field tags → dtypes
# ---------------------------------------------------------------------------

_TAG_DTYPE_ORDER = [
    ("$key", "key"),
    ("$date", "date"),
    ("$timestamp", "timestamp"),
    ("$numeric", "numeric"),
    ("$integer", "integer"),
    ("$text", "text"),
    ("$ascii", "text"),
]


def tags_to_dtype(tags: Optional[List[str]]) -> str:
    tags = tags or []
    for tag, dtype in _TAG_DTYPE_ORDER:
        if tag in tags:
            return dtype
    return "unknown"


# ---------------------------------------------------------------------------
# Catalog mappers
# ---------------------------------------------------------------------------


def app_key_for(app: Dict[str, Any]) -> str:
    """"Container/App" prefix for a table name — space on Cloud, stream on-prem."""
    app_name = app.get("name") or app.get("id") or ""
    container = app.get("space_name") or ""
    return f"{container}/{app_name}" if container else app_name


def build_tables_from_rest_metadata(
    app: Dict[str, Any],
    metadata: Dict[str, Any],
    *,
    namespace: str = "qlik_sense",
) -> List[Table]:
    """Convert REST ``/apps/{id}/data/metadata`` into Table objects.

    Served by Qlik Cloud and — through the proxy — by QSEoW, in both cases from
    the post-reload snapshot, so it needs no Engine session and does not load the
    app into engine memory.
    """
    app_id = app.get("id")
    app_name = app.get("name") or app_id or ""
    space_name = app.get("space_name") or ""
    app_key = app_key_for(app)

    # Build an index: table name -> list of (field_name, tags)
    fields_by_table: Dict[str, List[Dict[str, Any]]] = {}
    for field in metadata.get("fields") or []:
        src_tables = field.get("srcTables") or []
        for t in src_tables:
            fields_by_table.setdefault(t, []).append({
                "name": field.get("name") or "",
                "tags": field.get("tags") or [],
                "is_key": "$key" in (field.get("tags") or []),
            })

    # Cross-table key fields become approximate relationships — every table
    # that shares a key field is related via that field. Build pks from $key.
    key_to_tables: Dict[str, List[str]] = {}
    for field in metadata.get("fields") or []:
        if "$key" in (field.get("tags") or []):
            for t in field.get("srcTables") or []:
                key_to_tables.setdefault(field.get("name") or "", []).append(t)

    tables: List[Table] = []
    for tbl in metadata.get("tables") or []:
        tbl_name = tbl.get("name") or ""
        if not tbl_name:
            continue
        full_name = f"{app_key}/{tbl_name}" if app_key else tbl_name

        columns: List[TableColumn] = []
        pks: List[TableColumn] = []
        for f in fields_by_table.get(tbl_name, []):
            col = TableColumn(
                name=f["name"],
                dtype=tags_to_dtype(f["tags"]),
                description=None,
                metadata={"qlik_tags": f["tags"]},
            )
            columns.append(col)
            if f["is_key"]:
                pks.append(col)

        fks: List[ForeignKey] = []
        for f in fields_by_table.get(tbl_name, []):
            if not f["is_key"]:
                continue
            other_tables = [t for t in key_to_tables.get(f["name"], []) if t != tbl_name]
            for other in other_tables:
                fks.append(ForeignKey(
                    column=TableColumn(name=f["name"], dtype=tags_to_dtype(f["tags"])),
                    references_name=(f"{app_key}/{other}" if app_key else other),
                    references_column=TableColumn(name=f["name"], dtype=tags_to_dtype(f["tags"])),
                ))

        tables.append(Table(
            name=full_name,
            description=None,
            columns=columns,
            pks=pks,
            fks=fks,
            is_active=True,
            metadata_json={
                namespace: {
                    "appId": app_id,
                    "appName": app_name,
                    "spaceId": app.get("space_id"),
                    "spaceName": space_name,
                    "tableName": tbl_name,
                    "rowCount": tbl.get("rows"),
                    "source": "rest_metadata",
                }
            },
        ))

    return tables


def build_tables_from_qix(
    app: Dict[str, Any],
    qtr: List[Dict[str, Any]],
    qk: List[Dict[str, Any]],
    *,
    namespace: str = "qlik_sense",
) -> List[Table]:
    """Convert a QIX ``GetTablesAndKeys`` result into Table objects.

    Qlik is associative rather than relational: ``qk`` describes which tables a
    key field links, with no direction, so each link is emitted as a foreign key
    from both sides.
    """
    app_id = app.get("id")
    app_name = app.get("name") or app_id or ""
    space_name = app.get("space_name") or ""
    app_key = app_key_for(app)

    # Index keys (association links) by table name
    keys_by_table: Dict[str, List[Dict[str, Any]]] = {}
    for key in qk or []:
        key_fields = key.get("qKeyFields") or []
        tables_for_key = key.get("qTables") or []
        for t in tables_for_key:
            keys_by_table.setdefault(t, []).append({
                "fields": key_fields,
                "other_tables": [o for o in tables_for_key if o != t],
            })

    tables: List[Table] = []
    for tbl in qtr or []:
        tbl_name = tbl.get("qName") or ""
        if not tbl_name:
            continue
        full_name = f"{app_key}/{tbl_name}" if app_key else tbl_name

        key_fields_set: set = set()
        for entry in keys_by_table.get(tbl_name, []):
            for kf in entry["fields"]:
                key_fields_set.add(kf)

        columns: List[TableColumn] = []
        pks: List[TableColumn] = []
        for field in tbl.get("qFields") or []:
            fname = field.get("qName") or ""
            if not fname:
                continue
            tags = field.get("qTags") or []
            col = TableColumn(
                name=fname,
                dtype=tags_to_dtype(tags),
                description=None,
                metadata={"qlik_tags": tags},
            )
            columns.append(col)
            if fname in key_fields_set:
                pks.append(col)

        fks: List[ForeignKey] = []
        for entry in keys_by_table.get(tbl_name, []):
            for kf in entry["fields"]:
                for other in entry["other_tables"]:
                    fks.append(ForeignKey(
                        column=TableColumn(name=kf, dtype="key"),
                        references_name=(f"{app_key}/{other}" if app_key else other),
                        references_column=TableColumn(name=kf, dtype="key"),
                    ))

        tables.append(Table(
            name=full_name,
            description=None,
            columns=columns,
            pks=pks,
            fks=fks,
            is_active=True,
            metadata_json={
                namespace: {
                    "appId": app_id,
                    "appName": app_name,
                    "spaceId": app.get("space_id"),
                    "spaceName": space_name,
                    "tableName": tbl_name,
                    "rowCount": tbl.get("qNoOfRows"),
                    "source": "qix",
                }
            },
        ))
    return tables


# ---------------------------------------------------------------------------
# Hypercubes
# ---------------------------------------------------------------------------


def build_hypercube_def(
    dimensions: List[str],
    measures: List[Dict[str, str]],
    max_rows: int,
) -> Dict[str, Any]:
    qdims = [{
        "qDef": {"qFieldDefs": [d]},
        "qNullSuppression": False,
    } for d in dimensions]
    qmeas = [{
        "qDef": {
            "qDef": (m.get("expr") if isinstance(m, dict) else str(m)) or "",
            "qLabel": (m.get("alias") if isinstance(m, dict) else "") or "",
        }
    } for m in measures]
    width = max(1, len(qdims) + len(qmeas))
    return {
        "qDimensions": qdims,
        "qMeasures": qmeas,
        # The Engine caps a single data page at 10 000 cells, so the initial
        # fetch height is derived from the cube's width rather than max_rows.
        "qInitialDataFetch": [{
            "qTop": 0,
            "qLeft": 0,
            "qHeight": min(max_rows, max(1, 10000 // width)),
            "qWidth": width,
        }],
        "qSuppressZero": False,
        "qSuppressMissing": False,
    }


def hypercube_matrix_to_df(
    hypercube_def: Dict[str, Any],
    rows: List[List[Dict[str, Any]]],
) -> pd.DataFrame:
    dim_defs = hypercube_def.get("qDimensions") or []
    meas_defs = hypercube_def.get("qMeasures") or []

    col_names: List[str] = []
    col_is_measure: List[bool] = []
    for d in dim_defs:
        field_defs = (d.get("qDef") or {}).get("qFieldDefs") or []
        col_names.append(field_defs[0] if field_defs else "dim")
        col_is_measure.append(False)
    for m in meas_defs:
        q_def = (m.get("qDef") or {})
        label = q_def.get("qLabel") or q_def.get("qDef") or "measure"
        col_names.append(label)
        col_is_measure.append(True)

    # De-dupe column names (append .1, .2 ... for collisions)
    seen: Dict[str, int] = {}
    unique_names: List[str] = []
    for name in col_names:
        if name in seen:
            seen[name] += 1
            unique_names.append(f"{name}.{seen[name]}")
        else:
            seen[name] = 0
            unique_names.append(name)

    parsed_rows: List[List[Any]] = []
    for row in rows:
        parsed: List[Any] = []
        for idx, cell in enumerate(row):
            if idx >= len(col_is_measure):
                parsed.append(cell.get("qText") if isinstance(cell, dict) else cell)
                continue
            if isinstance(cell, dict):
                qnum = cell.get("qNum")
                qtext = cell.get("qText")
                if col_is_measure[idx]:
                    if isinstance(qnum, (int, float)):
                        parsed.append(qnum)
                    else:
                        parsed.append(qtext)
                else:
                    parsed.append(qtext if qtext not in (None, "") else qnum)
            else:
                parsed.append(cell)
        parsed_rows.append(parsed)

    if not parsed_rows:
        return pd.DataFrame(columns=unique_names)
    return pd.DataFrame(parsed_rows, columns=unique_names)


async def fetch_hypercube_pages(
    qix: QixSession,
    obj_handle: int,
    width: int,
    max_rows: int,
) -> List[List[Dict[str, Any]]]:
    """Page ``GetHyperCubeData`` until ``max_rows`` or the cube is exhausted."""
    rows: List[List[Dict[str, Any]]] = []
    top = 0
    page_height = max(1, min(max_rows, 10000 // max(1, width)))
    while len(rows) < max_rows:
        height = min(page_height, max_rows - len(rows))
        page = await qix.rpc(obj_handle, "GetHyperCubeData", [
            "/qHyperCubeDef",
            [{"qTop": top, "qLeft": 0, "qWidth": width, "qHeight": height}],
        ])
        data_pages = page.get("qDataPages") or []
        if not data_pages:
            break
        matrix = data_pages[0].get("qMatrix") or []
        if not matrix:
            break
        rows.extend(matrix)
        if len(matrix) < height:
            break
        top += len(matrix)
    return rows
