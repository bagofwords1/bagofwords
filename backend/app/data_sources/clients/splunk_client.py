"""Splunk data source client.

Talks to the Splunk REST API on the management port (`https://<host>:8089`).
There is no SQL endpoint; queries are **SPL** (Search Processing Language)
strings run as oneshot search jobs.

Schema discovery mirrors the **Zabbix** connector's curated-catalog + best-
effort-enrichment discipline, adapted to Splunk's schema-on-read model:

  1. **Tables = `index::sourcetype`.** Enumerated with ONE cheap search
     (`| tstats count where index=* by index, sourcetype`) that reads the
     tsidx *metadata*, not raw events. Cost is O(1) in searches regardless of
     how many sourcetypes exist — the property that keeps the 12h reindex from
     "taking forever".
  2. **Columns = sampled fields, capped.** Splunk has no free field catalog,
     so fields cost a real search. We sample fields for only the **top-K
     sourcetypes by volume** (`… | head N | fieldsummary`), bounded by a time
     window + head cap, cached, and best-effort (a sample failure degrades that
     one table to thin — it never fails discovery). Sourcetypes beyond the cap
     stay **thin** (no columns); the agent discovers their fields on demand via
     a `… | head 5` sample, per `system_prompt()`. An unknown field in Splunk
     is not an error — it silently matches nothing — so the thin-tail path is
     safe, just one extra peek.

Beyond raw events, discovery also catalogs the deployment's **knowledge
objects** — dashboards (`data/ui/views`) and saved searches/alerts
(`saved/searches`) — as `dashboard::<app>/<name>` and
`saved_search::<app>/<name>` tables. A dashboard's panels become its columns,
each carrying the panel's SPL; that SPL is the encoded tribal knowledge an
operator uses during a manual RCA, so surfacing it lets the agent replay the
same investigation (find the dashboard → read the panel SPL → re-run it
scoped to the incident window). Both Simple XML and Dashboard Studio (JSON)
definitions are parsed; listing is best-effort and never fails discovery.

Auth is Splunk-native:
  - `token`    → an authentication token sent as `Authorization: Bearer <token>`
                 (Settings → Tokens; works on Splunk Cloud and 8.x+).
  - `userpass` → HTTP basic against the management port (older on-prem installs).
"""
import json
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter

logger = logging.getLogger(__name__)

MAX_ROWS = 50_000              # hard cap per execute_query
DEFAULT_LIMIT = 1_000         # when the query spec omits `limit`
HTTP_TIMEOUT = 180            # seconds per REST request (searches can be slow)
SAMPLE_EVENTS = 500          # events sampled per sourcetype for field discovery
DEFAULT_MAX_SAMPLED = 50     # top-K sourcetypes that get field sampling
CATALOG_SEARCH = "| tstats count where index=* by index, sourcetype"
INDEX_CHUNK = 20             # indexes per OR-list tstats when wildcards are blocked

DASHBOARD_PREFIX = "dashboard::"
SAVED_SEARCH_PREFIX = "saved_search::"
MAX_KNOWLEDGE_OBJECTS = 300  # cap on dashboards / saved searches cataloged
SPL_DESC_MAX = 300           # panel/saved-search SPL shown in descriptions

# Apps whose knowledge objects are Splunk plumbing, not operator content.
_SYSTEM_APPS = frozenset({
    "system", "launcher", "learned", "legacy", "user-prefs", "appsbrowser",
    "alert_logevent", "alert_webhook", "introspection_generator_addon",
    "journald_input", "python_upgrade_readiness_app", "sample_app",
    "splunk_archiver", "splunk_assist", "splunk_essentials_9_0",
    "splunk_gdi", "splunk_httpinput", "splunk_instrumentation",
    "splunk_internal_metrics", "splunk_metrics_workspace",
    "splunk_monitoring_console", "splunk_rapid_diag", "splunk_secure_gateway",
    "splunk-dashboard-studio", "SplunkForwarder", "SplunkLightForwarder",
    "SplunkDeploymentServerConfig", "splunk-rolling-upgrade",
    "splunk_ingest_actions", "splunk_datasets_addon", "search_artifacts_helper",
})

# Stock knowledge objects shipped in the `search` app — Splunk self-monitoring
# plumbing, never the customer's RCA content.
_DEFAULT_VIEWS = frozenset({
    "analytics_workspace", "dashboards", "data_lab", "datasets", "search",
    "integrity_check_of_installed_files", "job_details_dashboard",
    "jquery_upgrade", "orphaned_scheduled_searches",
    "scheduled_export_dashboard", "secure_gateway_mobile_devices",
})
_DEFAULT_SAVED_SEARCHES = frozenset({
    "Errors in the last 24 hours", "Errors in the last hour",
    "License Usage Data Cube", "Messages by minute last 3 hours",
    "Orphaned scheduled searches", "Splunk errors last 24 hours",
    "Bucket Merge Retrieve Conf Settings", "Bucket Copy Trigger",
})

# An SPL wildcard-index reference (`index=*`). Hardened deployments (Splunk
# Cloud guardrails, ES-restricted roles) reject searches containing it.
_WILDCARD_INDEX = re.compile(r'index\s*=\s*"?\*', re.IGNORECASE)


class SplunkClient(DataSourceClient):

    def __init__(
        self,
        host: str,
        port: int = 8089,
        api_token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: bool = True,
        discovery_window_days: int = 7,
        max_sampled_sourcetypes: int = DEFAULT_MAX_SAMPLED,
        indexes: Optional[str] = None,
    ):
        # Accept a bare host, host:port, or a full URL. Normalize to the
        # management-port base URL (https by default).
        h = (host or "").strip().rstrip("/")
        if h.startswith("http://") or h.startswith("https://"):
            self.base_url = h
        else:
            self.base_url = f"https://{h}:{port}"
        self.host = host
        self.port = port
        self.api_token = api_token or None
        self.username = username or None
        self.password = password or None
        self.verify_ssl = verify_ssl
        self.discovery_window_days = int(discovery_window_days or 7)
        self.max_sampled_sourcetypes = int(max_sampled_sourcetypes or DEFAULT_MAX_SAMPLED)
        # Optional comma-separated index scope (also the escape hatch when the
        # deployment denies both wildcard searches and index enumeration).
        self._config_indexes: List[str] = []
        if isinstance(indexes, str) and indexes.strip():
            seen = set()
            for part in indexes.split(","):
                p = part.strip()
                if p and p not in seen:
                    seen.add(p)
                    self._config_indexes.append(p)
        self._indexes_cache: Optional[List[str]] = None

    @property
    def description(self):
        text = ("Splunk client — investigate machine data (logs/events) across "
                "indexes and sourcetypes with SPL (search, stats, timechart), "
                "and replay the team's dashboards and saved searches "
                "(`dashboard::app/name` / `saved_search::app/name` tables) for "
                "incident RCA.")
        return text + "\n\n" + self.system_prompt()

    # ── transport ─────────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        if self.api_token:
            return {"Authorization": f"Bearer {self.api_token}"}
        return {}

    def _auth(self):
        if self.api_token:
            return None
        if self.username:
            return (self.username, self.password or "")
        return None

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        params = dict(params or {})
        params.setdefault("output_mode", "json")
        try:
            resp = requests.get(
                f"{self.base_url}{path}", params=params,
                headers=self._headers(), auth=self._auth(),
                verify=self.verify_ssl, timeout=HTTP_TIMEOUT,
            )
        except requests.exceptions.SSLError as e:
            raise RuntimeError(f"Splunk TLS error: {e}. Set verify_ssl=false for self-signed certs.")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Splunk connection failed to {self.base_url}: {e}")
        return self._parse(resp, path)

    def _parse(self, resp: requests.Response, path: str) -> Any:
        if resp.status_code == 401:
            raise RuntimeError("Splunk authentication failed (401): check the token or username/password.")
        if resp.status_code == 402 or resp.status_code == 403:
            raise RuntimeError(f"Splunk access denied ({resp.status_code}): {resp.text[:300]}")
        if resp.status_code >= 400:
            raise RuntimeError(f"Splunk HTTP error ({resp.status_code}) on {path}: {resp.text[:400]}")
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(f"Splunk returned non-JSON response on {path}: {resp.text[:300]}")

    # ── search ────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_spl(spl: str) -> str:
        """Splunk's search endpoint requires a leading `search` for bare
        searches; generating/transforming commands begin with `|`."""
        s = (spl or "").strip()
        if not s:
            raise ValueError("Empty SPL search.")
        if s.startswith("|") or s.lower().startswith("search "):
            return s
        return f"search {s}"

    def _run_search(self, spl: str, *, earliest: Optional[str] = None,
                    latest: Optional[str] = None, count: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
        """Run a oneshot search job and return result rows (list of dicts).

        `exec_mode=oneshot` blocks and returns results in one call — no job
        polling. `count` caps rows server-side.
        """
        params = {
            "search": self._normalize_spl(spl),
            "exec_mode": "oneshot",
            "output_mode": "json",
            "count": int(count),
        }
        if earliest is not None:
            params["earliest_time"] = earliest
        if latest is not None:
            params["latest_time"] = latest
        try:
            resp = requests.post(
                f"{self.base_url}/services/search/jobs",
                data=params, headers=self._headers(), auth=self._auth(),
                verify=self.verify_ssl, timeout=HTTP_TIMEOUT,
            )
        except requests.exceptions.SSLError as e:
            raise RuntimeError(f"Splunk TLS error: {e}. Set verify_ssl=false for self-signed certs.")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Splunk search request failed: {e}")
        body = self._parse(resp, "/services/search/jobs")
        # Surface search-time messages (e.g. bad SPL) as errors.
        for msg in (body.get("messages") or []):
            if msg.get("type", "").upper() in ("ERROR", "FATAL"):
                text = f"Splunk search error: {msg.get('text')}"
                # A wildcard-index search rejected by a hardened deployment is
                # recoverable — tell the caller (usually the agent) which
                # indexes exist so it can rewrite with an explicit index.
                if _WILDCARD_INDEX.search(spl or ""):
                    known = self._known_indexes()
                    if known:
                        text += (" Hint: this deployment may forbid wildcard index "
                                 "searches — rewrite the search with an explicit index. "
                                 f"Known indexes: {', '.join(known)}.")
                raise RuntimeError(text)
        return body.get("results") or []

    # ── connection ──────────────────────────────────────────────────────────

    def test_connection(self):
        try:
            info = self._get("/services/server/info")
            entries = info.get("entry") or []
            version = "?"
            if entries:
                version = (entries[0].get("content") or {}).get("version", "?")
            return {"success": True, "message": f"Connected to Splunk {version}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── schema discovery ──────────────────────────────────────────────────────

    def _window(self) -> str:
        return f"-{self.discovery_window_days}d"

    def _list_indexes(self) -> List[str]:
        """Index names visible to the connection, via the REST index catalog
        (`GET /services/data/indexes`) — NO search runs, so it works even on
        deployments that reject wildcard index searches.

        Best-effort: an error (endpoint denied to the role) returns [].
        Internal indexes (leading `_`) are excluded. Cached per client.
        """
        if self._indexes_cache is not None:
            return self._indexes_cache
        names: List[str] = []
        try:
            body = self._get("/services/data/indexes", params={"count": 0})
            for entry in (body.get("entry") or []):
                name = entry.get("name")
                if name and not name.startswith("_"):
                    names.append(name)
        except Exception as e:
            logger.warning(f"Splunk index enumeration failed: {e}")
        self._indexes_cache = names
        return names

    def _known_indexes(self) -> List[str]:
        """Indexes to scope searches to: the admin-configured list wins,
        otherwise the REST-enumerated list."""
        return self._config_indexes or self._list_indexes()

    @staticmethod
    def _scoped_catalog_search(indexes: List[str]) -> str:
        clause = " OR ".join(f"index={i}" for i in indexes)
        return f"| tstats count where ({clause}) by index, sourcetype"

    def _catalog_pairs(self) -> List[Dict[str, Any]]:
        """`index::sourcetype` enumeration via tstats (tsidx metadata).

        Three tiers, all cheap:
          1. One global `index=*` tstats — the fast path on deployments that
             allow wildcard searches (skipped when the admin scoped `indexes`).
          2. Explicit OR-list tstats over the known indexes (configured or
             REST-enumerated) — no wildcard, so it survives hardened
             deployments that reject `index=*`. Chunked to bound SPL length.
          3. `| metadata` — sourcetypes only, no index pairing. The index is
             recorded as UNKNOWN (None), never `*`: storing a wildcard here
             poisons every later query on restricted deployments.
        """
        if not self._config_indexes:
            try:
                rows = self._run_search(CATALOG_SEARCH, earliest=self._window(),
                                        latest="now", count=MAX_ROWS)
                if rows:
                    return rows
            except Exception as e:
                logger.warning(
                    f"Splunk wildcard tstats catalog failed (deployment may forbid "
                    f"index=*); falling back to explicit index enumeration: {e}")
        # Tier 2: explicit OR-list tstats over known indexes.
        known = self._known_indexes()
        if known:
            rows: List[Dict[str, Any]] = []
            for i in range(0, len(known), INDEX_CHUNK):
                chunk = known[i:i + INDEX_CHUNK]
                try:
                    rows.extend(self._run_search(
                        self._scoped_catalog_search(chunk),
                        earliest=self._window(), latest="now", count=MAX_ROWS))
                except Exception as e:
                    logger.warning(f"Splunk scoped tstats catalog failed for {chunk}: {e}")
            if rows:
                return rows
        # Tier 3: metadata — no index pairing; record the index as unknown.
        try:
            rows = self._run_search("| metadata type=sourcetypes index=*",
                                    earliest=self._window(), latest="now", count=MAX_ROWS)
            return [{"index": None, "sourcetype": r.get("sourcetype"),
                     "count": r.get("totalCount")} for r in rows if r.get("sourcetype")]
        except Exception as e:
            logger.warning(f"Splunk metadata catalog failed: {e}")
            return []

    def _resolve_index(self, sourcetype: str) -> Optional[str]:
        """Find which known index holds a sourcetype — one OR-list tstats per
        chunk, no wildcard. Returns None when it can't be determined."""
        known = self._known_indexes()
        for i in range(0, len(known), INDEX_CHUNK):
            chunk = known[i:i + INDEX_CHUNK]
            clause = " OR ".join(f"index={x}" for x in chunk)
            spl = (f'| tstats count where ({clause}) sourcetype="{sourcetype}" '
                   f'by index | sort - count')
            try:
                rows = self._run_search(spl, earliest=self._window(), latest="now", count=10)
            except Exception as e:
                logger.warning(f"Splunk index resolution failed for {sourcetype}: {e}")
                continue
            for r in rows:
                if r.get("index"):
                    return r["index"]
        return None

    def _sample_fields(self, index: Optional[str], sourcetype: str) -> List[TableColumn]:
        """Sample fields for one sourcetype via a bounded fieldsummary search.

        Best-effort: any failure returns [] (the table stays thin). Bounded by
        the discovery window + a head cap so it can't scan all-time.

        NEVER emits `index=*` — hardened deployments reject wildcard index
        searches outright. An unknown index is resolved via the index catalog
        first; if it still can't be determined, the table stays thin.
        """
        idx = None if index in (None, "", "*") else index
        if idx is None:
            idx = self._resolve_index(sourcetype)
        if idx is None:
            logger.warning(
                f"Splunk field sample skipped for sourcetype='{sourcetype}': "
                f"index unknown and could not be resolved (wildcard searches not used).")
            return []
        spl = (f'search index={idx} sourcetype="{sourcetype}" '
               f'| head {SAMPLE_EVENTS} | fieldsummary maxvals=0')
        try:
            rows = self._run_search(spl, earliest=self._window(), latest="now", count=1000)
        except Exception as e:
            logger.warning(f"Splunk field sample failed for {idx}::{sourcetype}: {e}")
            return []
        columns: List[TableColumn] = []
        for r in rows:
            field = r.get("field")
            if not field or field.startswith("_") and field not in ("_time", "_raw"):
                # Skip most internal fields; keep _time and _raw (useful).
                if field not in ("_time", "_raw"):
                    continue
            try:
                numeric = int(float(r.get("numeric_count") or 0))
                total = int(float(r.get("count") or 0))
            except (TypeError, ValueError):
                numeric, total = 0, 0
            dtype = "float" if (total and numeric >= total * 0.9) else "str"
            columns.append(TableColumn(name=field, dtype=dtype))
        return columns

    # ── dashboards & saved searches (knowledge objects) ──────────────────────

    @staticmethod
    def _split_app_ref(ref: str, prefix: str) -> Tuple[str, str]:
        """`dashboard::payments/checkout` (or bare `payments/checkout`, or just
        `checkout`) → (app, name). App defaults to `-` (any app)."""
        r = (ref or "").strip()
        if r.startswith(prefix):
            r = r[len(prefix):]
        if "/" in r:
            app, name = r.split("/", 1)
            return (app or "-"), name
        return "-", r

    @staticmethod
    def _truncate_spl(spl: str) -> str:
        s = " ".join((spl or "").split())
        return s if len(s) <= SPL_DESC_MAX else s[:SPL_DESC_MAX] + " …"

    @staticmethod
    def _parse_studio_definition(defn: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Panels from a Dashboard Studio JSON definition.

        Studio splits a panel into a visualization + a data source; `ds.chain`
        sources extend a base source with post-processing SPL, so the runnable
        query is base + chain."""
        sources = defn.get("dataSources") or {}

        used_ds = set()

        def resolve_query(ds_id, seen=None):
            seen = seen or set()
            if ds_id in seen:
                return None, {}
            seen.add(ds_id)
            used_ds.add(ds_id)  # a base reached through a chain is consumed too
            ds = sources.get(ds_id) or {}
            opts = ds.get("options") or {}
            q = opts.get("query") or ""
            params = opts.get("queryParameters") or {}
            base_id = opts.get("extend")
            if base_id:
                base_q, base_params = resolve_query(base_id, seen)
                if base_q:
                    q = f"{base_q.rstrip()} {q.strip()}" if q.strip() else base_q
                params = {**base_params, **params}
            return (q or None), params

        panels: List[Dict[str, Any]] = []
        for viz_id, viz in (defn.get("visualizations") or {}).items():
            ds_ref = (viz.get("dataSources") or {}).get("primary")
            if not ds_ref:
                continue
            q, params = resolve_query(ds_ref)
            if not q:
                continue
            title = viz.get("title") or (sources.get(ds_ref) or {}).get("name") or viz_id
            panels.append({"title": str(title), "spl": q,
                           "earliest": params.get("earliest"),
                           "latest": params.get("latest")})
        # Data sources no visualization references (rare, but a search-only
        # dashboard is still knowledge worth surfacing).
        for ds_id, ds in sources.items():
            if ds_id in used_ds or (ds.get("type") or "") == "ds.chain":
                continue
            q, params = resolve_query(ds_id)
            if q and not any(p["spl"] == q for p in panels):
                panels.append({"title": str(ds.get("name") or ds_id), "spl": q,
                               "earliest": params.get("earliest"),
                               "latest": params.get("latest")})
        return panels

    @classmethod
    def _parse_dashboard_xml(cls, xml_text: str) -> Optional[Dict[str, Any]]:
        """Parse a view's `eai:data` into {label, type, panels} — or None when
        the view is not a dashboard (built-in app pages, HTML views)."""
        try:
            root = ET.fromstring(xml_text or "")
        except ET.ParseError:
            return None
        tag = root.tag.split("}")[-1]
        if tag not in ("dashboard", "form"):
            return None
        label = (root.findtext("label") or "").strip() or None
        if (root.get("version") or "").strip() in ("2", "1.1", "2.0"):
            defn_text = root.findtext("definition") or ""
            try:
                defn = json.loads(defn_text)
            except (ValueError, TypeError):
                return {"label": label, "type": "studio", "panels": []}
            return {"label": label or defn.get("title"), "type": "studio",
                    "panels": cls._parse_studio_definition(defn)}
        # Simple XML: <row>/<panel> holding viz elements with <search><query>.
        # Base searches (<search id=X>) are extended via <search base=X>.
        bases: Dict[str, str] = {}
        for s in root.iter("search"):
            sid = s.get("id")
            q = (s.findtext("query") or "").strip()
            if sid and q:
                bases[sid] = q
        panels: List[Dict[str, Any]] = []

        def search_to_panel(search_el, title):
            q = (search_el.findtext("query") or "").strip()
            base = search_el.get("base")
            if base and bases.get(base):
                q = f"{bases[base]} {q}".strip() if q else bases[base]
            if not q:
                return
            panels.append({"title": title, "spl": q,
                           "earliest": (search_el.findtext("earliest") or "").strip() or None,
                           "latest": (search_el.findtext("latest") or "").strip() or None})

        for panel_el in root.iter("panel"):
            p_title = (panel_el.findtext("title") or "").strip()
            for viz in list(panel_el):
                v_tag = viz.tag.split("}")[-1]
                if v_tag in ("title", "input", "html", "description"):
                    continue
                v_title = (viz.findtext("title") or "").strip() or p_title or v_tag
                s = viz.find("search")
                if s is not None:
                    search_to_panel(s, v_title)
                else:
                    legacy = (viz.findtext("searchString") or "").strip()
                    if legacy:
                        panels.append({"title": v_title, "spl": legacy,
                                       "earliest": None, "latest": None})
        return {"label": label, "type": "simple_xml", "panels": panels}

    def _list_views(self) -> List[Dict[str, Any]]:
        body = self._get("/servicesNS/-/-/data/ui/views", params={"count": 0})
        return body.get("entry") or []

    def _get_view(self, app: str, name: str) -> Dict[str, Any]:
        from urllib.parse import quote
        body = self._get(f"/servicesNS/-/{quote(app, safe='')}/data/ui/views/{quote(name, safe='')}",
                         params={"count": 1})
        entries = body.get("entry") or []
        if not entries:
            raise RuntimeError(f"Splunk dashboard not found: {app}/{name}")
        return entries[0]

    @staticmethod
    def _entry_app(entry: Dict[str, Any]) -> str:
        return ((entry.get("acl") or {}).get("app")
                or ((entry.get("content") or {}).get("eai:acl") or {}).get("app")
                or "search")

    def _dashboard_table(self, entry: Dict[str, Any],
                         parsed: Dict[str, Any]) -> Table:
        app = self._entry_app(entry)
        name = entry.get("name")
        label = parsed.get("label") or name
        owner = (entry.get("acl") or {}).get("owner")
        panels = parsed.get("panels") or []
        columns = [TableColumn(name=p["title"], dtype="panel",
                               description=self._truncate_spl(p["spl"]))
                   for p in panels]
        titles = "; ".join(p["title"] for p in panels[:8])
        desc = (f"Splunk dashboard '{label}' (app: {app}"
                + (f", owner: {owner}" if owner else "") + ")."
                + (f" Panels: {titles}." if titles else "")
                + " Each panel column's description is its saved SPL — the team's"
                  " curated investigation queries. Run a panel with execute_query"
                  f"({{\"dashboard\": \"{app}/{name}\", \"panel\": \"<title>\","
                  " \"earliest\": ..., \"latest\": ...}}) scoped to the incident"
                  " window, or adapt its SPL directly.")
        return Table(
            name=f"{DASHBOARD_PREFIX}{app}/{name}", description=desc,
            columns=columns, pks=[], fks=[],
            metadata_json={"splunk": {
                "kind": "dashboard", "app": app, "view_id": name,
                "dashboard_type": parsed.get("type"), "panel_count": len(panels),
                "panels": panels,
            }},
        )

    def _dashboard_tables(self) -> List[Table]:
        tables: List[Table] = []
        for entry in self._list_views():
            if len(tables) >= MAX_KNOWLEDGE_OBJECTS:
                break
            app = self._entry_app(entry)
            if app in _SYSTEM_APPS:
                continue
            if app == "search" and entry.get("name") in _DEFAULT_VIEWS:
                continue
            content = entry.get("content") or {}
            if str(content.get("isVisible", True)).lower() in ("0", "false"):
                continue
            parsed = self._parse_dashboard_xml(content.get("eai:data") or "")
            if parsed is None:
                continue
            try:
                tables.append(self._dashboard_table(entry, parsed))
            except Exception as e:
                logger.warning(f"Splunk dashboard catalog skipped {app}/{entry.get('name')}: {e}")
        return tables

    def _saved_search_tables(self) -> List[Table]:
        body = self._get("/servicesNS/-/-/saved/searches", params={"count": 0})
        tables: List[Table] = []
        for entry in (body.get("entry") or []):
            if len(tables) >= MAX_KNOWLEDGE_OBJECTS:
                break
            app = self._entry_app(entry)
            name = entry.get("name")
            if app in _SYSTEM_APPS or not name:
                continue
            if app == "search" and name in _DEFAULT_SAVED_SEARCHES:
                continue
            content = entry.get("content") or {}
            spl = (content.get("search") or "").strip()
            if not spl:
                continue
            is_alert = str(content.get("alert_type") or "always") != "always"
            scheduled = str(content.get("is_scheduled", "0")).lower() in ("1", "true")
            cron = content.get("cron_schedule") or None
            user_desc = (content.get("description") or "").strip()
            kind_word = "alert" if is_alert else "saved search"
            desc = (f"Splunk {kind_word} (app: {app})."
                    + (f" {user_desc}" if user_desc else "")
                    + (f" Scheduled: {cron}." if scheduled and cron else "")
                    + f" SPL: {self._truncate_spl(spl)}"
                    + " — run it with execute_query({\"saved_search\": "
                      f"\"{app}/{name}\", \"earliest\": ..., \"latest\": ...}})"
                      " or adapt the SPL.")
            tables.append(Table(
                name=f"{SAVED_SEARCH_PREFIX}{app}/{name}", description=desc,
                columns=[], pks=[], fks=[],
                metadata_json={"splunk": {
                    "kind": "alert" if is_alert else "saved_search",
                    "app": app, "saved_search_name": name, "spl": spl,
                    "cron": cron, "alert": is_alert,
                }},
            ))
        return tables

    def _knowledge_tables(self) -> List[Table]:
        """Dashboards + saved searches, best-effort: a failure (endpoint denied
        to the role, unparseable view) never fails event discovery."""
        tables: List[Table] = []
        try:
            tables.extend(self._dashboard_tables())
        except Exception as e:
            logger.warning(f"Splunk dashboard catalog failed: {e}")
        try:
            tables.extend(self._saved_search_tables())
        except Exception as e:
            logger.warning(f"Splunk saved-search catalog failed: {e}")
        return tables

    def get_schemas(self, progress_callback=None) -> List[Table]:
        """Discover `index::sourcetype` tables (cheap) and sample fields for
        the top-K sourcetypes by volume (capped)."""
        pairs = self._catalog_pairs()
        # Rank by event count so the sampling budget goes to the sourcetypes
        # people actually query.
        def _cnt(p):
            try:
                return int(float(p.get("count") or 0))
            except (TypeError, ValueError):
                return 0
        pairs = sorted(pairs, key=_cnt, reverse=True)

        total = len(pairs)
        if progress_callback:
            try:
                progress_callback("schema", "splunk catalog", 0, total)
            except Exception:
                pass

        tables: List[Table] = []
        for i, p in enumerate(pairs):
            index = p.get("index") or None
            if index == "*":
                index = None
            sourcetype = p.get("sourcetype")
            if not sourcetype:
                continue
            name = f"{index}::{sourcetype}" if index else sourcetype
            count = _cnt(p)
            columns: List[TableColumn] = []
            sampled = i < self.max_sampled_sourcetypes
            if sampled:
                columns = self._sample_fields(index, sourcetype)
            if columns:
                desc = (f"Splunk events: index='{index}', sourcetype='{sourcetype}' "
                        f"(~{count:,} events, fields sampled from last "
                        f"{self.discovery_window_days}d).")
            elif index:
                desc = (f"Splunk events: index='{index}', sourcetype='{sourcetype}' "
                        f"(~{count:,} events). Schema-on-read: fields NOT pre-sampled "
                        f"(cost cap) — the data IS present. You MUST discover fields "
                        f"yourself, do NOT ask the user: run `search index={index} "
                        f"sourcetype=\"{sourcetype}\" | head 1000 | fieldsummary`, read the "
                        f"field names, then query.")
            else:
                # Index unknown (metadata-only catalog on a deployment that
                # denied both wildcard searches and index enumeration).
                desc = (f"Splunk events: sourcetype='{sourcetype}' (~{count:,} events). "
                        f"The index could NOT be determined (this deployment restricts "
                        f"index discovery) — do NOT use `index=*`, it is rejected here. "
                        f"Query with `sourcetype=\"{sourcetype}\"` alone (searches the "
                        f"role's default indexes), or ask the user/admin which index "
                        f"holds this sourcetype and query `index=<that> "
                        f"sourcetype=\"{sourcetype}\"`.")
            tables.append(Table(
                name=name, description=desc, columns=columns, pks=[], fks=[],
                metadata_json={"index": index, "sourcetype": sourcetype,
                               "event_count": count, "fields_sampled": bool(columns)},
            ))
            if progress_callback:
                try:
                    progress_callback("schema", name, i + 1, total)
                except Exception:
                    pass
        tables.extend(self._knowledge_tables())
        return tables

    def get_schema(self, table_name: str) -> Table:
        """Fields for a single `index::sourcetype` table (samples on demand —
        this is how the thin-tail tables fill in their columns), or the full
        panel set for a `dashboard::app/name` / `saved_search::app/name` entry
        (re-fetched live, so a stale catalog still hydrates correctly).

        A bare-sourcetype name (or a legacy `*::sourcetype` one) has its index
        resolved via the index catalog — no wildcard search is ever emitted."""
        if table_name.startswith(DASHBOARD_PREFIX):
            app, name = self._split_app_ref(table_name, DASHBOARD_PREFIX)
            entry = self._get_view(app, name)
            parsed = self._parse_dashboard_xml(
                (entry.get("content") or {}).get("eai:data") or "")
            if parsed is None:
                raise RuntimeError(f"Splunk view {app}/{name} is not a dashboard.")
            return self._dashboard_table(entry, parsed)
        if table_name.startswith(SAVED_SEARCH_PREFIX):
            app, name = self._split_app_ref(table_name, SAVED_SEARCH_PREFIX)
            for t in self._saved_search_tables():
                if t.name == f"{SAVED_SEARCH_PREFIX}{app}/{name}" or (
                        app == "-" and t.name.endswith(f"/{name}")):
                    return t
            raise RuntimeError(f"Splunk saved search not found: {app}/{name}")
        if "::" in table_name:
            index, sourcetype = table_name.split("::", 1)
        else:
            index, sourcetype = None, table_name
        if index == "*":
            index = None
        columns = self._sample_fields(index, sourcetype)
        desc = f"Splunk events: index='{index or 'unknown'}', sourcetype='{sourcetype}'."
        return Table(name=table_name, description=desc, columns=columns, pks=[], fks=[],
                     metadata_json={"index": index, "sourcetype": sourcetype})

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    # ── querying ──────────────────────────────────────────────────────────────

    def execute_query(self, query) -> pd.DataFrame:
        """Execute an SPL search and return a DataFrame.

        `query` is either a bare SPL string, or a JSON envelope:
            {"spl": "search index=web status>=500 | stats count by host",
             "earliest": "-24h", "latest": "now", "limit": 1000}

        `earliest`/`latest` default to the connection's discovery window and
        `now` when omitted; `limit` caps rows (default 1000, hard cap 50k).

        Dashboard/saved-search envelopes run the SPL a knowledge object has
        stored (fetched live at query time, so it is never stale):
            {"dashboard": "payments/checkout_health"}            → panel inventory
            {"dashboard": "payments/checkout_health",
             "panel": "Error rate by service", "earliest": "-1h"} → run that panel
            {"saved_search": "payments/High error rate", "earliest": "-1h"}
        """
        spec = self._coerce_spec(query)
        if spec.get("dashboard"):
            return self._execute_dashboard(spec)
        if spec.get("saved_search"):
            return self._execute_saved_search(spec)
        spl, earliest, latest, limit = self._parse_spec(spec)
        rows = self._run_search(spl, earliest=earliest, latest=latest, count=limit)
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)

    @staticmethod
    def _coerce_spec(query) -> Dict[str, Any]:
        if isinstance(query, dict):
            return query
        s = (query or "").strip()
        # A JSON envelope, or a bare SPL string.
        if s.startswith("{"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return {"spl": s}
        return {"spl": s}

    def _parse_spec(self, query):
        earliest: Optional[str] = self._window()
        latest: Optional[str] = "now"
        limit = DEFAULT_LIMIT
        spec = self._coerce_spec(query)
        spl = spec.get("spl") or spec.get("search") or spec.get("query")
        if not spl:
            raise ValueError(
                'Splunk query must be an SPL string or a JSON envelope like '
                '{"spl": "search index=web | stats count by host", "earliest": "-24h", "limit": 1000}, '
                'or a knowledge-object envelope like {"dashboard": "<app>/<name>", "panel": "<title>"} '
                'or {"saved_search": "<app>/<name>"}.'
            )
        if spec.get("earliest") is not None:
            earliest = spec["earliest"]
        if spec.get("latest") is not None:
            latest = spec["latest"]
        if spec.get("limit") is not None:
            try:
                limit = min(int(spec["limit"]), MAX_ROWS)
            except (TypeError, ValueError):
                pass
        return spl, earliest, latest, limit

    def _time_and_limit(self, spec: Dict[str, Any],
                        default_earliest: Optional[str],
                        default_latest: Optional[str]):
        earliest = spec.get("earliest") or default_earliest or self._window()
        latest = spec.get("latest") or default_latest or "now"
        limit = DEFAULT_LIMIT
        if spec.get("limit") is not None:
            try:
                limit = min(int(spec["limit"]), MAX_ROWS)
            except (TypeError, ValueError):
                pass
        return earliest, latest, limit

    def _execute_dashboard(self, spec: Dict[str, Any]) -> pd.DataFrame:
        app, name = self._split_app_ref(str(spec["dashboard"]), DASHBOARD_PREFIX)
        entry = self._get_view(app, name)
        parsed = self._parse_dashboard_xml(
            (entry.get("content") or {}).get("eai:data") or "")
        if parsed is None:
            raise RuntimeError(f"Splunk view {app}/{name} is not a dashboard.")
        panels = parsed.get("panels") or []
        panel_ref = spec.get("panel")
        if panel_ref is None or panel_ref == "":
            # No panel selected → return the panel inventory so the agent can
            # read the SPL and pick (also covers "what does this dashboard show").
            return pd.DataFrame([{"panel": p["title"], "spl": p["spl"],
                                  "earliest": p.get("earliest"),
                                  "latest": p.get("latest")} for p in panels])
        panel = None
        if isinstance(panel_ref, int) or (isinstance(panel_ref, str) and panel_ref.isdigit()):
            i = int(panel_ref)
            if 0 <= i < len(panels):
                panel = panels[i]
        if panel is None:
            want = str(panel_ref).strip().lower()
            panel = next((p for p in panels if p["title"].strip().lower() == want), None)
        if panel is None:
            titles = "; ".join(p["title"] for p in panels)
            raise ValueError(f"Panel '{panel_ref}' not found on {app}/{name}. "
                             f"Panels: {titles}")
        earliest, latest, limit = self._time_and_limit(
            spec, panel.get("earliest"), panel.get("latest"))
        rows = self._run_search(panel["spl"], earliest=earliest, latest=latest,
                                count=limit)
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    def _execute_saved_search(self, spec: Dict[str, Any]) -> pd.DataFrame:
        from urllib.parse import quote
        app, name = self._split_app_ref(str(spec["saved_search"]), SAVED_SEARCH_PREFIX)
        body = self._get(
            f"/servicesNS/-/{quote(app, safe='')}/saved/searches/{quote(name, safe='')}",
            params={"count": 1})
        entries = body.get("entry") or []
        if not entries:
            raise RuntimeError(f"Splunk saved search not found: {app}/{name}")
        content = entries[0].get("content") or {}
        spl = (content.get("search") or "").strip()
        if not spl:
            raise RuntimeError(f"Splunk saved search {app}/{name} has no SPL.")
        earliest, latest, limit = self._time_and_limit(
            spec, content.get("dispatch.earliest_time"),
            content.get("dispatch.latest_time"))
        rows = self._run_search(spl, earliest=earliest, latest=latest, count=limit)
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    # ── prompts ───────────────────────────────────────────────────────────────

    def system_prompt(self):
        text = """
        ## Splunk Integration
        Query Splunk via `execute_query(query)` using SPL (Search Processing
        Language). `query` is either a bare SPL string or a JSON envelope:

        ```json
        {"spl": "search index=web sourcetype=access_combined status>=500 | stats count by host",
         "earliest": "-24h", "latest": "now", "limit": 1000}
        ```

        - Tables are named `index::sourcetype`. Query them with
          `index=<index> sourcetype="<sourcetype>"` in SPL.
        - `earliest`/`latest`: time bounds (e.g. "-24h", "-7d@d", "now",
          epoch). ALWAYS bound a search by time — an unbounded search is slow.
          Defaults are applied if omitted.
        - `limit`: max rows (default 1000).

        IMPORTANT specifics:
        - Splunk is SCHEMA-ON-READ. A table showing NO columns does NOT mean it
          is empty or misconfigured — it means the fields were not pre-sampled
          during indexing (a deliberate cost cap). The data is there. You MUST
          discover the fields YOURSELF by running:
          `search index=<idx> sourcetype="<st>" | head 1000 | fieldsummary`
          (this surfaces auto-extracted JSON/KV fields that a bare `| head` may
          not). Read the field names from that result, THEN write your real
          query. Do NOT ask the user what the fields are, do NOT skip to a
          different table, and do NOT assume the table is empty — just run the
          discovery search and proceed. A field that does not exist is not an
          error; it silently matches nothing, so confirm names via discovery.
        - `_time` is the event time; `_raw` is the raw event text.
        - Use transforming commands to aggregate: `| stats count by host`,
          `| timechart span=1h count`, `| top limit=10 status`.
        - ALWAYS name the index(es) you search — take them from the table
          names (`index::sourcetype`). Span multiple indexes with an explicit
          OR list: `(index=web OR index=app)`. Do NOT use `index=*`: many
          deployments (Splunk Cloud guardrails, restricted roles) REJECT
          wildcard index searches with "you must specify a specific index" —
          and even where allowed, an explicit index is faster.

        DASHBOARDS & SAVED SEARCHES (the team's curated knowledge):
        - Tables named `dashboard::<app>/<name>` are the deployment's Splunk
          dashboards — the SAME dashboards operators stare at during a manual
          investigation. Each panel is a column whose description is the
          panel's saved SPL. Tables named `saved_search::<app>/<name>` are
          saved searches/alerts; their SPL is in the description.
        - For an incident/RCA question, PREFER these over writing SPL from
          scratch: find the dashboard covering the affected service, read its
          panel SPL, then re-run panels scoped to the incident time window.
          They encode the fields, indexes, and thresholds the team actually
          uses.
        - Run them via envelopes (never paste `dashboard::...` into SPL):
          `{"dashboard": "<app>/<name>"}` → panel inventory (title + SPL);
          `{"dashboard": "<app>/<name>", "panel": "<title>", "earliest":
          "-1h"}` → run one panel; `{"saved_search": "<app>/<name>",
          "earliest": "-1h"}` → run a saved search. `earliest`/`latest`
          override the panel's own defaults — ALWAYS set them to the incident
          window. You can also copy a panel's SPL and adapt it as a normal
          `spl` query (e.g. add filters for one host/service).

        Examples:
        ```python
        # Discover fields for a thin (un-sampled) sourcetype first
        df = client.execute_query('search index=app sourcetype="log4j" | head 5')
        # Error events per host across two indexes, last 24h
        df = client.execute_query('{"spl": "search (index=web OR index=app) (level=ERROR OR log_level=error) | stats count by host", "earliest": "-24h"}')
        # HTTP 5xx over time
        df = client.execute_query('{"spl": "search index=web sourcetype=access_combined status>=500 | timechart span=1h count", "earliest": "-7d"}')
        # RCA: re-run a dashboard panel scoped to the incident window
        df = client.execute_query('{"dashboard": "payments/checkout_health", "panel": "Error rate by service", "earliest": "-2h", "latest": "now"}')
        ```
        """
        return text


# Alias so dynamic naming ("Splunk" → "SplunkClient") and the explicit
# client_path both resolve to the same class.
SplunkClient = SplunkClient
