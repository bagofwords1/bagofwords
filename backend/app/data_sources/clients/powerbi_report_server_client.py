from __future__ import annotations

from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import ProgressCallback, make_reporter
from app.ai.prompt_formatters import Table, TableColumn, ForeignKey, ServiceFormatter

from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote
import hashlib
import json
import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET

import pandas as pd
import requests
from requests_ntlm import HttpNtlmAuth


logger = logging.getLogger(__name__)


_RDL_NS_RE = re.compile(r"^\{[^}]+\}")

# Cache for PBIX-extracted schemas, keyed by (report_id, modified_date).
_PBIX_SCHEMA_CACHE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "uploads" / "pbirs_schema_cache"

# Cache for PBIX-extracted Parquet data (one file per model table), keyed by
# (report_id, modified_date). Populated lazily on first query; re-used across
# queries and invalidated on report edit via the modified_date key.
_PBIX_DATA_CACHE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "uploads" / "pbix_data_cache"

# Max pbix size we'll attempt to parse (bytes). Larger files are skipped — they
# use more memory/time than is worthwhile for metadata-only discovery.
_PBIX_MAX_BYTES = 200 * 1024 * 1024  # 200MB

# Per-table row cap when materializing pbix data to Parquet. Tables exceeding
# this are skipped — parsing very large Vertipaq tables blows memory on the
# decode path. 5M is comfortably above typical semantic-model fact tables.
_PBIX_MAX_ROWS_PER_TABLE = 5_000_000

# Auto-generated internal Power BI date tables — filtered out of schema output.
_AUTO_DATE_TABLE_RE = re.compile(r"^(LocalDateTable|DateTableTemplate)_[0-9a-fA-F\-]+$")

# DuckDB view names need to be simple identifiers. Vertipaq table names can
# contain spaces, symbols, or be reserved keywords — sanitize to a safe form.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")


def _safe_view_name(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("_")
    if not cleaned:
        cleaned = "tbl"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned


def _pbix_cache_path(report_id: str, modified_date: Optional[str]) -> Path:
    key = f"{report_id}|{modified_date or ''}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return _PBIX_SCHEMA_CACHE_DIR / f"{h}.json"


def _pbix_data_cache_dir(report_id: str, modified_date: Optional[str]) -> Path:
    key = f"{report_id}|{modified_date or ''}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return _PBIX_DATA_CACHE_DIR / h


def _dax_to_dtype(dax_type: Optional[str]) -> str:
    if not dax_type:
        return "unknown"
    t = str(dax_type).lower()
    if "int" in t:
        return "int"
    if "float" in t or "double" in t or "decimal" in t or "number" in t:
        return "float"
    if "bool" in t:
        return "bool"
    if "date" in t or "time" in t:
        return "datetime"
    if "string" in t or "text" in t or "object" in t:
        return "string"
    return t


def _strip_ns(tag: str) -> str:
    return _RDL_NS_RE.sub("", tag or "")


def _summarize_upstream(connection_summary: List[Dict[str, Any]]) -> str:
    """Produce a short human-readable hint of where a PBIX's data actually lives.

    Examples:
      "SQL (Server=dw01;Database=Sales)"
      "File: c:\\users\\alice\\sales.xlsx"
      "Web API + SQL (Server=...)"
    """
    if not connection_summary:
        return ""
    parts: List[str] = []
    for src in connection_summary:
        kind = src.get("kind") or src.get("type") or "Unknown"
        cs = (src.get("connection_string") or "").strip()
        if not cs:
            parts.append(kind)
        elif kind.lower() == "file":
            parts.append(f"File: {cs}")
        else:
            parts.append(f"{kind} ({cs})")
    # de-dup while preserving order
    seen = set()
    uniq = [p for p in parts if not (p in seen or seen.add(p))]
    return "; ".join(uniq)


def _clr_to_dtype(clr: Optional[str]) -> str:
    if not clr:
        return "unknown"
    c = clr.rsplit(".", 1)[-1].lower()
    mapping = {
        "int16": "int",
        "int32": "int",
        "int64": "int",
        "byte": "int",
        "sbyte": "int",
        "uint16": "int",
        "uint32": "int",
        "uint64": "int",
        "decimal": "decimal",
        "double": "float",
        "single": "float",
        "float": "float",
        "boolean": "bool",
        "bool": "bool",
        "string": "string",
        "char": "string",
        "datetime": "datetime",
        "datetimeoffset": "datetime",
        "timespan": "duration",
        "guid": "string",
        "object": "unknown",
    }
    return mapping.get(c, c)


class PowerBIReportServerClient(DataSourceClient):
    """
    Power BI Report Server (on-prem) client.

    Discovers metadata via REST API v2.0 with NTLM authentication:
      - Power BI reports (.pbix) — report metadata, data sources, parameters, roles
      - Paginated reports (RDL) — reports with embedded SQL queries extracted from RDL XML
      - Shared datasets (.rsd) — column schema + embedded query
      - Shared data sources — connection metadata for lineage
      - KPIs — threshold and trend definitions
      - Folder structure

    Executes queries:
      - RDL paginated reports: via /Reports({id})/Export/CSV
      - Shared datasets: via Model.GetData action
      - Power BI reports (.pbix): NotImplementedError — REST API does not expose the
        embedded semantic model. Requires XMLA (out of v1 scope).
    """

    API_SUFFIX = "/Reports/api/v2.0"

    def __init__(
        self,
        server_url: str,
        username: str,
        password: str,
        domain: Optional[str] = None,
        verify_ssl: bool = True,
        ca_bundle_path: Optional[str] = None,
        extract_pbix_schemas: bool = True,
        enable_pbix_query: bool = True,
    ):
        if not server_url:
            raise ValueError("server_url is required")
        if not username:
            raise ValueError("username is required")
        if password is None:
            raise ValueError("password is required")

        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self.domain = domain
        self.verify_ssl = verify_ssl
        self.ca_bundle_path = ca_bundle_path
        self.extract_pbix_schemas = extract_pbix_schemas
        self.enable_pbix_query = enable_pbix_query

        self._session: Optional[requests.Session] = None

    # ------------------------------------------------------------------
    # URL / auth plumbing
    # ------------------------------------------------------------------

    def _api_base(self) -> str:
        """Derive /Reports/api/v2.0 base URL from the configured server_url.

        Accepts all of:
          - http://host
          - http://host/Reports
          - http://host/Reports/api/v2.0
        """
        url = self.server_url
        if url.endswith("/api/v2.0"):
            return url
        if url.endswith("/Reports"):
            return url + "/api/v2.0"
        # server root — append /Reports/api/v2.0
        return url + self.API_SUFFIX

    def _report_server_root(self) -> str:
        """Return the /ReportServer base, used for legacy SOAP endpoints (export, XMLA)."""
        url = self.server_url
        for suffix in ("/Reports/api/v2.0", "/Reports"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        return url

    def _ntlm_user(self) -> str:
        if self.domain and "\\" not in self.username and "@" not in self.username:
            return f"{self.domain}\\{self.username}"
        return self.username

    def connect(self):
        if self._session is not None:
            return
        session = requests.Session()
        session.auth = HttpNtlmAuth(self._ntlm_user(), self.password)
        if self.ca_bundle_path:
            session.verify = self.ca_bundle_path
        else:
            session.verify = bool(self.verify_ssl)
        self._session = session
        self._prime_ntlm()

    def _prime_ntlm(self):
        """Complete the NTLM handshake once serially before any concurrent calls.

        requests-ntlm's challenge/response state can race when multiple worker
        threads fire on a cold session, producing spurious HTTP 400s on the
        first parallel burst. A single warm-up GET settles the auth state.
        """
        try:
            self._session.get(
                f"{self._api_base()}/System",
                headers={"Accept": "application/json"},
                timeout=30,
            )
        except Exception:
            pass

    def _get(self, path: str, *, accept: str = "application/json", stream: bool = False, timeout: int = 60) -> requests.Response:
        self.connect()
        base = self._api_base()
        url = path if path.startswith("http") else f"{base}{path}"
        return self._session.get(url, headers={"Accept": accept}, stream=stream, timeout=timeout)

    def _get_json(self, path: str, *, timeout: int = 60) -> Any:
        r = self._get(path, timeout=timeout)
        if r.status_code >= 300:
            raise RuntimeError(f"GET {path} failed: HTTP {r.status_code} {r.text[:300]}")
        return r.json()

    def _get_odata_value(self, path: str, *, timeout: int = 60) -> List[Dict]:
        data = self._get_json(path, timeout=timeout) or {}
        return data.get("value") or []

    def _post_json(self, path: str, body: Dict, *, timeout: int = 120) -> Any:
        self.connect()
        base = self._api_base()
        url = path if path.startswith("http") else f"{base}{path}"
        r = self._session.post(url, json=body, headers={"Accept": "application/json"}, timeout=timeout)
        if r.status_code >= 300:
            raise RuntimeError(f"POST {path} failed: HTTP {r.status_code} {r.text[:300]}")
        if not r.content:
            return None
        return r.json()

    # ------------------------------------------------------------------
    # Discovery — REST list endpoints
    # ------------------------------------------------------------------

    def get_system_info(self) -> Dict:
        return self._get_json("/System")

    def list_folders(self) -> List[Dict]:
        return self._get_odata_value("/Folders")

    def list_catalog_items(self) -> List[Dict]:
        return self._get_odata_value("/CatalogItems")

    def list_powerbi_reports(self) -> List[Dict]:
        return self._get_odata_value("/PowerBIReports")

    def list_paginated_reports(self) -> List[Dict]:
        return self._get_odata_value("/Reports")

    def list_shared_datasets(self) -> List[Dict]:
        return self._get_odata_value("/Datasets")

    def list_shared_data_sources(self) -> List[Dict]:
        return self._get_odata_value("/DataSources")

    def list_kpis(self) -> List[Dict]:
        return self._get_odata_value("/Kpis")

    def get_powerbi_report_datasources(self, report_id: str) -> List[Dict]:
        return self._get_odata_value(f"/PowerBIReports({report_id})/DataSources")

    def get_powerbi_report_parameters(self, report_id: str) -> List[Dict]:
        return self._get_odata_value(f"/PowerBIReports({report_id})/DataModelParameters")

    def get_powerbi_report_roles(self, report_id: str) -> List[Dict]:
        return self._get_odata_value(f"/PowerBIReports({report_id})/DataModelRoles")

    def get_paginated_report_datasources(self, report_id: str) -> List[Dict]:
        return self._get_odata_value(f"/Reports({report_id})/DataSources")

    def get_paginated_report_parameters(self, report_id: str) -> List[Dict]:
        return self._get_odata_value(f"/Reports({report_id})/ParameterDefinitions")

    def get_shared_dataset_schema(self, dataset_id: str) -> Optional[Dict]:
        r = self._get(f"/Datasets({dataset_id})/Model.GetSchema")
        if r.status_code >= 300:
            r = self._get(f"/Datasets({dataset_id})/Schema")
        if r.status_code >= 300:
            return None
        try:
            return r.json()
        except Exception:
            return None

    def get_shared_dataset_parameters(self, dataset_id: str) -> List[Dict]:
        try:
            return self._get_odata_value(f"/Datasets({dataset_id})/ParameterDefinitions")
        except Exception:
            return []

    def download_catalog_item_content(self, item_id: str) -> bytes:
        r = self._get(f"/CatalogItems({item_id})/Content/$value", accept="application/octet-stream", timeout=300)
        if r.status_code >= 300:
            raise RuntimeError(f"Download content for {item_id} failed: HTTP {r.status_code}")
        return r.content

    # ------------------------------------------------------------------
    # PBIX schema extraction via pbixray
    # ------------------------------------------------------------------

    def extract_pbix_schema(
        self,
        report_id: str,
        modified_date: Optional[str] = None,
        *,
        report_name: Optional[str] = None,
        use_cache: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Download a PBIX and parse its Vertipaq model schema with pbixray.

        Returns a dict with `tables`, `relationships`, `measures`, `source`, or
        None on failure. Result is cached on disk keyed by (report_id, modified_date)
        so a subsequent schema refresh is a cheap JSON read.
        """
        cache_file = _pbix_cache_path(report_id, modified_date)
        if use_cache and cache_file.exists():
            try:
                with cache_file.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.debug(f"pbix cache read failed for {report_id}: {e}")

        try:
            from pbixray import PBIXRay  # type: ignore
        except Exception as e:
            logger.warning(f"pbixray unavailable, skipping pbix schema extraction: {e}")
            return None

        try:
            content = self.download_catalog_item_content(report_id)
        except Exception as e:
            logger.info(f"pbix download failed for {report_id} ({report_name}): {e}")
            return None

        if len(content) > _PBIX_MAX_BYTES:
            logger.info(
                f"pbix {report_id} ({report_name}) is {len(content)} bytes — exceeds "
                f"{_PBIX_MAX_BYTES}; skipping schema extraction"
            )
            return None

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".pbix")
            with os.fdopen(fd, "wb") as f:
                f.write(content)

            model = PBIXRay(tmp_path)
            schema_df = model.schema
            rels_df = model.relationships
            measures_df = model.dax_measures

            # Group schema rows into tables, skipping auto-generated date tables.
            tables_out: Dict[str, Dict[str, Any]] = {}
            if schema_df is not None and not schema_df.empty:
                for _, row in schema_df.iterrows():
                    tname = str(row.get("TableName") or "")
                    if not tname or _AUTO_DATE_TABLE_RE.match(tname):
                        continue
                    t = tables_out.setdefault(tname, {"name": tname, "columns": [], "measures": []})
                    t["columns"].append({
                        "name": str(row.get("ColumnName") or ""),
                        "dtype": _dax_to_dtype(row.get("PandasDataType")),
                    })

            # Attach measures to their tables.
            if measures_df is not None and not measures_df.empty:
                for _, row in measures_df.iterrows():
                    tname = str(row.get("TableName") or "")
                    if not tname or _AUTO_DATE_TABLE_RE.match(tname):
                        continue
                    entry = tables_out.setdefault(tname, {"name": tname, "columns": [], "measures": []})
                    entry["measures"].append({
                        "name": str(row.get("Name") or ""),
                        "expression": str(row.get("Expression") or ""),
                        "display_folder": str(row.get("DisplayFolder") or ""),
                        "description": str(row.get("Description") or ""),
                    })

            relationships_out: List[Dict[str, Any]] = []
            if rels_df is not None and not rels_df.empty:
                for _, row in rels_df.iterrows():
                    from_t = str(row.get("FromTableName") or "")
                    to_t = str(row.get("ToTableName") or "")
                    if _AUTO_DATE_TABLE_RE.match(from_t) or _AUTO_DATE_TABLE_RE.match(to_t):
                        continue
                    relationships_out.append({
                        "from_table": from_t,
                        "from_column": str(row.get("FromColumnName") or ""),
                        "to_table": to_t,
                        "to_column": str(row.get("ToColumnName") or ""),
                        "is_active": bool(row.get("IsActive", True)),
                        "cardinality": str(row.get("Cardinality") or ""),
                    })

            result = {
                "source": "pbixray",
                "report_id": report_id,
                "report_name": report_name,
                "modified_date": modified_date,
                "tables": list(tables_out.values()),
                "relationships": relationships_out,
            }

            try:
                _PBIX_SCHEMA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                with cache_file.open("w", encoding="utf-8") as f:
                    json.dump(result, f)
            except Exception as e:
                logger.debug(f"pbix cache write failed for {report_id}: {e}")

            return result
        except Exception as e:
            logger.info(f"pbix schema extraction failed for {report_id} ({report_name}): {e}")
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def ensure_pbix_parquets(
        self,
        report_id: str,
        modified_date: Optional[str] = None,
        *,
        report_name: Optional[str] = None,
    ) -> Dict[str, Path]:
        """Materialize every queryable pbix model table to Parquet and return a
        {table_name: parquet_path} map. Cached on disk keyed by (report_id,
        modified_date) so edits invalidate cleanly. Auto-date tables and tables
        exceeding _PBIX_MAX_ROWS_PER_TABLE are skipped.

        Raises RuntimeError on download/parse failure so callers can surface a
        clear error back to the LLM.
        """
        cache_dir = _pbix_data_cache_dir(report_id, modified_date)
        manifest_path = cache_dir / "manifest.json"
        if manifest_path.exists():
            try:
                with manifest_path.open("r", encoding="utf-8") as f:
                    manifest = json.load(f)
                return {t: cache_dir / rel for t, rel in manifest.items() if (cache_dir / rel).exists()}
            except Exception as e:
                logger.debug(f"pbix parquet manifest read failed for {report_id}: {e}")

        try:
            from pbixray import PBIXRay  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "pbixray is required to query Power BI (.pbix) data but is not installed. "
                "Install it or set enable_pbix_query=False."
            ) from e

        content = self.download_catalog_item_content(report_id)
        if len(content) > _PBIX_MAX_BYTES:
            raise RuntimeError(
                f"PBIX report '{report_name or report_id}' is {len(content)} bytes "
                f"(> {_PBIX_MAX_BYTES} limit); refusing to materialize."
            )

        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".pbix")
            with os.fdopen(fd, "wb") as f:
                f.write(content)

            model = PBIXRay(tmp_path)
            schema_df = model.schema
            if schema_df is None or schema_df.empty:
                raise RuntimeError(f"PBIX '{report_name or report_id}' has no tables in its semantic model.")

            # Unique non-date table names from the schema.
            candidate_tables: List[str] = []
            seen = set()
            for _, row in schema_df.iterrows():
                tname = str(row.get("TableName") or "")
                if not tname or tname in seen or _AUTO_DATE_TABLE_RE.match(tname):
                    continue
                seen.add(tname)
                candidate_tables.append(tname)

            cache_dir.mkdir(parents=True, exist_ok=True)
            manifest: Dict[str, str] = {}
            used_filenames: set[str] = set()

            for tname in candidate_tables:
                try:
                    df = model.get_table(tname)
                except Exception as e:
                    logger.warning(f"pbix {report_id} table '{tname}' extract failed: {e}")
                    continue
                if df is None:
                    continue
                if len(df) > _PBIX_MAX_ROWS_PER_TABLE:
                    logger.warning(
                        f"pbix {report_id} table '{tname}' has {len(df)} rows "
                        f"(> {_PBIX_MAX_ROWS_PER_TABLE}); skipping materialization."
                    )
                    continue

                base = _safe_view_name(tname).lower()
                fname = f"{base}.parquet"
                i = 1
                while fname in used_filenames:
                    i += 1
                    fname = f"{base}_{i}.parquet"
                used_filenames.add(fname)

                fpath = cache_dir / fname
                try:
                    df.to_parquet(fpath, index=False)
                except Exception as e:
                    logger.warning(f"pbix {report_id} table '{tname}' parquet write failed: {e}")
                    continue
                manifest[tname] = fname

            if not manifest:
                raise RuntimeError(
                    f"PBIX '{report_name or report_id}' produced no materializable tables "
                    "(all skipped or failed)."
                )

            with manifest_path.open("w", encoding="utf-8") as f:
                json.dump(manifest, f)

            return {t: cache_dir / rel for t, rel in manifest.items()}
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def warm_pbix_caches(self) -> Dict[str, Any]:
        """Pre-materialize Parquet caches for every pbix report on this server so
        the first execute_query doesn't block on a cold pbixray parse. Safe to run
        on a schedule: each report is keyed by (report_id, modified_date), so an
        already-warm cache is a no-op (manifest hit returns immediately).
        """
        if not self.enable_pbix_query:
            return {"skipped": True, "reason": "enable_pbix_query=False"}

        self.connect()
        try:
            reports = self.list_powerbi_reports()
        except Exception as e:
            logger.warning(f"pbirs.warm.list_failed: {e}")
            return {"reports": 0, "warmed": 0, "failed": 0, "error": str(e)}

        warmed = 0
        failed = 0
        for r in reports:
            rid = r.get("Id")
            if not rid:
                continue
            try:
                self.ensure_pbix_parquets(
                    rid,
                    r.get("ModifiedDate"),
                    report_name=r.get("Name") or rid,
                )
                warmed += 1
            except Exception as e:
                logger.warning(
                    "pbirs.warm.report_failed",
                    extra={"report_id": rid, "report_name": r.get("Name"), "pbirs_error": str(e)},
                )
                failed += 1
        return {"reports": len(reports), "warmed": warmed, "failed": failed}

    def _execute_pbix_query(
        self,
        report_id: str,
        modified_date: Optional[str],
        *,
        report_name: Optional[str],
        query: Optional[str],
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """Run a DuckDB query against the pbix model's Parquet cache. All internal
        tables from the same pbix are registered as views so the LLM can JOIN freely
        using the bare table names it sees in the schema prompt.
        """
        if not query:
            raise ValueError(
                "execute_query on a pbix table requires a SQL `query`. "
                "Use DuckDB syntax with the internal table names exposed in the schema."
            )

        import duckdb  # type: ignore

        paths = self.ensure_pbix_parquets(report_id, modified_date, report_name=report_name)
        if not paths:
            raise RuntimeError(
                f"No queryable tables available for PBIX '{report_name or report_id}'."
            )

        con = duckdb.connect(database=":memory:")
        try:
            # Register each model table under a safe identifier. Collisions after
            # sanitization are resolved by suffixing.
            used: set[str] = set()
            registered: Dict[str, str] = {}
            for tname, ppath in paths.items():
                view = _safe_view_name(tname)
                base = view
                i = 1
                while view in used:
                    i += 1
                    view = f"{base}_{i}"
                used.add(view)
                registered[tname] = view
                sql_path = str(ppath).replace("'", "''")
                con.execute(
                    f'CREATE VIEW "{view}" AS SELECT * FROM read_parquet(\'{sql_path}\')'
                )

            df = con.execute(query).df()
            if max_rows is not None and max_rows > 0 and len(df) > max_rows:
                df = df.head(max_rows)
            return df
        finally:
            con.close()

    # ------------------------------------------------------------------
    # RDL parsing — extract CommandText, fields, parameters from report XML
    # ------------------------------------------------------------------

    def parse_rdl_content(self, xml_bytes: bytes) -> Dict[str, Any]:
        """Parse an RDL (.rdl) XML blob and extract datasets with their queries and fields.

        Returns:
            {
              "data_sources": [{"name", "connection_string", "data_provider"}],
              "datasets": [{
                  "name", "data_source_name", "command_type", "command_text",
                  "fields": [{"name", "data_field", "dtype"}],
                  "parameters": [{"name", "value"}]
              }],
              "parameters": [{"name", "data_type", "prompt", "default_values"}]
            }
        """
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            raise RuntimeError(f"Invalid RDL XML: {e}")

        out: Dict[str, Any] = {"data_sources": [], "datasets": [], "parameters": []}

        for ds in root.iter():
            tag = _strip_ns(ds.tag)
            if tag == "DataSource":
                name = ds.get("Name", "")
                cs = None
                dp = None
                for child in ds.iter():
                    ct = _strip_ns(child.tag)
                    if ct == "ConnectString":
                        cs = (child.text or "").strip()
                    elif ct == "DataProvider":
                        dp = (child.text or "").strip()
                out["data_sources"].append({"name": name, "connection_string": cs, "data_provider": dp})

        for node in root.iter():
            if _strip_ns(node.tag) != "DataSet":
                continue
            ds_name = node.get("Name", "")
            query_cmd_type: Optional[str] = None
            query_cmd_text: Optional[str] = None
            query_ds_name: Optional[str] = None
            query_params: List[Dict[str, Any]] = []
            fields: List[Dict[str, Any]] = []

            for child in node:
                ctag = _strip_ns(child.tag)
                if ctag == "Query":
                    for qc in child:
                        qtag = _strip_ns(qc.tag)
                        if qtag == "CommandType":
                            query_cmd_type = (qc.text or "").strip() or None
                        elif qtag == "CommandText":
                            query_cmd_text = (qc.text or "")
                        elif qtag == "DataSourceName":
                            query_ds_name = (qc.text or "").strip() or None
                        elif qtag == "QueryParameters":
                            for qp in qc:
                                if _strip_ns(qp.tag) == "QueryParameter":
                                    qp_name = qp.get("Name", "")
                                    qp_val = None
                                    for vnode in qp:
                                        if _strip_ns(vnode.tag) == "Value":
                                            qp_val = (vnode.text or "").strip()
                                            break
                                    query_params.append({"name": qp_name, "value": qp_val})
                elif ctag == "Fields":
                    for fnode in child:
                        if _strip_ns(fnode.tag) != "Field":
                            continue
                        fname = fnode.get("Name", "")
                        data_field = None
                        type_name = None
                        for fc in fnode:
                            fct = _strip_ns(fc.tag)
                            if fct == "DataField":
                                data_field = (fc.text or "").strip() or None
                            elif fct == "TypeName":
                                type_name = (fc.text or "").strip() or None
                        fields.append({
                            "name": fname,
                            "data_field": data_field,
                            "dtype": _clr_to_dtype(type_name),
                            "clr_type": type_name,
                        })

            out["datasets"].append({
                "name": ds_name,
                "data_source_name": query_ds_name,
                "command_type": query_cmd_type or "Text",
                "command_text": query_cmd_text,
                "fields": fields,
                "parameters": query_params,
            })

        for node in root.iter():
            if _strip_ns(node.tag) != "ReportParameter":
                continue
            p_name = node.get("Name", "")
            p_type = None
            p_prompt = None
            default_vals: List[str] = []
            for child in node:
                ct = _strip_ns(child.tag)
                if ct == "DataType":
                    p_type = (child.text or "").strip() or None
                elif ct == "Prompt":
                    p_prompt = (child.text or "").strip() or None
                elif ct == "DefaultValue":
                    for vs in child.iter():
                        if _strip_ns(vs.tag) == "Value" and vs.text:
                            default_vals.append(vs.text.strip())
            out["parameters"].append({
                "name": p_name,
                "data_type": p_type,
                "prompt": p_prompt,
                "default_values": default_vals,
            })

        return out

    # ------------------------------------------------------------------
    # test_connection
    # ------------------------------------------------------------------

    def test_connection(self) -> Dict:
        import time as _time

        t0 = _time.perf_counter()
        try:
            self.connect()
        except Exception as e:
            return {"success": False, "message": f"Session init failed: {e}", "details": {}}

        auth_ms = round((_time.perf_counter() - t0) * 1000, 1)
        t1 = _time.perf_counter()
        try:
            sys_info = self.get_system_info()
        except Exception as e:
            msg = str(e)
            if "401" in msg or "Unauthorized" in msg:
                return {
                    "success": False,
                    "message": f"Authentication failed: check username, domain, and password ({msg[:180]})",
                    "timings": {"auth_ms": auth_ms},
                    "details": {},
                }
            return {
                "success": False,
                "message": f"Cannot reach server: {msg[:200]}",
                "timings": {"auth_ms": auth_ms},
                "details": {},
            }

        system_ms = round((_time.perf_counter() - t1) * 1000, 1)

        product = sys_info.get("ProductName") or "Power BI Report Server"
        version = sys_info.get("ProductVersion") or ""

        t2 = _time.perf_counter()
        try:
            pbi = self.list_powerbi_reports()
            paginated = self.list_paginated_reports()
            shared_datasets = self.list_shared_datasets()
            kpis = self.list_kpis()
        except Exception as e:
            return {
                "success": False,
                "message": f"Authenticated with {product} {version} but could not list catalog: {e}",
                "connectivity": True,
                "timings": {"auth_ms": auth_ms, "system_ms": system_ms},
                "details": {"product_version": version},
            }
        catalog_ms = round((_time.perf_counter() - t2) * 1000, 1)

        auth_mode = "NTLM" if self.domain or "\\" in (self.username or "") else "Basic"

        return {
            "success": True,
            "message": (
                f"Connected to {product} {version}. "
                f"Found {len(pbi)} Power BI report(s), {len(paginated)} paginated report(s), "
                f"{len(shared_datasets)} shared dataset(s), {len(kpis)} KPI(s)."
            ),
            "powerbi_reports": len(pbi),
            "paginated_reports": len(paginated),
            "shared_datasets": len(shared_datasets),
            "kpis": len(kpis),
            "product_version": version,
            "timings": {
                "auth_ms": auth_ms,
                "system_ms": system_ms,
                "catalog_ms": catalog_ms,
            },
            "details": {
                "product": product,
                "product_version": version,
                "auth_mode": auth_mode,
                "powerbi_reports": len(pbi),
                "paginated_reports": len(paginated),
                "shared_datasets": len(shared_datasets),
                "kpis": len(kpis),
            },
        }

    # ------------------------------------------------------------------
    # get_schemas — build BOW Table objects
    # ------------------------------------------------------------------

    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        """Build Table objects for:
          - Each Power BI report (.pbix) — one Table per report (columns empty; metadata carries data sources)
          - Each paginated report (RDL) dataset — one Table per DataSet inside the RDL (columns + CommandText)
          - Each shared dataset (.rsd) — one Table with schema columns and CommandText
          - Each KPI — one Table representing the metric (for LLM awareness)
        """
        reporter = make_reporter(progress_callback)
        reporter.phase("listing")
        self.connect()
        tables: List[Table] = []

        # Fetch top-level lists in parallel
        with ThreadPoolExecutor(max_workers=6) as pool:
            pbi_f = pool.submit(self.list_powerbi_reports)
            rdl_f = pool.submit(self.list_paginated_reports)
            ds_f = pool.submit(self.list_shared_datasets)
            kpi_f = pool.submit(self.list_kpis)
            dsrc_f = pool.submit(self.list_shared_data_sources)

            try:
                pbi_reports = pbi_f.result()
            except Exception as e:
                logger.warning(f"list_powerbi_reports failed: {e}")
                pbi_reports = []
            try:
                rdl_reports = rdl_f.result()
            except Exception as e:
                logger.warning(f"list_paginated_reports failed: {e}")
                rdl_reports = []
            try:
                shared_datasets = ds_f.result()
            except Exception as e:
                logger.warning(f"list_shared_datasets failed: {e}")
                shared_datasets = []
            try:
                kpis = kpi_f.result()
            except Exception as e:
                logger.warning(f"list_kpis failed: {e}")
                kpis = []
            try:
                shared_ds_sources = dsrc_f.result()
            except Exception as e:
                logger.warning(f"list_shared_data_sources failed: {e}")
                shared_ds_sources = []

        reporter.phase(
            "pbix_reports",
            total=len(pbi_reports) if self.extract_pbix_schemas else 0,
        )
        # ---- Power BI reports ----
        if pbi_reports:
            with ThreadPoolExecutor(max_workers=8) as pool:
                ds_futs = {pool.submit(self.get_powerbi_report_datasources, r["Id"]): r for r in pbi_reports}
                param_futs = {pool.submit(self.get_powerbi_report_parameters, r["Id"]): r for r in pbi_reports}
                role_futs = {pool.submit(self.get_powerbi_report_roles, r["Id"]): r for r in pbi_reports}

                pbi_data: Dict[str, Dict[str, Any]] = {r["Id"]: {"data_sources": [], "parameters": [], "roles": []} for r in pbi_reports}
                for fut in as_completed(ds_futs):
                    r = ds_futs[fut]
                    try:
                        pbi_data[r["Id"]]["data_sources"] = fut.result()
                    except Exception as e:
                        logger.debug(f"pbi {r['Id']} DataSources failed: {e}")
                for fut in as_completed(param_futs):
                    r = param_futs[fut]
                    try:
                        pbi_data[r["Id"]]["parameters"] = fut.result()
                    except Exception as e:
                        logger.debug(f"pbi {r['Id']} Parameters failed: {e}")
                for fut in as_completed(role_futs):
                    r = role_futs[fut]
                    try:
                        pbi_data[r["Id"]]["roles"] = fut.result()
                    except Exception as e:
                        logger.debug(f"pbi {r['Id']} Roles failed: {e}")

            # Parallel pbix schema extraction — best-effort, each failure is
            # contained so the umbrella discovery row still renders.
            pbix_schemas: Dict[str, Optional[Dict[str, Any]]] = {r["Id"]: None for r in pbi_reports}
            if self.extract_pbix_schemas:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    ext_futs = {
                        pool.submit(
                            self.extract_pbix_schema,
                            r["Id"],
                            r.get("ModifiedDate"),
                            report_name=r.get("Name") or r["Id"],
                        ): r
                        for r in pbi_reports
                    }
                    for fut in as_completed(ext_futs):
                        r = ext_futs[fut]
                        try:
                            pbix_schemas[r["Id"]] = fut.result()
                        except Exception as e:
                            logger.debug(f"pbix schema extract failed for {r['Id']}: {e}")
                        reporter.item(r.get("Name") or r["Id"])

            for r in pbi_reports:
                rid = r["Id"]
                name = r.get("Name") or rid
                info = pbi_data[rid]

                connection_summary = []
                for src in info["data_sources"]:
                    dmd = src.get("DataModelDataSource") or {}
                    connection_summary.append({
                        "type": dmd.get("Type"),
                        "kind": dmd.get("Kind"),
                        "auth_type": dmd.get("AuthType"),
                        "connection_string": src.get("ConnectionString") or "",
                        "model_connection_name": dmd.get("ModelConnectionName"),
                    })

                upstream_hint = _summarize_upstream(connection_summary)

                schema_info = pbix_schemas.get(rid)
                schema_tables = (schema_info or {}).get("tables") or []
                schema_rels = (schema_info or {}).get("relationships") or []
                schema_table_names = [t["name"] for t in schema_tables]

                metadata_json = {
                    "powerbi_report_server": {
                        "report_type": "PowerBIReport",
                        "report_id": rid,
                        "report_name": name,
                        "path": r.get("Path"),
                        "parent_folder_id": r.get("ParentFolderId"),
                        "size": r.get("Size"),
                        "created_by": r.get("CreatedBy"),
                        "modified_by": r.get("ModifiedBy"),
                        "modified_date": r.get("ModifiedDate"),
                        "data_sources": connection_summary,
                        "parameters": [
                            {"name": p.get("Name"), "value_type": p.get("ValueType"), "is_required": p.get("IsRequired"), "current_value": p.get("CurrentValue")}
                            for p in info["parameters"]
                        ],
                        "roles": [{"name": rl.get("Name"), "model_permissions": rl.get("ModelPermissions")} for rl in info["roles"]],
                        "queryable": False,
                        "upstream_source": upstream_hint,
                        "model_tables": schema_table_names,
                        "model_relationships": schema_rels,
                        "schema_source": (schema_info or {}).get("source"),
                        "query_note": (
                            "This is a discovery entry — the PBIX embedded model is NOT queryable through PBIRS. "
                            f"To query its data, connect the upstream source directly: {upstream_hint or 'see data_sources[] for connection details'}."
                        ),
                    }
                }

                desc = f"Power BI report (discovery only). Upstream: {upstream_hint}" if upstream_hint else "Power BI report (discovery only)."
                tables.append(Table(
                    name=f"pbix:{name}",
                    description=desc,
                    columns=[],
                    pks=[],
                    fks=[],
                    is_active=True,
                    metadata_json=metadata_json,
                ))

                # Emit one Table per internal pbix model table when extraction succeeded.
                # Relationships within the model become fks on the "from" side.
                if schema_tables:
                    rels_by_from: Dict[str, List[Dict[str, Any]]] = {}
                    for rel in schema_rels:
                        rels_by_from.setdefault(rel["from_table"], []).append(rel)

                    for st in schema_tables:
                        tname = st["name"]
                        columns = [
                            TableColumn(name=c["name"], dtype=c.get("dtype") or "unknown")
                            for c in st.get("columns", [])
                        ]
                        col_by_name = {c.name: c for c in columns}

                        fks: List[ForeignKey] = []
                        for rel in rels_by_from.get(tname, []):
                            fc = col_by_name.get(rel["from_column"])
                            if fc is None:
                                continue
                            fks.append(ForeignKey(
                                column=fc,
                                references_name=f"pbix:{name}/{rel['to_table']}",
                                references_column=TableColumn(name=rel["to_column"], dtype="unknown"),
                            ))

                        tables.append(Table(
                            name=f"pbix:{name}/{tname}",
                            description=f"Internal table in Power BI report '{name}'. Not queryable via PBIRS.",
                            columns=columns,
                            pks=[],
                            fks=fks,
                            is_active=True,
                            metadata_json={
                                "powerbi_report_server": {
                                    "report_type": "PowerBIReportTable",
                                    "report_id": rid,
                                    "report_name": name,
                                    "modified_date": r.get("ModifiedDate"),
                                    "model_table": tname,
                                    "measures": st.get("measures", []),
                                    "queryable": bool(self.enable_pbix_query),
                                    "upstream_source": upstream_hint,
                                    "schema_source": (schema_info or {}).get("source"),
                                    "query_note": (
                                        "Queryable via DuckDB over the cached semantic model. "
                                        "All tables from the same pbix share a session so you can JOIN them. "
                                        "Note: data reflects the last PBIX refresh, not live upstream."
                                        if self.enable_pbix_query
                                        else "Internal PBIX model table — queryability disabled. Use upstream source for data."
                                    ),
                                }
                            },
                        ))

        reporter.phase("rdl_reports", total=len(rdl_reports))
        # ---- Paginated RDL reports: download content + parse ----
        if rdl_reports:
            with ThreadPoolExecutor(max_workers=6) as pool:
                content_futs = {pool.submit(self.download_catalog_item_content, r["Id"]): r for r in rdl_reports}
                for fut in as_completed(content_futs):
                    r = content_futs[fut]
                    rid = r["Id"]
                    rname = r.get("Name") or rid
                    reporter.item(rname)
                    try:
                        xml_bytes = fut.result()
                        parsed = self.parse_rdl_content(xml_bytes)
                    except Exception as e:
                        logger.warning(f"RDL {rname} parse failed: {e}")
                        tables.append(Table(
                            name=f"rdl:{rname}",
                            description="Paginated RDL report (failed to parse content)",
                            columns=[],
                            pks=[],
                            fks=[],
                            is_active=True,
                            metadata_json={"powerbi_report_server": {
                                "report_type": "Report",
                                "report_id": rid,
                                "report_name": rname,
                                "path": r.get("Path"),
                                "parse_error": str(e),
                                "queryable": True,
                                "query_note": "Execute via execute_query(report_id=..., format='CSV').",
                            }},
                        ))
                        continue

                    report_params = parsed.get("parameters") or []
                    report_sources = parsed.get("data_sources") or []

                    for dset in parsed.get("datasets") or []:
                        dsname = dset.get("name") or ""
                        columns = [
                            TableColumn(
                                name=fld["name"],
                                dtype=fld.get("dtype") or "unknown",
                                description=fld.get("data_field") if fld.get("data_field") and fld.get("data_field") != fld["name"] else None,
                                metadata={"role": "column", "data_field": fld.get("data_field"), "clr_type": fld.get("clr_type")},
                            )
                            for fld in dset.get("fields") or []
                            if fld.get("name")
                        ]
                        cmd_text = dset.get("command_text") or ""
                        metadata_json = {
                            "powerbi_report_server": {
                                "report_type": "Report",
                                "report_id": rid,
                                "report_name": rname,
                                "path": r.get("Path"),
                                "dataset_name": dsname,
                                "data_source_name": dset.get("data_source_name"),
                                "command_type": dset.get("command_type"),
                                "command_text": cmd_text,
                                "query_parameters": dset.get("parameters") or [],
                                "report_parameters": report_params,
                                "report_data_sources": report_sources,
                                "queryable": True,
                                "query_note": "Execute via execute_query(report_id=..., format='CSV'). Single dataset per execution — RDL export returns full rendered report data.",
                            }
                        }
                        tables.append(Table(
                            name=f"rdl:{rname}/{dsname}" if dsname else f"rdl:{rname}",
                            description=(cmd_text or "")[:240] if cmd_text else None,
                            columns=columns,
                            pks=[],
                            fks=[],
                            is_active=True,
                            metadata_json=metadata_json,
                        ))

                    if not (parsed.get("datasets") or []):
                        tables.append(Table(
                            name=f"rdl:{rname}",
                            description="Paginated RDL report (no DataSets declared)",
                            columns=[],
                            pks=[],
                            fks=[],
                            is_active=True,
                            metadata_json={"powerbi_report_server": {
                                "report_type": "Report",
                                "report_id": rid,
                                "report_name": rname,
                                "path": r.get("Path"),
                                "queryable": True,
                                "query_note": "Execute via execute_query(report_id=..., format='CSV').",
                            }},
                        ))

        reporter.phase("shared_datasets", total=len(shared_datasets))
        # ---- Shared datasets: fetch schema + content ----
        if shared_datasets:
            with ThreadPoolExecutor(max_workers=6) as pool:
                schema_futs = {pool.submit(self.get_shared_dataset_schema, d["Id"]): d for d in shared_datasets}
                content_futs = {pool.submit(self.download_catalog_item_content, d["Id"]): d for d in shared_datasets}
                param_futs = {pool.submit(self.get_shared_dataset_parameters, d["Id"]): d for d in shared_datasets}

                schemas: Dict[str, Any] = {}
                contents: Dict[str, bytes] = {}
                params: Dict[str, List[Dict]] = {}

                for fut in as_completed(schema_futs):
                    d = schema_futs[fut]
                    try:
                        schemas[d["Id"]] = fut.result()
                    except Exception as e:
                        logger.debug(f"dataset {d['Id']} schema failed: {e}")
                    reporter.item(d.get("Name") or d["Id"])
                for fut in as_completed(content_futs):
                    d = content_futs[fut]
                    try:
                        contents[d["Id"]] = fut.result()
                    except Exception as e:
                        logger.debug(f"dataset {d['Id']} content failed: {e}")
                for fut in as_completed(param_futs):
                    d = param_futs[fut]
                    try:
                        params[d["Id"]] = fut.result()
                    except Exception as e:
                        logger.debug(f"dataset {d['Id']} params failed: {e}")

            for d in shared_datasets:
                did = d["Id"]
                dname = d.get("Name") or did
                columns: List[TableColumn] = []
                schema_obj = schemas.get(did) or {}
                for col in schema_obj.get("Columns") or schema_obj.get("columns") or []:
                    cname = col.get("Name") or col.get("name")
                    if not cname:
                        continue
                    columns.append(TableColumn(
                        name=cname,
                        dtype=(col.get("DataType") or col.get("dataType") or "unknown"),
                        description=None,
                        metadata={"role": "column"},
                    ))

                cmd_text = None
                parsed_content: Dict[str, Any] = {}
                if did in contents:
                    try:
                        parsed_content = self.parse_rdl_content(contents[did])
                        datasets = parsed_content.get("datasets") or []
                        if datasets and datasets[0].get("command_text"):
                            cmd_text = datasets[0]["command_text"]
                            if not columns:
                                for fld in datasets[0].get("fields") or []:
                                    if fld.get("name"):
                                        columns.append(TableColumn(
                                            name=fld["name"],
                                            dtype=fld.get("dtype") or "unknown",
                                            description=None,
                                            metadata={"role": "column", "data_field": fld.get("data_field")},
                                        ))
                    except Exception as e:
                        logger.debug(f"dataset {did} RSD parse failed: {e}")

                metadata_json = {
                    "powerbi_report_server": {
                        "report_type": "Dataset",
                        "dataset_id": did,
                        "dataset_name": dname,
                        "path": d.get("Path"),
                        "command_text": cmd_text,
                        "parameters": [
                            {"name": p.get("Name"), "value_type": p.get("ValueType"), "is_required": p.get("IsRequired")}
                            for p in params.get(did, [])
                        ],
                        "queryable": True,
                        "query_note": "Execute via execute_query(dataset_id=...). Uses Model.GetData action. Supports parameters via the parameters kwarg.",
                    }
                }
                tables.append(Table(
                    name=f"dataset:{dname}",
                    description=(cmd_text or "")[:240] if cmd_text else None,
                    columns=columns,
                    pks=[],
                    fks=[],
                    is_active=True,
                    metadata_json=metadata_json,
                ))

        reporter.phase("kpis", total=len(kpis or []))
        # ---- KPIs ----
        for k in kpis or []:
            reporter.item(k.get("Name") or k.get("Id"))
            kid = k.get("Id")
            kname = k.get("Name") or kid
            metadata_json = {
                "powerbi_report_server": {
                    "report_type": "Kpi",
                    "kpi_id": kid,
                    "kpi_name": kname,
                    "path": k.get("Path"),
                    "value_format": k.get("ValueFormat"),
                    "visualization": k.get("Visualization"),
                    "current_value": (k.get("Values") or {}).get("Value") if isinstance(k.get("Values"), dict) else None,
                    "goal_value": (k.get("Values") or {}).get("Goal") if isinstance(k.get("Values"), dict) else None,
                    "status": (k.get("Values") or {}).get("Status") if isinstance(k.get("Values"), dict) else None,
                    "queryable": False,
                    "query_note": "KPIs are computed metrics, not queryable tables. Value is accessible via metadata_json.",
                }
            }
            tables.append(Table(
                name=f"kpi:{kname}",
                description=f"KPI metric (format={k.get('ValueFormat')})",
                columns=[],
                pks=[],
                fks=[],
                is_active=True,
                metadata_json=metadata_json,
            ))

        reporter.done()
        return tables

    def get_schema(self, table_name: str) -> Table:
        tables = self.get_schemas()
        for t in tables:
            if t.name == table_name:
                return t

        lowered = (table_name or "").lower()
        for t in tables:
            pbi = (t.metadata_json or {}).get("powerbi_report_server") or {}
            candidates = [
                pbi.get("report_id"),
                pbi.get("dataset_id"),
                pbi.get("kpi_id"),
                pbi.get("report_name"),
                pbi.get("dataset_name"),
                pbi.get("kpi_name"),
                pbi.get("path"),
            ]
            for c in candidates:
                if c and str(c).lower() == lowered:
                    return t

        raise RuntimeError(f"Table not found: {table_name}")

    # ------------------------------------------------------------------
    # execute_query
    # ------------------------------------------------------------------

    def execute_query(
        self,
        query: Optional[str] = None,
        table_name: Optional[str] = None,
        report_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
        format: str = "CSV",
        parameters: Optional[Dict[str, Any]] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """Execute a query against a PBIRS asset.

        Routing:
          - If report_id → Reports/Export/{format} (RDL paginated report).
          - If dataset_id → Datasets({id})/Model.GetData action (shared dataset).
          - If table_name starts with "rdl:" or "dataset:" or "pbix:" → resolve IDs from table metadata.
          - PBIX tables (when enable_pbix_query=True) run the supplied `query` via DuckDB
            against a cached Parquet snapshot of the semantic model.

        `query` is required for PBIX but ignored for RDL/Dataset (those queries live in
        the report/dataset definition). Pass RDL/Dataset params via `parameters`.
        """
        if table_name and not (report_id or dataset_id):
            t = self.get_schema(table_name)
            pbi = (t.metadata_json or {}).get("powerbi_report_server") or {}
            rt = pbi.get("report_type")
            if rt == "Report":
                report_id = pbi.get("report_id")
            elif rt == "Dataset":
                dataset_id = pbi.get("dataset_id")
            elif rt in ("PowerBIReport", "PowerBIReportTable"):
                if self.enable_pbix_query and query:
                    rid = pbi.get("report_id")
                    if not rid:
                        raise ValueError(f"PBIX table '{table_name}' is missing report_id in metadata.")
                    return self._execute_pbix_query(
                        rid,
                        pbi.get("modified_date"),
                        report_name=pbi.get("report_name") or table_name,
                        query=query,
                        max_rows=max_rows,
                    )

                upstream = pbi.get("upstream_source") or ""
                srcs = pbi.get("data_sources") or []
                hint = f" Upstream: {upstream}." if upstream else ""
                detail = ""
                if srcs:
                    first = srcs[0]
                    detail = (
                        f" First data source: kind={first.get('kind')}, "
                        f"connection_string={first.get('connection_string')!r}. "
                        "Add that source as a separate data source in the app to query its data."
                    )
                if not self.enable_pbix_query:
                    raise NotImplementedError(
                        "Power BI (.pbix) query support is disabled on this connector. "
                        f"Set enable_pbix_query=True to materialize the semantic model.{hint}{detail}"
                    )
                # enable_pbix_query=True but no query string — ask for SQL.
                raise ValueError(
                    f"PBIX table '{table_name}' is queryable via DuckDB over its cached "
                    "semantic model, but execute_query requires a SQL `query` argument. "
                    "Use DuckDB syntax against the internal table names shown in the schema."
                )
            elif rt == "Kpi":
                raise ValueError(f"KPI '{table_name}' is a computed metric, not a queryable table. Inspect its metadata_json for the current value.")
            else:
                raise ValueError(f"Could not route execute_query for table '{table_name}' (report_type={rt}).")

        if report_id:
            return self._execute_paginated_report(report_id, fmt=format, parameters=parameters, max_rows=max_rows)
        if dataset_id:
            return self._execute_shared_dataset(dataset_id, parameters=parameters, max_rows=max_rows)

        raise ValueError(
            "execute_query requires one of: table_name, report_id, or dataset_id. "
            "Power BI (.pbix) reports are not queryable; use paginated (RDL) reports or shared datasets."
        )

    def _execute_paginated_report(
        self,
        report_id: str,
        *,
        fmt: str = "CSV",
        parameters: Optional[Dict[str, Any]] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        self.connect()
        export_url = f"{self._report_server_root()}/ReportServer/Pages/ReportViewer.aspx"
        # Try REST v2 export first
        v2_url = f"{self._api_base()}/Reports({report_id})/Export/{fmt}"
        params = {}
        if parameters:
            for k, v in parameters.items():
                params[k] = v
        r = self._session.get(v2_url, params=params, timeout=300)
        if r.status_code >= 300:
            # Fallback to legacy ReportServer URL access
            path = self._lookup_report_path(report_id)
            if not path:
                raise RuntimeError(f"Export failed: HTTP {r.status_code}. And no report path resolved for fallback.")
            qs = {"rs:Format": fmt, "rs:Command": "Render"}
            if parameters:
                qs.update(parameters)
            r = self._session.get(
                f"{self._report_server_root()}/ReportServer?{quote(path, safe='/')}",
                params=qs,
                timeout=300,
            )
            if r.status_code >= 300:
                raise RuntimeError(f"Export fallback failed: HTTP {r.status_code} {r.text[:300]}")

        content_type = (r.headers.get("Content-Type") or "").lower()
        if fmt.upper() == "CSV" or "csv" in content_type or "text" in content_type:
            df = pd.read_csv(StringIO(r.content.decode("utf-8-sig", errors="replace")))
        else:
            raise RuntimeError(f"Unsupported export format '{fmt}' for DataFrame conversion (Content-Type={content_type}).")

        if max_rows is not None and max_rows > 0 and len(df) > max_rows:
            df = df.head(max_rows)
        return df

    def _lookup_report_path(self, report_id: str) -> Optional[str]:
        try:
            data = self._get_json(f"/Reports({report_id})")
            return data.get("Path")
        except Exception:
            return None

    def _execute_shared_dataset(
        self,
        dataset_id: str,
        *,
        parameters: Optional[Dict[str, Any]] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        body: Dict[str, Any] = {}
        if parameters:
            body["Parameters"] = [{"Name": k, "Value": v} for k, v in parameters.items()]
        if max_rows is not None and max_rows > 0:
            body["maxRows"] = max_rows

        result = self._post_json(f"/Datasets({dataset_id})/Model.GetData", body, timeout=300)
        # Per OData metadata, GetData returns Edm.String — usually a CSV or JSON blob
        if result is None:
            return pd.DataFrame()
        if isinstance(result, dict):
            val = result.get("value")
            if val is None:
                return pd.DataFrame()
            text = val
        elif isinstance(result, str):
            text = result
        else:
            text = str(result)

        text = text.strip()
        if not text:
            return pd.DataFrame()
        if text.startswith("{") or text.startswith("["):
            try:
                import json as _json
                parsed = _json.loads(text)
                if isinstance(parsed, list):
                    return pd.DataFrame(parsed)
                if isinstance(parsed, dict) and isinstance(parsed.get("rows"), list):
                    return pd.DataFrame(parsed["rows"])
            except Exception:
                pass
        return pd.read_csv(StringIO(text))

    # ------------------------------------------------------------------
    # Prompt / description
    # ------------------------------------------------------------------

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    @property
    def description(self) -> str:
        pbix_note = (
            "PBIX semantic models are queryable via DuckDB over a cached Parquet snapshot of "
            "the embedded Vertipaq tables — the data reflects the last PBIX refresh, not live "
            "upstream. For up-to-the-minute data, connect the upstream source directly."
            if self.enable_pbix_query
            else "PBIX models are NOT queryable through this connector."
        )
        return (
            "Power BI Report Server (on-prem). Discovers reports, paginated reports, shared "
            "datasets, and KPIs via the PBIRS REST API. "
            f"{pbix_note}"
        ) + self.system_prompt()

    def system_prompt(self) -> str:
        pbix_section = (
            """
- `pbix:<ReportName>` — a Power BI (.pbix) interactive report (umbrella entry).
  `metadata_json.powerbi_report_server` contains:
    - `data_sources[]` — each entry has `kind`, `connection_string`, `auth_type`.
    - `upstream_source` — where the data originally came from.
    - `model_tables[]` / `model_relationships[]` — internal model structure.

- `pbix:<ReportName>/<ModelTable>` — an internal table inside a PBIX semantic model.
  **Queryable via DuckDB over a cached Parquet snapshot of the Vertipaq data.**
  All tables from the same PBIX register in a single DuckDB session, so you can
  JOIN them using bare internal table names. The data reflects the last PBIX refresh,
  NOT live upstream. For live data, prefer the upstream source.

  Example:
    execute_query(
        table_name="pbix:Sales Dashboard/Orders",
        query="SELECT c.Name, SUM(o.Amount) FROM Orders o JOIN Customers c "
              "ON o.CustomerID = c.CustomerID GROUP BY c.Name",
    )

  DAX measures in `metadata_json.powerbi_report_server.measures` are NOT executable —
  they are DAX expressions, not data. Rewrite them as SQL over the exported columns
  if the user needs measure-like aggregates.
"""
            if self.enable_pbix_query
            else """
- `pbix:<ReportName>` — a Power BI (.pbix) interactive report. **Metadata only, not queryable.**
- `pbix:<ReportName>/<ModelTable>` — internal table; metadata only. Direct users to the
  upstream source from `pbix:<ReportName>.metadata_json.powerbi_report_server.data_sources`.
"""
        )
        pbix_flavor = (
            "queryable via a cached Parquet snapshot"
            if self.enable_pbix_query
            else "NOT queryable"
        )
        header = (
            "\n## Power BI Report Server (on-prem) Guide\n\n"
            "A discovery catalog for Power BI Report Server assets (reports, paginated reports,\n"
            "shared datasets, KPIs). Paginated and shared datasets are queryable via REST export.\n"
            f"PBIX semantic models are {pbix_flavor} through this connector.\n\n"
            "### Table naming convention\n\n"
            "Tables returned by `get_schemas()` are prefixed by kind:\n"
        )
        footer = """

- `rdl:<ReportName>/<DataSetName>` — a paginated (RDL) report dataset. **Queryable.**
  - Backend SQL is in `metadata_json.powerbi_report_server.command_text`.
  - Run: `execute_query(table_name="rdl:...", parameters={...})` → DataFrame (CSV export).

- `dataset:<SharedDatasetName>` — a shared dataset. **Queryable** via `Model.GetData`.

- `kpi:<KpiName>` — a KPI tile. Metadata only (current value, goal, status).

### How to help the user when they ask for PBIX data

PBIX queryability via this connector is a **cached Vertipaq snapshot**, not live upstream.
When that's acceptable, query `pbix:<Report>/<Table>` with DuckDB SQL (see example above).
When the user needs current data, point them at `metadata_json.powerbi_report_server.data_sources`
on the `pbix:<Report>` umbrella row and tell them to connect that source directly.

DAX measures in `metadata_json.powerbi_report_server.measures` are expressions, not values —
they can't be executed as-is. If the user asks for a measure value, rewrite the DAX as
equivalent SQL over the exported columns.

### Paginated reports and shared datasets

Those have real, queryable SQL behind them and execute via REST export:

    df = client.execute_query(table_name="rdl:Sales Report/MainDataset",
                              parameters={"StartDate": "2024-01-01"})
    df = client.execute_query(table_name="dataset:Daily Orders",
                              parameters={"Region": "EU"})

For RDL/Dataset, the `query` argument is ignored — the query lives in the report definition.
For PBIX, `query` is required (DuckDB SQL against the internal table names).
"""
        return header + pbix_section + footer


# Compatibility aliases for dynamic resolver
PowerbiReportServerClient = PowerBIReportServerClient


async def warm_all_pbirs_caches() -> None:
    """Scheduled maintenance: walk every active PBIRS Connection and warm its
    pbix Parquet caches so a first create_data/inspect_data on a large .pbix
    doesn't stall the UI on pbixray parse. Designed for APScheduler interval.
    """
    import asyncio
    import time as _time

    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.connection import Connection

    t0 = _time.perf_counter()
    async with async_session_maker() as db:
        rows = (
            await db.execute(
                select(Connection).where(
                    Connection.type == "powerbi_report_server",
                    Connection.is_active.is_(True),
                    Connection.deleted_at.is_(None),
                )
            )
        ).scalars().all()

    if not rows:
        logger.info("pbirs.warmup.sweep", extra={"pbirs_connections": 0})
        return

    logger.info("pbirs.warmup.sweep.start", extra={"pbirs_connections": len(rows)})
    warmed = 0
    failed = 0
    for conn in rows:
        try:
            client = conn.get_client()
        except Exception as exc:
            logger.warning(
                "pbirs.warmup.client_init_failed",
                extra={"connection_id": str(conn.id), "pbirs_error": str(exc)},
            )
            failed += 1
            continue
        if not isinstance(client, PowerBIReportServerClient):
            continue
        try:
            # warm_pbix_caches is sync (requests/pbixray); offload to a thread so
            # the scheduler loop isn't blocked by a slow PBIRS server.
            await asyncio.to_thread(client.warm_pbix_caches)
            warmed += 1
        except Exception as exc:
            logger.warning(
                "pbirs.warmup.connection_failed",
                extra={"connection_id": str(conn.id), "pbirs_error": str(exc)},
            )
            failed += 1

    logger.info(
        "pbirs.warmup.sweep.done",
        extra={
            "pbirs_connections": len(rows),
            "pbirs_warmed": warmed,
            "pbirs_failed": failed,
            "pbirs_elapsed_s": round(_time.perf_counter() - t0, 2),
        },
    )
