"""CrowdStrike Falcon data source client.

Talks to the Falcon platform REST API (`https://api.<cloud>.crowdstrike.com`)
with OAuth2 client credentials. There is no SQL endpoint: queries are JSON
specs (see `system_prompt`) that map to a fixed catalog of virtual tables —
hosts, host groups, alerts, incidents, CrowdScore, Spotlight vulnerabilities,
prevention policies and custom IOCs. Filtering is pushed down as FQL (Falcon
Query Language), the API's native filter syntax.

Read-only by construction: the only non-GET calls are the token request and
the documented "get entities by id" POST endpoints (alerts, incidents,
devices), which read but never change state. The raw-path escape hatch is
GET-only.

Route, parameter and limit facts below come from CrowdStrike's official SDK
(FalconPy) endpoint definitions:

  POST /oauth2/token                              form client_id/client_secret[/member_cid]
  GET  /devices/combined/devices/v1               hosts, offset token, limit ≤10000
       (fallback: /devices/queries/devices-scroll/v1 + POST /devices/entities/devices/v2 {ids})
  GET  /devices/combined/host-groups/v1           offset int, limit ≤5000
  GET  /alerts/queries/alerts/v2                  ids, offset int, limit ≤10000, sort `f|dir`
  POST /alerts/entities/alerts/v2                 {"composite_ids": [...]}
  GET  /incidents/queries/incidents/v1            ids, offset int, limit ≤500, sort `f.dir`
  POST /incidents/entities/incidents/GET/v1       {"ids": [...]}
  GET  /incidents/combined/crowdscores/v1         offset int, limit ≤2500
  GET  /spotlight/combined/vulnerabilities/v1     `after` cursor, limit ≤5000, filter REQUIRED
  GET  /policy/combined/prevention/v1             offset int, limit ≤5000
  GET  /iocs/combined/indicator/v1                `after` cursor, limit ≤2000

Cloud region: CrowdStrike runs separate clouds (us-1, us-2, eu-1, us-gov-1,
us-gov-2). With region "auto" the token is requested from us-1 and the client
follows the `X-Cs-Region` header the token endpoint returns, the same
autodiscovery FalconPy performs.
"""
import json
import time
from collections.abc import Generator
from contextlib import contextmanager

import pandas as pd
import requests

from app.ai.prompt_formatters import ForeignKey, ServiceFormatter, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, discovery_items, discovery_progress

MAX_ROWS = 10_000        # hard cap per execute_query
DEFAULT_LIMIT = 500      # when the query spec omits `limit`
HTTP_TIMEOUT = 60        # seconds per request
TOKEN_SKEW = 60          # refresh the token this many seconds before expiry
MAX_RETRIES_429 = 3      # retries on rate limiting
MAX_RETRY_WAIT = 10      # seconds; never sleep longer than this on a 429

REGIONS: dict[str, str] = {
    "us-1": "https://api.crowdstrike.com",
    "us-2": "https://api.us-2.crowdstrike.com",
    "eu-1": "https://api.eu-1.crowdstrike.com",
    "us-gov-1": "https://api.laggar.gcw.crowdstrike.com",
    "us-gov-2": "https://api.us-gov-2.crowdstrike.mil",
}
REGION_OPTIONS = ["auto", *REGIONS]


# ── virtual table catalog ─────────────────────────────────────────────────────
#
# `mode` decides how rows are fetched:
#   combined  — one GET returns full records
#   ids       — a query GET returns ids, then a POST hydrates them in batches
# `paging`: "offset" (integer offset), "token" (offset token from meta), or
# "after" (cursor in meta.pagination.after).
# `sort_sep`: the API's sort syntax — "|" (field|desc) or "." (field.desc).
# Columns use dotted names for nested fields — they are also the FQL names.
_CATALOG = {
    "hosts": {
        "mode": "combined",
        "path": "/devices/combined/devices/v1",
        "paging": "token",
        "page_max": 10_000,
        "sort_sep": ".",
        "scope": "Hosts: Read",
        "pk": "device_id",
        "columns": [
            ("device_id", "str"), ("hostname", "str"), ("platform_name", "str"),
            ("os_version", "str"), ("os_product_name", "str"), ("product_type_desc", "str"),
            ("agent_version", "str"), ("status", "str"), ("local_ip", "str"),
            ("external_ip", "str"), ("mac_address", "str"), ("machine_domain", "str"),
            ("site_name", "str"), ("ou", "list"), ("tags", "list"), ("groups", "list"),
            ("first_seen", "datetime"), ("last_seen", "datetime"),
            ("reduced_functionality_mode", "str"), ("system_manufacturer", "str"),
            ("system_product_name", "str"), ("serial_number", "str"),
            ("device_policies.prevention.policy_id", "str"),
        ],
        "fks": [],
        "desc": ("Endpoints running the Falcon sensor. status: normal | containment_pending | "
                 "contained | lift_containment_pending. product_type_desc: Workstation | Server | "
                 "Domain Controller. last_seen is ISO-8601 UTC. groups holds host_groups ids."),
    },
    "host_groups": {
        "mode": "combined",
        "path": "/devices/combined/host-groups/v1",
        "paging": "offset",
        "page_max": 5_000,
        "sort_sep": ".",
        "scope": "Host groups: Read",
        "pk": "id",
        "columns": [
            ("id", "str"), ("name", "str"), ("description", "str"), ("group_type", "str"),
            ("assignment_rule", "str"), ("created_timestamp", "datetime"),
            ("modified_timestamp", "datetime"),
        ],
        "fks": [],
        "desc": "Host groups. group_type: static | dynamic | staticByID. assignment_rule is the FQL rule for dynamic groups.",
    },
    "alerts": {
        "mode": "ids",
        "path": "/alerts/queries/alerts/v2",
        "entities_path": "/alerts/entities/alerts/v2",
        "entities_key": "composite_ids",
        "entities_batch": 1_000,
        "paging": "offset",
        "page_max": 10_000,
        "sort_sep": "|",
        "scope": "Alerts: Read",
        "pk": "composite_id",
        "columns": [
            ("composite_id", "str"), ("status", "str"), ("severity", "int"),
            ("severity_name", "str"), ("confidence", "int"), ("product", "str"),
            ("type", "str"), ("display_name", "str"), ("name", "str"), ("description", "str"),
            ("tactic", "str"), ("tactic_id", "str"), ("technique", "str"),
            ("technique_id", "str"), ("objective", "str"), ("scenario", "str"),
            ("created_timestamp", "datetime"), ("updated_timestamp", "datetime"),
            ("assigned_to_name", "str"), ("device.device_id", "str"),
            ("device.hostname", "str"), ("device.platform_name", "str"),
            ("user_name", "str"), ("filename", "str"), ("filepath", "str"),
            ("cmdline", "str"), ("sha256", "str"), ("falcon_host_link", "str"),
            ("tags", "list"),
        ],
        "fks": [("device.device_id", "hosts", "device_id")],
        "desc": ("Detections/alerts across Falcon products (endpoint, identity, cloud, NG-SIEM; "
                 "replaces the legacy Detects API). status: new | in_progress | closed | reopened. "
                 "severity is 1-100 (severity_name: Informational<20, Low, Medium, High, Critical>=90). "
                 "MITRE ATT&CK tactic/technique per alert."),
    },
    "incidents": {
        "mode": "ids",
        "path": "/incidents/queries/incidents/v1",
        "entities_path": "/incidents/entities/incidents/GET/v1",
        "entities_key": "ids",
        "entities_batch": 500,
        "paging": "offset",
        "page_max": 500,
        "sort_sep": ".",
        "scope": "Incidents: Read",
        "pk": "incident_id",
        "columns": [
            ("incident_id", "str"), ("name", "str"), ("description", "str"),
            ("state", "str"), ("status", "int"), ("fine_score", "int"),
            ("start", "datetime"), ("end", "datetime"), ("created", "datetime"),
            ("host_ids", "list"), ("users", "list"), ("tactics", "list"),
            ("techniques", "list"), ("objectives", "list"), ("assigned_to_name", "str"),
            ("tags", "list"),
        ],
        "fks": [],
        "desc": ("Incidents (correlated detections). state: open | closed. status: 20=New, "
                 "25=Reopened, 30=In Progress, 40=Closed. fine_score/10 is the 0-10 incident "
                 "score shown in the console. host_ids are hosts.device_id values."),
    },
    "crowdscores": {
        "mode": "combined",
        "path": "/incidents/combined/crowdscores/v1",
        "paging": "offset",
        "page_max": 2_500,
        "sort_sep": ".",
        "scope": "Incidents: Read",
        "pk": "id",
        "columns": [
            ("id", "str"), ("timestamp", "datetime"), ("score", "int"), ("adjusted_score", "int"),
        ],
        "fks": [],
        "desc": "CrowdScore time series — the tenant-wide threat level (0-100) over time.",
    },
    "vulnerabilities": {
        "mode": "combined",
        "path": "/spotlight/combined/vulnerabilities/v1",
        "paging": "after",
        "page_max": 5_000,
        "sort_sep": "|",
        "scope": "Vulnerabilities: Read",
        "pk": "id",
        "default_filter": "status:['open','reopen']",
        "default_params": {"facet": ["cve", "host_info"]},
        "columns": [
            ("id", "str"), ("aid", "str"), ("status", "str"),
            ("created_timestamp", "datetime"), ("updated_timestamp", "datetime"),
            ("closed_timestamp", "datetime"), ("cve.id", "str"), ("cve.severity", "str"),
            ("cve.base_score", "float"), ("cve.exprt_rating", "str"),
            ("cve.exploit_status", "int"), ("cve.is_cisa_kev", "bool"),
            ("cve.description", "str"), ("host_info.hostname", "str"),
            ("host_info.platform", "str"), ("host_info.os_version", "str"),
            ("host_info.local_ip", "str"), ("apps", "list"),
        ],
        "fks": [("aid", "hosts", "device_id")],
        "desc": ("Spotlight vulnerabilities (one row per CVE per host). status: open | closed | "
                 "reopen | expired. cve.severity: CRITICAL | HIGH | MEDIUM | LOW | NONE. "
                 "cve.exprt_rating is CrowdStrike's ExPRT rating. Defaults to open+reopen."),
    },
    "prevention_policies": {
        "mode": "combined",
        "path": "/policy/combined/prevention/v1",
        "paging": "offset",
        "page_max": 5_000,
        "sort_sep": ".",
        "scope": "Prevention policies: Read",
        "pk": "id",
        "columns": [
            ("id", "str"), ("name", "str"), ("description", "str"), ("platform_name", "str"),
            ("enabled", "bool"), ("groups", "list"), ("created_timestamp", "datetime"),
            ("modified_timestamp", "datetime"),
        ],
        "fks": [],
        "desc": "Prevention policies per platform and the host groups they apply to.",
    },
    "iocs": {
        "mode": "combined",
        "path": "/iocs/combined/indicator/v1",
        "paging": "after",
        "page_max": 2_000,
        "sort_sep": ".",
        "scope": "IOC Management: Read",
        "pk": "id",
        "columns": [
            ("id", "str"), ("type", "str"), ("value", "str"), ("action", "str"),
            ("severity", "str"), ("platforms", "list"), ("description", "str"),
            ("applied_globally", "bool"), ("host_groups", "list"), ("tags", "list"),
            ("expiration", "datetime"), ("created_on", "datetime"), ("modified_on", "datetime"),
            ("source", "str"), ("expired", "bool"),
        ],
        "fks": [],
        "desc": ("Custom indicators of compromise. type: sha256 | md5 | domain | ipv4 | ipv6. "
                 "action: no_action | allow | prevent | prevent_no_ui | detect."),
    },
}


class CrowdStrikeError(RuntimeError):
    """A readable Falcon API failure (status code kept for callers)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class CrowdStrikeAuthError(CrowdStrikeError):
    """The token request failed — credentials, not scopes."""


class CrowdStrikeClient(DataSourceClient):

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        region: str = "auto",
        base_url: str | None = None,
        member_cid: str | None = None,
        verify_ssl: bool = True,
    ):
        self.client_id = (client_id or "").strip()
        self.client_secret = client_secret or ""
        self.member_cid = (member_cid or "").strip() or None
        self.verify_ssl = verify_ssl
        self.region = (region or "auto").strip().lower()
        override = (base_url or "").strip().rstrip("/")
        if override:
            if not override.startswith(("http://", "https://")):
                override = f"https://{override}"
            self.base_url = override
            self._autodiscover = False
        elif self.region in REGIONS:
            self.base_url = REGIONS[self.region]
            self._autodiscover = False
        else:
            # "auto" (or an unknown value): start at us-1, follow X-Cs-Region.
            self.base_url = REGIONS["us-1"]
            self._autodiscover = True
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._session: requests.Session | None = None

    @property
    def description(self):
        text = ("CrowdStrike Falcon client — query endpoint security data (hosts, alerts, "
                "incidents, CrowdScore, Spotlight vulnerabilities, prevention policies and "
                "custom IOCs) via the Falcon REST API.")
        return text + "\n\n" + self.system_prompt()

    # ── connection / auth ─────────────────────────────────────────────────────

    @contextmanager
    def connect(self) -> Generator[requests.Session]:
        session = requests.Session()
        session.verify = self.verify_ssl
        session.headers.update({"Accept": "application/json"})
        self._session = session
        try:
            yield session
        finally:
            self._session = None
            session.close()

    def _fetch_token(self, session: requests.Session) -> str:
        if not (self.client_id and self.client_secret):
            raise CrowdStrikeError("CrowdStrike needs an API client ID and secret.")
        data = {"client_id": self.client_id, "client_secret": self.client_secret}
        if self.member_cid:
            data["member_cid"] = self.member_cid
        url = f"{self.base_url}/oauth2/token"
        try:
            resp = session.post(url, data=data, timeout=HTTP_TIMEOUT, allow_redirects=False)
        except requests.exceptions.SSLError as e:
            raise CrowdStrikeError(f"CrowdStrike TLS error: {e}")
        except requests.exceptions.RequestException as e:
            raise CrowdStrikeError(f"CrowdStrike connection failed to {self.base_url}: {e}")
        if resp.status_code in (400, 401, 403):
            raise CrowdStrikeAuthError(
                f"CrowdStrike rejected the API client credentials ({resp.status_code}): "
                f"{self._error_text(resp)}. Check the client ID/secret"
                + (" and the member CID" if self.member_cid else "")
                + " and that the API client is not disabled.",
                resp.status_code,
            )
        if resp.status_code not in (200, 201):
            raise CrowdStrikeAuthError(
                f"CrowdStrike token request failed ({resp.status_code}): {self._error_text(resp)}",
                resp.status_code,
            )
        body = self._json(resp)
        token = body.get("access_token")
        if not token:
            raise CrowdStrikeError("CrowdStrike token response had no access_token.")
        self._token = token
        self._token_expiry = time.time() + max(int(body.get("expires_in") or 1799) - TOKEN_SKEW, 30)
        # Region autodiscovery: the token endpoint answers on any cloud and
        # names the client's home cloud in X-Cs-Region.
        if self._autodiscover:
            home = (resp.headers.get("X-Cs-Region") or "").strip().lower()
            if home in REGIONS and REGIONS[home] != self.base_url:
                self.base_url = REGIONS[home]
            self.region = home or self.region
            self._autodiscover = False
        return token

    def _auth_headers(self, session: requests.Session) -> dict:
        if not self._token or time.time() >= self._token_expiry:
            self._fetch_token(session)
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method: str, path: str, *, params=None, body=None) -> dict:
        """One API call with token refresh on 401 and bounded 429 back-off.
        Returns the parsed JSON body."""
        if self._session is None:
            with self.connect():
                return self._request(method, path, params=params, body=body)
        session = self._session
        refreshed = False
        attempts = 0
        while True:
            # Headers first: the first token fetch may switch base_url (auto region).
            headers = self._auth_headers(session)
            url = f"{self.base_url}{path}"
            try:
                resp = session.request(
                    method, url, params=params, json=body, headers=headers,
                    timeout=HTTP_TIMEOUT,
                )
            except requests.exceptions.SSLError as e:
                raise CrowdStrikeError(f"CrowdStrike TLS error: {e}")
            except requests.exceptions.RequestException as e:
                raise CrowdStrikeError(f"CrowdStrike connection failed to {self.base_url}: {e}")

            if resp.status_code == 401 and not refreshed:
                self._token = None
                refreshed = True
                continue
            if resp.status_code == 429 and attempts < MAX_RETRIES_429:
                attempts += 1
                time.sleep(self._retry_wait(resp))
                continue
            if resp.status_code == 403:
                raise CrowdStrikeError(
                    f"CrowdStrike denied {path} (403): {self._error_text(resp)}. "
                    "The API client is missing the read scope for this data.",
                    403,
                )
            if resp.status_code == 404:
                raise CrowdStrikeError(f"CrowdStrike endpoint not found (404): {path}", 404)
            if resp.status_code >= 400:
                raise CrowdStrikeError(
                    f"CrowdStrike API error ({resp.status_code}) on {path}: {self._error_text(resp)}",
                    resp.status_code,
                )
            return self._json(resp)

    @staticmethod
    def _retry_wait(resp) -> float:
        # X-RateLimit-RetryAfter is an epoch second; Retry-After is seconds.
        try:
            retry_at = float(resp.headers.get("X-RateLimit-RetryAfter") or 0)
            if retry_at > 0:
                return min(max(retry_at - time.time(), 0.5), MAX_RETRY_WAIT)
        except ValueError:
            pass
        try:
            return min(max(float(resp.headers.get("Retry-After") or 1), 0.5), MAX_RETRY_WAIT)
        except ValueError:
            return 1.0

    @staticmethod
    def _json(resp) -> dict:
        try:
            body = resp.json()
        except ValueError:
            raise CrowdStrikeError(f"CrowdStrike returned non-JSON response: {resp.text[:300]}")
        return body if isinstance(body, dict) else {"resources": body}

    @staticmethod
    def _error_text(resp) -> str:
        try:
            body = resp.json()
            errs = body.get("errors") or []
            msgs = [e.get("message", "") for e in errs if isinstance(e, dict)]
            if msgs:
                return "; ".join(m for m in msgs if m)
            if body.get("message"):
                return str(body["message"])
        except Exception:
            pass
        return (resp.text or "")[:300]

    def test_connection(self):
        try:
            with self.connect():
                readable, denied, host_total = self._probe_scopes()
            where = self.base_url.replace("https://", "")
            if not readable:
                return {
                    "success": False,
                    "message": (
                        f"Authenticated to CrowdStrike ({where}), but the API client can read "
                        "none of the supported data. Grant read scopes (e.g. Hosts, Alerts, "
                        "Incidents, Vulnerabilities) to the API client."
                    ),
                }
            msg = f"Connected to CrowdStrike Falcon ({where})"
            if host_total is not None:
                msg += f" — {host_total} hosts visible"
            if denied:
                msg += ". Missing read scope for: " + ", ".join(
                    f"{t} ({_CATALOG[t]['scope']})" for t in denied
                )
            return {"success": True, "message": msg}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def _probe_scopes(self):
        """Cheap limit=1 read per table. Returns (readable, denied, host_total)."""
        readable, denied, host_total = [], [], None
        for name, spec in _CATALOG.items():
            try:
                params = {"limit": 1}
                if spec.get("default_filter"):
                    params["filter"] = spec["default_filter"]
                body = self._request("GET", spec["path"], params=params)
                readable.append(name)
                if name == "hosts":
                    host_total = ((body.get("meta") or {}).get("pagination") or {}).get("total")
            except CrowdStrikeAuthError:
                raise
            except CrowdStrikeError as e:
                if e.status_code in (403, 404):
                    denied.append(name)
                    continue
                raise
        return readable, denied, host_total

    # ── schema discovery ──────────────────────────────────────────────────────

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> list[Table]:
        tables = [self._build_table(name) for name in _CATALOG]
        # Best-effort: note tables the API client cannot read so the agent does
        # not chase 403s. Never fail discovery on this.
        try:
            with self.connect():
                _, denied, _ = self._probe_scopes()
            for t in discovery_items(tables, 'tables', label=lambda table: table.name):
                if t.name in denied:
                    t.description = (
                        (t.description or "")
                        + f" NOTE: this API client lacks '{_CATALOG[t.name]['scope']}' — queries will fail."
                    )
        except IndexingCancelled:
            raise
        except Exception:
            pass
        return tables

    def get_schema(self, table_name: str) -> Table:
        if table_name not in _CATALOG:
            raise ValueError(
                f"Unknown CrowdStrike table '{table_name}'. Available: {', '.join(_CATALOG)}."
            )
        return self._build_table(table_name)

    def _build_table(self, name: str) -> Table:
        spec = _CATALOG[name]
        columns = [TableColumn(name=c, dtype=d) for c, d in spec["columns"]]
        by_name = {c.name: c for c in columns}
        fks = [
            ForeignKey(
                column=by_name[col],
                references_name=ref_table,
                references_column=TableColumn(name=ref_col, dtype="str"),
            )
            for col, ref_table, ref_col in spec["fks"]
            if col in by_name
        ]
        pks = [by_name[spec["pk"]]] if spec.get("pk") in by_name else []
        return Table(name=name, description=spec["desc"], columns=columns, pks=pks, fks=fks)

    # ── querying ──────────────────────────────────────────────────────────────

    def execute_query(self, query) -> pd.DataFrame:
        """Execute a CrowdStrike query spec and return a DataFrame.

        `query` is a JSON string (or dict):
            {"table": "alerts",                               (required)
             "filter": "status:'new'+severity:>=70",          (optional FQL)
             "sort": "created_timestamp|desc",                (optional)
             "fields": ["composite_id", "severity_name"],     (optional projection)
             "params": {"facet": ["cve"]},                    (optional extra query params)
             "limit": 500}                                    (optional, default 500, max 10000)

        Escape hatch: {"path": "/some/read/endpoint/v1", "params": {...}} issues a
        single GET and returns its `resources`.
        """
        spec = self._parse_spec(query)
        limit = max(1, min(int(spec.get("limit") or DEFAULT_LIMIT), MAX_ROWS))

        with self.connect():
            if spec.get("path"):
                body = self._request("GET", spec["path"], params=spec.get("params") or None)
                records = body.get("resources") or []
                records = records[:limit] if isinstance(records, list) else [records]
            else:
                records = self._fetch_table(spec["table"], spec, limit)

        df = self._to_frame(records)
        fields = spec.get("fields")
        if fields and not df.empty:
            keep = [f for f in fields if f in df.columns]
            if keep:
                df = df[keep]
        return df

    def _fetch_table(self, table: str, spec: dict, limit: int) -> list:
        cat = _CATALOG[table]
        params = dict(cat.get("default_params") or {})
        params.update(spec.get("params") or {})
        fql = spec.get("filter") if spec.get("filter") is not None else cat.get("default_filter")
        if fql:
            params["filter"] = fql
        if spec.get("sort"):
            params["sort"] = self._normalize_sort(spec["sort"], cat["sort_sep"])

        if cat["mode"] == "ids":
            ids = self._paginate(cat, cat["path"], params, limit)
            return self._hydrate(cat, ids)
        try:
            return self._paginate(cat, cat["path"], params, limit)
        except CrowdStrikeError as e:
            # Older clouds may lack the combined devices endpoint: fall back to
            # the scroll query + entity hydration it replaced.
            if table == "hosts" and e.status_code == 404:
                fallback = {"paging": "token", "page_max": 5_000}
                ids = self._paginate(fallback, "/devices/queries/devices-scroll/v1", params, limit)
                return self._hydrate(
                    {"entities_path": "/devices/entities/devices/v2", "entities_key": "ids",
                     "entities_batch": 1_000}, ids,
                )
            raise

    def _paginate(self, cat: dict, path: str, params: dict, limit: int) -> list:
        out: list = []
        cursor = None
        offset = 0
        while len(out) < limit:
            page = dict(params)
            page["limit"] = min(cat["page_max"], limit - len(out))
            if cat["paging"] == "offset" and offset:
                page["offset"] = offset
            elif cat["paging"] == "token" and cursor:
                page["offset"] = cursor
            elif cat["paging"] == "after" and cursor:
                page["after"] = cursor
            body = self._request("GET", path, params=page)
            batch = body.get("resources") or []
            if not batch:
                break
            out.extend(batch)
            pagination = (body.get("meta") or {}).get("pagination") or {}
            if cat["paging"] == "offset":
                offset += len(batch)
                total = pagination.get("total")
                if total is not None and offset >= int(total):
                    break
            else:
                nxt = pagination.get("after") if cat["paging"] == "after" else (
                    pagination.get("next") or pagination.get("offset")
                )
                if not nxt or not isinstance(nxt, str) or nxt == cursor:
                    break
                cursor = nxt
            if len(batch) < page["limit"]:
                break
        return out[:limit]

    def _hydrate(self, cat: dict, ids: list) -> list:
        records: list = []
        size = cat["entities_batch"]
        for i in range(0, len(ids), size):
            chunk = ids[i:i + size]
            body = self._request("POST", cat["entities_path"], body={cat["entities_key"]: chunk})
            records.extend(body.get("resources") or [])
        return records

    @staticmethod
    def _normalize_sort(sort: str, sep: str) -> str:
        """Accept `field|desc`, `field.desc` or `field desc`; emit the API's syntax."""
        s = str(sort).strip()
        for cand in ("|", " "):
            if cand in s:
                field, _, direction = s.rpartition(cand)
                return f"{field.strip()}{sep}{direction.strip().lower()}"
        field, dot, direction = s.rpartition(".")
        if dot and direction.lower() in ("asc", "desc"):
            return f"{field}{sep}{direction.lower()}"
        return s

    @staticmethod
    def _to_frame(records: list) -> pd.DataFrame:
        if not records:
            return pd.DataFrame()
        if all(isinstance(r, dict) for r in records):
            return pd.json_normalize(records, sep=".")
        return pd.DataFrame({"value": records})

    def _parse_spec(self, query) -> dict:
        if isinstance(query, dict):
            spec = query
        else:
            try:
                spec = json.loads(query)
            except (TypeError, json.JSONDecodeError):
                raise ValueError(
                    "CrowdStrike query must be a JSON object like "
                    '{"table": "alerts", "filter": "status:\'new\'", "limit": 100} — got: '
                    f"{str(query)[:200]}"
                )
        if not isinstance(spec, dict) or not (spec.get("table") or spec.get("path")):
            raise ValueError('CrowdStrike query spec must include a "table" (or "path") key.')
        if spec.get("table") and spec["table"] not in _CATALOG:
            raise ValueError(
                f"Unknown CrowdStrike table '{spec['table']}'. Available: {', '.join(_CATALOG)}."
            )
        path = spec.get("path")
        if path and (not isinstance(path, str) or not path.startswith("/") or "://" in path
                     or ".." in path or path.startswith("//")):
            raise ValueError('CrowdStrike "path" must be an API path such as "/devices/queries/devices/v1".')
        return spec

    # ── prompts ───────────────────────────────────────────────────────────────

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    def system_prompt(self):
        # Table descriptions carry the value semantics (status codes, severity
        # scales); the indexer stores columns only, so restate them here.
        tables = "\n".join(f"        - `{name}`: {spec['desc']}" for name, spec in _CATALOG.items())
        return self._PROMPT + "\n        Tables:\n" + tables + "\n"

    _PROMPT = """
        ## CrowdStrike Falcon Integration
        Query Falcon via `execute_query(query)` where `query` is a JSON string:

        ```json
        {"table": "alerts",
         "filter": "status:'new'+severity:>=70",
         "sort": "created_timestamp|desc",
         "fields": ["composite_id", "severity_name", "display_name", "device.hostname", "tactic", "technique"],
         "limit": 200}
        ```

        - `table` (required): hosts, host_groups, alerts, incidents, crowdscores,
          vulnerabilities, prevention_policies, iocs.
        - `filter` (optional): an FQL (Falcon Query Language) expression, pushed
          down to the API. Syntax: `field:'value'` exact, `field:*'abc*'` wildcard,
          `field:>=70` / `field:>'2026-01-01T00:00:00Z'` ranges,
          `field:['a','b']` any-of, `!field:'x'` negation. Combine with `+` (AND)
          and `,` (OR). Nested fields use dots: `device.hostname:'web-01'`.
          String values MUST be single-quoted.
        - `sort` (optional): `field|desc` or `field|asc`.
        - `fields` (optional): columns to keep; nested fields are flattened to
          dotted column names (e.g. `device.hostname`, `cve.severity`).
        - `limit` (optional): max rows, default 500, max 10000.
        - `params` (optional): extra API query params (e.g. vulnerabilities
          `{"facet": ["cve", "host_info", "remediation"]}`).
        - `path` (escape hatch): GET any other Falcon read endpoint, e.g.
          `{"path": "/devices/combined/host-group-members/v1", "params": {"id": "<group id>"}}`.

        IMPORTANT specifics:
        - Timestamps are ISO-8601 UTC strings; convert with `pd.to_datetime(df[col], utc=True)`.
          For relative windows compute the bound in Python at run time, e.g.
          `since = (pd.Timestamp.utcnow() - pd.Timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')`
          then `"filter": "created_timestamp:>='" + since + "'"`.
        - alerts.severity is 1-100; severity_name is Informational/Low/Medium/High/Critical.
          Open alerts: `status:['new','in_progress','reopened']`.
        - incidents: state 'open'|'closed'; status 20 New, 25 Reopened, 30 In Progress,
          40 Closed; fine_score/10 = console score. host_ids join to hosts.device_id.
        - vulnerabilities REQUIRE a filter (default: open+reopen). Filter examples:
          `cve.severity:['CRITICAL','HIGH']`, `cve.is_cisa_kev:true`, `aid:'<device_id>'`.
          Wildcards are not supported in vulnerability filters.
        - hosts: `platform_name:'Windows'`, `status:'contained'`,
          `last_seen:<='<iso>'` (stale sensors), `hostname:*'web*'`.
        - Aggregate (counts by severity, top hosts, MITRE breakdown) by fetching rows
          and grouping in pandas. Join alerts → hosts on `device.device_id` = `device_id`,
          vulnerabilities → hosts on `aid` = `device_id`.

        Examples:
        ```python
        # Critical/high alerts in the last 7 days, newest first
        since = (pd.Timestamp.utcnow() - pd.Timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')
        df = client.execute_query(json.dumps({"table": "alerts", "filter": f"severity:>=70+created_timestamp:>='{since}'", "sort": "created_timestamp|desc", "limit": 1000}))
        # Contained hosts
        df = client.execute_query('{"table": "hosts", "filter": "status:\\'contained\\'"}')
        # CISA-KEV vulnerabilities with host context
        df = client.execute_query('{"table": "vulnerabilities", "filter": "status:[\\'open\\',\\'reopen\\']+cve.is_cisa_kev:true", "limit": 2000}')
        ```
        """


# Alias so dynamic naming ("crowdstrike" → "CrowdstrikeClient", used by the
# integration-test harness) and the explicit client_path resolve the same class.
CrowdstrikeClient = CrowdStrikeClient
