"""Dremio client — read-only SQL over Dremio Software or Dremio Cloud.

Uses Dremio's REST API (submit a SQL job, poll it, page through results) so
the same code path serves Dremio Software (`/api/v3`) and Dremio Cloud
(`/v0/projects/<id>`), with no driver to install. Auth: a personal access
token (both editions) or username/password (Software).

The catalog comes from INFORMATION_SCHEMA: every table and view (virtual
dataset) under the configured spaces/sources, with typed columns. Table
names are the full dotted Dremio path, exactly as SQL addresses them.

Queries are single read statements (checked by the SQL guard in the dremio
dialect before submission); results are paged and capped at `max_rows`, and
a job that exceeds the time budget is cancelled.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd
import requests

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.core.egress import pinned_session
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import discovery_items, discovery_phase, discovery_progress

logger = logging.getLogger(__name__)

PAGE = 500  # the REST results endpoint's maximum page size
_SYSTEM_SCHEMAS = ("INFORMATION_SCHEMA", "sys", "$scratch")


class DremioClient(DataSourceClient):

    # Dremio SQL (Calcite): '' only (sqlglot dremio).
    sql_literal_style = "standard"

    relative_date_hint = "Relative dates (Dremio SQL): CURRENT_DATE, DATE_SUB(CURRENT_DATE, 7), DATE_TRUNC('MONTH', CURRENT_DATE)."

    def __init__(
        self,
        url: str,
        project_id: str | None = None,
        spaces: str | None = None,
        verify_ssl: bool = True,
        max_rows: int = 100_000,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout_seconds: int = 300,
        **_ignored,
    ):
        super().__init__()
        self.url = (url or "").strip().rstrip("/")
        self.project_id = (project_id or "").strip() or None
        self.spaces = [s.strip() for s in (spaces or "").replace("\n", ",").split(",") if s.strip()]
        self.verify_ssl = bool(verify_ssl)
        self.max_rows = max(1, int(max_rows or 100_000))
        self.token = token
        self.username = username
        self.password = password
        self.timeout_seconds = int(timeout_seconds or 300)
        self._http: requests.Session | None = None

    # ------------------------------------------------------------------ http

    @property
    def _api(self) -> str:
        if self.project_id:
            return f"{self.url}/v0/projects/{self.project_id}"
        return f"{self.url}/api/v3"

    def _session(self) -> requests.Session:
        if self._http is not None:
            return self._http
        # Pinned: the outbound policy applies to every connection, redirects included.
        s = pinned_session()
        s.verify = self.verify_ssl
        if self.token:
            s.headers["Authorization"] = f"Bearer {self.token}"
        elif self.username:
            r = s.post(f"{self.url}/apiv2/login", json={"userName": self.username, "password": self.password or ""}, timeout=30)
            if r.status_code != 200:
                raise ValueError(f"Dremio login failed ({r.status_code}): {_err(r)}")
            s.headers["Authorization"] = f"_dremio{r.json()['token']}"
        else:
            raise ValueError("Provide a personal access token or a username and password.")
        s.headers["Content-Type"] = "application/json"
        self._http = s
        return s

    def connect(self):
        return self._session()

    def _request(self, method: str, path: str, **kw) -> Any:
        r = self._session().request(method, f"{self._api}{path}", timeout=60, **kw)
        if r.status_code >= 400:
            raise ValueError(f"Dremio API {method} {path} failed ({r.status_code}): {_err(r)}")
        return r.json() if r.content else {}

    # ------------------------------------------------------------------- jobs

    def _run(self, sql: str, max_rows: int) -> pd.DataFrame:
        job_id = self._request("POST", "/sql", json={"sql": sql})["id"]
        deadline = time.monotonic() + self.timeout_seconds
        delay = 0.2
        while True:
            job = self._request("GET", f"/job/{job_id}")
            state = job.get("jobState")
            if state == "COMPLETED":
                break
            if state in ("FAILED", "CANCELED", "CANCELLED"):
                raise ValueError(f"Dremio query {state.lower()}: {job.get('errorMessage') or job.get('cancellationReason') or ''}".strip())
            if time.monotonic() > deadline:
                try:
                    self._request("POST", f"/job/{job_id}/cancel")
                finally:
                    raise TimeoutError(f"Dremio query exceeded {self.timeout_seconds}s and was cancelled.")
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)

        total = int(job.get("rowCount") or 0)
        rows: list[dict[str, Any]] = []
        columns: list[str] = []
        offset = 0
        while offset < min(total, max_rows):
            page = self._request("GET", f"/job/{job_id}/results", params={"offset": offset, "limit": min(PAGE, max_rows - offset)})
            if not columns:
                columns = [c["name"] for c in page.get("schema", [])]
            batch = page.get("rows") or []
            if not batch:
                break
            rows.extend(batch)
            offset += len(batch)
        if not columns and total == 0:
            page = self._request("GET", f"/job/{job_id}/results", params={"offset": 0, "limit": 1})
            columns = [c["name"] for c in page.get("schema", [])]
        df = pd.DataFrame(rows, columns=columns or None)
        df.attrs["truncated"] = total > len(rows)
        return df

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Run one read-only Dremio SQL statement → DataFrame (capped at max_rows)."""
        from app.ai.code_execution.sql_guard import check_sql

        check_sql(sql, "dremio")
        return self._run(sql.strip().rstrip(";"), self.max_rows)

    # ---------------------------------------------------------------- catalog

    def _scope_clause(self, col: str) -> str:
        excluded = ", ".join(f"'{s}'" for s in _SYSTEM_SCHEMAS)
        clause = f"{col} NOT IN ({excluded}) AND {col} NOT LIKE '@%'"
        if self.spaces:
            ors = " OR ".join(
                f"{col} = '{_q(s)}' OR {col} LIKE '{_q(s)}.%'" for s in self.spaces
            )
            clause += f" AND ({ors})"
        return clause

    def get_tables(self) -> list[Table]:
        discovery_phase("reading_tables")
        tables_df = self._run(
            'SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA."TABLES" '
            f"WHERE {self._scope_clause('TABLE_SCHEMA')} AND TABLE_TYPE <> 'SYSTEM_TABLE'",
            1_000_000,
        )
        discovery_phase("reading_columns")
        cols_df = self._run(
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, ORDINAL_POSITION "
            f'FROM INFORMATION_SCHEMA."COLUMNS" WHERE {self._scope_clause("TABLE_SCHEMA")}',
            5_000_000,
        )
        by_table: dict[tuple, list[TableColumn]] = {}
        if not cols_df.empty:
            cols_df = cols_df.sort_values(["TABLE_SCHEMA", "TABLE_NAME", "ORDINAL_POSITION"])
            for r in cols_df.itertuples(index=False):
                by_table.setdefault((r.TABLE_SCHEMA, r.TABLE_NAME), []).append(
                    TableColumn(name=r.COLUMN_NAME, dtype=str(r.DATA_TYPE))
                )
        out: list[Table] = []
        rows = list(tables_df.itertuples(index=False)) if not tables_df.empty else []
        for r in discovery_items(rows, "tables", label=lambda r: f"{r.TABLE_SCHEMA}.{r.TABLE_NAME}"):
            kind = "View (virtual dataset)" if str(r.TABLE_TYPE).upper() == "VIEW" else "Table"
            out.append(Table(
                name=f"{r.TABLE_SCHEMA}.{r.TABLE_NAME}",
                description=f"{kind}. Query as {_sql_path(r.TABLE_SCHEMA, r.TABLE_NAME)}.",
                columns=by_table.get((r.TABLE_SCHEMA, r.TABLE_NAME), []),
                pks=[],
                fks=[],
                metadata_json={"dremio": {"schema": r.TABLE_SCHEMA, "type": r.TABLE_TYPE,
                                          "sql_path": _sql_path(r.TABLE_SCHEMA, r.TABLE_NAME)}},
            ))
        return out

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> list[Table]:
        return self.get_tables()

    def get_schema(self, table_name: str) -> Table | None:
        for t in self.get_schemas():
            if t.name == table_name:
                return t
        return None

    def prompt_schema(self) -> str:
        return TableFormatter(self.get_schemas()).table_str

    # ------------------------------------------------------------ lifecycle

    def test_connection(self):
        try:
            df = self._run("SELECT 1 AS ok", 1)
            if df.empty:
                raise ValueError("Dremio returned no rows for SELECT 1")
            edition = "Dremio Cloud" if self.project_id else "Dremio"
            return {"success": True, "message": f"Connected to {edition} at {self.url}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self) -> str:
        return f"""Dremio lakehouse at {self.url} — read-only SQL (Dremio / Apache Calcite dialect).

Use client.execute_query(sql) → pandas DataFrame (capped at {self.max_rows} rows; df.attrs['truncated'] is True when the cap cut it).
Rules:
1. Address datasets by their full path, double-quoting any segment with special characters or mixed case:
   SELECT * FROM "Marketing"."campaigns" or SELECT * FROM lakehouse.sales."orders 2025". Each table's description shows the exact path.
2. Dremio SQL: DATE_TRUNC('MONTH', ts), TO_DATE, CAST, CONVERT_FROM, FLATTEN for arrays, nested fields as t.col.field.
3. Views (virtual datasets) are often the curated layer — prefer them over raw source tables when both exist.
4. Aggregate in SQL; always add LIMIT when exploring.
Only SELECT is allowed.
"""


def _q(s: str) -> str:
    return s.replace("'", "''")


def _sql_path(schema: str, table: str) -> str:
    parts = [p for p in str(schema).split(".") if p] + [table]
    return ".".join(f'"{p}"' for p in parts)


def _err(r: requests.Response) -> str:
    try:
        body = r.json()
        return body.get("errorMessage") or body.get("message") or r.text[:300]
    except Exception:
        return r.text[:300]
