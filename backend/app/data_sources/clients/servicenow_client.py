"""ServiceNow data source client.

Talks to the ServiceNow REST Table API (`/api/now/table/{table}`). There is no
SQL endpoint; queries are JSON specs (see `system_prompt`) that map 1:1 to a
Table API call with an encoded query (`sysparm_query`).

Schema discovery reads ServiceNow's own metadata tables in bulk
(`sys_db_object` for tables + inheritance, `sys_dictionary` for fields), so a
full snapshot — custom `u_*`/`x_*` tables included — is a handful of requests,
not one per table. Reference-type fields become foreign keys.
"""
import io
import json
import re
from contextlib import contextmanager
from typing import Generator, List, Optional, Tuple

import pandas as pd
import requests

from app.data_sources.clients._document_text import (
    DOC_EXTS,
    doc_text_is_usable,
    extract_document_text_from_bytes,
)
from app.data_sources.clients._file_source_common import DocumentText, NamedBytes
from app.data_sources.clients.base import Capability, DataSourceClient
from app.ai.prompt_formatters import ForeignKey, Table, TableColumn, ServiceFormatter


# Curated ITSM-centric default. Mirrors the Salesforce client's curated object
# list; admins extend/replace via the `tables` config or `discover_all`.
DEFAULT_TABLES = [
    "incident",
    "change_request",
    "problem",
    "task",
    "sc_request",
    "sc_req_item",
    "sc_task",
    "sys_user",
    "sys_user_group",
    "cmdb_ci",
    "kb_knowledge",
]

# Roots whose descendants count as "business" tables in discover_all mode.
DISCOVER_HIERARCHY_ROOTS = {"task", "cmdb_ci"}

# Table-name prefixes that are platform internals, never business data.
INTERNAL_PREFIXES = ("sys_", "v_", "ts_", "sysauto_", "syslog_", "sysevent")

# Standalone tables always worth exposing even though they start with sys_.
DISCOVER_ALLOWLIST = {"sys_user", "sys_user_group", "kb_knowledge"}

MAX_ROWS = 10_000          # hard cap per execute_query
PAGE_SIZE = 1_000          # rows per Table API page when paginating
METADATA_PAGE_SIZE = 10_000  # sys_dictionary/sys_db_object pages (API max)
DEFAULT_LIMIT = 100        # when the query spec omits `limit`
SAMPLE_ROWS = 20           # rows sampled per table in infer_schema_from_data mode

# ServiceNow internal_type → pandas-ish dtype for prompt schemas.
_TYPE_MAP = {
    "integer": "int",
    "longint": "int",
    "decimal": "float",
    "float": "float",
    "currency": "float",
    "price": "float",
    "boolean": "bool",
    "glide_date": "date",
    "glide_date_time": "datetime",
    "glide_time": "time",
    "glide_duration": "duration",
    "due_date": "datetime",
    "reference": "reference",
    "journal": "str",
    "journal_input": "str",
    "journal_list": "str",
    "string": "str",
    "translated_text": "str",
    "translated_field": "str",
    "choice": "str",
    "sys_class_name": "str",
    "table_name": "str",
    "field_name": "str",
    "GUID": "str",
    "domain_id": "str",
    "user_image": "str",
    "html": "str",
    "url": "str",
    "email": "str",
    "phone_number_e164": "str",
    "condition_string": "str",
    "user_roles": "str",
    "glide_list": "str",
    "documentation_field": "str",
    "password": "str",
}


def _scalar(value):
    """Table API values arrive either as scalars or as {link, value} objects
    (reference fields, and metadata rows like sys_dictionary.internal_type).
    Normalize to the scalar."""
    if isinstance(value, dict):
        return value.get("display_value", value.get("value"))
    return value


# Table API returns every value as a string, so dtypes in infer_schema_from_data
# mode come from matching those strings. Anchored and deliberately narrow: a
# false positive here mislabels a column in the agent's schema.
_RE_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$")
_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_RE_INT = re.compile(r"^-?\d+$")
_RE_FLOAT = re.compile(r"^-?\d+\.\d+$")


def _infer_dtype(value) -> Optional[str]:
    """Best-effort dtype for one sampled Table API value.

    Returns None when the value carries no type signal (empty/unset), so the
    caller can keep looking at other sampled rows instead of locking the column
    to `str` on the first blank.
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.lower() in ("true", "false"):
        return "bool"
    if _RE_DATETIME.match(text):
        return "datetime"
    if _RE_DATE.match(text):
        return "date"
    if _RE_TIME.match(text):
        return "time"
    if _RE_INT.match(text):
        return "int"
    if _RE_FLOAT.match(text):
        return "float"
    return "str"


def _reconcile_dtype(dtypes: List[str]) -> str:
    """Collapse the dtypes seen across sampled rows into one.

    Disagreement means the guess is unreliable, so widen to `str` rather than
    let the first row win — except int/float, where float is the safe superset.
    """
    distinct = set(dtypes)
    if not distinct:
        return "str"
    if len(distinct) == 1:
        return distinct.pop()
    if distinct == {"int", "float"}:
        return "float"
    return "str"


# Attachment API base path. Metadata queries and binary downloads both live
# here — the raw sys_attachment_doc chunk table is never touched (the binary
# is stored gzipped in base64 chunks; the API reassembles it server-side).
ATTACHMENT_API = "/api/now/attachment"

# Default table whose attachments are exposed via the file tools.
DEFAULT_ATTACHMENT_TABLES = "kb_knowledge"

# Max attachments returned by one list_files/search_files call.
ATTACHMENT_LIST_LIMIT = 500

_RE_SYS_ID = re.compile(r"^[0-9a-f]{32}$")

# Extensions parsed as tabular / plain text in read_file. Anything else that
# isn't a DOC_EXTS document comes back as NamedBytes for the vision fallback.
_TABULAR_EXTS = {"csv", "tsv", "xlsx", "xls"}
_TEXT_EXTS = {"txt", "md", "html", "htm", "log", "yaml", "yml", "xml"}


class ServiceNowClient(DataSourceClient):
    # Hybrid connector: tabular Table API queries plus record attachments as
    # files (KB articles by default). The class advertises the file
    # capabilities so the tools appear in the agent catalog; an instance with
    # enable_attachments=False narrows itself back to QUERY-only below, so
    # runtime resolution rejects file calls (the network_dir WRITE_FILE
    # pattern).
    capabilities = {
        Capability.QUERY,
        Capability.LIST_FILES,
        Capability.READ_FILE,
        Capability.SEARCH_FILES,
    }

    # Listing attachments is one Attachment API call — cheap enough to serve
    # list_files live from this connection instead of the shared catalog cache
    # (attachments are deliberately never indexed into the catalog).
    cheap_live_listing = True

    def __init__(
        self,
        instance_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        access_token: Optional[str] = None,
        tables: Optional[str] = None,
        discover_all: bool = False,
        display_values: bool = True,
        verify_ssl: bool = True,
        infer_schema_from_data: bool = False,
        enable_attachments: bool = True,
        attachment_tables: Optional[str] = None,
    ):
        self.instance_url = (instance_url or "").rstrip("/")
        self.username = username
        self.password = password
        # Per-user delegated OAuth: when present, requests authenticate with
        # `Authorization: Bearer` instead of basic auth, so Table API ACLs
        # apply as the signed-in user. Populated from user_connection_credentials
        # (construct_client passes the token fields the signature accepts).
        self.access_token = access_token
        self.tables = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
        self.discover_all = discover_all
        self.display_values = display_values
        self.verify_ssl = bool(verify_ssl)
        # Derive schemas from sampled rows instead of sys_dictionary/sys_db_object.
        # For users granted read on the business tables but not on ServiceNow's
        # metadata tables (a common ACL posture for scoped integration accounts).
        self.infer_schema_from_data = bool(infer_schema_from_data)
        self.enable_attachments = bool(enable_attachments)
        self.attachment_tables = [
            t.strip() for t in (attachment_tables or DEFAULT_ATTACHMENT_TABLES).split(",")
            if t.strip()
        ]
        if not self.enable_attachments:
            self.capabilities = {Capability.QUERY}

    # ── connection ──────────────────────────────────────────────────────────

    @contextmanager
    def connect(self) -> Generator[requests.Session, None, None]:
        if not self.access_token and not (self.username or self.password):
            raise RuntimeError(
                "ServiceNow client has no credentials: provide username/password or sign in with OAuth."
            )
        session = requests.Session()
        session.verify = self.verify_ssl
        session.headers.update({"Accept": "application/json"})
        if self.access_token:
            session.headers["Authorization"] = f"Bearer {self.access_token}"
        else:
            session.auth = (self.username, self.password)
        try:
            yield session
        finally:
            session.close()

    def _get(self, session: requests.Session, path: str, params: dict) -> dict:
        response = session.get(f"{self.instance_url}{path}", params=params, timeout=120)
        if response.status_code == 401:
            if self.access_token:
                raise RuntimeError(
                    "ServiceNow authentication failed (401): the OAuth token was rejected "
                    "(expired or revoked) — sign in again."
                )
            raise RuntimeError("ServiceNow authentication failed (401): check username/password.")
        if response.status_code == 403:
            raise RuntimeError(
                "ServiceNow access denied (403): the user lacks an ACL/role for this resource."
            )
        if response.status_code == 404:
            raise RuntimeError(
                f"ServiceNow resource not found (404) at {path}: check the instance URL and table name."
            )
        if response.status_code == 429:
            raise RuntimeError("ServiceNow rate limit hit (429): retry later or reduce query volume.")
        if response.status_code != 200:
            detail = response.text[:500]
            try:
                detail = response.json().get("error", {}).get("message", detail)
            except Exception:
                pass
            raise RuntimeError(f"ServiceNow API error ({response.status_code}): {detail}")
        return response.json()

    def _can_read_table(self, session: requests.Session, table: str) -> bool:
        try:
            self._get(session, f"/api/now/table/{table}", {"sysparm_limit": 1})
            return True
        except RuntimeError:
            return False

    def test_connection(self):
        try:
            with self.connect() as session:
                if self.infer_schema_from_data:
                    # Probe a configured table rather than sys_user/sys_dictionary:
                    # in this mode the account is assumed to lack both, so the
                    # standard probes below would report a false negative.
                    targets = self._targets_without_metadata()
                    reachable = [
                        t for t in targets
                        if self._can_read_table(session, t)
                    ]
                    if not reachable:
                        return {
                            "success": False,
                            "message": (
                                "Connected, but none of the configured tables could be read: "
                                f"{', '.join(targets)}. Check the table names and the user's ACLs."
                            ),
                        }
                    skipped = [t for t in targets if t not in reachable]
                    message = f"Connected to ServiceNow ({len(reachable)} table(s) readable)"
                    if skipped:
                        message += f"; no access to: {', '.join(skipped)}"
                    return {"success": True, "message": message}
                self._get(session, "/api/now/table/sys_user", {"sysparm_limit": 1})
                # Metadata access is required for schema discovery and fails
                # SILENTLY for under-privileged users (200 + empty result while
                # X-Total-Count shows the truth) — so probe it explicitly here.
                meta = self._get(
                    session,
                    "/api/now/table/sys_dictionary",
                    {"sysparm_query": "name=task", "sysparm_limit": 1, "sysparm_fields": "element"},
                )
                if not meta.get("result"):
                    return {
                        "success": False,
                        "message": (
                            "Connected, but the user cannot read schema metadata (sys_dictionary "
                            "returned empty). Grant read access to sys_db_object, sys_dictionary "
                            "and sys_glide_object (e.g. via a custom role), then retry."
                        ),
                    }
                return {"success": True, "message": "Connected to ServiceNow"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── schema discovery ────────────────────────────────────────────────────

    def _fetch_all(self, session: requests.Session, path: str, params: dict) -> List[dict]:
        """Paginate a Table API endpoint to exhaustion (metadata dumps)."""
        rows: List[dict] = []
        offset = 0
        while True:
            page = self._get(
                session,
                path,
                {**params, "sysparm_limit": METADATA_PAGE_SIZE, "sysparm_offset": offset},
            )
            batch = page.get("result", [])
            rows.extend(batch)
            if len(batch) < METADATA_PAGE_SIZE:
                return rows
            offset += METADATA_PAGE_SIZE

    def _table_hierarchy(self, session: requests.Session) -> dict:
        """name → {label, parent} for every table, from one sys_db_object dump."""
        rows = self._fetch_all(
            session,
            "/api/now/table/sys_db_object",
            {"sysparm_fields": "name,label,super_class.name"},
        )
        return {
            r["name"]: {"label": _scalar(r.get("label")), "parent": _scalar(r.get("super_class.name")) or None}
            for r in rows
            if r.get("name")
        }

    def _ancestors(self, table: str, hierarchy: dict) -> List[str]:
        chain, seen = [], set()
        current = hierarchy.get(table, {}).get("parent")
        while current and current not in seen:
            chain.append(current)
            seen.add(current)
            current = hierarchy.get(current, {}).get("parent")
        return chain

    def _target_tables(self, hierarchy: dict) -> List[str]:
        if self.tables:
            return self.tables
        if not self.discover_all:
            return DEFAULT_TABLES

        def is_business(name: str) -> bool:
            if name in DISCOVER_ALLOWLIST:
                return True
            if name.startswith("u_") or name.startswith("x_"):
                return True
            if name.startswith(INTERNAL_PREFIXES):
                return False
            root = name
            seen = set()
            while root in hierarchy and hierarchy[root]["parent"] and root not in seen:
                seen.add(root)
                root = hierarchy[root]["parent"]
            return root in DISCOVER_HIERARCHY_ROOTS or name in DISCOVER_HIERARCHY_ROOTS

        return sorted(name for name in hierarchy if is_business(name))

    def _dictionary_rows(self, session: requests.Session, tables: List[str]) -> List[dict]:
        """Bulk-fetch sys_dictionary rows for the given tables (batched IN queries)."""
        rows: List[dict] = []
        BATCH = 50
        for i in range(0, len(tables), BATCH):
            batch = tables[i:i + BATCH]
            rows.extend(self._fetch_all(
                session,
                "/api/now/table/sys_dictionary",
                {
                    "sysparm_query": f"nameIN{','.join(batch)}^elementISNOTEMPTY",
                    "sysparm_fields": "name,element,internal_type,reference.name,column_label",
                },
            ))
        return rows

    # ── schema discovery without metadata access ────────────────────────────

    def _targets_without_metadata(self) -> List[str]:
        """Target tables when sys_db_object is off-limits, so no hierarchy exists."""
        if self.tables:
            return self.tables
        if self.discover_all:
            raise RuntimeError(
                "'Discover All Tables' requires read access to sys_db_object, which "
                "'Infer Schema From Data' assumes is unavailable. List the tables "
                "explicitly in the Tables field, or grant metadata read access."
            )
        return DEFAULT_TABLES

    def _sample_rows(self, session: requests.Session, table: str) -> List[dict]:
        """Fetch a few raw rows of `table` to read its shape off the response.

        Raw values (no display values, no reference links) keep dtype inference
        stable: a reference comes back as a sys_id string rather than a display
        name whose type varies with the referenced record.
        """
        page = self._get(
            session,
            f"/api/now/table/{table}",
            {
                "sysparm_limit": SAMPLE_ROWS,
                "sysparm_display_value": "false",
                "sysparm_exclude_reference_link": "true",
            },
        )
        return page.get("result", [])

    def _build_table_from_sample(self, name: str, rows: List[dict]) -> Table:
        """Build a Table from sampled rows.

        No foreign keys: the Table API response cannot say which table a
        reference field points at — that mapping lives only in
        sys_dictionary.reference. Callers get columns and a sys_id primary key.
        """
        dtypes_seen: dict = {}
        for row in rows:
            for key, value in row.items():
                dtype = _infer_dtype(_scalar(value))
                bucket = dtypes_seen.setdefault(key, [])
                if dtype is not None:
                    bucket.append(dtype)

        columns = [
            TableColumn(name=key, dtype=_reconcile_dtype(seen))
            for key, seen in dtypes_seen.items()
        ]
        if "sys_id" not in dtypes_seen:
            columns.insert(0, TableColumn(name="sys_id", dtype="str"))
        return Table(
            name=name,
            columns=columns,
            pks=[TableColumn(name="sys_id", dtype="str")],
            fks=[],
        )

    def _schemas_from_data(self, session: requests.Session) -> List[Table]:
        """Sample every target table, tolerating individually inaccessible ones.

        A single 403 must not sink the whole index here: the reason to be in this
        mode at all is a narrowly-scoped account, so some configured tables being
        unreadable is expected. Only a clean sweep of failures is fatal.
        """
        targets = self._targets_without_metadata()
        schemas: List[Table] = []
        failures: List[str] = []
        for target in targets:
            try:
                rows = self._sample_rows(session, target)
            except RuntimeError as e:
                failures.append(f"{target}: {e}")
                continue
            if not rows:
                failures.append(
                    f"{target}: no rows returned, so no columns could be inferred "
                    "(the table may be empty or fully filtered by ACLs)"
                )
                continue
            schemas.append(self._build_table_from_sample(target, rows))
        if not schemas:
            raise RuntimeError(
                "Could not infer a schema for any configured table. Details — "
                + "; ".join(failures)
            )
        return schemas

    def get_schemas(self) -> List[Table]:
        if self.infer_schema_from_data:
            with self.connect() as session:
                return self._schemas_from_data(session)
        with self.connect() as session:
            hierarchy = self._table_hierarchy(session)
            targets = self._target_tables(hierarchy)

            # Fields live on the table that DEFINES them, so a child table's
            # full schema = its rows + its ancestors' rows (incident extends task).
            needed = list(dict.fromkeys(
                [t for t in targets] +
                [a for t in targets for a in self._ancestors(t, hierarchy)]
            ))
            dict_rows = self._dictionary_rows(session, needed)
            if targets and not dict_rows:
                raise RuntimeError(
                    "sys_dictionary returned no rows — the ServiceNow user lacks metadata read "
                    "access (this fails silently with HTTP 200). Grant read on sys_db_object, "
                    "sys_dictionary and sys_glide_object."
                )

            by_table: dict = {}
            for row in dict_rows:
                by_table.setdefault(_scalar(row.get("name")), []).append(row)

            schemas = []
            for target in targets:
                table = self._build_table(target, hierarchy, by_table)
                if table.columns:
                    schemas.append(table)
            return schemas

    def _build_table(self, name: str, hierarchy: dict, by_table: dict) -> Table:
        columns: List[TableColumn] = []
        fks: List[ForeignKey] = []
        seen = set()
        # Own fields first, then inherited (parents last so overrides win visually).
        for source in [name] + self._ancestors(name, hierarchy):
            for row in by_table.get(source, []):
                element = _scalar(row.get("element"))
                if not element or element in seen:
                    continue
                seen.add(element)
                internal_type = _scalar(row.get("internal_type")) or "string"
                dtype = _TYPE_MAP.get(internal_type, "str")
                column = TableColumn(name=element, dtype=dtype)
                columns.append(column)
                reference = _scalar(row.get("reference.name"))
                if internal_type == "reference" and reference:
                    fks.append(ForeignKey(
                        column=column,
                        references_name=reference,
                        references_column=TableColumn(name="sys_id", dtype="str"),
                    ))
        if "sys_id" not in seen:
            columns.insert(0, TableColumn(name="sys_id", dtype="str"))
        return Table(
            name=name,
            columns=columns,
            pks=[TableColumn(name="sys_id", dtype="str")],
            fks=fks,
        )

    def get_schema(self, table_name: str) -> Table:
        if self.infer_schema_from_data:
            with self.connect() as session:
                return self._build_table_from_sample(
                    table_name, self._sample_rows(session, table_name)
                )
        with self.connect() as session:
            hierarchy = self._table_hierarchy(session)
            needed = [table_name] + self._ancestors(table_name, hierarchy)
            dict_rows = self._dictionary_rows(session, needed)
            by_table: dict = {}
            for row in dict_rows:
                by_table.setdefault(_scalar(row.get("name")), []).append(row)
            return self._build_table(table_name, hierarchy, by_table)

    # ── querying ────────────────────────────────────────────────────────────

    def execute_query(self, query: str) -> pd.DataFrame:
        """Execute a ServiceNow query spec and return a DataFrame.

        `query` is a JSON string:
            {"table": "incident",                       (required)
             "query": "active=true^priority=1",         (optional encoded query)
             "fields": ["number", "short_description"], (optional)
             "limit": 100}                              (optional, default 100)
        """
        spec = self._parse_spec(query)
        table = spec["table"]
        limit = min(int(spec.get("limit") or DEFAULT_LIMIT), MAX_ROWS)

        params = {
            "sysparm_display_value": "true" if self.display_values else "false",
            "sysparm_exclude_reference_link": "true",
        }
        if spec.get("query"):
            params["sysparm_query"] = spec["query"]
        if spec.get("fields"):
            fields = spec["fields"]
            params["sysparm_fields"] = ",".join(fields) if isinstance(fields, list) else str(fields)

        rows: List[dict] = []
        with self.connect() as session:
            offset = 0
            while len(rows) < limit:
                page_size = min(PAGE_SIZE, limit - len(rows))
                page = self._get(
                    session,
                    f"/api/now/table/{table}",
                    {**params, "sysparm_limit": page_size, "sysparm_offset": offset},
                )
                batch = page.get("result", [])
                rows.extend(batch)
                if len(batch) < page_size:
                    break
                offset += page_size

        df = pd.DataFrame([{k: _scalar(v) for k, v in row.items()} for row in rows])
        return df

    def _parse_spec(self, query) -> dict:
        if isinstance(query, dict):
            spec = query
        else:
            try:
                spec = json.loads(query)
            except (TypeError, json.JSONDecodeError):
                raise ValueError(
                    "ServiceNow query must be a JSON object like "
                    '{"table": "incident", "query": "active=true", "limit": 100} — got: '
                    f"{str(query)[:200]}"
                )
        if not isinstance(spec, dict) or not spec.get("table"):
            raise ValueError('ServiceNow query spec must include a "table" key.')
        return spec

    # ── attachments (file capabilities) ─────────────────────────────────────
    #
    # Record attachments exposed as files. Metadata lives in sys_attachment
    # (file_name, content_type, size_bytes, table_name + table_sys_id pointing
    # at the owning record); binaries are served reassembled by the Attachment
    # API. Inline images embedded in article HTML are stored under a
    # "ZZ_YY"-prefixed table_name, so filtering by the real table names also
    # keeps that noise out of listings.

    def _require_attachments(self):
        if not self.enable_attachments:
            raise RuntimeError(
                "Attachments are disabled on this ServiceNow connection "
                "(enable_attachments=false)."
            )

    def _attachment_scope_query(self, folder_id: Optional[str]) -> str:
        """Encoded query for a listing scope.

        Folder conventions (mirrors the path-shaped ids the entries carry):
          None                      → all configured attachment tables
          "kb_knowledge"            → one table
          "kb_knowledge/<sys_id>"   → one record's attachments
        """
        if folder_id:
            scope = str(folder_id).strip().strip("/")
            if "/" in scope:
                table, record = scope.split("/", 1)
                return f"table_name={table}^table_sys_id={record.split('/', 1)[0]}"
            return f"table_name={scope}"
        return "table_nameIN" + ",".join(self.attachment_tables)

    def _attachment_entry(self, row: dict) -> dict:
        table = _scalar(row.get("table_name")) or ""
        record = _scalar(row.get("table_sys_id")) or ""
        name = _scalar(row.get("file_name")) or _scalar(row.get("sys_id"))
        size = _scalar(row.get("size_bytes"))
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = None
        return {
            "id": _scalar(row.get("sys_id")),
            "name": name,
            "path": f"{table}/{record}/{name}",
            "mime_type": _scalar(row.get("content_type")),
            "size": size,
            "modified_at": _scalar(row.get("sys_updated_on")),
            "is_folder": False,
            "web_url": f"{self.instance_url}/sys_attachment.do?sys_id={_scalar(row.get('sys_id'))}",
        }

    _ATTACHMENT_FIELDS = "sys_id,file_name,content_type,size_bytes,table_name,table_sys_id,sys_updated_on"

    def _list_attachments(self, session: requests.Session, query: str, limit: int) -> List[dict]:
        page = self._get(
            session,
            ATTACHMENT_API,
            {
                "sysparm_query": query,
                "sysparm_limit": min(int(limit or ATTACHMENT_LIST_LIMIT), ATTACHMENT_LIST_LIMIT),
                "sysparm_fields": self._ATTACHMENT_FIELDS,
            },
        )
        return [self._attachment_entry(r) for r in page.get("result", [])]

    def list_files(self, folder_id: Optional[str] = None, recursive: bool = False,
                   limit: Optional[int] = None) -> List[dict]:
        self._require_attachments()
        with self.connect() as session:
            return self._list_attachments(
                session, self._attachment_scope_query(folder_id), limit or ATTACHMENT_LIST_LIMIT
            )

    def search_files(self, query: str, **_) -> List[dict]:
        self._require_attachments()
        safe = str(query or "").strip()
        if not safe:
            return []
        scope = "table_nameIN" + ",".join(self.attachment_tables)
        with self.connect() as session:
            return self._list_attachments(
                session, f"file_nameLIKE{safe}^{scope}", ATTACHMENT_LIST_LIMIT
            )

    def _attachment_meta(self, session: requests.Session, file_id: str) -> dict:
        """Resolve a file id to its sys_attachment metadata row.

        Accepts the attachment sys_id (the canonical id), the path-shaped id
        the listings hand out (table/record_sys_id/file_name), or a bare file
        name searched across the configured attachment tables — the model
        frequently passes the readable name it saw.
        """
        fid = str(file_id or "").strip().strip("/")
        if _RE_SYS_ID.match(fid):
            data = self._get(session, f"{ATTACHMENT_API}/{fid}", {})
            meta = data.get("result")
            if meta:
                return meta
            raise ValueError(f"ServiceNow attachment '{fid}' not found.")
        if "/" in fid:
            table, rest = fid.split("/", 1)
            record, _, name = rest.partition("/")
            query = f"table_name={table}^table_sys_id={record}"
            if name:
                query += f"^file_name={name}"
        else:
            scope = "table_nameIN" + ",".join(self.attachment_tables)
            query = f"file_name={fid}^{scope}"
        page = self._get(
            session, ATTACHMENT_API,
            {"sysparm_query": query, "sysparm_limit": 1},
        )
        rows = page.get("result", [])
        if not rows:
            raise ValueError(
                f"ServiceNow attachment '{file_id}' not found — pass the attachment "
                "sys_id or the table/record/file_name path from list_files."
            )
        return rows[0]

    def _download_attachment(self, session: requests.Session, sys_id: str) -> bytes:
        response = session.get(
            f"{self.instance_url}{ATTACHMENT_API}/{sys_id}/file",
            headers={"Accept": "*/*"},
            timeout=120,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"ServiceNow attachment download failed ({response.status_code}) for {sys_id}."
            )
        return response.content

    def read_file(self, file_id: str, max_bytes: Optional[int] = None, **_ignored):
        """Read one attachment. Same contract as the drive clients:
        tabular → DataFrame, text → str, documents → extracted text
        (DocumentText carrying the original bytes), everything else →
        NamedBytes so the tool layer keeps its vision/preview fallbacks
        (attachment sys_ids are opaque — the payload must carry the name)."""
        self._require_attachments()
        with self.connect() as session:
            meta = self._attachment_meta(session, file_id)
            name = _scalar(meta.get("file_name")) or str(file_id)
            mime = _scalar(meta.get("content_type")) or ""
            content = self._download_attachment(session, _scalar(meta.get("sys_id")))
        if max_bytes and len(content) > int(max_bytes):
            content = content[: int(max_bytes)]

        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in DOC_EXTS:
            text = extract_document_text_from_bytes(content, name)
            if doc_text_is_usable(text, ext):
                return DocumentText(text, raw=content, name=name, mime=mime)
            return NamedBytes(content, name=name, mime=mime)
        if ext in _TABULAR_EXTS:
            try:
                if ext == "csv":
                    return pd.read_csv(io.BytesIO(content))
                if ext == "tsv":
                    return pd.read_csv(io.BytesIO(content), sep="\t")
                return pd.read_excel(io.BytesIO(content))
            except Exception:
                return NamedBytes(content, name=name, mime=mime)
        if ext == "json":
            try:
                return json.loads(content.decode("utf-8", errors="replace"))
            except Exception:
                return content.decode("utf-8", errors="replace")
        if ext in _TEXT_EXTS or mime.startswith("text/"):
            return content.decode("utf-8", errors="replace")
        return NamedBytes(content, name=name, mime=mime)

    def read_raw_bytes(self, file_id: str) -> Tuple[bytes, str, Optional[str]]:
        """Original bytes + name + mime, unparsed — for the viewer/attach paths."""
        self._require_attachments()
        with self.connect() as session:
            meta = self._attachment_meta(session, file_id)
            content = self._download_attachment(session, _scalar(meta.get("sys_id")))
            return (
                content,
                _scalar(meta.get("file_name")) or str(file_id),
                _scalar(meta.get("content_type")) or None,
            )

    def file_version(self, file_id: str) -> Optional[str]:
        """Cheap staleness token (metadata only, no download): updated_on + size."""
        if not self.enable_attachments:
            return None
        try:
            with self.connect() as session:
                meta = self._attachment_meta(session, file_id)
            return f"{_scalar(meta.get('sys_updated_on'))}|{_scalar(meta.get('size_bytes'))}"
        except Exception:
            return None

    # ── prompts ─────────────────────────────────────────────────────────────

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    def system_prompt(self):
        text = """
        ## ServiceNow Integration
        Query ServiceNow via `execute_query(query)` where `query` is a JSON string:

        ```json
        {"table": "incident",
         "query": "active=true^priority=1^ORDERBYDESCsys_created_on",
         "fields": ["number", "short_description", "priority", "state", "assigned_to"],
         "limit": 100}
        ```

        - `table` (required): the ServiceNow table, e.g. incident, change_request,
          problem, sc_request, sys_user, cmdb_ci.
        - `query` (optional): a ServiceNow *encoded query*. Operators:
          `^` = AND, `^OR` = OR, `field=value`, `field!=value`, `fieldLIKEvalue`,
          `fieldSTARTSWITHvalue`, `fieldISEMPTY`, `fieldISNOTEMPTY`,
          `field>value` / `field<value` (also for dates),
          `ORDERBYfield` / `ORDERBYDESCfield`.
          Relative dates: `sys_created_on>javascript:gs.beginningOfLastMonth()`,
          `opened_at>javascript:gs.daysAgoStart(7)`.
          Dot-walk reference fields: `assigned_to.name=Beth Anglin`,
          `caller_id.department.name=IT`.
        - `fields` (optional): columns to return; omit for all.
        - `limit` (optional): max rows, default 100.

        IMPORTANT — results return DISPLAY VALUES (human-readable strings), not
        raw codes: `priority` is "1 - Critical" (not 1), `state` is
        "In Progress" (not 2), reference fields are names ("Beth Anglin"), and
        booleans/dates are strings. Never `pd.to_numeric` such columns directly —
        parse labels (e.g. `df["priority"].str.split(" - ").str[0].astype(int)`)
        or group by the label as-is. Encoded-query FILTER values still use raw
        codes: `priority=1`, `active=true`, `state=2`.
        Aggregate by fetching rows and grouping in pandas.

        Examples:
        ```python
        df = client.execute_query('{"table": "incident", "query": "active=true^ORDERBYDESCopened_at", "fields": ["number", "short_description", "priority", "state", "opened_at", "assigned_to"], "limit": 200}')
        df = client.execute_query('{"table": "sys_user", "query": "active=true", "fields": ["name", "email", "department"], "limit": 500}')
        ```
        """
        if self.enable_attachments:
            text += (
                "\n        ### Attachments (Knowledge Base files etc.)\n"
                "        Record attachments are FILES — use the file tools, not execute_query:\n"
                f"        - list_files(folder_id=None) lists attachments on: {', '.join(self.attachment_tables)}.\n"
                "          folder_id can scope to a table ('kb_knowledge') or one record\n"
                "          ('kb_knowledge/<sys_id>' — take sys_id from a query result).\n"
                "        - read_file(file_id=<attachment id from list_files>) downloads and reads it\n"
                "          (documents return extracted text; images/PDFs render for viewing).\n"
                "        - search_files(query) matches attachment file names.\n"
                "        The article BODY is the kb_knowledge.text column (HTML) via execute_query;\n"
                "        the attachments are separate files on the article.\n"
            )
        return text

    @property
    def description(self):
        text = "ServiceNow client — query ITSM data (incidents, changes, requests, CMDB, users) via the Table API."
        return text + "\n\n" + self.system_prompt()
