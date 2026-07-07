"""ServiceNow data source client.

Talks to the ServiceNow REST Table API (`/api/now/table/{table}`). There is no
SQL endpoint; queries are JSON specs (see `system_prompt`) that map 1:1 to a
Table API call with an encoded query (`sysparm_query`).

Schema discovery reads ServiceNow's own metadata tables in bulk
(`sys_db_object` for tables + inheritance, `sys_dictionary` for fields), so a
full snapshot — custom `u_*`/`x_*` tables included — is a handful of requests,
not one per table. Reference-type fields become foreign keys.
"""
import json
from contextlib import contextmanager
from typing import Generator, List, Optional

import pandas as pd
import requests

from app.data_sources.clients.base import DataSourceClient
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


class ServiceNowClient(DataSourceClient):

    def __init__(
        self,
        instance_url: str,
        username: str,
        password: str,
        tables: Optional[str] = None,
        discover_all: bool = False,
        display_values: bool = True,
    ):
        self.instance_url = (instance_url or "").rstrip("/")
        self.username = username
        self.password = password
        self.tables = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
        self.discover_all = discover_all
        self.display_values = display_values

    # ── connection ──────────────────────────────────────────────────────────

    @contextmanager
    def connect(self) -> Generator[requests.Session, None, None]:
        session = requests.Session()
        session.auth = (self.username, self.password)
        session.headers.update({"Accept": "application/json"})
        try:
            yield session
        finally:
            session.close()

    def _get(self, session: requests.Session, path: str, params: dict) -> dict:
        response = session.get(f"{self.instance_url}{path}", params=params, timeout=120)
        if response.status_code == 401:
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

    def test_connection(self):
        try:
            with self.connect() as session:
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

    def get_schemas(self) -> List[Table]:
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
        return text

    @property
    def description(self):
        text = "ServiceNow client — query ITSM data (incidents, changes, requests, CMDB, users) via the Table API."
        return text + "\n\n" + self.system_prompt()
