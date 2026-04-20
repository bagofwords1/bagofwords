from __future__ import annotations

from app.data_sources.clients.base import DataSourceClient
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

# Max pbix size we'll attempt to parse (bytes). Larger files are skipped — they
# use more memory/time than is worthwhile for metadata-only discovery.
_PBIX_MAX_BYTES = 200 * 1024 * 1024  # 200MB

# Auto-generated internal Power BI date tables — filtered out of schema output.
_AUTO_DATE_TABLE_RE = re.compile(r"^(LocalDateTable|DateTableTemplate)_[0-9a-fA-F\-]+$")


def _pbix_cache_path(report_id: str, modified_date: Optional[str]) -> Path:
    key = f"{report_id}|{modified_date or ''}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return _PBIX_SCHEMA_CACHE_DIR / f"{h}.json"


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
        try:
            self.connect()
        except Exception as e:
            return {"success": False, "message": f"Session init failed: {e}"}

        try:
            sys_info = self.get_system_info()
        except Exception as e:
            msg = str(e)
            if "401" in msg or "Unauthorized" in msg:
                return {"success": False, "message": f"Authentication failed: check username, domain, and password ({msg[:180]})"}
            return {"success": False, "message": f"Cannot reach server: {msg[:200]}"}

        product = sys_info.get("ProductName") or "Power BI Report Server"
        version = sys_info.get("ProductVersion") or ""

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
            }

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
        }

    # ------------------------------------------------------------------
    # get_schemas — build BOW Table objects
    # ------------------------------------------------------------------

    def get_schemas(self) -> List[Table]:
        """Build Table objects for:
          - Each Power BI report (.pbix) — one Table per report (columns empty; metadata carries data sources)
          - Each paginated report (RDL) dataset — one Table per DataSet inside the RDL (columns + CommandText)
          - Each shared dataset (.rsd) — one Table with schema columns and CommandText
          - Each KPI — one Table representing the metric (for LLM awareness)
        """
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
                                    "model_table": tname,
                                    "measures": st.get("measures", []),
                                    "queryable": False,
                                    "upstream_source": upstream_hint,
                                    "schema_source": (schema_info or {}).get("source"),
                                    "query_note": (
                                        "Internal PBIX model table — not queryable through PBIRS. "
                                        "Use the upstream source for live data."
                                    ),
                                }
                            },
                        ))

        # ---- Paginated RDL reports: download content + parse ----
        if rdl_reports:
            with ThreadPoolExecutor(max_workers=6) as pool:
                content_futs = {pool.submit(self.download_catalog_item_content, r["Id"]): r for r in rdl_reports}
                for fut in as_completed(content_futs):
                    r = content_futs[fut]
                    rid = r["Id"]
                    rname = r.get("Name") or rid
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

        # ---- KPIs ----
        for k in kpis or []:
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
          - Power BI (.pbix) reports raise NotImplementedError.

        `query` is accepted for API symmetry but not used for RDL/Dataset executions
        (queries are stored in the RDL/RSD content). For shared datasets you can
        pass parameters via the `parameters` kwarg.
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
                raise NotImplementedError(
                    "Power BI Report Server is a discovery/exploration data source. "
                    "Power BI (.pbix) reports expose metadata (reports, parameters, owners, "
                    "data sources, lineage) but their embedded tabular model is NOT queryable "
                    f"through PBIRS on this server.{hint}{detail}"
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
        return (
            "Power BI Report Server (on-prem): METADATA-ONLY discovery/exploration catalog. "
            "This is NOT a queryable data source — you cannot run SQL, DAX, or any data query "
            "against a PBIX report through this connector. Use it only to browse what exists "
            "(reports, datasets, KPIs, owners, parameters, lineage) and to identify the "
            "upstream database/file that actually holds the data. To answer data questions, "
            "connect the upstream source as its own data source and query that."
        ) + self.system_prompt()

    def system_prompt(self) -> str:
        return """
## Power BI Report Server (on-prem) Guide

**IMPORTANT — this is NOT a queryable data source.** It is a **metadata-only catalog** of what
exists on a Power BI Report Server: reports, their owners, parameters, and (critically) the
upstream data sources that feed them. You cannot run SQL or DAX against PBIX reports through
this connector. Any actual data question must be answered by connecting the real upstream
database/file as its own data source in the app and querying that.

Use this connector to answer:
- "What reports exist on our PBIRS?"
- "Who owns the Sales dashboard? When was it last modified?"
- "What data source does the HR report use?" — then direct the user to connect that source.
- "Is there a report covering revenue by region?"

Do NOT use this connector to answer:
- "What were sales last quarter?" — that requires querying the upstream database directly.
- "Show me the top 10 products" — same.
- Any question whose answer is a row of data from a PBIX model.

### Table naming convention

Tables returned by `get_schemas()` are prefixed by kind:

- `pbix:<ReportName>` — a Power BI (.pbix) interactive report. **Metadata only, not queryable.**
  `metadata_json.powerbi_report_server` contains:
    - `data_sources[]` — each entry has `kind` (SQL/File/OData/etc.), `connection_string`, `auth_type`.
    - `upstream_source` — short human-readable summary of where the data actually lives.
    - `parameters[]`, `roles[]`, `path`, `modified_by`, etc.
    - `model_tables[]` / `model_relationships[]` — internal semantic-model structure (when extractable).

- `pbix:<ReportName>/<ModelTable>` — an internal table inside a PBIX semantic model.
  **Still not queryable**, but exposes real `columns[]` and DAX `measures[]` in metadata so the
  LLM can reason about structure. When the user asks for actual data from these columns, route
  them to the upstream source from the parent `pbix:<ReportName>` entry.

- `rdl:<ReportName>/<DataSetName>` — a paginated (RDL) report dataset. **Queryable.**
  - Backend SQL is in `metadata_json.powerbi_report_server.command_text`.
  - Run: `execute_query(table_name="rdl:...", parameters={...})` → DataFrame (CSV export).

- `dataset:<SharedDatasetName>` — a shared dataset. **Queryable** via `Model.GetData`.

- `kpi:<KpiName>` — a KPI tile. Metadata only (current value, goal, status).

### How to help the user when they ask to query a PBIX report

1. **Do not try to execute it** — `execute_query` on a `pbix:*` table will raise
   `NotImplementedError`. There is no DAX/XMLA path on this server.
2. **Read `metadata_json.powerbi_report_server.data_sources`** — this tells you the real
   upstream (SQL server, Excel file path, OData endpoint, etc.).
3. **Tell the user where the data lives and offer the next step:** connect the upstream
   source as its own data source in the app and write queries against it. Example:
   > "The 'AdventureWorks Sales' report is a Power BI report backed by an Excel file at
   > `c:\\...\\adventureworks sales.xlsx`. To query this data, add that file as an
   > Excel/SharePoint data source — I can't query the pbix model directly from PBIRS."
4. **If they want report-level output** (a rendered PDF/Excel of the visuals), note that
   this server's PBIRS build does not expose a pbix export action either — only RDL
   paginated reports can be rendered.

### When RDL or shared datasets are available

Prefer those — they have real, queryable SQL behind them:

```python
df = client.execute_query(table_name="rdl:Sales Report/MainDataset",
                          parameters={"StartDate": "2024-01-01"})
df = client.execute_query(table_name="dataset:Daily Orders",
                          parameters={"Region": "EU"})
```

The `query` argument is accepted for API symmetry but is ignored — the query lives in the
report/dataset definition on the server.
"""


# Compatibility aliases for dynamic resolver
PowerbiReportServerClient = PowerBIReportServerClient
