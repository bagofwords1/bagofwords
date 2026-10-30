"""Microsoft Configuration Manager (SCCM / MECM) — the AdminService REST API.

The administration service is an OData v4 REST API that the SMS Provider role
serves over HTTPS (https://<provider>/AdminService). It has two routes:

* ``/AdminService/wmi/<Class>`` — the 700+ SMS Provider WMI classes
  (SMS_R_System, SMS_Collection, SMS_G_System_*, …). This is the main data
  surface and what the catalog is built from. Class names are
  case-insensitive here (since 2006).
* ``/AdminService/v1.0/<Entity>`` — newer, versioned entities (Device, …).
  Class names are case-sensitive here, so the client corrects the case from
  the catalog when it can.

Design notes
------------
* **Classes are tables.** ``$metadata`` describes every class as an entity
  set, so one request builds the whole catalog. A table's name is its path
  under /AdminService (``SMS_R_System`` for the WMI route, ``v1.0/Device`` for
  the versioned one), so a query string maps one-to-one onto a URL.

* **Read-only.** Only GET is ever sent. Static WMI methods (POST) can change
  the site, and CMPivot (POST) runs scripts on clients, so neither is exposed.

* **Auth is Windows auth.** The SMS Provider accepts NTLM / Negotiate on its
  HTTP.sys listener; the account must be a Configuration Manager
  administrative user, and its security scopes filter what it sees — which is
  why the NTLM variant also offers per-user scope.

* **TLS.** Out of the box the site binds a self-signed certificate. Instead of
  turning verification off, an admin can paste that certificate (PEM) and the
  client trusts exactly it.

* **Collection reads omit lazy properties** (e.g. SMS_Collection.CollectionRules,
  SMS_Application.SDMPackageXML). WMI only returns those on a single-instance
  read (``SMS_Collection('SMS00001')``); the query guide says so.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import pandas as pd
import requests
from defusedxml import ElementTree as SafeET

from app.ai.prompt_formatters import ForeignKey, ServiceFormatter, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import discovery_items, discovery_progress

# Routes under /AdminService.
_WMI = "wmi"
_V1 = "v1.0"

# The full WMI $metadata describes 700+ classes and can take a while to build
# on a busy provider.
_METADATA_TIMEOUT = 180
_REQUEST_TIMEOUT = 120
_CONNECT_TIMEOUT = 15
# The connection test is interactive (a button in the connect form): fail fast.
_TEST_TIMEOUT = 30
# Wall-clock budget for one execute_query, all pages included. Replaced by the
# connection's query timeout (less a margin) via `apply_query_timeout`.
_DEFAULT_QUERY_BUDGET = 110
# Safety ceiling on rows gathered across nextLink pages when the caller gives
# no max_rows. Inventory classes on a large site hold millions of rows.
_ROW_CAP = 100_000

# Curated starter catalog: the classes most reporting questions need. Used
# when neither `classes` nor `discover_all` is set — 700+ cryptic class names
# would bury the useful ones. Only those present on the site are kept.
_DEFAULT_CLASSES = [
    "SMS_Site",
    "SMS_R_System",
    "SMS_R_User",
    "SMS_CombinedDeviceResources",
    "SMS_Collection",
    "SMS_FullCollectionMembership",
    "SMS_G_System_OPERATING_SYSTEM",
    "SMS_G_System_COMPUTER_SYSTEM",
    "SMS_G_System_PC_BIOS",
    "SMS_G_System_LOGICAL_DISK",
    "SMS_G_System_INSTALLED_SOFTWARE",
    "SMS_ApplicationLatest",
    "SMS_DeploymentSummary",
    "SMS_AppDeploymentAssetDetails",
    "SMS_SoftwareUpdate",
    "SMS_UpdateComplianceStatus",
    "SMS_Package",
    "SMS_Boundary",
    "SMS_BoundaryGroup",
]

# Plain-language descriptions for common classes. WMI class names are cryptic;
# these are what lets the agent pick the right class for a question.
_CLASS_DESCRIPTIONS = {
    "SMS_Site": "Configuration Manager sites in the hierarchy: site code, name, version, type and status.",
    "SMS_R_System": "Discovered devices (one row per device resource): name, domain, OS, AD site, IP/MAC addresses, client install state. Join other device classes on ResourceId.",
    "SMS_R_User": "Discovered users from Active Directory: user name, full name, domain, UPN, department.",
    "SMS_CombinedDeviceResources": "Console device view: one row per device with client activity, health, last policy request, primary user and currently logged-on user.",
    "SMS_Collection": "Device and user collections: CollectionID, name, type (2 = device, 1 = user), member count, limiting collection.",
    "SMS_FullCollectionMembership": "Which resources are members of which collections (CollectionID ↔ ResourceID). Filter by CollectionID to list a collection's members.",
    "SMS_G_System_OPERATING_SYSTEM": "Hardware inventory: operating system per device — caption, version, build, install date, last boot time.",
    "SMS_G_System_COMPUTER_SYSTEM": "Hardware inventory: manufacturer, model, memory and domain role per device.",
    "SMS_G_System_PC_BIOS": "Hardware inventory: BIOS vendor, version, serial number and release date per device.",
    "SMS_G_System_LOGICAL_DISK": "Hardware inventory: logical disks per device — drive letter, size and free space (MB).",
    "SMS_G_System_INSTALLED_SOFTWARE": "Hardware inventory: installed software per device (Programs and Features) — product name, version, publisher, install date.",
    "SMS_ApplicationLatest": "Applications (latest revision only): display name, version, publisher, deployment and install counts.",
    "SMS_Application": "Applications, every revision. Prefer SMS_ApplicationLatest unless revision history matters.",
    "SMS_DeploymentSummary": "Deployment status rollup per deployment: software, target collection, success/in-progress/error/unknown counts.",
    "SMS_AppDeploymentAssetDetails": "Application deployment status per device: application, collection, device, enforcement state.",
    "SMS_SoftwareUpdate": "Software updates in the catalog: article ID, title, severity, release date, required/installed counts, deployed or expired flags.",
    "SMS_UpdateComplianceStatus": "Per-device compliance state for each software update (CI_ID ↔ MachineID). Status 1 = not required, 2 = required (missing), 3 = installed.",
    "SMS_Package": "Classic packages: PackageID, name, version, manufacturer, source path.",
    "SMS_Boundary": "Network boundaries: type, value (IP range, subnet, AD site), assigned site.",
    "SMS_BoundaryGroup": "Boundary groups: name, member boundary count, assigned site systems.",
    "SMS_ClientHealthSummary": "Client health rollup.",
    "SMS_Advertisement": "Classic package/program deployments (advertisements).",
    "SMS_TaskSequencePackage": "Task sequences.",
    "SMS_DistributionPoint": "Distribution points and the packages assigned to them.",
    "Device": "v1.0 Device entity: one row per device with client and OS details. Navigate to related data with $expand.",
}

# Detects SQL/WQL handed to execute_query. Class names are bare identifiers,
# so a leading SELECT/WITH can only be a dialect mix-up.
_SQL_PREFIX_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
# A class or entity name, optionally route-prefixed.
_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# System properties WMI returns on every instance; noise for analysis.
_SYSTEM_PREFIXES = ("@odata", "__")


class MecmError(RuntimeError):
    """A request to the administration service failed in a way the caller should read."""


class _NotFound(MecmError):
    pass


class _BudgetExceeded(Exception):
    pass


def normalize_base_url(server_url: str) -> str:
    """Accept what an admin is likely to paste and return the AdminService root.

    ``provider.contoso.com``, ``https://provider.contoso.com``,
    ``https://provider.contoso.com/AdminService/wmi/`` and a CMG URL ending in
    ``/AdminService`` all normalize to ``https://<host>[:port]/<path>/AdminService``.
    """
    raw = (server_url or "").strip()
    if not raw:
        raise ValueError("Server URL is required (the SMS Provider host name).")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme.lower() != "https":
        raise ValueError("The administration service is HTTPS-only. Use an https:// URL.")
    if not parts.hostname:
        raise ValueError("Server URL must include a host name.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Server URL must not contain credentials, a query string or a fragment.")
    path = unquote(parts.path or "")
    if ".." in path.split("/"):
        raise ValueError("Server URL path must not contain '..'.")
    idx = path.lower().find("/adminservice")
    if idx >= 0:
        path = path[: idx + len("/adminservice")]
        # Keep the admin's prefix (e.g. a CMG /CCM_Proxy_MutualAuth/<id>) but
        # spell the service segment the documented way.
        path = path[: idx] + "/AdminService"
    else:
        path = path.rstrip("/") + "/AdminService"
    return f"https://{parts.netloc}{path}"


class MecmClient(DataSourceClient):
    """Query Microsoft Configuration Manager through the AdminService REST API."""

    def __init__(
        self,
        server_url: str,
        username: str | None = None,
        password: str | None = None,
        kerberos: bool = False,
        principal: str | None = None,
        classes: str | None = None,
        discover_all: bool = False,
        include_v1: bool = False,
        verify_ssl: bool = True,
        ca_certificate: str | None = None,
    ):
        self.base_url = normalize_base_url(server_url)
        parts = urlsplit(self.base_url)
        self._origin = (parts.scheme.lower(), parts.netloc.lower())
        self.username = (username or "").strip() or None
        self.password = password
        self.kerberos = bool(kerberos)
        self.principal = (principal or "").strip() or None
        if not self.kerberos and (not self.username or not self.password):
            raise ValueError("Windows authentication needs a domain user name (DOMAIN\\user) and password.")
        self.classes = _split_list(classes)
        self.discover_all = bool(discover_all)
        self.include_v1 = bool(include_v1)
        self.verify_ssl = bool(verify_ssl)
        self._ca_file = _write_ca_bundle(ca_certificate) if ca_certificate and ca_certificate.strip() else None
        self._local = threading.local()
        self._schemas_cache: list[Table] | None = None
        # Canonical table names (lower-cased key) learned from the catalog,
        # used to fix the case of v1.0 names (case-sensitive on the server).
        self._known: dict[str, str] = {}
        self._unindexed: list[str] = []
        self._query_budget = float(_DEFAULT_QUERY_BUDGET)

    # ── connection ──────────────────────────────────────────────────────────

    def apply_query_timeout(self, seconds: int) -> None:
        """Adopt the connection's query timeout as this client's budget, a
        little under it, so a slow read ends here with an actionable message."""
        if isinstance(seconds, (int, float)) and seconds > 0:
            self._query_budget = float(max(5.0, seconds - max(3.0, seconds * 0.1)))

    def _verify(self):
        if self._ca_file:
            return self._ca_file
        if not self.verify_ssl:
            return False
        return os.environ.get("REQUESTS_CA_BUNDLE") or True

    def _session(self) -> requests.Session:
        # requests.Session is not thread-safe, and NTLM authenticates a TCP
        # connection, so each worker thread keeps its own session.
        creds = getattr(self._local, "creds", None)
        if self.kerberos and creds is not None and getattr(creds, "lifetime", 999) < 120:
            self._local.session.close()
            del self._local.session
        session = getattr(self._local, "session", None)
        if session is not None:
            return session
        session = requests.Session()
        session.trust_env = False  # no ambient proxies / .netrc credentials
        if self.kerberos:
            try:
                import gssapi
                from requests_gssapi import REQUIRED, HTTPSPNEGOAuth
            except ImportError as exc:  # pragma: no cover - depends on the optional extra
                raise MecmError(
                    "Kerberos needs the 'kerberos' extra installed plus krb5.conf and a client "
                    "keytab (KRB5_CLIENT_KTNAME) mounted on the Bag of Words server."
                ) from exc
            keytab = os.environ.get("KRB5_CLIENT_KTNAME")
            name = gssapi.Name(self.principal, gssapi.NameType.kerberos_principal) if self.principal else None
            creds = gssapi.Credentials(usage="initiate", name=name,
                                       **({"store": {"client_keytab": keytab}} if keytab else {}))
            self._local.creds = creds
            session.auth = HTTPSPNEGOAuth(creds=creds, mutual_authentication=REQUIRED,
                                          mech=gssapi.MechType.kerberos, opportunistic_auth=True)
        else:
            from requests_ntlm import HttpNtlmAuth
            session.auth = HttpNtlmAuth(self.username, self.password)
        session.verify = self._verify()
        session.headers.update({"Accept": "application/json"})
        self._local.session = session
        return session

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _get(self, url: str, timeout: float = _REQUEST_TIMEOUT, accept: str = "application/json",
             what: str = "") -> requests.Response:
        """GET one URL under the AdminService root and classify failures."""
        parts = urlsplit(url)
        if (parts.scheme.lower(), parts.netloc.lower()) != self._origin:
            raise MecmError("Refusing to follow a link outside the configured AdminService host.")
        label = what or unquote(parts.path.rsplit("/AdminService/", 1)[-1])
        try:
            # (connect, read): an unreachable host fails fast; a slow class read may take longer.
            resp = self._session().get(url, timeout=(min(_CONNECT_TIMEOUT, timeout), timeout), headers={"Accept": accept},
                                       allow_redirects=False)
        except requests.exceptions.SSLError as e:
            raise MecmError(
                "TLS certificate verification failed for the SMS Provider. By default the site uses "
                "a self-signed certificate: paste it (PEM) into 'SMS Provider certificate', or bind a "
                f"certificate your CA issued. Details: {_short(e)}"
            ) from e
        except requests.exceptions.Timeout as e:
            raise MecmError(f"The administration service did not answer in time ({label}).") from e
        except requests.exceptions.ConnectionError as e:
            raise MecmError(
                "Could not reach the administration service. Check the SMS Provider host name and "
                f"that HTTPS (TCP 443) is open from this server. Details: {_short(e)}"
            ) from e
        status = resp.status_code
        if 300 <= status < 400:
            raise MecmError(
                f"The server redirected the request (HTTP {status}). Use the SMS Provider's own "
                "host name as the Server URL."
            )
        if status == 401:
            raise MecmError(
                "Authentication failed (HTTP 401). Check the user name (DOMAIN\\user or user@domain) "
                "and password, and that the account can sign in to the SMS Provider over NTLM/Kerberos."
            )
        if status == 403:
            raise MecmError(
                "Access denied (HTTP 403). The account authenticated but is not a Configuration "
                "Manager administrative user, or its security role has no read permission on "
                f"'{label}'. Add it under Administration > Security > Administrative Users "
                "(the Read-only Analyst role is enough)."
            )
        if status == 404:
            raise _NotFound(
                f"Not found (HTTP 404): '{label}'. Check the class name; WMI classes are addressed "
                "as SMS_<Name> (e.g. SMS_R_System) and v1.0 entities as v1.0/<Name> with exact case."
            )
        if status >= 400:
            raise MecmError(f"The administration service returned HTTP {status} for '{label}': {_error_detail(resp)}")
        return resp

    # ── catalog ─────────────────────────────────────────────────────────────

    def _fetch_metadata(self, route: str) -> dict[str, dict]:
        resp = self._get(self._url(f"{route}/$metadata"), timeout=_METADATA_TIMEOUT,
                         accept="application/xml", what=f"{route}/$metadata")
        return parse_metadata(resp.text or "")

    def _catalog(self) -> dict[str, dict]:
        """Every entity set the site exposes, keyed by table name."""
        self._unindexed = []
        catalog: dict[str, dict] = {}
        for name, info in self._fetch_metadata(_WMI).items():
            catalog[name] = {**info, "route": _WMI, "path": f"{_WMI}/{name}"}
        if self._wants_v1():
            try:
                for name, info in self._fetch_metadata(_V1).items():
                    catalog[f"{_V1}/{name}"] = {**info, "route": _V1, "path": f"{_V1}/{name}"}
            except MecmError:
                # Older sites (pre-1906) have no v1.0 route; the WMI catalog
                # still stands. Keep any v1.0 tables indexed earlier.
                self._unindexed.append(_V1)
        return catalog

    def _wants_v1(self) -> bool:
        if self.discover_all or self.include_v1:
            return True
        return any(c.lower().startswith(_V1 + "/") for c in (self.classes or []))

    def _select(self, catalog: dict[str, dict]) -> list[str]:
        lower = {name.lower(): name for name in catalog}
        if self.classes:
            picked = []
            for item in self.classes:
                key = _strip_route(item).lower()
                if key in lower and lower[key] not in picked:
                    picked.append(lower[key])
            return picked
        if self.discover_all:
            return sorted(catalog, key=lambda n: (n.startswith(_V1 + "/"), n.lower()))
        picked = [lower[c.lower()] for c in _DEFAULT_CLASSES if c.lower() in lower]
        if self.include_v1:
            picked += sorted(n for n in catalog if n.startswith(_V1 + "/"))
        return picked or sorted(catalog)

    def _build_table(self, name: str, info: dict, catalog: dict[str, dict]) -> Table:
        columns = [TableColumn(name=p["name"], dtype=p["type"], description=p.get("description"))
                   for p in info["properties"]]
        by_lower = {c.name.lower(): c for c in columns}
        pks = [by_lower[k.lower()] for k in info["keys"] if k.lower() in by_lower]
        fks: list[ForeignKey] = []
        for col_name, target, target_col in (("resourceid", "SMS_R_System", "ResourceId"),
                                             ("machineid", "SMS_R_System", "ResourceId"),
                                             ("collectionid", "SMS_Collection", "CollectionID")):
            col = by_lower.get(col_name)
            if col is None or name == target or target not in catalog:
                continue
            ref = next((p for p in catalog[target]["properties"] if p["name"].lower() == target_col.lower()), None)
            if ref is None:
                continue
            fks.append(ForeignKey(column=col, references_name=target,
                                  references_column=TableColumn(name=ref["name"], dtype=ref["type"])))
        short = name.split("/", 1)[-1]
        description = _CLASS_DESCRIPTIONS.get(short) or info.get("description")
        return Table(
            name=name,
            description=description,
            columns=columns,
            pks=pks,
            fks=fks,
            # The description also rides in metadata_json: that is what the
            # catalog persists and the planner's schema renders for this type.
            metadata_json={"mecm": {"route": info["route"], "path": info["path"],
                                    "entity_type": info.get("entity_type"),
                                    "description": description}},
        )

    @discovery_progress
    def get_schemas(self, force_refresh: bool = False, progress_callback=None) -> list[Table]:
        if self._schemas_cache is not None and not force_refresh:
            return self._schemas_cache
        catalog = self._catalog()
        self._known = {name.lower(): name for name in catalog}
        tables = [self._build_table(name, catalog[name], catalog)
                  for name in discovery_items(self._select(catalog), "classes", label=lambda n: n)]
        self._schemas_cache = tables
        return tables

    def unindexed_table_prefixes(self) -> list[str]:
        return list(self._unindexed)

    def attach_table_metadata(self, tables: list[dict]) -> None:
        """Learn canonical table names from the indexed catalog (no request)."""
        for t in tables or []:
            name = t.get("name") if isinstance(t, dict) else None
            if name:
                self._known.setdefault(name.lower(), name)

    def get_schema(self, table_name: str) -> Table:
        for t in self.get_schemas():
            if t.name.lower() == (table_name or "").lower():
                return t
        raise ValueError(f"Class '{table_name}' is not in this connection's catalog.")

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    # ── DataSourceClient contract ───────────────────────────────────────────

    def test_connection(self) -> dict:
        """Prove auth, TLS and read access with the smallest real read: the site list."""
        try:
            resp = self._get(self._url(f"{_WMI}/SMS_Site"), timeout=_TEST_TIMEOUT, what="SMS_Site")
            payload = resp.json()
        except MecmError as e:
            return {"success": False, "message": str(e)}
        except ValueError:
            return {"success": False, "message": (
                "The server answered, but not with OData JSON. Check that the URL points at the "
                "SMS Provider (https://<provider>/AdminService).")}
        except Exception as e:  # noqa: BLE001 - a probe reports, never raises
            return {"success": False, "message": f"Connection failed: {_short(e)}"}
        sites = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(sites, list):
            return {"success": False, "message": "Connected, but the response was not an OData collection."}
        if not sites:
            return {"success": True, "message": (
                "Connected to the administration service, but this account sees no sites. "
                "Check its security scopes.")}
        site = sites[0]
        label = ", ".join(
            f"{s.get('SiteCode')} ({s.get('SiteName')})" if s.get("SiteName") else str(s.get("SiteCode"))
            for s in sites[:5]
        )
        version = site.get("Version")
        return {
            "success": True,
            "message": f"Connected to Configuration Manager site {label}" + (f", version {version}." if version else "."),
        }

    def execute_query(self, query: str = "", table_name: str | None = None,
                      max_rows: int | None = None) -> pd.DataFrame:
        """Run an OData read and return a DataFrame.

        ``query`` is a class name plus optional OData options, e.g.
        ``SMS_R_System?$filter=Client eq 1&$select=Name,ResourceId&$top=50``,
        a single instance (``SMS_Collection('SMS00001')``) or a v1.0 entity
        (``v1.0/Device``). Pages (``@odata.nextLink``) are followed until
        ``max_rows`` or the safety cap.
        """
        target = (query or table_name or "").strip()
        if not target:
            raise ValueError("A Configuration Manager class name (optionally with OData query options) is required.")
        if _SQL_PREFIX_RE.match(target):
            raise ValueError(
                "Configuration Manager's AdminService is queried with OData, not SQL or WQL. Pass a "
                "class name with OData options instead of a SELECT statement, e.g. "
                "\"SMS_R_System?$filter=Client eq 1&$select=Name,ResourceId,OperatingSystemNameandVersion\" or "
                "\"SMS_FullCollectionMembership?$filter=CollectionID eq 'SMS00001'&$select=Name,ResourceID\"."
            )
        path, query_string = self._resolve(target)
        cap = int(max_rows) if max_rows else _ROW_CAP
        injected_top = bool(max_rows) and "$top" not in unquote(query_string)
        qs = query_string + (("&" if query_string else "") + f"$top={cap}" if injected_top else "")

        deadline = time.monotonic() + self._query_budget
        try:
            payload, url = self._get_json(path, qs, deadline)
        except MecmError as e:
            # Not every route implements $top; ours was only an optimisation.
            if injected_top and not isinstance(e, _NotFound) and re.search(r"HTTP (400|501)", str(e)):
                payload, url = self._get_json(path, query_string, deadline)
            else:
                raise
        rows, truncated = self._collect(payload, cap, deadline, url)
        df = _frame(rows, _selected_columns(query_string))
        if truncated and not max_rows:
            df.attrs["bow_notice"] = (
                f"{path}: stopped at {len(df)} rows ({truncated}). Narrow the read with $filter / "
                "$select, or aggregate per class instead of reading every row."
            )
        return df.head(cap) if len(df) > cap else df

    def _resolve(self, target: str) -> tuple[str, str]:
        """Turn a query target into (path under /AdminService, raw query string)."""
        if target.lower().startswith(("http://", "https://")):
            if not target.lower().startswith(self.base_url.lower() + "/"):
                raise ValueError("Absolute URLs must point at this connection's AdminService.")
            target = target[len(self.base_url) + 1:]
        path, _, query_string = target.partition("?")
        path = path.strip().strip("/")
        if path.lower().startswith("adminservice/"):
            path = path.split("/", 1)[1]
        segments = [s for s in path.split("/") if s]
        if not segments or any(s in (".", "..") for s in segments):
            raise ValueError(f"Invalid class path '{path}'.")
        if segments[0].lower() not in (_WMI, _V1):
            segments.insert(0, _WMI)
        if len(segments) < 2:
            raise ValueError(f"'{path}' names a route, not a class. Add the class, e.g. wmi/SMS_R_System.")
        route = segments[0].lower()
        segments[0] = route
        head = segments[1]
        name, paren, rest = head.partition("(")
        if not _CLASS_RE.match(name):
            raise ValueError(f"'{name}' is not a Configuration Manager class name.")
        key = name.lower() if route == _WMI else f"{_V1}/{name}".lower()
        canonical = self._known.get(key)
        if canonical:
            segments[1] = canonical.split("/", 1)[-1] + paren + rest
        if any(s.startswith("$") for s in segments[1:]):
            raise ValueError("Only class reads are supported (no $metadata, $batch or $count segments).")
        return "/".join(segments), query_string.strip().lstrip("&")

    def _get_json(self, path: str, query_string: str, deadline: float) -> tuple[dict, str]:
        url = self._url(path) + (f"?{query_string}" if query_string else "")
        resp = self._get_within(url, deadline)
        try:
            return resp.json(), url
        except ValueError as e:
            raise MecmError(f"The administration service returned a non-JSON response for '{path}'.") from e

    def _get_within(self, url: str, deadline: float) -> requests.Response:
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise _BudgetExceeded()
        return self._get(url, timeout=min(_REQUEST_TIMEOUT, remaining))

    def _anchor_next_link(self, next_link: str, current: str) -> str:
        """Re-anchor a server-built nextLink onto the configured AdminService root.

        The provider builds the link from its own view of the request (an FQDN
        where the admin typed a short name, a load-balancer host, a relative
        link). Only the part after /AdminService/ is taken from it, so paging
        survives host aliasing and a request can never leave the configured host.
        """
        parts = urlsplit(urljoin(current, next_link))
        idx = parts.path.lower().find("/adminservice/")
        if idx < 0:
            raise MecmError("The server returned a paging link outside the AdminService; stopping.")
        rest = parts.path[idx + len("/adminservice/"):]
        if any(seg in (".", "..") for seg in unquote(rest).split("/")):
            raise MecmError("The server returned a paging link with '..' segments; stopping.")
        return self._url(rest) + (f"?{parts.query}" if parts.query else "")

    def _collect(self, payload: dict, cap: int, deadline: float, current: str) -> tuple[list[dict], str]:
        rows: list[dict] = []
        while True:
            page = payload.get("value") if isinstance(payload, dict) else None
            if page is None:
                # A single-instance read returns the entity itself.
                return ([payload] if isinstance(payload, dict) else []), ""
            rows.extend(page)
            next_link = payload.get("@odata.nextLink")
            if len(rows) >= cap:
                return rows[:cap], ("row cap reached" if next_link or len(rows) > cap else "")
            if not next_link:
                return rows, ""
            current = self._anchor_next_link(next_link, current)
            try:
                payload = self._get_within(current, deadline).json()
            except _BudgetExceeded:
                return rows, "time budget reached while paging"

    @property
    def description(self) -> str:
        # The description is the only channel that carries per-connector query
        # guidance into the code-generation prompt, so the guide rides along.
        text = f"Microsoft Configuration Manager (SCCM / MECM) AdminService at {self.base_url}"
        return text + "\n\n" + self.system_prompt()

    def system_prompt(self) -> str:
        return """
## Configuration Manager (SCCM / MECM) Query Guide

Query Configuration Manager through its AdminService OData v4 REST API. Each
schema table is an SMS Provider **class** (SMS_R_System, SMS_Collection,
SMS_G_System_* …); v1.0 entities are named `v1.0/<Entity>`.

**This is NOT a SQL or WQL data source.** `execute_query` takes ONE string: a
class name plus optional OData query options. `SELECT ... FROM ...` is refused.

```
SMS_R_System?$select=Name,ResourceId,OperatingSystemNameandVersion,Client&$filter=Client eq 1
SMS_FullCollectionMembership?$filter=CollectionID eq 'SMS00001'&$select=CollectionID,ResourceID,Name
SMS_G_System_OPERATING_SYSTEM?$select=ResourceID,Caption,Version,BuildNumber,LastBootUpTime
SMS_Collection('SMS00001')
v1.0/Device?$top=20
```

**Always `$select` the columns you need**, and `$filter` server-side when the
question names a collection, a device or a value. Inventory classes are large.

### Query options
- `$filter`: `eq ne gt ge lt le and or not`, string literals in single quotes
  (`Name eq 'PC001'`), numbers bare (`Client eq 1`), dates as
  `2026-01-01T00:00:00Z`. String functions are written with `eq true`:
  `startswith(Name,'All') eq true`, `contains(Name,'LON') eq true`.
- `$select`, `$top`, `$skip`, `$orderby` (`$orderby=Name desc`).
- No `$apply`/aggregation: read the rows (with `$select`) and aggregate in pandas.
- If a filter function is rejected (HTTP 400/501), read a bounded set of rows
  and filter in pandas instead of retrying the same function.

### Joining classes
- Device classes join on the resource id: `SMS_R_System.ResourceId` =
  `SMS_G_System_*.ResourceID` = `SMS_FullCollectionMembership.ResourceID` =
  `SMS_CombinedDeviceResources.ResourceID`; software update compliance uses
  `SMS_UpdateComplianceStatus.MachineID` (= ResourceId) and `CI_ID`
  (= `SMS_SoftwareUpdate.CI_ID`).
- To scope to a collection, read its members from SMS_FullCollectionMembership
  (`$filter=CollectionID eq '<id>'`) and join in pandas.
- Built-in collections: SMS00001 = All Systems, SMS00002 = All Users,
  SMS00004 = All Users and User Groups, SMSDM003 = All Desktop and Server Clients.

### Values and gotchas
- Columns that hold lists (IPAddresses, MACAddresses, SystemOUName …) come back
  as one comma-separated string.
- Dates are ISO-8601 strings; convert with `pd.to_datetime(df[col], utc=True)`.
- Codes are numeric: SMS_R_System.Client 1 = client installed; SMS_Collection
  CollectionType 2 = device, 1 = user; SMS_UpdateComplianceStatus.Status
  1 = not required, 2 = required (missing), 3 = installed.
- Lazy properties (e.g. SMS_Collection.CollectionRules, SMS_Application
  SDMPackageXML) are only returned on a single-instance read like
  `SMS_Collection('SMS00001')`, never in a collection query.
- Results are limited by the account's Configuration Manager security scopes.
"""


# ── helpers ────────────────────────────────────────────────────────────────


def parse_metadata(xml_text: str) -> dict[str, dict]:
    """Parse an OData v4 CSDL document into ``{entity_set: info}``.

    ``info`` has ``keys``, ``properties`` (name/type/description), ``description``
    and ``entity_type``. Base types are followed so inherited properties and keys
    are kept. Namespaces are matched by local name, so any edm/edmx version works.
    """
    if not xml_text or not xml_text.strip():
        return {}
    # $metadata comes from a remote server: refuse DTDs/entities (billion laughs, XXE).
    root = SafeET.fromstring(xml_text)

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def description_of(node) -> str | None:
        for child in node:
            if local(child.tag) == "Annotation" and (child.get("Term") or "").endswith(".Description"):
                return child.get("String")
        return None

    types: dict = {}
    for schema in (n for n in root.iter() if local(n.tag) == "Schema"):
        ns = schema.get("Namespace") or ""
        alias = schema.get("Alias")
        for et in schema:
            if local(et.tag) != "EntityType" or not et.get("Name"):
                continue
            types[et.get("Name")] = et
            types[f"{ns}.{et.get('Name')}"] = et
            if alias:
                types[f"{alias}.{et.get('Name')}"] = et

    def resolve(type_name: str, seen=None) -> tuple[list[str], list[dict]]:
        seen = seen or set()
        et = types.get(type_name)
        if et is None or type_name in seen:
            return [], []
        seen.add(type_name)
        keys, props = ([], [])
        if et.get("BaseType"):
            keys, props = resolve(et.get("BaseType"), seen)
        for child in et:
            tag = local(child.tag)
            if tag == "Key":
                keys = [ref.get("Name") for ref in child if local(ref.tag) == "PropertyRef" and ref.get("Name")]
            elif tag == "Property" and child.get("Name"):
                props = [p for p in props if p["name"] != child.get("Name")]
                props.append({"name": child.get("Name"), "type": child.get("Type") or "Edm.String",
                              "description": description_of(child)})
        return keys, props

    out: dict[str, dict] = {}
    sets = [n for n in root.iter() if local(n.tag) == "EntitySet" and n.get("Name")]
    pairs = [(s.get("Name"), s.get("EntityType") or "") for s in sets]
    if not pairs:
        # No container: fall back to the entity types themselves.
        pairs = [(et.get("Name"), et.get("Name")) for name, et in types.items() if "." not in name]
    for set_name, type_name in pairs:
        keys, props = resolve(type_name)
        if not props:
            keys, props = resolve(type_name.rsplit(".", 1)[-1])
        et = types.get(type_name)
        if et is None:
            et = types.get(type_name.rsplit(".", 1)[-1])
        out[set_name] = {
            "keys": keys,
            "properties": props,
            "description": description_of(et) if et is not None else None,
            "entity_type": type_name,
        }
    return out


def _split_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [v.strip() for v in re.split(r"[,\n;]", value) if v.strip()]
    return items or None


def _strip_route(name: str) -> str:
    n = name.strip().strip("/")
    if n.lower().startswith("adminservice/"):
        n = n.split("/", 1)[1]
    if n.lower().startswith(_WMI + "/"):
        return n.split("/", 1)[1]
    if n.lower().startswith(_V1 + "/"):
        return f"{_V1}/{n.split('/', 1)[1]}"
    return n


def _write_ca_bundle(pem: str) -> str:
    """Write a pasted certificate to a stable temp file and return its path."""
    text = pem.strip()
    if "-----BEGIN CERTIFICATE-----" not in text:
        raise ValueError("SMS Provider certificate must be PEM text starting with -----BEGIN CERTIFICATE-----.")
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    path = os.path.join(tempfile.gettempdir(), f"bow-mecm-ca-{digest}.pem")
    if not os.path.exists(path):
        fd, tmp = tempfile.mkstemp(prefix="bow-mecm-ca-", suffix=".pem")
        with os.fdopen(fd, "w") as f:
            f.write(text + "\n")
        os.replace(tmp, path)
    return path


def _selected_columns(query_string: str) -> list[str]:
    for k, v in parse_qsl(query_string, keep_blank_values=True):
        if k.strip() == "$select":
            return [c.strip() for c in v.split(",") if c.strip()]
    return []


def _clean(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if k.startswith(_SYSTEM_PREFIXES):
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v) if all(not isinstance(x, (dict, list)) for x in v) else json.dumps(v)
        elif isinstance(v, dict):
            v = json.dumps(v)
        out[k] = v
    return out


def _frame(rows: list[dict], selected: list[str]) -> pd.DataFrame:
    cleaned = [_clean(r) for r in rows if isinstance(r, dict)]
    if not cleaned:
        return pd.DataFrame(columns=selected)
    return pd.DataFrame(cleaned)


def _error_detail(resp: requests.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        message = err.get("message")
        if isinstance(message, dict):
            message = message.get("value")
        inner = (err.get("innererror") or {}).get("message") if isinstance(err.get("innererror"), dict) else None
        if message:
            return str(message).strip() + (f" ({inner})" if inner and inner != message else "")
    except (ValueError, AttributeError):
        pass
    text = " ".join((resp.text or "").split())
    return text[:500] or (resp.reason or "no details returned")


def _short(e: Exception) -> str:
    text = str(e)
    return text if len(text) <= 300 else text[:300] + "…"
