"""
Qlik Sense Enterprise on Windows (QSEoW) client — the on-prem sibling of
``qlik_sense_client`` (Qlik Cloud).

The products share Engine discovery and query behavior, with separate transports:

  =============  ==========================  ================================
                 Qlik Cloud                  Qlik Sense Enterprise on Windows
  =============  ==========================  ================================
  Discovery      REST /api/v1/items          QRS REST on :4242
  Auth           Bearer token                mutual TLS (client certificate)
  Grouping       Space                       Stream
  Engine         wss://tenant/app/{id}       wss://host:4747/app/{id}
  Identity       the token's own user        X-Qlik-User impersonation header
  =============  ==========================  ================================

Discovery therefore runs against the Qlik Repository Service (QRS) and every
model detail comes from the Engine, since QSEoW has no equivalent of Cloud's
``/apps/{id}/data/metadata`` snapshot endpoint.

Two QRS quirks are load-bearing and easy to get wrong:

* ``xrfkey`` — a 16-character anti-CSRF nonce that must appear BOTH in the
  query string and in the ``X-Qlik-Xrfkey`` header, with identical values.
  Omitting either gives HTTP 400 with a body that does not mention xrfkey.
* ``X-Qlik-User`` — the certificate authenticates the *service*, not a person.
  This header names the account Qlik should evaluate the request as, so it also
  decides which apps are visible and which Section Access rules apply.

What gets extracted (the Power BI connector is the bar):
  - streams and apps, including publish state and last reload time
  - the data model — tables, fields, associative keys — via GetTablesAndKeys
  - master measures WITH their Qlik expressions, master dimensions, variables,
    master visualizations
  - every shared sheet and the objects on it: each chart's dimensions and
    measures (inline expressions and master-item references), filter panes,
    calculation conditions and alternate states
  - lineage (the LOAD/SELECT sources behind the app) and load-script size
"""

from __future__ import annotations
from app.data_sources.clients.progress import discovery_progress, discovery_items, IndexingCancelled

import asyncio
import logging
import os
import re
import secrets
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from app.core.egress import pinned_session

from app.ai.prompt_formatters import ServiceFormatter, Table
from app.data_sources.clients._qix_common import (
    QixSession,
    build_ssl_context,
)
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients._qix_catalog import (
    QlikEngineCatalog, _mask_secrets as _mask_secrets, _compact, _tag_names, _custom_properties,
    _owner_name,
)


logger = logging.getLogger(__name__)


_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# The default QRS service identity. It exists on every QSEoW site, always has
# repository access, and needs no lookup — which is what makes environment
# discovery work before anything about the customer's directory is known.
_DEFAULT_USER_DIRECTORY = "INTERNAL"
_DEFAULT_USER_ID = "sa_repository"

_NAMESPACE = "qlik_sense_onprem"


class QlikSenseOnPremClient(QlikEngineCatalog, DataSourceClient):
    """Qlik Sense Enterprise on Windows — QRS discovery + QIX query."""
    _metadata_namespace = _NAMESPACE

    def __init__(
        self,
        server_url: str,
        client_cert: Optional[str] = None,
        client_key: Optional[str] = None,
        client_key_password: Optional[str] = None,
        root_ca: Optional[str] = None,
        user_directory: Optional[str] = None,
        user_id: Optional[str] = None,
        qrs_port: int = 4242,
        engine_port: int = 4747,
        verify_ssl: bool = True,
        stream_filter: Optional[str] = None,
        published_only: bool = True,
        impersonate_app_owner: bool = True,
        include_master_items: bool = True,
        include_lineage: bool = True,
        include_sheet_objects: bool = True,
        max_apps: Optional[int] = None,
        max_concurrency: int = 4,
        timeout_sec: int = 60,
        app_filter: Optional[str] = None,
    ):
        self.server_url = (server_url or "").strip().rstrip("/")
        self.host = self._parse_host(self.server_url)
        self.qrs_port = int(qrs_port or 4242)
        self.engine_port = int(engine_port or 4747)
        self.verify_ssl = bool(verify_ssl)
        self.published_only = bool(published_only)
        self.impersonate_app_owner = bool(impersonate_app_owner)
        self.include_master_items = bool(include_master_items)
        self.include_lineage = bool(include_lineage)
        self.include_sheet_objects = bool(include_sheet_objects)
        self.timeout_sec = int(timeout_sec or 60)
        self.max_apps = int(max_apps) if max_apps else None
        # Every app opened is an app loaded into the Engine's memory, so this
        # stays deliberately lower than the Cloud client's default of 10.
        self.max_concurrency = max(1, int(max_concurrency or 4))

        # Private + read-only properties: when a connection's identity mapping
        # pins the member's Qlik account, generated code (barred from
        # ``_``-prefixed attributes) must not be able to reassign it.
        self._user_directory = (user_directory or "").strip() or _DEFAULT_USER_DIRECTORY
        self._user_id = (user_id or "").strip() or None

        self._client_cert_pem = client_cert or None
        self._client_key_pem = client_key or None
        self._client_key_password = client_key_password or None
        self._root_ca_pem = root_ca or None

        self._stream_filter: Optional[set] = None
        if stream_filter:
            tokens = {t.strip() for t in str(stream_filter).split(",") if t.strip()}
            self._stream_filter = tokens or None
        app_tokens = {t.strip() for t in (app_filter or "").split(",") if t.strip()}
        self._app_filter = {t.casefold() for t in app_tokens} or None

        # A fresh nonce per client instance. Any 16 alphanumeric characters are
        # valid; what matters is that the query-string and header copies match.
        self._xrfkey = secrets.token_hex(8)

        self._http: Optional[requests.Session] = None
        # `Stream/App` keys of the apps the last get_schemas could not read.
        self._unread_app_keys: List[str] = []
        self._stream_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._query_app_cache: Optional[Tuple[float, List[Dict[str, Any]]]] = None
        self._temp_dir: Optional[str] = None
        self._cert_path: Optional[str] = None
        self._key_path: Optional[str] = None
        self._ca_path: Optional[str] = None

    @property
    def user_directory(self) -> str:
        return self._user_directory

    @property
    def user_id(self) -> Optional[str]:
        return self._user_id

    @staticmethod
    def _parse_host(server_url: str) -> str:
        """Reduce whatever the admin pasted to a bare hostname.

        QRS and the Engine listen on their own fixed ports, so any port in the
        URL is the QMC/hub port and must not leak into either call.
        """
        if not server_url:
            return ""
        candidate = server_url if "//" in server_url else f"https://{server_url}"
        return (urlparse(candidate).hostname or "").strip()

    # ------------------------------------------------------------------
    # Certificate material
    # ------------------------------------------------------------------

    def _write_pem(self, name: str, content: str) -> str:
        """Drop one PEM blob into a private temp file and return its path."""
        if self._temp_dir is None:
            self._temp_dir = tempfile.mkdtemp(prefix="qlik_onprem_")
            os.chmod(self._temp_dir, 0o700)
        path = os.path.join(self._temp_dir, name)
        # 0600 from the moment it exists — never world-readable, not even briefly.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(content if content.endswith("\n") else content + "\n")
        return path

    def _decrypt_key(self, pem: str) -> str:
        """Return an unencrypted PKCS#8 copy of a password-protected key.

        `requests` has no way to pass a passphrase for the client key, so the
        passphrase is spent once here and the decrypted key lives only in the
        0600 temp file that is deleted in `close()`.
        """
        from cryptography.hazmat.primitives import serialization

        key = serialization.load_pem_private_key(
            pem.encode("utf-8"),
            password=self._client_key_password.encode("utf-8"),
        )
        return key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

    def _ensure_cert_files(self) -> None:
        if self._cert_path and self._key_path:
            return
        if not self._client_cert_pem or not self._client_key_pem:
            raise RuntimeError(
                "Qlik Sense on-prem requires a client certificate. Export one from the QMC "
                "(Certificates > export for the BOW machine, format 'Platform independent') "
                "and paste client.pem into Client Certificate and client_key.pem into Client Key."
            )
        key_pem = self._client_key_pem
        if self._client_key_password:
            try:
                key_pem = self._decrypt_key(key_pem)
            except IndexingCancelled:
                raise
            except Exception as e:
                raise RuntimeError(f"Could not decrypt the Qlik client key with the given password: {e}")

        self._cert_path = self._write_pem("client.pem", self._client_cert_pem)
        self._key_path = self._write_pem("client_key.pem", key_pem)
        if self._root_ca_pem:
            self._ca_path = self._write_pem("root.pem", self._root_ca_pem)

    def _verify_arg(self):
        """What to hand `requests` as `verify=`.

        `verify_ssl` decides whether to verify at all — turning it off must work
        even with a root.pem saved. When on, a pasted root.pem is the trust
        store: QSEoW signs its service certificates with its own root, so
        verifying against that root is the only way to get real TLS
        verification on a default install.
        """
        if not self.verify_ssl:
            return False
        return self._ca_path or True

    def close(self) -> None:
        """Drop the HTTP session and shred the materialized certificate files."""
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = None
        if self._temp_dir and os.path.isdir(self._temp_dir):
            import shutil

            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir = None
        self._cert_path = self._key_path = self._ca_path = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # QRS (REST over mutual TLS, port 4242)
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if not self.host:
            raise RuntimeError("server_url is required (e.g. https://qlik.corp.example.com)")
        self._ensure_cert_files()
        if self._http is None:
            self._http = pinned_session()

    def _acting_user(self, app: Optional[Dict[str, Any]] = None) -> str:
        """The value for X-Qlik-User on this request.

        An explicitly configured identity always wins — that is the account the
        admin (or the signed-in user, on a user-scoped credential) chose to be
        evaluated as, and quietly swapping it for someone else would sidestep
        exactly the Section Access rules it exists to apply.
        """
        if self.user_id:
            return f"UserDirectory={self.user_directory}; UserId={self.user_id}"
        if app and self.impersonate_app_owner:
            owner = app.get("owner") or {}
            if owner.get("userId"):
                directory = owner.get("userDirectory") or _DEFAULT_USER_DIRECTORY
                return f"UserDirectory={directory}; UserId={owner['userId']}"
        return f"UserDirectory={_DEFAULT_USER_DIRECTORY}; UserId={_DEFAULT_USER_ID}"

    def _qrs_get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        user: Optional[str] = None,
    ) -> Any:
        """GET a QRS resource. Returns parsed JSON (list or dict)."""
        self.connect()
        url = f"https://{self.host}:{self.qrs_port}/qrs/{path.lstrip('/')}"
        try:
            resp = self._http.get(
                url,
                params={"xrfkey": self._xrfkey, **(params or {})},
                headers={
                    "X-Qlik-Xrfkey": self._xrfkey,
                    "X-Qlik-User": user or self._acting_user(),
                    "Accept": "application/json",
                },
                cert=(self._cert_path, self._key_path),
                verify=self._verify_arg(),
                timeout=self.timeout_sec,
            )
        except requests.exceptions.SSLError as e:
            raise RuntimeError(
                f"TLS failed talking to QRS at {url}: {e}. A default QSEoW install signs its "
                "service certificates with its own root — paste root.pem into Root CA "
                "Certificate and use the hostname the certificate was issued for as the "
                "Server URL, or turn off Verify SSL."
            )
        if resp.status_code >= 300:
            raise RuntimeError(
                f"Qlik QRS call failed: GET {url} HTTP {resp.status_code} {resp.text[:500]}"
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise RuntimeError(f"Invalid JSON from Qlik QRS ({url}): {e}")

    def test_connection(self) -> Dict[str, Any]:
        """Health probe: QRS reachable, certificate accepted, apps listable.

        Kept deliberately light — the status sweep runs this every few
        minutes. It reads ``about``, a stream count and the *condensed* app
        list; the full app and stream objects (owners, tags, custom
        properties for every app on the site) are only needed for indexing.
        """
        try:
            self.connect()
        except Exception as e:
            return {"success": False, "message": str(e)}

        try:
            about = self._qrs_get("about") or {}
        except Exception as e:
            return {"success": False, "message": f"Could not reach the Qlik Repository Service: {e}"}

        version = (about or {}).get("buildVersion") or "unknown"

        try:
            count = self._qrs_get("stream/count") or {}
            stream_count = int((count or {}).get("value") or 0)
        except Exception as e:
            return {
                "success": False,
                "connectivity": True,
                "message": f"Authenticated against QRS {version} but could not list streams: {e}",
            }

        try:
            apps = self._apps_from_rows(self._qrs_get("app") or [])
        except Exception as e:
            return {
                "success": False,
                "connectivity": True,
                "message": f"Authenticated against QRS {version} but could not list apps: {e}",
            }

        acting = self._acting_user()
        if apps:
            msg = (
                f"Connected to Qlik Sense Enterprise {version} as '{acting}'. "
                f"{stream_count} stream(s), {len(apps)} app(s) visible."
            )
        else:
            msg = (
                f"Connected to Qlik Sense Enterprise {version} as '{acting}', but no apps are "
                "visible. Check the app filter, stream filter, and that this identity has read access to "
                "the streams you expect."
            )
        return {
            "success": True,
            "message": msg,
            "version": version,
            "user": acting,
            "stream_count": stream_count,
            "app_count": len(apps),
        }

    # ------------------------------------------------------------------
    # Enumeration
    # ------------------------------------------------------------------

    def list_streams(self) -> List[Dict[str, Any]]:
        rows = self._qrs_get("stream/full") or []
        return [
            _compact({
                "id": s.get("id"),
                "name": s.get("name"),
                "created": s.get("createdDate"),
                "modified": s.get("modifiedDate"),
                "owner": _owner_name(s.get("owner")),
                "tags": _tag_names(s.get("tags")),
                "customProperties": _custom_properties(s.get("customProperties")),
            })
            for s in rows
            if isinstance(s, dict)
        ]

    def _streams_by_id(self) -> Dict[str, Dict[str, Any]]:
        """Stream rows keyed by id, fetched once per crawl.

        A stream carries its own tags and custom properties — on a governed site
        that is where "Certified", "Finance", "PII" live — and they describe
        every app published into it, so they are folded into each app's metadata.
        """
        if self._stream_cache is None:
            try:
                self._stream_cache = {s["id"]: s for s in self.list_streams() if s.get("id")}
            except Exception as e:
                logger.debug("Could not read streams: %s", e)
                self._stream_cache = {}
        return self._stream_cache

    def list_apps(self) -> List[Dict[str, Any]]:
        """Enumerate apps via /qrs/app/full, honouring stream and app filters.

        The app dicts use the same key names as the Cloud client's (`space_id` /
        `space_name` carry the stream) so the shared helpers in `_qix_common`
        and the `Stream/App/Table` naming both fall out without a translation
        layer.
        """
        apps = self._scoped_apps(refresh=True)
        return apps[:self.max_apps] if self.max_apps else apps

    def _scoped_apps(self, *, refresh: bool = False) -> List[Dict[str, Any]]:
        """Briefly reuse owner metadata; indexing caps never restrict queries.

        Cache only successful reads, on this identity-bound client instance.
        Expired reads fail closed on QRS errors; Engine still enforces access.
        """
        if not refresh and self._query_app_cache is not None:
            fetched_at, apps = self._query_app_cache
            if time.monotonic() - fetched_at < 30:
                return apps
        apps = self._apps_from_rows(self._qrs_get("app/full") or [], apply_cap=False)
        self._query_app_cache = (time.monotonic(), apps)
        return apps

    def _apps_from_rows(self, rows: Any, *, apply_cap: bool = True) -> List[Dict[str, Any]]:
        """Apply published/stream/app filters before the max_apps cap to QRS app
        rows. Works on both ``app/full`` and condensed ``app`` rows — the
        condensed form lacks the descriptive fields, which then stay empty."""
        apps: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            published = bool(row.get("published"))
            if self.published_only and not published:
                continue
            stream = row.get("stream") or {}
            stream_id = stream.get("id")
            stream_name = stream.get("name") or ""
            if self._stream_filter is not None:
                if stream_id not in self._stream_filter and stream_name not in self._stream_filter:
                    continue
            if self._app_filter is not None:
                if (row.get("id") or "").casefold() not in self._app_filter and (row.get("name") or "").casefold() not in self._app_filter:
                    continue
            apps.append({
                "id": row.get("id"),
                "name": row.get("name") or row.get("id"),
                "space_id": stream_id,
                "space_name": stream_name,
                "published": published,
                "publish_time": row.get("publishTime"),
                "last_reload": row.get("lastReloadTime"),
                "updated_at": row.get("modifiedDate"),
                "created_at": row.get("createdDate"),
                "description": row.get("description") or None,
                "owner": row.get("owner") or {},
                "modified_by": row.get("modifiedByUserName"),
                "file_size": row.get("fileSize"),
                "availability_status": row.get("availabilityStatus"),
                "tags": _tag_names(row.get("tags")),
                "custom_properties": _custom_properties(row.get("customProperties")),
            })

        # Newest reload first, so a max_apps cap keeps the apps most likely to
        # hold current data rather than an arbitrary slice.
        apps.sort(key=lambda a: (a.get("last_reload") or ""), reverse=True)
        if apply_cap and self.max_apps:
            apps = apps[: self.max_apps]
        return apps

    def list_data_connections(self) -> List[Dict[str, Any]]:
        """Data connections, with their credentials removed.

        ``/qrs/dataconnection/full`` returns `username` and `password` in the
        clear for every connection on the site. Only the name and type ever
        leave this method — enough to say "this app reads from a Google Sheets
        connection", nothing that could be replayed.
        """
        rows = self._qrs_get("dataconnection/full") or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append({
                "id": row.get("id"),
                "name": row.get("name"),
                "type": row.get("type"),
                "architecture": row.get("architecture"),
            })
        return out

    # ------------------------------------------------------------------
    # Engine (QIX)
    # ------------------------------------------------------------------

    def _qix_session(self, app_id: str, user: str) -> QixSession:
        self.connect()
        ssl_ctx = build_ssl_context(
            verify_ssl=self.verify_ssl,
            ca_file=self._ca_path,
            cert_file=self._cert_path,
            key_file=self._key_path,
        )
        return QixSession(
            f"wss://{self.host}:{self.engine_port}/app/{app_id}",
            headers=[("X-Qlik-User", user)],
            ssl_context=ssl_ctx,
            timeout_sec=self.timeout_sec,
        )

    def _app_meta(self, app: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The app-level block stamped onto every table of the app.

        Mirrors what the Power BI connector puts on each of its tables — ids,
        names, owner, a deep link, the artifacts built on the model and the
        row-level-security flag — so the same questions are answerable on either
        connector without re-crawling.
        """
        stream = self._streams_by_id().get(app.get("space_id") or "") or {}
        meta = {
            "appId": app.get("id"),
            "appName": app.get("name"),
            "appDescription": app.get("description"),
            "streamId": app.get("space_id"),
            "streamName": app.get("space_name") or None,
            "streamTags": stream.get("tags"),
            "streamCustomProperties": stream.get("customProperties"),
            "published": app.get("published"),
            "publishTime": app.get("publish_time"),
            "lastReload": app.get("last_reload"),
            "createdDate": app.get("created_at"),
            "modifiedDate": app.get("updated_at"),
            "modifiedBy": app.get("modified_by"),
            "owner": _owner_name(app.get("owner")),
            "fileSizeBytes": app.get("file_size"),
            "availabilityStatus": app.get("availability_status"),
            "tags": app.get("tags"),
            "customProperties": app.get("custom_properties"),
            # The hub deep link, so a table row can be traced back to the app a
            # human would open — the equivalent of Power BI's `webUrl`.
            "webUrl": (
                f"https://{self.host}/sense/app/{app.get('id')}" if self.host and app.get("id") else None
            ),
        }
        if payload:
            meta.update({
                "hasData": payload.get("has_data"),
                "hasScript": payload.get("has_script"),
                "engineFileName": payload.get("file_name"),
                "alternateStates": payload.get("state_names"),
                "scriptChars": payload.get("script_chars"),
                # Section Access is Qlik's row-level security. Declared in the
                # load script, so the script is the only place it can be seen —
                # and knowing it is on explains why two users get different
                # numbers from the same app.
                "sectionAccess": payload.get("section_access"),
                # Distinct upstream sources, names only. The full lineage with
                # statements is large, so it lives once on the Master Items row
                # while every table carries the short list.
                "lineageSources": payload.get("lineage_sources"),
                # Sheets on every table, the way Power BI stamps `reports`.
                "sheets": payload.get("sheet_names"),
            })
        return _compact(meta)

    def _crawl_app(self, app: Dict[str, Any]) -> List[Table]:
        """Crawl one app. Never raises — a bad app must not sink the site."""
        app_key = self._app_key(app)
        user = self._acting_user(app)
        try:
            payload = asyncio.run(self._read_app_async(app, user))
        except Exception as e:
            logger.warning("Qlik on-prem app crawl failed for %s (%s): %s", app.get("name"), app.get("id"), e)
            self._mark_unread(app_key)
            meta = dict(self._app_meta(app))
            meta.update({"status": "error", "error": str(e)[:500], "impersonatedUser": user})
            return [Table(
                name=app_key or (app.get("id") or "unknown-app"),
                description=f"Qlik app failed to crawl: {e}",
                columns=[],
                pks=[],
                fks=[],
                is_active=False,
                metadata_json={_NAMESPACE: meta},
            )]

        tables = self._build_data_tables(
            app, payload.get("qtr") or [], payload.get("qk") or [], payload
        )

        if self.include_master_items:
            master = self._build_master_table(app, payload, {t.name for t in tables})
            if master is not None:
                tables.append(master)

        if self.include_sheet_objects:
            tables.extend(self._build_sheet_tables(app, payload, {t.name for t in tables}))

        if not tables:
            # An app whose script has never been run has no data model and no
            # master items. That is a real, valid state — not a failure — so it
            # is recorded as an inactive row rather than an error, and rather
            # than a phantom table with no columns.
            logger.info(
                "Qlik app '%s' (%s) has no data model and no master items (hasData=%s)",
                app.get("name"), app.get("id"), payload.get("has_data"),
            )
            # Usually an app that was never reloaded, but an app caught
            # mid-reload looks the same — so its earlier tables are kept.
            self._mark_unread(app_key)
            meta = dict(self._app_meta(app, payload))
            meta.update({"status": "empty"})
            return [Table(
                name=app_key or (app.get("id") or "unknown-app"),
                description=(
                    "Qlik app opened successfully but exposes no data model — it has most likely "
                    "never been reloaded."
                ),
                columns=[],
                pks=[],
                fks=[],
                is_active=False,
                metadata_json={_NAMESPACE: meta},
            )]
        return tables

    def _mark_unread(self, app_key: str) -> None:
        if app_key:
            self._unread_app_keys.append(app_key)

    def unindexed_table_prefixes(self) -> List[str]:
        return list(self._unread_app_keys)

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> List[Table]:
        self._unread_app_keys = []
        apps = self.list_apps()
        if not apps:
            return []
        tables: List[Table] = []
        with ThreadPoolExecutor(max_workers=self.max_concurrency) as pool:
            try:
                futures = {pool.submit(self._crawl_app, app): app for app in apps}
                for fut in discovery_items(as_completed(futures), 'applications', label=lambda fut: futures[fut].get('name') or futures[fut].get('id'), total=len(futures)):
                    try:
                        tables.extend(fut.result() or [])
                    except IndexingCancelled:
                        raise
                    except Exception as e:
                        app = futures[fut]
                        logger.warning("Unhandled Qlik crawl exception for %s: %s", app.get("id"), e)
                        self._mark_unread(self._app_key(app))
            except IndexingCancelled:
                for pending in futures:
                    pending.cancel()
                raise
        return tables

    def get_schema(self, table_name: str) -> Table:
        """Find one Table by 'Stream/App/Table' name, or by app/table name."""
        all_tables = self.get_schemas()
        for tbl in all_tables:
            if tbl.name == table_name:
                return tbl
        for tbl in all_tables:
            meta = (tbl.metadata_json or {}).get(_NAMESPACE) or {}
            if meta.get("tableName") == table_name:
                return tbl
        for tbl in all_tables:
            meta = (tbl.metadata_json or {}).get(_NAMESPACE) or {}
            if meta.get("appId") == table_name or meta.get("appName") == table_name:
                return tbl
        raise RuntimeError(f"Table not found for '{table_name}'")

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def _resolve_app(self, target: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Turn 'Stream/App', 'Stream/App/Table', 'App' or a GUID into an app id.

        Requires a unique app in the configured filter scope, without the
        discovery-only max_apps cap. Returns the
        id and its row, which decides which user the Engine should act as.
        """
        if not target:
            raise ValueError("app is required")
        # Resolve IDs too: dropping the row drops its owner and silently changes
        # X-Qlik-User to sa_repository. Listing failures must remain failures.
        apps = self._scoped_apps()
        target_id = target.split("/", 1)[0]
        if _GUID_RE.fullmatch(target_id):
            matches = [a for a in apps if (a.get("id") or "").lower() == target_id.lower()]
        elif "/" not in target:
            matches = [a for a in apps if target in (a.get("id"), a.get("name"))]
        else:
            # Prefer exact qualified names/IDs, then the longest app prefix of
            # a full table path. Never strip a supplied stream and try elsewhere.
            matches = [a for a in apps if target in (self._app_key(a), a.get("id"))]
            if not matches:
                matches = [a for a in apps if target.startswith(self._app_key(a) + "/")]
                if matches:
                    longest = max(len(self._app_key(a)) for a in matches)
                    matches = [a for a in matches if len(self._app_key(a)) == longest]
        if len(matches) > 1:
            raise ValueError(f"Qlik app {target!r} is ambiguous; use its full Stream/App name or app ID")
        if not matches:
            raise ValueError(
                f"Qlik app {target!r} was not found in the configured app/stream scope. "
                "Use the full table name from describe_tables and check the connection's filters and access."
            )
        return matches[0]["id"], matches[0]

    execute_query = QlikEngineCatalog._execute_engine_query
    _execute_hypercube = QlikEngineCatalog._execute_engine_hypercube

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    @property
    def description(self) -> str:
        return (
            "Qlik Sense Enterprise on Windows Client: discover streams and apps via the Qlik "
            "Repository Service and run hypercube queries against them through the Engine API "
            "(QIX) over WebSocket."
        ) + self.system_prompt()



# Registry alias — `client_path` resolves the class by name, and the historic
# convention in this package is `<Type>Client` with a single capital.
QlikSenseOnpremClient = QlikSenseOnPremClient
