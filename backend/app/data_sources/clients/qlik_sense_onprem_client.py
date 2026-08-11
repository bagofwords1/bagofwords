"""
Qlik Sense Enterprise on Windows (QSEoW) client — the on-prem sibling of
``qlik_sense_client`` (Qlik Cloud).

The two products speak the same Engine protocol but nothing else is shared:

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
  - master measures WITH their Qlik expressions, master dimensions, variables
  - lineage (the LOAD/SELECT sources behind the app) and load-script size
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pandas as pd
import requests

from app.ai.prompt_formatters import ForeignKey, ServiceFormatter, Table, TableColumn
from app.data_sources.clients._qix_common import (
    QixSession,
    build_hypercube_def,
    build_ssl_context,
    fetch_hypercube_pages,
    hypercube_matrix_to_df,
    tags_to_dtype,
)
from app.data_sources.clients.base import DataSourceClient


logger = logging.getLogger(__name__)


_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# `Password=...` / `pwd = ...` inside a connection string or a SELECT that
# lineage hands back verbatim. Lineage is stored in table metadata, so anything
# secret-shaped is masked before it gets that far.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|pwd|passwd|secret|api[_-]?key|token)\s*=\s*[^;,\s\]]+"
)

# The default QRS service identity. It exists on every QSEoW site, always has
# repository access, and needs no lookup — which is what makes environment
# discovery work before anything about the customer's directory is known.
_DEFAULT_USER_DIRECTORY = "INTERNAL"
_DEFAULT_USER_ID = "sa_repository"

# Master items are app-level, not table-level: a Qlik measure is an expression
# over any fields in the model, so it has no home table the way a Power BI
# measure does. They are collected into one synthetic table per app.
_MASTER_TABLE_SUFFIX = "Master Items"

_NAMESPACE = "qlik_sense_onprem"


def _mask_secrets(text: str) -> str:
    """Blank out `password=...`-shaped assignments in free text."""
    return _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}=***", text or "")


class QlikSenseOnPremClient(DataSourceClient):
    """Qlik Sense Enterprise on Windows — QRS discovery + QIX query."""

    # A key field shared by more than this many tables is an associative hub
    # (a calendar key, an order number). Emitting foreign keys for it produces
    # n*(n-1) directed edges that say nothing a single "these tables associate"
    # note doesn't, so past this point the fan-out is recorded, not expanded.
    _MAX_KEY_FANOUT = 12

    _MAX_LINEAGE_ENTRIES = 60
    _MAX_STATEMENT_CHARS = 400
    _MAX_MASTER_ITEMS = 400
    _MAX_VARIABLES = 100

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
        max_apps: Optional[int] = None,
        max_concurrency: int = 4,
        timeout_sec: int = 60,
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
        self.timeout_sec = int(timeout_sec or 60)
        self.max_apps = int(max_apps) if max_apps else None
        # Every app opened is an app loaded into the Engine's memory, so this
        # stays deliberately lower than the Cloud client's default of 10.
        self.max_concurrency = max(1, int(max_concurrency or 4))

        self.user_directory = (user_directory or "").strip() or _DEFAULT_USER_DIRECTORY
        self.user_id = (user_id or "").strip() or None

        self._client_cert_pem = client_cert or None
        self._client_key_pem = client_key or None
        self._client_key_password = client_key_password or None
        self._root_ca_pem = root_ca or None

        self._stream_filter: Optional[set] = None
        if stream_filter:
            tokens = {t.strip() for t in str(stream_filter).split(",") if t.strip()}
            self._stream_filter = tokens or None

        # A fresh nonce per client instance. Any 16 alphanumeric characters are
        # valid; what matters is that the query-string and header copies match.
        self._xrfkey = secrets.token_hex(8)

        self._http: Optional[requests.Session] = None
        self._temp_dir: Optional[str] = None
        self._cert_path: Optional[str] = None
        self._key_path: Optional[str] = None
        self._ca_path: Optional[str] = None

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
            except Exception as e:
                raise RuntimeError(f"Could not decrypt the Qlik client key with the given password: {e}")

        self._cert_path = self._write_pem("client.pem", self._client_cert_pem)
        self._key_path = self._write_pem("client_key.pem", key_pem)
        if self._root_ca_pem:
            self._ca_path = self._write_pem("root.pem", self._root_ca_pem)

    def _verify_arg(self):
        """What to hand `requests` as `verify=`.

        A pasted root.pem always wins: QSEoW signs its service certificates with
        its own root, so verifying against that root is the only way to get real
        TLS verification on a default install.
        """
        if self._ca_path:
            return self._ca_path
        return self.verify_ssl

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
            self._http = requests.Session()

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
                "Certificate, or turn off Verify SSL."
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
            streams = self._qrs_get("stream/full") or []
        except Exception as e:
            return {
                "success": False,
                "connectivity": True,
                "message": f"Authenticated against QRS {version} but could not list streams: {e}",
            }

        try:
            apps = self.list_apps()
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
                f"{len(streams)} stream(s), {len(apps)} app(s) visible."
            )
        else:
            msg = (
                f"Connected to Qlik Sense Enterprise {version} as '{acting}', but no apps are "
                "visible. Check the stream filter, and that this identity has read access to "
                "the streams you expect."
            )
        return {
            "success": True,
            "message": msg,
            "version": version,
            "user": acting,
            "stream_count": len(streams),
            "app_count": len(apps),
        }

    # ------------------------------------------------------------------
    # Enumeration
    # ------------------------------------------------------------------

    def list_streams(self) -> List[Dict[str, Any]]:
        rows = self._qrs_get("stream/full") or []
        return [
            {"id": s.get("id"), "name": s.get("name"), "created": s.get("createdDate")}
            for s in rows
            if isinstance(s, dict)
        ]

    def list_apps(self) -> List[Dict[str, Any]]:
        """Enumerate apps via /qrs/app/full, honouring the stream filter.

        The app dicts use the same key names as the Cloud client's (`space_id` /
        `space_name` carry the stream) so the shared helpers in `_qix_common`
        and the `Stream/App/Table` naming both fall out without a translation
        layer.
        """
        rows = self._qrs_get("app/full") or []
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
            apps.append({
                "id": row.get("id"),
                "name": row.get("name") or row.get("id"),
                "space_id": stream_id,
                "space_name": stream_name,
                "published": published,
                "publish_time": row.get("publishTime"),
                "last_reload": row.get("lastReloadTime"),
                "updated_at": row.get("modifiedDate"),
                "description": row.get("description") or None,
                "owner": row.get("owner") or {},
            })

        # Newest reload first, so a max_apps cap keeps the apps most likely to
        # hold current data rather than an arbitrary slice.
        apps.sort(key=lambda a: (a.get("last_reload") or ""), reverse=True)
        if self.max_apps:
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

    @staticmethod
    async def _session_list(qix: QixSession, doc_handle: int, layout_key: str, definition: Dict[str, Any]):
        """CreateSessionObject + GetLayout, returning one list's qItems.

        Master items are not readable in bulk any other way: the Engine exposes
        them as a *list object* you define, ask for the layout of, and throw
        away with the session.
        """
        obj = await qix.rpc(doc_handle, "CreateSessionObject", [definition])
        handle = (obj.get("qReturn") or {}).get("qHandle")
        if handle is None:
            return []
        layout = await qix.rpc(handle, "GetLayout", [])
        return ((layout.get("qLayout") or {}).get(layout_key) or {}).get("qItems") or []

    _MEASURE_LIST_DEF = {
        "qInfo": {"qType": "MeasureList"},
        "qMeasureListDef": {
            "qType": "measure",
            "qData": {
                "title": "/qMetaDef/title",
                "description": "/qMetaDef/description",
                "expression": "/qMeasure/qDef",
                "label": "/qMeasure/qLabel",
            },
        },
    }

    _DIMENSION_LIST_DEF = {
        "qInfo": {"qType": "DimensionList"},
        "qDimensionListDef": {
            "qType": "dimension",
            "qData": {
                "title": "/qMetaDef/title",
                "description": "/qMetaDef/description",
                "grouping": "/qDim/qGrouping",
            },
        },
    }

    _VARIABLE_LIST_DEF = {
        "qInfo": {"qType": "VariableList"},
        "qVariableListDef": {
            "qType": "variable",
            # Reserved and config variables are Qlik's own hundreds of
            # formatting/system entries — noise in a catalog.
            "qShowReserved": False,
            "qShowConfig": False,
            "qData": {"definition": "/qDefinition"},
        },
    }

    async def _read_app_async(self, app: Dict[str, Any], user: str) -> Dict[str, Any]:
        """One Engine session per app: model, master items, lineage, script size."""
        app_id = app.get("id")
        out: Dict[str, Any] = {
            "qtr": [], "qk": [], "measures": [], "dimensions": [],
            "variables": [], "lineage": [], "has_data": None, "script_chars": None,
        }
        async with self._qix_session(app_id, user) as qix:
            opendoc = await qix.rpc(-1, "OpenDoc", [app_id])
            doc_handle = (opendoc.get("qReturn") or {}).get("qHandle")
            if doc_handle is None:
                raise RuntimeError("OpenDoc returned no document handle")

            layout = await qix.rpc(doc_handle, "GetAppLayout", [])
            out["has_data"] = ((layout.get("qLayout") or {}).get("qHasData"))

            tk = await qix.rpc(doc_handle, "GetTablesAndKeys", {
                "qWindowSize": {"qcx": 0, "qcy": 0},
                "qNullSize": {"qcx": 0, "qcy": 0},
                "qCellHeight": 0,
                "qSyntheticMode": False,
                "qIncludeSysVars": False,
            })
            out["qtr"] = tk.get("qtr") or []
            out["qk"] = tk.get("qk") or []

            if self.include_master_items:
                for key, layout_key, definition in (
                    ("measures", "qMeasureList", self._MEASURE_LIST_DEF),
                    ("dimensions", "qDimensionList", self._DIMENSION_LIST_DEF),
                    ("variables", "qVariableList", self._VARIABLE_LIST_DEF),
                ):
                    try:
                        out[key] = await self._session_list(qix, doc_handle, layout_key, definition)
                    except Exception as e:
                        # A missing master-item list is not a reason to lose the
                        # data model we already have in hand.
                        logger.debug("Qlik %s list failed for app %s: %s", key, app_id, e)

            if self.include_lineage:
                try:
                    out["lineage"] = await qix.rpc(doc_handle, "GetLineage", []) or []
                except Exception as e:
                    logger.debug("Qlik GetLineage failed for app %s: %s", app_id, e)
                try:
                    script = await qix.rpc(doc_handle, "GetScript", [])
                    out["script_chars"] = len(script.get("qScript") or "")
                except Exception as e:
                    logger.debug("Qlik GetScript failed for app %s: %s", app_id, e)
        return out

    # ------------------------------------------------------------------
    # Schema construction
    # ------------------------------------------------------------------

    @staticmethod
    def _app_key(app: Dict[str, Any]) -> str:
        """The `Stream/App` prefix every table of this app is named under."""
        name = app.get("name") or app.get("id") or ""
        stream = app.get("space_name") or ""
        return f"{stream}/{name}" if stream else name

    @classmethod
    def _app_meta(cls, app: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "appId": app.get("id"),
            "appName": app.get("name"),
            "streamId": app.get("space_id"),
            "streamName": app.get("space_name") or None,
            "published": app.get("published"),
            "lastReload": app.get("last_reload"),
        }

    def _clean_lineage(self, lineage: Any) -> List[Dict[str, str]]:
        """Dedupe, cap and de-secret the lineage entries."""
        cleaned: List[Dict[str, str]] = []
        seen: set = set()
        for entry in lineage or []:
            if not isinstance(entry, dict):
                continue
            discriminator = _mask_secrets(str(entry.get("qDiscriminator") or ""))
            statement = _mask_secrets(str(entry.get("qStatement") or ""))
            if not discriminator and not statement:
                continue
            statement = statement[: self._MAX_STATEMENT_CHARS]
            key = (discriminator, statement)
            if key in seen:
                continue
            seen.add(key)
            item = {}
            if discriminator:
                item["source"] = discriminator
            if statement:
                item["statement"] = statement
            cleaned.append(item)
            if len(cleaned) >= self._MAX_LINEAGE_ENTRIES:
                break
        return cleaned

    def _build_data_tables(self, app: Dict[str, Any], qtr: List[Dict], qk: List[Dict]) -> List[Table]:
        """Map GetTablesAndKeys onto Tables.

        Qlik associates rather than joins: `qk` says a key field links a set of
        tables, with no direction and no notion of parent/child. Each link is
        therefore emitted from both sides.
        """
        app_key = self._app_key(app)
        app_meta = self._app_meta(app)

        keys_by_table: Dict[str, List[Dict[str, Any]]] = {}
        for key in qk or []:
            key_fields = key.get("qKeyFields") or []
            linked = key.get("qTables") or []
            for t in linked:
                keys_by_table.setdefault(t, []).append({
                    "fields": key_fields,
                    "others": [o for o in linked if o != t],
                    "fanout": len(linked),
                })

        tables: List[Table] = []
        for tbl in qtr or []:
            tbl_name = tbl.get("qName") or ""
            if not tbl_name:
                continue

            entries = keys_by_table.get(tbl_name, [])
            key_fields = {kf for e in entries for kf in e["fields"]}

            columns: List[TableColumn] = []
            pks: List[TableColumn] = []
            for field in tbl.get("qFields") or []:
                fname = field.get("qName") or ""
                if not fname:
                    continue
                tags = field.get("qTags") or []
                col_meta: Dict[str, Any] = {"role": "column", "qlik_tags": tags}
                # Qlik's own system fields are queryable but never meant for a
                # report, exactly like a hidden Power BI column.
                if "$hidden" in tags or "$system" in tags:
                    col_meta["hidden"] = True
                col = TableColumn(
                    name=fname,
                    dtype=tags_to_dtype(tags),
                    description=None,
                    metadata=col_meta,
                )
                columns.append(col)
                if fname in key_fields:
                    pks.append(col)

            fks: List[ForeignKey] = []
            hubs: List[str] = []
            for entry in entries:
                if entry["fanout"] > self._MAX_KEY_FANOUT:
                    hubs.extend(entry["fields"])
                    continue
                for kf in entry["fields"]:
                    for other in entry["others"]:
                        fks.append(ForeignKey(
                            column=TableColumn(name=kf, dtype="key"),
                            references_name=f"{app_key}/{other}" if app_key else other,
                            references_column=TableColumn(name=kf, dtype="key"),
                        ))

            meta = dict(app_meta)
            meta.update({
                "tableName": tbl_name,
                "rowCount": tbl.get("qNoOfRows"),
                "source": "qix",
            })
            if hubs:
                meta["associativeHubKeys"] = sorted(set(hubs))
            if tbl.get("qLoose"):
                meta["loose"] = True

            tables.append(Table(
                name=f"{app_key}/{tbl_name}" if app_key else tbl_name,
                description=None,
                columns=columns,
                pks=pks,
                fks=fks,
                is_active=True,
                metadata_json={_NAMESPACE: meta},
            ))
        return tables

    def _build_master_table(
        self,
        app: Dict[str, Any],
        payload: Dict[str, Any],
        taken_names: set,
    ) -> Optional[Table]:
        """One synthetic table per app holding its reusable definitions.

        A master measure carries the business logic the app's authors agreed on
        — `Sum({$<[Region]={'North'}>} [Qty]*[Price])` — and re-deriving it from
        raw fields will not reproduce its set analysis. Surfacing the expression
        is the point: the agent can pass it straight to `execute_query`.
        """
        app_key = self._app_key(app)
        columns: List[TableColumn] = []

        for item in (payload.get("measures") or [])[: self._MAX_MASTER_ITEMS]:
            data = item.get("qData") or {}
            meta_def = item.get("qMeta") or {}
            name = data.get("title") or meta_def.get("title") or ""
            if not name:
                continue
            expression = data.get("expression") or ""
            item_meta: Dict[str, Any] = {"role": "measure", "expression": expression}
            if data.get("label"):
                item_meta["label"] = data["label"]
            columns.append(TableColumn(
                name=name,
                dtype="measure",
                description=(data.get("description") or meta_def.get("description") or expression)[:200] or None,
                metadata=item_meta,
            ))

        for item in (payload.get("dimensions") or [])[: self._MAX_MASTER_ITEMS]:
            data = item.get("qData") or {}
            meta_def = item.get("qMeta") or {}
            name = data.get("title") or meta_def.get("title") or ""
            if not name:
                continue
            item_meta: Dict[str, Any] = {"role": "dimension"}
            # "H" is a drill-down group — several fields the user descends
            # through, not a single selectable field.
            if data.get("grouping") == "H":
                item_meta["drilldown"] = True
            columns.append(TableColumn(
                name=name,
                dtype="dimension",
                description=(data.get("description") or meta_def.get("description") or None),
                metadata=item_meta,
            ))

        variables = []
        for item in (payload.get("variables") or [])[: self._MAX_VARIABLES]:
            data = item.get("qData") or {}
            name = (item.get("qName") or (item.get("qMeta") or {}).get("title") or "")
            if not name:
                continue
            variables.append({"name": name, "definition": _mask_secrets(str(data.get("definition") or ""))[:200]})

        lineage = self._clean_lineage(payload.get("lineage"))

        if not columns and not variables and not lineage:
            return None

        base = f"{app_key}/{_MASTER_TABLE_SUFFIX}" if app_key else _MASTER_TABLE_SUFFIX
        name = base if base not in taken_names else f"{base} ({app.get('id')})"

        meta = dict(self._app_meta(app))
        meta.update({
            "objectType": "master_items",
            "hasData": payload.get("has_data"),
            "measureCount": sum(1 for c in columns if c.dtype == "measure"),
            "dimensionCount": sum(1 for c in columns if c.dtype == "dimension"),
        })
        if variables:
            meta["variables"] = variables
        if lineage:
            meta["lineage"] = lineage
        if payload.get("script_chars") is not None:
            meta["scriptChars"] = payload["script_chars"]

        return Table(
            name=name,
            description=(
                f"Reusable definitions published in the Qlik app '{app.get('name')}': master "
                "measures (with their Qlik expressions), master dimensions and variables. Not a "
                "physical table — pass a measure's expression to execute_query(measures=[...])."
            ),
            columns=columns,
            pks=[],
            fks=[],
            is_active=True,
            metadata_json={_NAMESPACE: meta},
        )

    def _crawl_app(self, app: Dict[str, Any]) -> List[Table]:
        """Crawl one app. Never raises — a bad app must not sink the site."""
        app_key = self._app_key(app)
        user = self._acting_user(app)
        try:
            payload = asyncio.run(self._read_app_async(app, user))
        except Exception as e:
            logger.warning("Qlik on-prem app crawl failed for %s (%s): %s", app.get("name"), app.get("id"), e)
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

        tables = self._build_data_tables(app, payload.get("qtr") or [], payload.get("qk") or [])

        if self.include_master_items:
            master = self._build_master_table(app, payload, {t.name for t in tables})
            if master is not None:
                tables.append(master)

        if not tables:
            # An app whose script has never been run has no data model and no
            # master items. That is a real, valid state — not a failure — so it
            # is recorded as an inactive row rather than an error, and rather
            # than a phantom table with no columns.
            logger.info(
                "Qlik app '%s' (%s) has no data model and no master items (hasData=%s)",
                app.get("name"), app.get("id"), payload.get("has_data"),
            )
            meta = dict(self._app_meta(app))
            meta.update({"status": "empty", "hasData": payload.get("has_data")})
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

    def get_schemas(self) -> List[Table]:
        apps = self.list_apps()
        if not apps:
            return []
        tables: List[Table] = []
        with ThreadPoolExecutor(max_workers=self.max_concurrency) as pool:
            futures = {pool.submit(self._crawl_app, app): app for app in apps}
            for fut in as_completed(futures):
                try:
                    tables.extend(fut.result() or [])
                except Exception as e:
                    app = futures[fut]
                    logger.warning("Unhandled Qlik crawl exception for %s: %s", app.get("id"), e)
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

        Returns the id plus the app row when one was found, since the row is
        what decides which user the Engine session should act as.
        """
        if not target:
            raise ValueError("app is required")
        if _GUID_RE.match(target):
            # Already an app id — skip the listing round-trip entirely.
            return target, None

        try:
            apps = self.list_apps()
        except Exception as e:
            logger.debug("list_apps() failed while resolving %r: %s", target, e)
            return target, None

        candidates = [target]
        # "Stream/App/Table" — drop the table segment and match the app.
        parts = target.split("/")
        if len(parts) >= 3:
            candidates.append("/".join(parts[:2]))
        if len(parts) >= 2:
            candidates.append(parts[-2])
        candidates.append(parts[-1])

        for candidate in candidates:
            for app in apps:
                if candidate in (self._app_key(app), app.get("name"), app.get("id")):
                    return app.get("id") or target, app
        return target, None

    def execute_query(
        self,
        app: Optional[str] = None,
        dimensions: Optional[List[str]] = None,
        measures: Optional[List[Dict[str, str]]] = None,
        filters: Optional[Dict[str, List[Any]]] = None,
        max_rows: int = 10000,
        table_name: Optional[str] = None,
        **_kwargs: Any,
    ) -> pd.DataFrame:
        """Run a hypercube query against one app and return a DataFrame.

        Args:
            app: "Stream/AppName", "Stream/AppName/Table" or a raw app GUID.
                Alias: ``table_name``.
            dimensions: Qlik field names to group by.
            measures: ``{"expr": "Sum([Sales])", "alias": "Total"}`` dicts.
            filters: ``{field: [values]}``, applied as Qlik selections before
                the cube is built, so they propagate associatively.
            max_rows: row cap; paging is handled internally.
        """
        target = app or table_name
        if not target:
            raise ValueError("app (or table_name) is required")
        dimensions = dimensions or []
        measures = measures or []
        if not dimensions and not measures:
            raise ValueError("At least one dimension or measure is required")

        app_id, app_row = self._resolve_app(target)
        user = self._acting_user(app_row)
        hypercube_def = build_hypercube_def(dimensions, measures, max_rows)
        selections = list((filters or {}).items())
        return asyncio.run(
            self._execute_hypercube(app_id, user, selections, hypercube_def, max_rows)
        )

    async def _execute_hypercube(
        self,
        app_id: str,
        user: str,
        selections: List,
        hypercube_def: Dict[str, Any],
        max_rows: int,
    ) -> pd.DataFrame:
        async with self._qix_session(app_id, user) as qix:
            opendoc = await qix.rpc(-1, "OpenDoc", [app_id])
            doc_handle = (opendoc.get("qReturn") or {}).get("qHandle")
            if doc_handle is None:
                raise RuntimeError("Qlik OpenDoc did not return a document handle")

            for field, values in selections:
                await qix.rpc(doc_handle, "SelectInField", [
                    field,
                    {"qMatchMode": 0, "qSoftLock": False,
                     "qValues": [{"qText": str(v)} for v in (values or [])]},
                    False,
                ])

            session = await qix.rpc(doc_handle, "CreateSessionObject", [{
                "qInfo": {"qType": "bow-hypercube"},
                "qHyperCubeDef": hypercube_def,
            }])
            obj_handle = (session.get("qReturn") or {}).get("qHandle")
            if obj_handle is None:
                raise RuntimeError("Qlik CreateSessionObject did not return a handle")

            width = len(hypercube_def.get("qDimensions") or []) + len(
                hypercube_def.get("qMeasures") or []
            )
            rows = await fetch_hypercube_pages(qix, obj_handle, width, max_rows)

        return hypercube_matrix_to_df(hypercube_def, rows)

    # ------------------------------------------------------------------
    # Prompt / description
    # ------------------------------------------------------------------

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    @property
    def description(self) -> str:
        return (
            "Qlik Sense Enterprise on Windows Client: discover streams and apps via the Qlik "
            "Repository Service and run hypercube queries against them through the Engine API "
            "(QIX) over WebSocket."
        ) + self.system_prompt()

    def system_prompt(self) -> str:
        return """

## Qlik Sense (on-prem) Hypercube Query Guide

Each Qlik app is a self-contained associative model published to a stream
(a stream is to Qlik what a workspace is to Power BI). Queries are hypercubes:
dimensions (Qlik field names) crossed with measures (Qlik expressions).

### Schema Structure

Tables are named `Stream/AppName/TableName`, or `AppName/TableName` for an app
that is not in a stream. Pass the full name — or just `Stream/AppName` — to
`execute_query()` as the `app` argument.

Every app also exposes a `Stream/AppName/Master Items` entry. It is NOT a
physical table: its columns are the app's published master measures (each with
its Qlik expression in `metadata.expression`), master dimensions, and variables.
Prefer a master measure's expression over one you invent — it encodes the set
analysis the app's authors agreed on, which a raw `Sum()` will not reproduce.

### How to Execute Queries

Signature: `execute_query(app, dimensions, measures, filters=None, max_rows=10000)`

```python
# Total sales by region
df = client.execute_query(
    app="Sales/Consumer Sales",
    dimensions=["Region Name"],
    measures=[{"expr": "Sum([Sales Quantity] * [Sales Price])", "alias": "Revenue"}],
)

# Re-use a published master measure verbatim (set analysis included)
df = client.execute_query(
    app="Sales/Consumer Sales",
    dimensions=["Product Group Desc"],
    measures=[{"expr": "Sum({$<[Region Name]={'Northeast'}>} [Sales Quantity])",
               "alias": "Northeast Qty"}],
    max_rows=50,
)

# Row count / profiling
df = client.execute_query(
    app="Sales/Consumer Sales",
    measures=[{"expr": "Count([Order Number])", "alias": "Orders"}],
)
```

### Key Query Rules

- **Field names are case-sensitive** and must match the schema exactly. Wrap
  anything containing a space in square brackets: `Sum([Net Sales])`.
- **Measures are Qlik expressions, not SQL** — `Sum(...)`, `Count(distinct ...)`,
  `Avg(...)`, and set analysis `{$<Field={'Value'}>}`.
- **Filters are Qlik selections**, not a SQL `WHERE`: they narrow the associative
  state and propagate to every linked table automatically.
- **Do not write joins.** Fields associate through shared key fields, so name
  fields from several tables in one cube and Qlik resolves the path.
- A table whose entry is inactive with `status: empty` belongs to an app that
  has never been reloaded — it holds no data and cannot be queried.
- Results are a flat DataFrame of (dimensions..., measures...), capped at
  `max_rows`.
"""


# Registry alias — `client_path` resolves the class by name, and the historic
# convention in this package is `<Type>Client` with a single capital.
QlikSenseOnpremClient = QlikSenseOnPremClient
