"""Read-only Siemens Teamcenter connector: business objects, BOM, where-used and change links as tables,
attached documents (datasets) as files.

Wire protocol. Teamcenter Services (SOA) over HTTP/JSON: every operation is a
``POST {base}/JsonRestServices/<Library>-<YYYY>-<MM>-<Service>/<operation>``
with the envelope ``{"header": {"state": {}, "policy": {}}, "body": {...}}``.
The session lives in the web tier's cookies; an Active Workspace gateway also
sets ``XSRF-TOKEN``, echoed back as the ``X-XSRF-TOKEN`` header. A failed call
answers with an exception object whose ``.QName`` names the exception class.
Model objects come back in ``ServiceData.modelObjects`` keyed by UID, each
property as ``{"dbValues": [...], "uiValues": [...]}``; a map in a response is
either a JSON object or Teamcenter's ``[[keys], [values]]`` pair of arrays.

Service versions differ between Teamcenter releases, so each operation lists
the versions it may live under (newest first) and the client remembers, per
server, the first one that answers (``_VERSIONS``). A missing operation (HTTP
404/405/501, or an exception saying the operation is unknown) moves on to the
next version; anything else is a real answer. Responses are read leniently: a
missing property is an empty value, an unknown type is skipped.

Sessions. A Teamcenter login starts a server process, so sessions are cached
per identity (``_SESSIONS``: keyed by an HMAC of the base URL and the identity's
credential material), serialized by a lock (a Teamcenter session serves one
request at a time anyway), re-established once when the server says the
session ended, and logged out when evicted after ``_SESSION_IDLE_S``.

Sign-in (the identity every call runs as, so Teamcenter access rules apply):
  * ``password``  — a Teamcenter user and password (``Session/login``);
  * ``sso_token`` — a Teamcenter Security Services (TcSS) token for a user who
    signed in through the browser flow (Entra ID or Active Directory behind
    TcSS), redeemed with ``Session/loginSSO``;
  * ``kerberos``  — TcSS with Windows integrated sign-in: the client answers
    the login service's Negotiate challenge with BOW's own Kerberos identity,
    or a member's delegated credential (S4U2Proxy), reads the token from the
    login service's redirect, then redeems it like ``sso_token``.

Query model. Saved queries are Teamcenter's only search, so an object table
(one per configured business object type) is read with the first configured
saved query that can filter on the object type; filters on columns the query
has an entry for are sent to Teamcenter (``*`` wildcards), the rest are applied
here. Properties are then read in batches with ``getProperties`` (only the
requested columns). ``bom`` opens a BOM window under a revision rule and
expands it level by level (always closed again), ``where_used`` and
``change_items`` follow Teamcenter's own relations.

Messages leaving this module are fixed strings: never a request URL, a
response body or a credential.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

import pandas as pd
import requests

from app.ai.prompt_formatters import ServiceFormatter, Table, TableColumn
from app.core.egress import pinned_session
from app.data_sources.clients._document_text import (
    DOC_EXTS, doc_text_is_usable, extract_document_text_from_bytes,
)
from app.data_sources.clients._file_source_common import (
    EXCEL_EXTS, DocumentText, FileTooLargeError, GlobScopeError, NamedBytes, read_excel_sheet,
)
from app.data_sources.clients.base import Capability, DataSourceClient, SearchStopped
from app.data_sources.clients.progress import discovery_progress

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- limits
DEFAULT_LIMIT = 1000
DEFAULT_MAX_ROWS = 2000
DEFAULT_BOM_DEPTH = 10
_PROP_BATCH = 200              # objects per getProperties call
_EXPAND_BATCH = 100            # parent BOM lines per expandPSOneLevel call
_MAX_CHANGES = 200             # change objects one change_items query may follow
_MAX_CUSTOM_COLUMNS = 60
_SESSION_IDLE_S = 900
_MAX_SESSIONS = 64
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_SSO_HOPS = 8
_SNIPPET_CHARS = 300

DEFAULT_OBJECT_TYPES = "ItemRevision, ChangeRequestRevision, ChangeNoticeRevision, ProblemReportRevision"
DEFAULT_SAVED_QUERIES = "Item Revision..., General..."
DEFAULT_DATASET_QUERY = "Dataset..."
DEFAULT_REVISION_RULE = "Latest Working"
DEFAULT_FILE_EXTENSIONS = "pdf, docx, doc, xlsx, xls, pptx, ppt, txt, csv, rtf, md, html, xml, json"
DEFAULT_APP_ID_PARAM = "appID"

# --------------------------------------------------------------------------- services (newest first)
OPS: dict[str, tuple[str, ...]] = {
    "login": ("Core-2011-06-Session/login", "Core-2006-03-Session/login"),
    "login_sso": ("Core-2011-06-Session/loginSSO", "Core-2006-03-Session/loginSSO"),
    "logout": ("Core-2006-03-Session/logout",),
    "session_info": ("Core-2007-01-Session/getTCSessionInfo",),
    "type_descriptions": ("Core-2015-10-Session/getTypeDescriptions2", "Core-2011-06-Session/getTypeDescriptions"),
    "get_properties": ("Core-2006-03-DataManagement/getProperties",),
    "saved_queries": ("Query-2006-03-SavedQuery/getSavedQueries",),
    "describe_queries": ("Query-2006-03-SavedQuery/describeSavedQueries",),
    "execute_query": ("Query-2008-06-SavedQuery/executeSavedQueries", "Query-2007-06-SavedQuery/executeSavedQueries",
                      "Query-2006-03-SavedQuery/executeSavedQuery"),
    "revision_rules": ("Cad-2007-01-StructureManagement/getRevisionRules",),
    "create_bom_windows": ("Cad-2007-01-StructureManagement/createBOMWindows",),
    "expand_one_level": ("Cad-2007-01-StructureManagement/expandPSOneLevel",),
    "close_bom_windows": ("Cad-2007-01-StructureManagement/closeBOMWindows",),
    "where_used": ("Core-2007-01-DataManagement/whereUsed", "Core-2012-02-DataManagement/whereUsed"),
    "read_tickets": ("Core-2006-03-FileManagement/getFileReadTickets",),
}

# Teamcenter property value types (PROP_char=1 … PROP_untyped_relation=14).
_VALUE_TYPES = {1: "str", 2: "datetime", 3: "float", 4: "float", 5: "int", 6: "bool", 7: "int", 8: "str",
                9: "ref", 10: "ref", 11: "str", 12: "str", 13: "rel", 14: "rel"}
_VALUE_TYPE_NAMES = {"char": "str", "date": "datetime", "double": "float", "float": "float", "int": "int",
                     "integer": "int", "logical": "bool", "bool": "bool", "boolean": "bool", "short": "int",
                     "string": "str", "note": "str", "external_reference": "str", "typed_reference": "ref",
                     "untyped_reference": "ref", "typed_relation": "rel", "untyped_relation": "rel"}

# Columns every object table offers when the type has them (all, when type metadata is unavailable).
STANDARD_COLUMNS: list[tuple[str, str]] = [
    ("item_id", "str"), ("item_revision_id", "str"), ("object_name", "str"), ("object_desc", "str"),
    ("object_type", "str"), ("release_status_list", "str"), ("date_released", "datetime"),
    ("owning_user", "str"), ("owning_group", "str"), ("last_mod_user", "str"),
    ("creation_date", "datetime"), ("last_mod_date", "datetime"), ("checked_out", "str"),
]
_REVISION_ONLY = {"item_revision_id", "date_released"}
CHANGE_COLUMNS: list[tuple[str, str]] = [("CMMaturity", "str"), ("CMDisposition", "str"), ("CMClosure", "str")]
_CHANGE_TYPE_HINTS = ("ChangeItemRevision", "ChangeRequestRevision", "ChangeNoticeRevision", "ProblemReportRevision",
                      "ChangeItem")
# Change Manager relations on a change revision -> the relation name shown in change_items.
CHANGE_RELATIONS = {"CMHasProblemItem": "problem", "CMHasImpactedItem": "impacted",
                    "CMHasSolutionItem": "solution", "CMReferences": "reference"}
# Relations under which documents hang off an item revision.
ATTACHMENT_RELATIONS = ("IMAN_specification", "IMAN_reference", "IMAN_manifestation", "IMAN_Rendering", "TC_Attaches")
# Saved-query entry labels per column, for queries whose description does not name the attribute.
_ENTRY_LABELS = {
    "item_id": ("item id", "itemid", "id"), "item_revision_id": ("revision", "revision id", "rev"),
    "object_name": ("name",), "object_type": ("type",), "object_desc": ("description",),
    "owning_user": ("owning user", "owner"), "owning_group": ("owning group", "group"),
    "release_status_list": ("release status",), "last_mod_user": ("last modifying user",),
}
_CUSTOM_PROP_RE = re.compile(r"^[A-Za-z]{1,4}[1-9][A-Za-z0-9]*_")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ \-.]{0,127}$")
_UID_RE = re.compile(r"^[A-Za-z0-9$_\-]{8,40}$")
_ITEM_ID_LIKE = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9_.\-]{2,63}$")   # PRT-1000, 000123, ECN-0042
_SEARCH_WORDS = 6
_SEARCH_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "what", "which", "who", "where", "when", "how", "are",
    "was", "were", "does", "did", "has", "have", "about", "into", "its", "document", "documents", "file", "files",
    "doc", "docs", "teamcenter", "show", "find", "give", "tell", "please", "rev", "revision",
}

FIXED_TABLES: dict[str, list[tuple[str, str]]] = {
    "bom": [("level", "int"), ("parent_item_id", "str"), ("parent_revision", "str"), ("item_id", "str"),
            ("revision", "str"), ("name", "str"), ("type", "str"), ("quantity", "float"), ("find_number", "str"),
            ("release_status", "str"), ("uid", "str")],
    "where_used": [("item_id", "str"), ("revision", "str"), ("level", "int"), ("parent_item_id", "str"),
                   ("parent_revision", "str"), ("parent_name", "str"), ("parent_type", "str"),
                   ("parent_release_status", "str")],
    "change_items": [("change_id", "str"), ("change_revision", "str"), ("change_name", "str"), ("change_type", "str"),
                     ("change_maturity", "str"), ("change_closure", "str"), ("relation", "str"),
                     ("item_id", "str"), ("item_revision", "str"), ("item_name", "str"), ("item_type", "str"),
                     ("item_release_status", "str")],
    "revision_rules": [("name", "str"), ("description", "str"), ("uid", "str")],
}
_UNRELEASED = "empty = not released (work in progress)"
FIXED_COLUMN_NOTES = {
    ("bom", "level"): "1 = direct child of the root",
    ("bom", "quantity"): "quantity per parent assembly",
    ("bom", "release_status"): "release status of the line's revision; " + _UNRELEASED,
    ("where_used", "parent_release_status"): "release status of the parent revision; " + _UNRELEASED,
    ("change_items", "change_closure"): "the change's closure: Open = not closed",
    ("change_items", "change_maturity"): "the change's maturity, e.g. Elaborating, Reviewing, Executing, Complete",
    ("change_items", "item_release_status"): "release status of the linked item revision (not the change); "
                                             + _UNRELEASED,
}
FIXED_TABLE_DESCRIPTIONS = {
    "bom": "Bill of materials of one assembly revision, configured by a revision rule (one row per BOM line).",
    "where_used": "Assemblies that use a part revision (one row per parent, with its level above the part).",
    "change_items": ("Parts linked to change objects (one row per change and linked item): problem, impacted, "
                     "solution and reference items. change_* columns describe the change, item_* the linked item."),
    "revision_rules": "Revision rules the BOM table can be configured with.",
}

# --------------------------------------------------------------------------- messages (fixed strings)
_URL_MESSAGE = ("Teamcenter URL must be the HTTP(S) address of the Teamcenter web tier, such as "
                "https://plm.example.com/tc (the address Teamcenter clients use, ending in its context path).")
_HTTPS_REQUIRED = "HTTPS is required. Enable Allow HTTP only for an isolated test lab."
_NO_CREDENTIAL = ("Provide a Teamcenter username and password, sign in with Teamcenter SSO, or use Kerberos with "
                  "Teamcenter Security Services.")
_SSO_NOT_CONFIGURED = ("Teamcenter SSO needs the Teamcenter Security Services login URL and this application's ID "
                       "in the connection settings.")
_BAD_SSO_URL = ("The SSO login URL must be the HTTP(S) address of the Teamcenter Security Services login service, "
                "ending in /weblogin/login_redirect.")
_LOGIN_REFUSED = "Teamcenter refused the sign-in. Check the username and password (and the group or role, if set)."
_SSO_REFUSED = "Teamcenter did not accept the SSO sign-in. Sign in with Teamcenter SSO again."
_SESSION_LOST = "The Teamcenter session ended and could not be restored. Try again."
_NOT_API = ("The URL did not answer as Teamcenter services (JsonRestServices). Use the web tier address, such as "
            "https://plm.example.com/tc.")
_SSO_ENFORCED = ("Teamcenter redirected the request to its sign-in page: this site signs in through Teamcenter "
                 "Security Services. Use Teamcenter SSO or Kerberos instead of a password.")
_UNREACHABLE = "Cannot reach Teamcenter. Check the URL, DNS and network access."
_TLS = "Teamcenter TLS verification failed. Install the issuing CA in the backend trust store."
_TIMEOUT = "Teamcenter did not answer in time. Narrow the query and try again."
_TOO_LARGE = "Teamcenter's answer exceeded the size limit. Narrow the query (filters or a lower limit)."
_SERVER_ERROR = "Teamcenter answered with a server error (HTTP {status}). Try again later."
_UNSUPPORTED = ("This Teamcenter server does not offer the service needed for {what}. Its services differ from the "
                "versions this connector knows; contact your Teamcenter administrator.")
_SERVICE_ERROR = "Teamcenter could not complete {what} ({kind})."
_KERBEROS_FAILED = ("Kerberos sign-in to Teamcenter Security Services failed. Check krb5.conf, the keytab and the "
                    "login service's Windows integrated sign-in.")
_KERBEROS_PASSWORD_PAGE = ("Teamcenter Security Services asked for a password instead of accepting the Kerberos "
                           "ticket: Windows integrated sign-in is not enabled for this login service.")
_SSO_NO_TOKEN = ("Teamcenter Security Services finished the sign-in without returning a token. Check the "
                 "application ID and the SSO parameter names in the connection settings.")
_DELEGATION_SID = "Kerberos delegation requires a verified directory identity."
_BAD_SPEC = ('A Teamcenter query must be a JSON object like {"table": "item_revision", "filter": {"item_id": '
             '"PRT-1*"}, "columns": ["item_id", "object_name"], "limit": 500}.')
_UNKNOWN_TABLE = "Unknown Teamcenter table {table!r}. Available tables: {tables}."
_UNKNOWN_COLUMNS = "Unknown column(s) for {table}: {columns}. Available: {available}."
_BAD_LIMIT = "The limit must be a positive whole number."
_BAD_FILTER = ("A filter is a JSON object of column -> value: a string (with * wildcards), a number, true/false, or "
               "a range such as {\"gte\": \"2024-01-01\", \"lt\": \"2025-01-01\"}.")
_NO_TYPE_QUERY = ("None of the configured saved queries ({queries}) can search by object type on this server. Set "
                  "Saved queries to a query with a Type entry.")
_NO_ITEM_QUERY = "None of the configured saved queries can look up an item by its ID. Add one with an Item ID entry."
_ITEM_NOT_FOUND = "No item revision {ref!r} that this identity can see."
_BAD_ITEM_REF = "Give an item as its ID, optionally with a revision: PRT-1000 or PRT-1000/B."
_RULE_NOT_FOUND = "No revision rule named {rule!r}. Query the revision_rules table for the names this server offers."
_BOM_NEEDS_ROOT = 'The bom table needs a root item: {"table": "bom", "root": "PRT-1000/B"}.'
_WHERE_USED_NEEDS_ITEM = 'The where_used table needs an item: {"table": "where_used", "item": "PRT-1001/A"}.'
_NO_CHANGE_TABLES = "No change object type is configured as a table, so change_items has no changes to read."
_DOCS_DISABLED = "Reading documents is turned off for this connection; query the tables instead."
_BAD_FILE_ID = "That is not a Teamcenter document id; use an id returned by list_files or search_files."
_FILE_TYPE = "That document's file type is not one this connection reads (allowed: {exts})."
_NO_FILE = "That document has no file this connection can read."
_FILE_TOO_LARGE = "Document exceeds the configured byte limit; increase it explicitly to read this document."
_BAD_FOLDER = "That folder is not valid; pass a folder id from list_files or an item such as PRT-1000/B."


class TeamcenterError(ValueError):
    """A fixed, user-facing Teamcenter connector error."""


class TeamcenterHTTPError(TeamcenterError):
    """One failed Teamcenter exchange. ``kind`` is a stable machine word; the text is fixed."""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class _Unsupported(Exception):
    """The server has no such operation under this service version."""


# --------------------------------------------------------------------------- caches (per process)
_SESSIONS: dict[str, "_TcSession"] = {}
_SESSIONS_LOCK = threading.Lock()
_VERSIONS: dict[tuple[str, str], str] = {}
_META: dict[tuple, tuple[float, object]] = {}
_META_TTL_S = 600
_KEY_SECRET = os.urandom(32)


def clear_cache() -> None:
    """Drop every cached session (logging each out), service version and metadata entry."""
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
        _VERSIONS.clear()
        _META.clear()
    for s in sessions:
        s.close()


# --------------------------------------------------------------------------- helpers
def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _split_list(value) -> list[str]:
    if value in (None, ""):
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    out = []
    for item in items:
        text = " ".join(str(item).split())
        if text and text not in out:
            out.append(text)
    return out


def table_name_for_type(type_name: str) -> str:
    """``ItemRevision`` -> ``item_revision``, ``XX4_PartRevision`` -> ``xx4_part_revision``, ``Part Revision`` -> ``part_revision``."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(type_name))
    text = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()
    return text if text and not text[0].isdigit() else f"t_{text}"


def parse_base_url(value, allow_http: bool) -> tuple[str, str]:
    """(normalized base ``scheme://host[:port]/context``, host) from what an admin pasted."""
    text = str(value or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        raise TeamcenterError(_URL_MESSAGE)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise TeamcenterError(_URL_MESSAGE)
    if parts.scheme == "http" and not allow_http:
        raise TeamcenterError(_HTTPS_REQUIRED)
    path = parts.path.rstrip("/")
    for suffix in ("/JsonRestServices", "/RestServices", "/services"):
        if path.lower().endswith(suffix.lower()):
            path = path[: -len(suffix)]
    if ".." in path.split("/"):
        raise TeamcenterError(_URL_MESSAGE)
    return f"{parts.scheme}://{parts.netloc}{path}", parts.hostname.lower()


def tcss_login_url(config: dict) -> str:
    """The browser URL that starts a Teamcenter Security Services sign-in for this connection."""
    cfg = config or {}
    url = str(cfg.get("sso_login_url") or "").strip()
    app_id = str(cfg.get("sso_app_id") or "").strip()
    if not url or not app_id:
        raise TeamcenterError(_SSO_NOT_CONFIGURED)
    try:
        parts = urlsplit(url)
    except ValueError:
        raise TeamcenterError(_BAD_SSO_URL)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.fragment:
        raise TeamcenterError(_BAD_SSO_URL)
    if parts.scheme == "http" and not _flag(cfg.get("allow_http")):
        raise TeamcenterError(_HTTPS_REQUIRED)
    param = str(cfg.get("sso_app_id_param") or DEFAULT_APP_ID_PARAM).strip() or DEFAULT_APP_ID_PARAM
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    query.append((param, app_id))
    return parts._replace(query=urlencode(query)).geturl()


def extract_sso_result(params, token_param: str | None = None, user_param: str | None = None) -> tuple[str | None, str | None]:
    """(token, user) from the parameters Teamcenter Security Services sent back.

    With configured names they are looked up case-insensitively; otherwise the
    first parameter whose name contains "token" carries the token and the first
    whose name contains "user" the Teamcenter user ID.
    """
    pairs = list(params.items()) if isinstance(params, dict) else list(params or [])
    lowered = [(str(k).lower(), v) for k, v in pairs if v not in (None, "")]

    def pick(name, hint):
        if name:
            wanted = name.strip().lower()
            return next((str(v) for k, v in lowered if k == wanted), None)
        return next((str(v) for k, v in lowered if hint in k), None)

    return pick(token_param, "token"), pick(user_param, "user")


def _uid(obj) -> str | None:
    if isinstance(obj, dict):
        value = obj.get("uid") or obj.get("objectID") or obj.get("objectId")
        return str(value) if value else None
    if isinstance(obj, str) and obj and obj != "AAAAAAAAAAAAAA":
        return obj
    return None


def _ref(uid: str, type_name: str | None = None) -> dict:
    ref = {"uid": uid}
    if type_name:
        ref["type"] = type_name
    return ref


def _pairs(value) -> list[tuple]:
    """A Teamcenter map: a JSON object, or ``[[keys], [values]]``."""
    if isinstance(value, dict):
        return list(value.items())
    if (isinstance(value, list) and len(value) == 2 and all(isinstance(v, list) for v in value)
            and len(value[0]) == len(value[1])):
        return list(zip(value[0], value[1]))
    return []


def _service_data(resp) -> dict:
    if not isinstance(resp, dict):
        return {}
    for key in ("ServiceData", "serviceData"):
        if isinstance(resp.get(key), dict):
            return resp[key]
    if "modelObjects" in resp or "plain" in resp:
        return resp
    return {}


def _model_objects(resp) -> dict:
    objects = _service_data(resp).get("modelObjects")
    out = {}
    for key, obj in _pairs(objects) if not isinstance(objects, dict) else objects.items():
        if isinstance(obj, dict):
            out[str(_uid(obj) or key)] = obj
    return out


def _values(obj: dict | None, prop: str) -> tuple[list, list]:
    """(dbValues, uiValues) of one property of a model object; empty when absent."""
    props = (obj or {}).get("props") or (obj or {}).get("properties") or {}
    value = props.get(prop) if isinstance(props, dict) else None
    if not isinstance(value, dict):
        return [], []
    db = value.get("dbValues")
    ui = value.get("uiValues")
    if db is None and "dbValue" in value:
        db = [value["dbValue"]]
    if ui is None and "uiValue" in value:
        ui = [value["uiValue"]]
    db = [v for v in (db if isinstance(db, list) else [db]) if v not in (None, "")]
    ui = [v for v in (ui if isinstance(ui, list) else [ui]) if v not in (None, "")]
    return db, ui


def _text(obj, prop) -> str | None:
    db, ui = _values(obj, prop)
    parts = ui or db
    return ", ".join(str(v) for v in parts) if parts else None


def _cell(obj, prop: str, dtype: str):
    db, ui = _values(obj, prop)
    if dtype == "datetime":
        return db[0] if db else (ui[0] if ui else None)
    if dtype in ("int", "float"):
        raw = db[0] if db else (ui[0] if ui else None)
        try:
            return float(raw) if dtype == "float" else int(float(raw))
        except (TypeError, ValueError):
            return None
    if dtype == "bool":
        raw = db[0] if db else (ui[0] if ui else None)
        if raw is None:
            return None
        return str(raw).strip().lower() in ("1", "true", "y", "yes")
    parts = ui or db
    return ", ".join(str(v) for v in parts) if parts else None


def _ext(name) -> str:
    text = str(name or "")
    return text.rsplit(".", 1)[-1].lower() if "." in text else ""


def _exception_kind(qname: str) -> str:
    tail = str(qname or "").rsplit(".", 1)[-1]
    return re.sub(r"[^A-Za-z]", "", tail)[:60] or "Exception"


_UNSUPPORTED_TEXT = re.compile(r"(not a valid (service|operation)|no such (service|operation)|unknown (service|operation)|"
                               r"service .* (was )?not found|operation .* (was )?not found|is not (a )?supported)",
                               re.IGNORECASE)


class _TcSession:
    """One authenticated Teamcenter session (cookies + XSRF token) and its lock."""

    def __init__(self, key: str):
        self.key = key
        self.lock = threading.RLock()
        self.http: requests.Session | None = None
        self.logged_in = False
        self.last_used = time.monotonic()
        self.identity: dict = {}
        self.base: str | None = None

    def new_http(self) -> requests.Session:
        if self.http is not None:
            self.http.close()
        http = pinned_session()
        http.trust_env = False  # no ambient proxy / .netrc credentials
        http.verify = os.environ.get("REQUESTS_CA_BUNDLE") or True
        http.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self.http = http
        self.logged_in = False
        return http

    def close(self) -> None:
        http, self.http = self.http, None
        if http is None:
            return
        if self.logged_in and self.base:
            try:
                http.post(f"{self.base}/JsonRestServices/{OPS['logout'][0]}", data=json.dumps(
                    {"header": {"state": {}, "policy": {}}, "body": {}}), timeout=(5, 10), allow_redirects=False)
            except Exception:
                pass
        http.close()
        self.logged_in = False


def _evict_idle(now: float) -> list[_TcSession]:
    stale = [k for k, s in _SESSIONS.items() if now - s.last_used > _SESSION_IDLE_S]
    if len(_SESSIONS) - len(stale) >= _MAX_SESSIONS:
        oldest = sorted((s.last_used, k) for k, s in _SESSIONS.items() if k not in stale)
        stale += [k for _, k in oldest[: len(_SESSIONS) - len(stale) - _MAX_SESSIONS + 1]]
    return [_SESSIONS.pop(k) for k in stale]


def planner_scope_note(config: dict) -> str:
    types = _split_list((config or {}).get("object_types") or DEFAULT_OBJECT_TYPES)
    return (f"Siemens Teamcenter PLM ({', '.join(types[:6])}{'…' if len(types) > 6 else ''}, BOM, where-used, change "
            "links); live, as the connection's Teamcenter identity, so Teamcenter access rules apply.")


# --------------------------------------------------------------------------- client
class TeamcenterClient(DataSourceClient):
    # Hybrid: tables plus attached documents through the file tools (live, no file catalog).
    capabilities = {Capability.QUERY, Capability.LIST_FILES, Capability.READ_FILE, Capability.SEARCH_FILES}
    cheap_live_listing = True
    sql_literal_style = "standard"

    def __init__(self, base_url: str, object_types: str | None = None, saved_queries: str | None = None,
                 dataset_query: str | None = None, revision_rule: str | None = None,
                 extra_properties: str | None = None, include_documents: bool = True,
                 file_extensions: str | None = None, file_server_url: str | None = None,
                 max_rows: int = DEFAULT_MAX_ROWS, max_bom_depth: int = DEFAULT_BOM_DEPTH,
                 max_file_size_mb: int = 50, tc_group: str | None = None, tc_role: str | None = None,
                 sso_login_url: str | None = None, sso_app_id: str | None = None,
                 sso_app_id_param: str | None = None, sso_token_param: str | None = None,
                 sso_user_param: str | None = None, allow_http: bool = False,
                 username: str | None = None, password: str | None = None,
                 access_token: str | None = None, tcss_user: str | None = None,
                 use_kerberos: bool = False, kerberos: bool = False, kerberos_principal: str | None = None,
                 principal: str | None = None, kerberos_impersonate: str | None = None,
                 kerberos_expected_sid: str | None = None, auth_type: str | None = None, **kwargs):
        self.allow_http = _flag(allow_http)
        self._base, self.host = parse_base_url(base_url, self.allow_http)
        self.object_types = [t for t in _split_list(object_types or DEFAULT_OBJECT_TYPES) if _IDENT_RE.match(t)]
        self.saved_queries = _split_list(saved_queries or DEFAULT_SAVED_QUERIES)
        self.dataset_query = " ".join(str(dataset_query or DEFAULT_DATASET_QUERY).split())
        self.revision_rule = " ".join(str(revision_rule or DEFAULT_REVISION_RULE).split())
        self.extra_properties = [p for p in _split_list(extra_properties) if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", p)]
        self.include_documents = _flag(include_documents) if include_documents is not None else True
        if not self.include_documents:
            self.capabilities = {Capability.QUERY}
        self.file_extensions = {e.lstrip(".").lower() for e in _split_list(file_extensions or DEFAULT_FILE_EXTENSIONS)}
        if file_server_url:
            self._fms, _ = parse_base_url(file_server_url, self.allow_http)
        else:
            p = urlsplit(self._base)
            self._fms = f"{p.scheme}://{p.netloc}/fms"
        try:
            self.max_rows = max(1, min(10_000, int(max_rows or DEFAULT_MAX_ROWS)))
        except (TypeError, ValueError):
            self.max_rows = DEFAULT_MAX_ROWS
        try:
            self.max_bom_depth = max(1, min(50, int(max_bom_depth or DEFAULT_BOM_DEPTH)))
        except (TypeError, ValueError):
            self.max_bom_depth = DEFAULT_BOM_DEPTH
        try:
            self._max_bytes = max(1, min(250, int(max_file_size_mb or 50))) * 1024 * 1024
        except (TypeError, ValueError):
            self._max_bytes = 50 * 1024 * 1024
        self.tc_group = (tc_group or "").strip()
        self.tc_role = (tc_role or "").strip()
        self._sso_config = {"sso_login_url": sso_login_url, "sso_app_id": sso_app_id,
                            "sso_app_id_param": sso_app_id_param, "allow_http": self.allow_http}
        self._sso_token_param = (sso_token_param or "").strip() or None
        self._sso_user_param = (sso_user_param or "").strip() or None

        # --- identity -------------------------------------------------------
        self._username = (username or "").strip() or None
        self._password = password
        self._sso_token = (access_token or "").strip() or None
        self._sso_user = (tcss_user or "").strip() or None
        self._kerberos = _flag(use_kerberos) or _flag(kerberos) or auth_type == "kerberos"
        self._krb_principal = (kerberos_principal or principal or "").strip() or None
        self._krb_impersonate = (kerberos_impersonate or "").strip() or None
        self._krb_expected_sid = (kerberos_expected_sid or "").strip() or None
        if self._sso_token:
            self.auth_method = "sso_token"
            material = "sso\0" + self._sso_token
        elif self._kerberos:
            if self._krb_impersonate and not self._krb_expected_sid:
                raise TeamcenterError(_DELEGATION_SID)
            tcss_login_url(self._sso_config)          # Kerberos goes through the TcSS login service
            self.auth_method = "kerberos"
            material = "krb\0" + (self._krb_impersonate or "svc:" + (self._krb_principal or ""))
        elif self._username and self._password:
            self.auth_method = "password"
            material = "pwd\0" + self._username + "\0" + self._password
        else:
            raise TeamcenterError(_NO_CREDENTIAL)
        self._key = hmac.new(_KEY_SECRET, (self._base + "\0" + self.tc_group + "\0" + self.tc_role + "\0" + material)
                             .encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()
        self.calls = 0  # service calls made by this client (for tests and diagnostics)

    # ================================================================== transport
    def _session(self) -> _TcSession:
        now = time.monotonic()
        with _SESSIONS_LOCK:
            stale = _evict_idle(now)
            session = _SESSIONS.get(self._key)
            if session is None:
                session = _TcSession(self._key)
                session.base = self._base
                _SESSIONS[self._key] = session
            session.last_used = now
        for s in stale:
            s.close()
        return session

    def _post(self, http: requests.Session, path: str, body: dict, timeout=(10, 120)):
        """One JSON service call. Returns the decoded answer; raises _Unsupported or TeamcenterHTTPError."""
        self.calls += 1
        headers = {}
        xsrf = http.cookies.get("XSRF-TOKEN")
        if xsrf:
            headers["X-XSRF-TOKEN"] = xsrf
        payload = json.dumps({"header": {"state": {}, "policy": {}}, "body": body})
        try:
            with http.post(f"{self._base}/JsonRestServices/{path}", data=payload, headers=headers, timeout=timeout,
                           allow_redirects=False, stream=True) as resp:
                status = resp.status_code
                if status in (404, 405, 501):
                    raise _Unsupported(path)
                if 300 <= status < 400:
                    raise TeamcenterHTTPError("sso_redirect", _SSO_ENFORCED, status)
                if status in (401, 403):
                    raise TeamcenterHTTPError("session", _SESSION_LOST, status)
                if status >= 500 and status != 500:
                    raise TeamcenterHTTPError("server", _SERVER_ERROR.format(status=status), status)
                if int(resp.headers.get("Content-Length") or 0) > _MAX_RESPONSE_BYTES:
                    raise TeamcenterHTTPError("too_large", _TOO_LARGE, status)
                chunks, size = [], 0
                for chunk in resp.iter_content(65536):
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        raise TeamcenterHTTPError("too_large", _TOO_LARGE, status)
                    chunks.append(chunk)
                raw = b"".join(chunks)
                ctype = (resp.headers.get("Content-Type") or "").lower()
        except requests.exceptions.SSLError:
            raise TeamcenterHTTPError("tls", _TLS)
        except requests.exceptions.Timeout:
            raise TeamcenterHTTPError("timeout", _TIMEOUT)
        except requests.exceptions.RequestException:
            raise TeamcenterHTTPError("unreachable", _UNREACHABLE)
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            if status == 500:
                raise TeamcenterHTTPError("server", _SERVER_ERROR.format(status=status), status)
            return {}
        if "html" in ctype or text[:1] == "<":
            if status == 500:
                raise TeamcenterHTTPError("server", _SERVER_ERROR.format(status=status), status)
            raise TeamcenterHTTPError("not_api", _NOT_API, status)
        try:
            data = json.loads(text)
        except ValueError:
            raise TeamcenterHTTPError("not_api", _NOT_API, status)
        qname = str(data.get(".QName") or "") if isinstance(data, dict) else ""
        if "exception" in qname.lower() or (status == 500 and isinstance(data, dict)):
            message = " ".join(str(data.get("message") or data.get("errorMessage") or "").split()) if isinstance(data, dict) else ""
            kind = _exception_kind(qname) if qname else "ServerError"
            if _UNSUPPORTED_TEXT.search(message) or _UNSUPPORTED_TEXT.search(qname):
                raise _Unsupported(path)
            low = kind.lower()
            if "invalidcredentials" in low:
                raise TeamcenterHTTPError("auth", _LOGIN_REFUSED, status)
            if "invaliduser" in low or "notloggedin" in low or "sessionexpired" in low:
                raise TeamcenterHTTPError("session", _SESSION_LOST, status)
            raise TeamcenterHTTPError("service:" + kind, kind, status)
        return data

    def _with_versions(self, http: requests.Session, op: str, bodies) -> object:
        """Call ``op`` under the first service version the server offers. ``bodies``: path -> body."""
        remembered = _VERSIONS.get((self._base, op))
        paths = list(OPS[op])
        if remembered in paths:
            paths.remove(remembered)
            paths.insert(0, remembered)
        for path in paths:
            body = bodies(path) if callable(bodies) else bodies
            try:
                result = self._post(http, path, body)
            except _Unsupported:
                if _VERSIONS.get((self._base, op)) == path:
                    _VERSIONS.pop((self._base, op), None)
                continue
            _VERSIONS[(self._base, op)] = path
            return result
        raise _Unsupported(op)

    def _call(self, op: str, bodies, what: str):
        """An authenticated call (logging in first, and once more if the session ended)."""
        session = self._session()
        with session.lock:
            for attempt in range(2):
                if session.http is None or not session.logged_in:
                    self._login(session)
                try:
                    result = self._with_versions(session.http, op, bodies)
                    session.last_used = time.monotonic()
                    return result
                except TeamcenterHTTPError as exc:
                    if exc.kind == "session" and attempt == 0:
                        session.logged_in = False
                        continue
                    if exc.kind.startswith("service:"):
                        raise TeamcenterHTTPError(exc.kind, _SERVICE_ERROR.format(what=what, kind=str(exc)), exc.status)
                    raise
                except _Unsupported:
                    raise TeamcenterHTTPError("unsupported", _UNSUPPORTED.format(what=what))
            raise TeamcenterHTTPError("session", _SESSION_LOST)

    # ------------------------------------------------------------------ sign-in
    def _login(self, session: _TcSession) -> None:
        http = session.new_http()
        discriminator = "bow-" + self._key[:16]
        try:
            if self.auth_method == "password":
                def body(path):
                    if path.startswith("Core-2011-06"):
                        return {"credentials": {"user": self._username, "password": self._password,
                                                "group": self.tc_group, "role": self.tc_role, "locale": "",
                                                "descrimator": discriminator}}
                    return {"username": self._username, "password": self._password, "group": self.tc_group,
                            "role": self.tc_role, "locale": "", "sessionDiscriminator": discriminator}
                answer = self._with_versions(http, "login", body)
            else:
                token, user = (self._sso_token, self._sso_user) if self.auth_method == "sso_token" else self._kerberos_token()

                def body(path):
                    if path.startswith("Core-2011-06"):
                        return {"credentials": {"user": user or "", "password": token, "group": self.tc_group,
                                                "role": self.tc_role, "locale": "", "descrimator": discriminator}}
                    return {"username": user or "", "ssoCredentials": token, "group": self.tc_group,
                            "role": self.tc_role, "locale": "", "sessionDiscriminator": discriminator}
                try:
                    answer = self._with_versions(http, "login_sso", body)
                except TeamcenterHTTPError as exc:
                    if exc.kind == "auth" or (exc.kind == "session" and exc.status in (401, 403, None)):
                        raise TeamcenterHTTPError("auth", _SSO_REFUSED, exc.status)
                    raise
        except _Unsupported:
            # No version of the login answers: this address does not serve Teamcenter's JSON services.
            raise TeamcenterHTTPError("not_api", _NOT_API)
        except TeamcenterHTTPError as exc:
            if exc.kind.startswith("service:"):
                # A login refused with another exception (a wrong group or role, a locked account).
                raise TeamcenterHTTPError("auth", _LOGIN_REFUSED if self.auth_method == "password" else _SSO_REFUSED,
                                          exc.status)
            raise
        session.logged_in = True
        info = answer.get("serverInfo") if isinstance(answer, dict) else None
        session.identity = {"server_info": info if isinstance(info, dict) else {}}

    def _kerberos_token(self) -> tuple[str, str | None]:
        """A TcSS token from the login service, answering its Negotiate challenge with Kerberos."""
        url = tcss_login_url(self._sso_config)
        login_host = (urlsplit(url).hostname or "").lower()
        ccache = None
        try:
            from app.data_sources.kerberos import _import_gssapi, get_ticket_manager
            gssapi = _import_gssapi()
            manager = get_ticket_manager()
            ccache = (manager.delegated_ccache(self._krb_impersonate) if self._krb_impersonate
                      else manager.service_ccache(self._krb_principal))
        except Exception:
            raise TeamcenterHTTPError("kerberos", _KERBEROS_FAILED)
        http = pinned_session()
        http.trust_env = False
        http.verify = os.environ.get("REQUESTS_CA_BUNDLE") or True
        try:
            for _ in range(_MAX_SSO_HOPS):
                host = (urlsplit(url).hostname or "").lower()
                headers = {}
                if host == login_host:
                    try:
                        with manager.activate(ccache):
                            creds = gssapi.Credentials(usage="initiate")
                            target = gssapi.Name(f"HTTP@{host}", gssapi.NameType.hostbased_service)
                            spnego = gssapi.OID.from_int_seq("1.3.6.1.5.5.2")
                            ctx = gssapi.SecurityContext(name=target, creds=creds, usage="initiate", mech=spnego)
                            headers["Authorization"] = "Negotiate " + base64.b64encode(ctx.step()).decode("ascii")
                    except Exception:
                        raise TeamcenterHTTPError("kerberos", _KERBEROS_FAILED)
                try:
                    resp = http.get(url, headers=headers, timeout=(10, 30), allow_redirects=False)
                except requests.exceptions.SSLError:
                    raise TeamcenterHTTPError("tls", _TLS)
                except requests.exceptions.RequestException:
                    raise TeamcenterHTTPError("unreachable", _UNREACHABLE)
                if 300 <= resp.status_code < 400 and resp.headers.get("Location"):
                    nxt = urljoin(url, resp.headers["Location"])
                    token, user = extract_sso_result(parse_qsl(urlsplit(nxt).query), self._sso_token_param,
                                                     self._sso_user_param)
                    if token:
                        return token, user
                    if (urlsplit(nxt).hostname or "").lower() != login_host:
                        raise TeamcenterHTTPError("kerberos", _SSO_NO_TOKEN)
                    url = nxt
                    continue
                if resp.status_code == 401:
                    raise TeamcenterHTTPError("kerberos", _KERBEROS_FAILED, 401)
                token, user = extract_sso_result(parse_qsl(urlsplit(resp.url or url).query), self._sso_token_param,
                                                 self._sso_user_param)
                if token:
                    return token, user
                if resp.status_code == 200 and "<form" in (resp.text or "").lower():
                    raise TeamcenterHTTPError("kerberos", _KERBEROS_PASSWORD_PAGE)
                raise TeamcenterHTTPError("kerberos", _SSO_NO_TOKEN, resp.status_code)
            raise TeamcenterHTTPError("kerberos", _SSO_NO_TOKEN)
        finally:
            http.close()

    # ================================================================== model helpers
    def _cached(self, name: str, loader):
        key = (self._key, name)
        now = time.monotonic()
        hit = _META.get(key)
        if hit and hit[0] > now:
            return hit[1]
        value = loader()
        if len(_META) > 512:
            _META.clear()
        _META[key] = (now + _META_TTL_S, value)
        return value

    def _properties(self, uids, props, what="reading properties") -> dict:
        """uid -> model object holding ``props``, read in batches. Objects the identity cannot see are left out."""
        uids = [u for u in dict.fromkeys(uids) if u]
        props = list(dict.fromkeys(p for p in props if p))
        out: dict = {}
        for i in range(0, len(uids), _PROP_BATCH):
            batch = uids[i:i + _PROP_BATCH]
            resp = self._call("get_properties", {"objects": [_ref(u) for u in batch], "attributes": props}, what)
            objects = _model_objects(resp)
            for uid in batch:
                if uid in objects:
                    out[uid] = objects[uid]
        return out

    def _identity(self) -> dict:
        def load():
            resp = self._call("session_info", {}, "reading the session")
            user_uid = _uid((resp or {}).get("user")) if isinstance(resp, dict) else None
            ident = {"user_uid": user_uid, "user_id": None, "user_name": None, "home_folder": None,
                     "version": None}
            extra = (resp or {}).get("extraInfo") if isinstance(resp, dict) else None
            for k, v in _pairs(extra):
                if str(k).lower() in ("tcserverversion", "version", "serverversion", "tcversion"):
                    ident["version"] = str(v)[:80]
            if user_uid:
                user = self._properties([user_uid], ["user_id", "user_name", "home_folder"]).get(user_uid)
                db_home, _ = _values(user, "home_folder")
                ident.update(user_id=_text(user, "user_id"), user_name=_text(user, "user_name"),
                             home_folder=_uid(db_home[0]) if db_home else None)
            return ident
        return self._cached("identity", load)

    # ------------------------------------------------------------------ type metadata
    def _type_meta(self) -> dict | None:
        """type name -> {"display", "parent", "hierarchy", "props": {name: (dtype, display)}}; None when unavailable."""
        def load():
            names = list(dict.fromkeys(self.object_types + ["Dataset", "ImanFile"]))
            try:
                resp = self._call("type_descriptions", lambda path: {"typeNames": names, "options": {}}
                                  if "2015-10" in path else {"typeNames": names}, "reading type metadata")
            except TeamcenterHTTPError as exc:
                if exc.kind == "unsupported" or exc.kind.startswith("service:"):
                    return None
                raise
            types = (resp or {}).get("types") if isinstance(resp, dict) else None
            if not isinstance(types, list):
                return None
            out = {}
            for t in types:
                if not isinstance(t, dict) or not t.get("name"):
                    continue
                descriptors = t.get("propertyDescriptors") or t.get("propDescriptors") or {}
                items = descriptors.values() if isinstance(descriptors, dict) else (
                    [v for _, v in _pairs(descriptors)] or descriptors if isinstance(descriptors, list) else [])
                props = {}
                for d in items:
                    if not isinstance(d, dict) or not d.get("name"):
                        continue
                    vt = d.get("valueType")
                    dtype = _VALUE_TYPES.get(vt) if isinstance(vt, int) else _VALUE_TYPE_NAMES.get(
                        str(vt or "").lower().replace("prop_", ""), "str")
                    if _flag(d.get("isArray")) and dtype not in ("rel", "ref"):
                        dtype = "str"
                    props[str(d["name"])] = (dtype or "str", str(d.get("displayName") or d["name"]))
                hierarchy = t.get("typeHierarchy") or t.get("typeHierarchyArray") or []
                out[str(t["name"])] = {"display": str(t.get("displayName") or t["name"]),
                                       "parent": t.get("parentTypeName"),
                                       "hierarchy": [str(h) for h in hierarchy if h] if isinstance(hierarchy, list) else [],
                                       "props": props}
            return out
        return self._cached("types", load)

    def _is_change_type(self, type_name: str, meta: dict | None) -> bool:
        info = (meta or {}).get(type_name) or {}
        chain = [type_name, info.get("parent") or ""] + list(info.get("hierarchy") or [])
        return any(h in _CHANGE_TYPE_HINTS for h in chain if h) or "change" in type_name.lower() or \
            "problemreport" in type_name.lower().replace(" ", "")

    def _type_columns(self, type_name: str, meta: dict | None) -> list[tuple[str, str, str | None]]:
        """[(column, dtype, description)] for one business object type."""
        cols = self._type_columns_raw(type_name, meta)
        return [(c, d, "Release Status; " + _UNRELEASED if c == "release_status_list" else desc) for c, d, desc in cols]

    def _type_columns_raw(self, type_name: str, meta: dict | None) -> list[tuple[str, str, str | None]]:
        info = (meta or {}).get(type_name)
        is_rev = type_name.lower().replace(" ", "").endswith("revision")
        cols: list[tuple[str, str, str | None]] = [("uid", "str", "Teamcenter object UID")]
        standard = list(STANDARD_COLUMNS) + (CHANGE_COLUMNS if self._is_change_type(type_name, meta) else [])
        if info is None:
            for name, dtype in standard:
                if name in _REVISION_ONLY and not is_rev:
                    continue
                cols.append((name, dtype, None))
            for name in self.extra_properties:
                if name not in {c[0] for c in cols}:
                    cols.append((name, "str", None))
            return cols
        props = info["props"]
        for name, dtype in standard:
            if name in props:
                pdtype, display = props[name]
                cols.append((name, dtype if pdtype in ("ref", "rel") else (pdtype if pdtype != "rel" else dtype),
                             display))
        taken = {c[0] for c in cols}
        custom = []
        for name, (dtype, display) in sorted(props.items()):
            if name in taken or dtype == "rel":
                continue
            if _CUSTOM_PROP_RE.match(name) or name in self.extra_properties:
                custom.append((name, "str" if dtype == "ref" else dtype, display))
        cols += custom[:_MAX_CUSTOM_COLUMNS]
        return cols

    def _tables(self) -> dict:
        """table name -> {"type": type name or None, "columns": [(name, dtype, description)], "description"}."""
        def load():
            meta = self._type_meta()
            tables: dict = {}
            for type_name in self.object_types:
                if meta is not None and type_name not in meta:
                    continue  # not a type on this server
                name = table_name_for_type(type_name)
                while name in tables or name in FIXED_TABLES:
                    name += "_t"
                info = (meta or {}).get(type_name) or {}
                display = info.get("display") or type_name
                parent = info.get("parent")
                desc = f"Teamcenter {display} objects (business object type {type_name}"
                desc += f", a kind of {parent})" if parent else ")"
                tables[name] = {"type": type_name, "columns": self._type_columns(type_name, meta), "description": desc}
            for name, cols in FIXED_TABLES.items():
                if name == "change_items" and not any(t["type"] and self._is_change_type(t["type"], meta)
                                                      for t in tables.values()):
                    continue
                tables[name] = {"type": None, "columns": [(c, d, FIXED_COLUMN_NOTES.get((name, c))) for c, d in cols],
                                "description": FIXED_TABLE_DESCRIPTIONS[name]}
            return tables
        return self._cached("tables", load)

    # ------------------------------------------------------------------ saved queries
    def _query_index(self) -> dict:
        """saved query name (lower) -> {"uid", "name"}."""
        def load():
            resp = self._call("saved_queries", {}, "listing saved queries")
            out = {}
            for q in (resp or {}).get("queries") or [] if isinstance(resp, dict) else []:
                if not isinstance(q, dict):
                    continue
                name = " ".join(str(q.get("name") or "").split())
                uid = _uid(q.get("query")) or _uid(q)
                if name and uid:
                    out[name.lower()] = {"uid": uid, "name": name}
            return out
        return self._cached("queries", load)

    def _query_entries(self, query_name: str) -> list[dict] | None:
        """[{"entry": display name, "attr": attribute name}] of a saved query; None when the server lacks it."""
        index = self._query_index()
        q = index.get(query_name.lower())
        if not q:
            return None

        def load():
            resp = self._call("describe_queries", {"queries": [_ref(q["uid"], "ImanQuery")]}, "describing a saved query")
            lists = (resp or {}).get("fieldLists") or (resp or {}).get("queryDescriptions") or [] if isinstance(resp, dict) else []
            entries = []
            for fl in lists if isinstance(lists, list) else []:
                fields = fl.get("fields") if isinstance(fl, dict) else None
                for f in fields if isinstance(fields, list) else []:
                    if isinstance(f, dict) and (f.get("entryName") or f.get("name")):
                        entries.append({"entry": str(f.get("entryName") or f.get("name")),
                                        "attr": str(f.get("attributeName") or "")})
            return entries
        return self._cached("query:" + query_name.lower(), load)

    @staticmethod
    def _entry_for(entries: list[dict], column: str) -> str | None:
        col = column.lower()
        for e in entries:
            attr = e["attr"].lower()
            if attr and (attr == col or attr.rsplit(".", 1)[-1] == col):
                return e["entry"]
        labels = _ENTRY_LABELS.get(column, ())
        for e in entries:
            label = " ".join(e["entry"].lower().replace("_", " ").split())
            if label in labels:
                return e["entry"]
        return None

    def _pick_query(self, needed: list[str], queries: list[str] | None = None) -> tuple[str, list[dict]] | None:
        for name in queries or self.saved_queries:
            entries = self._query_entries(name)
            if entries is None:
                continue
            if all(self._entry_for(entries, c) for c in needed):
                return name, entries
        return None

    def _run_query(self, query_name: str, criteria: list[tuple[str, str]], limit: int, what: str) -> list[str]:
        """UIDs found by one saved query with entry=value criteria (at most ``limit``)."""
        q = self._query_index()[query_name.lower()]
        entries = [e for e, _ in criteria]
        values = [v for _, v in criteria]

        def body(path):
            if path.startswith("Query-2006-03"):
                return {"query": _ref(q["uid"], "ImanQuery"), "entries": entries, "values": values, "limit": limit}
            item = {"query": _ref(q["uid"], "ImanQuery"), "entries": entries, "values": values,
                    "maxNumToReturn": limit, "limitList": [], "resultsType": 0, "clientId": "bow"}
            if path.startswith("Query-2008-06"):
                item["limitListCount"] = 0
            return {"input": [item]}
        resp = self._call("execute_query", body, what)
        uids: list[str] = []
        if isinstance(resp, dict):
            results = resp.get("arrayOfResults") or resp.get("results")
            groups = results if isinstance(results, list) else [resp]
            for group in groups:
                if not isinstance(group, dict):
                    continue
                for key in ("objectUIDS", "objectUids", "objectUIDs"):
                    uids += [str(u) for u in group.get(key) or [] if u]
                for obj in group.get("objects") or []:
                    u = _uid(obj)
                    if u:
                        uids.append(u)
        return list(dict.fromkeys(uids))[:limit]

    # ------------------------------------------------------------------ items
    def _parse_item_ref(self, ref) -> tuple[str, str | None]:
        text = str(ref or "").strip()
        if not text or len(text) > 200:
            raise TeamcenterError(_BAD_ITEM_REF)
        item, _, rev = text.partition("/")
        item, rev = item.strip(), rev.strip() or None
        if not item:
            raise TeamcenterError(_BAD_ITEM_REF)
        return item, rev

    def _find_revision(self, ref) -> str:
        """The UID of the item revision ``PRT-1000/B`` (or the newest revision of ``PRT-1000``)."""
        item, rev = self._parse_item_ref(ref)
        picked = self._pick_query(["item_id"])
        if not picked:
            raise TeamcenterError(_NO_ITEM_QUERY)
        name, entries = picked
        criteria = [(self._entry_for(entries, "item_id"), item)]
        rev_entry = self._entry_for(entries, "item_revision_id")
        if rev and rev_entry:
            criteria.append((rev_entry, rev))
        uids = self._run_query(name, criteria, 200, "finding an item")
        objs = self._properties(uids, ["item_id", "item_revision_id", "object_type", "last_mod_date", "revision_list"],
                                "finding an item")
        revisions = []
        for uid, obj in objs.items():
            if (_text(obj, "item_id") or "").lower() != item.lower():
                continue
            rev_id = _text(obj, "item_revision_id")
            if rev_id is None:
                # An Item (not a revision): take its revisions.
                db, _ = _values(obj, "revision_list")
                rev_objs = self._properties([_uid(u) for u in db], ["item_id", "item_revision_id"], "finding an item")
                revisions += [(u, _text(o, "item_revision_id") or "") for u, o in rev_objs.items()]
            else:
                revisions.append((uid, rev_id))
        if rev:
            revisions = [r for r in revisions if r[1].lower() == rev.lower()]
        if not revisions:
            raise TeamcenterError(_ITEM_NOT_FOUND.format(ref=str(ref)[:80]))
        revisions.sort(key=lambda r: (len(r[1]), r[1]))
        return revisions[-1][0]

    def _rev_rows(self, uids) -> dict:
        """uid -> {item_id, revision, name, type, release_status} for item revisions."""
        objs = self._properties(uids, ["item_id", "item_revision_id", "object_name", "object_type",
                                       "release_status_list"])
        return {u: {"item_id": _text(o, "item_id"), "revision": _text(o, "item_revision_id"),
                    "name": _text(o, "object_name"), "type": _text(o, "object_type"),
                    "release_status": _text(o, "release_status_list")} for u, o in objs.items()}

    def _revision_rules(self) -> list[dict]:
        def load():
            resp = self._call("revision_rules", {}, "listing revision rules")
            uids = []
            for o in (resp or {}).get("output") or [] if isinstance(resp, dict) else []:
                u = _uid((o or {}).get("revRule")) if isinstance(o, dict) else _uid(o)
                if u:
                    uids.append(u)
            objs = self._properties(uids, ["object_name", "object_desc"], "listing revision rules")
            return [{"name": _text(objs.get(u), "object_name"), "description": _text(objs.get(u), "object_desc"),
                     "uid": u} for u in uids if u in objs]
        return self._cached("rules", load)

    def _rule_uid(self, name: str | None) -> str | None:
        wanted = " ".join(str(name or self.revision_rule).split()).lower()
        for rule in self._revision_rules():
            if (rule.get("name") or "").lower() == wanted:
                return rule["uid"]
        raise TeamcenterError(_RULE_NOT_FOUND.format(rule=str(name or self.revision_rule)[:80]))

    # ================================================================== meta / prompts
    @property
    def description(self):
        return f"Siemens Teamcenter at {self.host} — {planner_scope_note({'object_types': ','.join(self.object_types)})}" \
            + "\n\n" + self.system_prompt()

    def system_prompt(self):
        tables = list(self._tables_safe())
        object_tables = [t for t in tables if t not in FIXED_TABLES]
        example = object_tables[0] if object_tables else "item_revision"
        text = f"""
        ## Teamcenter Integration (Siemens PLM, read-only, live)
        Query Teamcenter via `execute_query(query)` where `query` is a JSON string. Tables: {', '.join(tables)}.

        Object tables (one per business object type) — filter, then aggregate in pandas:
        ```json
        {{"table": "{example}", "filter": {{"item_id": "PRT-1*", "release_status_list": "Released"}},
         "columns": ["item_id", "item_revision_id", "object_name", "owning_user", "last_mod_date"], "limit": 500}}
        ```
        - `filter` (optional): column -> value. Strings match whole values case-insensitively with `*`
          wildcards ("PRT-1*", "*bracket*"); `""` or null matches an empty value; ranges for dates and numbers:
          {{"last_mod_date": {{"gte": "2025-01-01"}}}}. Filters Teamcenter's saved query can take run on the server,
          the rest here — so always filter (an ID prefix, a type, a status) to stay under the row limit.
        - `columns` (optional): omit for all. `limit` (optional): default {DEFAULT_LIMIT}, at most {self.max_rows}.
        - `release_status_list` is empty for work in progress (unreleased); `uid` identifies an object.

        Relations:
        - BOM: {{"table": "bom", "root": "PRT-1000/B", "revision_rule": "{self.revision_rule}", "depth": 3}} — one row
          per line; level 1 = direct children; `quantity` is per parent; omit the revision for the newest one.
        - Where used: {{"table": "where_used", "item": "PRT-1001/A", "levels": "all"}} (default 1 = direct parents).
        - Change links: {{"table": "change_items", "change": "ECN-0042"}} or {{"table": "change_items",
          "filter": {{"CMClosure": "Open", "item_id": "PRT-1001"}}}} — one row per change and linked item; relation
          is problem | impacted | solution | reference; change_* columns describe the change (change_closure Open =
          not closed), item_* the linked item (item_release_status is the item's, not the change's).
        - {{"table": "revision_rules"}} lists the revision rules a BOM can use.

        Examples:
        ```python
        parts = client.execute_query('{{"table": "{example}", "filter": {{"item_id": "PRT-*"}}, "columns": ["item_id", "object_name", "release_status_list"]}}')
        bom = client.execute_query('{{"table": "bom", "root": "PRT-1000", "depth": 2}}')
        ```
        """
        if self.include_documents:
            text += (
                "\n        ### Documents\n"
                "        Specifications, drawings and reports attached to items are files, and their contents are\n"
                "        read with read_file(file_id). search_files(query) matches document names and the items they\n"
                "        are attached to: search with the item ID and a word or two of the name ('PRT-1000 spec'),\n"
                "        then read the hit. list_files(folder_id='PRT-1000/B') lists one revision's documents (no\n"
                "        folder_id: the identity's home folder).\n"
            )
        return text

    def _tables_safe(self) -> dict:
        """The tables without a server call: the discovered ones when cached, else the configured names."""
        hit = _META.get((self._key, "tables"))
        if hit and hit[0] > time.monotonic():
            return hit[1]
        return {**{table_name_for_type(t): {} for t in self.object_types}, **FIXED_TABLES}

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    def get_schema_text(self):
        return self.prompt_schema()

    # ================================================================== discovery
    def _table(self, name: str, spec: dict) -> Table:
        columns = [TableColumn(name=c, dtype=d, description=desc) for c, d, desc in spec["columns"]]
        pks = [TableColumn(name="uid", dtype="str")] if spec.get("type") else []
        return Table(name=name, description=spec.get("description"), columns=columns, pks=pks, fks=[])

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        """One table per configured business object type that exists on the server, plus the relation tables."""
        return [self._table(name, spec) for name, spec in self._tables().items()]

    def get_schema(self, table_name):
        spec = self._tables().get(table_name)
        return self._table(table_name, spec) if spec else None

    # ================================================================== test
    def test_connection(self):
        """Sign in, then check each capability separately so a partial setup says exactly what is missing."""
        try:
            ident = self._identity()
        except TeamcenterHTTPError as exc:
            logger.info("teamcenter test_connection failed: %s", exc.kind)
            result = {"success": False, "message": str(exc)}
            if exc.kind == "auth":
                result["login_refused"] = "rejected"
            return result
        except TeamcenterError as exc:
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            logger.warning("teamcenter test_connection failed unexpectedly: %s", type(exc).__name__)
            return {"success": False, "message": "Teamcenter connection test failed."}
        who = ident.get("user_name") or ident.get("user_id") or "?"
        if ident.get("user_name") and ident.get("user_id") and ident["user_name"] != ident["user_id"]:
            who = f"{ident['user_name']} ({ident['user_id']})"
        checks: dict[str, dict] = {}

        def check(name, fn):
            try:
                ok, detail = fn()
                checks[name] = {"ok": bool(ok), "detail": detail}
            except TeamcenterError as exc:
                checks[name] = {"ok": False, "detail": str(exc)}
            except Exception as exc:  # a check must never fail the whole test
                logger.warning("teamcenter check %s failed: %s", name, type(exc).__name__)
                checks[name] = {"ok": False, "detail": "The check failed unexpectedly."}

        def types_check():
            meta = self._type_meta()
            if meta is None:
                return True, "Type metadata service unavailable: standard columns are used."
            missing = [t for t in self.object_types if t not in meta]
            found = [t for t in self.object_types if t in meta]
            if not found:
                return False, "None of the configured object types exist: " + ", ".join(self.object_types)
            return True, ("Object types: " + ", ".join(found)
                          + (f" (not on this server: {', '.join(missing)})" if missing else ""))

        def queries_check():
            index = self._query_index()
            present = [q for q in self.saved_queries if q.lower() in index]
            if not present:
                return False, "None of the configured saved queries exist: " + ", ".join(self.saved_queries)
            typed = self._pick_query(["object_type"])
            if not typed:
                return False, _NO_TYPE_QUERY.format(queries=", ".join(self.saved_queries))
            return True, f"Saved query for objects: {typed[0]}"

        def rule_check():
            self._rule_uid(None)
            return True, f"Revision rule: {self.revision_rule}"

        def docs_check():
            entries = self._query_entries(self.dataset_query)
            if entries is None:
                return False, f"Saved query {self.dataset_query!r} for documents does not exist."
            return True, f"Document search: {self.dataset_query}"

        check("object_types", types_check)
        check("saved_queries", queries_check)
        check("revision_rule", rule_check)
        if self.include_documents:
            check("documents", docs_check)
        essential = checks.get("saved_queries", {}).get("ok") and checks.get("object_types", {}).get("ok")
        problems = [v["detail"] for v in checks.values() if not v["ok"]]
        message = f"Connected to Teamcenter at {self.host} as {who}."
        if problems:
            message += " " + " ".join(problems)
        result = {"success": bool(essential), "message": message,
                  "details": {"host": self.host, "identity": ident.get("user_id"), "display_name": ident.get("user_name"),
                              "auth_method": self.auth_method, "server_version": ident.get("version"),
                              "checks": checks}}
        if not essential:
            result["reachable"] = True
        return result

    # ================================================================== query
    def _parse_spec(self, query) -> dict:
        spec = query
        if not isinstance(query, dict):
            try:
                spec = json.loads(str(query))
            except (TypeError, ValueError, RecursionError):
                spec = None
        if isinstance(spec, str):
            spec = {"table": spec}
        if not isinstance(spec, dict) or not spec.get("table"):
            raise TeamcenterError(_BAD_SPEC)
        tables = self._tables()
        table = str(spec["table"]).strip().lower()
        if table not in tables:
            raise TeamcenterError(_UNKNOWN_TABLE.format(table=str(spec["table"])[:80], tables=", ".join(tables)))
        spec["table"] = table
        return spec

    def _limit(self, spec) -> int:
        raw = spec.get("limit")
        if raw is None:
            return min(DEFAULT_LIMIT, self.max_rows)
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            raise TeamcenterError(_BAD_LIMIT)
        if limit <= 0 or isinstance(raw, bool):
            raise TeamcenterError(_BAD_LIMIT)
        return min(limit, self.max_rows)

    def execute_query(self, query=None, **kwargs):
        """Run one JSON spec and return a DataFrame with the table's columns (see ``system_prompt``)."""
        spec = self._parse_spec(query if query is not None else kwargs)
        table, limit = spec["table"], self._limit(spec)
        tspec = self._tables()[table]
        all_columns = [c for c, _, _ in tspec["columns"]]
        types = {c: d for c, d, _ in tspec["columns"]}
        wanted = spec.get("columns") or spec.get("fields")
        if isinstance(wanted, str):
            wanted = [w.strip() for w in wanted.split(",") if w.strip()]
        if wanted:
            unknown = [w for w in wanted if w not in all_columns]
            if unknown:
                raise TeamcenterError(_UNKNOWN_COLUMNS.format(table=table, columns=", ".join(map(str, unknown))[:300],
                                                              available=", ".join(all_columns)))
        columns = list(dict.fromkeys(wanted)) if wanted else all_columns
        filters = self._parse_filter(spec.get("filter"), all_columns, types)
        if tspec.get("type"):
            rows = self._object_rows(tspec["type"], columns, types, filters, limit)
        elif table == "bom":
            rows = self._bom_rows(spec, limit)
        elif table == "where_used":
            rows = self._where_used_rows(spec, limit)
        elif table == "change_items":
            rows = self._change_rows(spec, filters, limit)
        else:
            rows = self._revision_rules()
        if not tspec.get("type") and filters:
            rows = [r for r in rows if self._matches(r, filters, types)]
        return self._frame(rows[:limit], columns, types)

    def _parse_filter(self, raw, columns, types) -> dict:
        if raw in (None, "", {}):
            return {}
        if not isinstance(raw, dict):
            raise TeamcenterError(_BAD_FILTER)
        unknown = [k for k in raw if k not in columns]
        if unknown and not (set(unknown) <= set(c for c, _ in CHANGE_COLUMNS + STANDARD_COLUMNS)):
            raise TeamcenterError(_UNKNOWN_COLUMNS.format(table="filter", columns=", ".join(map(str, unknown))[:300],
                                                          available=", ".join(columns)))
        out = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                ops = {k: v for k, v in value.items() if k in ("gt", "gte", "lt", "lte", "eq", "ne")}
                if not ops or len(ops) != len(value):
                    raise TeamcenterError(_BAD_FILTER)
                out[key] = ops
            elif isinstance(value, (str, int, float, bool)) or value is None:
                out[key] = value
            elif isinstance(value, list) and all(isinstance(v, (str, int, float)) for v in value):
                out[key] = {"in": [str(v) for v in value]}
            else:
                raise TeamcenterError(_BAD_FILTER)
        return out

    @staticmethod
    def _cmp_value(value, dtype):
        if value is None:
            return None
        if dtype == "datetime":
            ts = pd.to_datetime(value, utc=True, errors="coerce")
            return None if pd.isna(ts) else ts
        if dtype in ("int", "float"):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        return str(value).lower()

    def _matches(self, row: dict, filters: dict, types: dict) -> bool:
        for col, cond in filters.items():
            value = row.get(col)
            dtype = types.get(col, "str")
            if isinstance(cond, dict) and "in" in cond:
                if str(value or "").lower() not in {v.lower() for v in cond["in"]}:
                    return False
                continue
            if isinstance(cond, dict):
                have = self._cmp_value(value, dtype)
                if have is None:
                    return False
                for op, raw in cond.items():
                    want = self._cmp_value(raw, dtype)
                    if want is None:
                        raise TeamcenterError(_BAD_FILTER)
                    try:
                        ok = {"gt": have > want, "gte": have >= want, "lt": have < want, "lte": have <= want,
                              "eq": have == want, "ne": have != want}[op]
                    except TypeError:
                        raise TeamcenterError(_BAD_FILTER)
                    if not ok:
                        return False
                continue
            if cond is None or cond == "":
                if value not in (None, ""):
                    return False
                continue
            if isinstance(cond, bool):
                if bool(value) != cond:
                    return False
                continue
            if isinstance(cond, (int, float)) and dtype in ("int", "float"):
                if value is None or float(value) != float(cond):
                    return False
                continue
            text = str(value or "")
            # A multi-valued property (release statuses) matches when any of its values does.
            candidates = [text] + [p.strip() for p in text.split(",")] if "," in text else [text]
            if not any(fnmatch.fnmatchcase(c.lower(), str(cond).lower()) for c in candidates):
                return False
        return True

    def _frame(self, rows: list[dict], columns: list[str], types: dict) -> pd.DataFrame:
        df = pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)
        for col in columns:
            kind = types.get(col)
            if kind == "datetime":
                df[col] = pd.to_datetime(df[col], utc=True, errors="coerce", format="mixed")
            elif kind in ("int", "float") and not df.empty:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    # ---- object tables
    def _object_rows(self, type_name: str, columns: list[str], types: dict, filters: dict, limit: int) -> list[dict]:
        picked = self._pick_query(["object_type"])
        if not picked:
            raise TeamcenterError(_NO_TYPE_QUERY.format(queries=", ".join(self.saved_queries)))
        name, entries = picked
        criteria = [(self._entry_for(entries, "object_type"), type_name)]
        local: dict = {}
        for col, cond in filters.items():
            entry = self._entry_for(entries, col) if col != "object_type" else None
            if entry and isinstance(cond, (str, int, float)) and not isinstance(cond, bool) and str(cond) != "":
                # The saved query matches on the stored value (a user ID for owning_user), which the
                # display value read back may not show: a filter sent to Teamcenter is not re-checked here.
                criteria.append((entry, str(cond)))
            else:
                local[col] = cond
        # Without local filters the server's cap is the limit; with them, read up to max_rows and filter here.
        fetch = limit if not local else self.max_rows
        uids = self._run_query(name, criteria, fetch, "searching objects")
        needed = list(dict.fromkeys([c for c in columns if c != "uid"] + [c for c in local if c != "uid"]))
        objs = self._properties(uids, needed + ["object_type"], "reading objects")
        rows = []
        for uid in uids:
            obj = objs.get(uid)
            if obj is None:
                continue
            obj_type = _text(obj, "object_type") or obj.get("type")
            if obj_type and obj_type != type_name:
                continue  # a subtype the query also matched: it belongs to its own table
            row = {"uid": uid}
            for col in needed:
                row[col] = _cell(obj, col, types.get(col, "str"))
            if local and not self._matches(row, local, types):
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    # ---- BOM
    def _bom_rows(self, spec, limit) -> list[dict]:
        root_ref = spec.get("root") or spec.get("item")
        if not root_ref:
            raise TeamcenterError(_BOM_NEEDS_ROOT)
        try:
            depth = max(1, min(self.max_bom_depth, int(spec.get("depth") or self.max_bom_depth)))
        except (TypeError, ValueError):
            raise TeamcenterError(_BAD_LIMIT)
        root = self._find_revision(root_ref)
        rule = self._rule_uid(spec.get("revision_rule"))
        resp = self._call("create_bom_windows", {"info": [{
            "clientId": "bow", "item": {}, "itemRev": _ref(root), "bomView": {},
            "revRuleConfigInfo": {"clientId": "bow", "revRule": _ref(rule), "props": {}},
            "objectForConfigure": {}, "activeAssemblyArrangement": {}}]}, "opening the BOM")
        out = ((resp or {}).get("output") or [{}])[0] if isinstance(resp, dict) else {}
        window, top = _uid((out or {}).get("bomWindow")), _uid((out or {}).get("bomLine"))
        if not top:
            raise TeamcenterHTTPError("service:NoBOMLine", _SERVICE_ERROR.format(what="opening the BOM",
                                                                                  kind="no BOM line"))
        edges: list[tuple[str, str, str | None, int]] = []   # (parent line, child line, child revision uid, level)
        try:
            frontier, level = [top], 0
            line_rev: dict[str, str | None] = {top: root}
            while frontier and level < depth and len(edges) < limit:
                level += 1
                nxt = []
                for i in range(0, len(frontier), _EXPAND_BATCH):
                    resp = self._call("expand_one_level", {
                        "input": {"parentBomLines": [_ref(u) for u in frontier[i:i + _EXPAND_BATCH]],
                                  "excludeFilter": "None"},
                        "pref": {"expItemRev": True, "info": []}}, "expanding the BOM")
                    for o in (resp or {}).get("output") or [] if isinstance(resp, dict) else []:
                        parent = _uid(((o or {}).get("parent") or {}).get("bomLine"))
                        for child in (o or {}).get("children") or []:
                            line = _uid((child or {}).get("bomLine"))
                            if not line or not parent:
                                continue
                            line_rev[line] = _uid((child or {}).get("itemRevOfBOMLine"))
                            edges.append((parent, line, line_rev[line], level))
                            nxt.append(line)
                    if len(edges) >= limit:
                        break
                frontier = nxt
            edges = edges[:limit]
            # BOM lines live only as long as their window: read them before it closes.
            lines = self._properties([e[1] for e in edges], ["bl_quantity", "bl_sequence_no"], "reading BOM lines")
        finally:
            if window:
                try:
                    self._call("close_bom_windows", {"bomWindows": [_ref(window)]}, "closing the BOM")
                except TeamcenterError:
                    logger.info("teamcenter: closing a BOM window failed")
        revs = self._rev_rows([r for r in line_rev.values() if r])
        rows = []
        for parent, line, rev_uid, lvl in edges:
            info, pinfo = revs.get(rev_uid or "", {}), revs.get(line_rev.get(parent) or "", {})
            qty = _cell(lines.get(line), "bl_quantity", "float")
            rows.append({"level": lvl, "parent_item_id": pinfo.get("item_id"), "parent_revision": pinfo.get("revision"),
                         "item_id": info.get("item_id"), "revision": info.get("revision"), "name": info.get("name"),
                         "type": info.get("type"), "quantity": 1.0 if qty is None else qty,
                         "find_number": _cell(lines.get(line), "bl_sequence_no", "str"),
                         "release_status": info.get("release_status"), "uid": rev_uid})
        return rows

    # ---- where used
    def _where_used_rows(self, spec, limit) -> list[dict]:
        ref = spec.get("item") or spec.get("root")
        if not ref:
            raise TeamcenterError(_WHERE_USED_NEEDS_ITEM)
        levels = spec.get("levels", 1)
        num = -1 if str(levels).lower() == "all" else max(1, min(self.max_bom_depth, int(levels or 1)))
        target = self._find_revision(ref)
        try:
            rule = self._rule_uid(spec.get("revision_rule")) if spec.get("revision_rule") else None
        except TeamcenterError:
            raise

        def body(path):
            if path.startswith("Core-2012-02"):
                cfg = {"boolMap": {"whereUsedPreciseFlag": False}, "intMap": {"numLevels": num}}
                if rule:
                    cfg["tagMap"] = {"revision_rule": _ref(rule)}
                return {"input": [{"inputObject": _ref(target), "useLocalParams": False, "clientId": "bow",
                                   "inputParams": {}}], "configParams": cfg}
            return {"objects": [_ref(target)], "numLevels": num, "whereUsedPrecise": False,
                    "rule": _ref(rule) if rule else {}}
        resp = self._call("where_used", body, "reading where-used")
        found: list[tuple[str, int]] = []
        for o in (resp or {}).get("output") or [] if isinstance(resp, dict) else []:
            for info in (o or {}).get("info") or []:
                if not isinstance(info, dict):
                    continue
                parent = _uid(info.get("parentItemRev")) or _uid(info.get("parentObject"))
                if parent:
                    try:
                        lvl = int(info.get("level") or 1)
                    except (TypeError, ValueError):
                        lvl = 1
                    found.append((parent, lvl))
        found = list(dict.fromkeys(found))[:limit]
        revs = self._rev_rows([target] + [p for p, _ in found])
        me = revs.get(target, {})
        return [{"item_id": me.get("item_id"), "revision": me.get("revision"), "level": lvl,
                 "parent_item_id": revs.get(p, {}).get("item_id"), "parent_revision": revs.get(p, {}).get("revision"),
                 "parent_name": revs.get(p, {}).get("name"), "parent_type": revs.get(p, {}).get("type"),
                 "parent_release_status": revs.get(p, {}).get("release_status")} for p, lvl in found]

    # ---- change links
    def _change_rows(self, spec, filters, limit) -> list[dict]:
        tables = self._tables()
        meta = self._type_meta()
        change_types = [t["type"] for t in tables.values() if t.get("type") and self._is_change_type(t["type"], meta)]
        if not change_types:
            raise TeamcenterError(_NO_CHANGE_TABLES)
        if spec.get("change"):
            changes = [self._find_revision(spec["change"])]
        else:
            # Change-level filters (CMClosure, owning_user, ...) select the changes; item-level ones apply to rows.
            change_cols = {c for c, _ in STANDARD_COLUMNS + CHANGE_COLUMNS}
            change_filters = {k: v for k, v in filters.items() if k in change_cols and k not in
                              ("item_id",)}
            types = dict(STANDARD_COLUMNS + CHANGE_COLUMNS)
            changes = []
            for t in change_types:
                rows = self._object_rows(t, ["item_id"], types, change_filters, _MAX_CHANGES - len(changes))
                changes += [r["uid"] for r in rows]
                if len(changes) >= _MAX_CHANGES:
                    break
            for k in list(change_filters):
                filters.pop(k, None)
        objs = self._properties(changes, ["item_id", "item_revision_id", "object_name", "object_type", "CMMaturity",
                                          "CMClosure"] + list(CHANGE_RELATIONS), "reading change links")
        links: list[tuple[str, str, str]] = []
        for uid in changes:
            obj = objs.get(uid)
            for prop, relation in CHANGE_RELATIONS.items():
                db, _ = _values(obj, prop)
                links += [(uid, relation, _uid(v)) for v in db if _uid(v)]
        links = links[: limit * 2]
        revs = self._rev_rows([l[2] for l in links])
        rows = []
        for uid, relation, target in links:
            obj, info = objs.get(uid), revs.get(target)
            if info is None:
                continue
            rows.append({"change_id": _text(obj, "item_id"), "change_revision": _text(obj, "item_revision_id"),
                         "change_name": _text(obj, "object_name"), "change_type": _text(obj, "object_type"),
                         "change_maturity": _text(obj, "CMMaturity"), "change_closure": _text(obj, "CMClosure"),
                         "relation": relation, "item_id": info["item_id"], "item_revision": info["revision"],
                         "item_name": info["name"], "item_type": info["type"],
                         "item_release_status": info["release_status"]})
        return rows

    # ================================================================== documents
    def _require_docs(self):
        if not self.include_documents:
            raise TeamcenterError(_DOCS_DISABLED)

    def _datasets(self, uids, owner: str | None = None) -> list[dict]:
        """File entries for datasets whose file type this connection reads."""
        objs = self._properties(uids, ["object_name", "object_type", "last_mod_date", "ref_list"], "reading documents")
        files_of: dict[str, list[str]] = {}
        for uid, obj in objs.items():
            db, _ = _values(obj, "ref_list")
            files_of[uid] = [_uid(v) for v in db if _uid(v)]
        file_objs = self._properties([f for fs in files_of.values() for f in fs], ["original_file_name", "byte_size",
                                                                                   "file_size"], "reading documents")
        entries = []
        for uid in uids:
            obj = objs.get(uid)
            if obj is None:
                continue
            names = [(f, _text(file_objs.get(f), "original_file_name")) for f in files_of.get(uid, [])]
            readable = [(f, n) for f, n in names if n and _ext(n) in self.file_extensions]
            if not readable:
                continue
            fuid, fname = readable[0]
            title = _text(obj, "object_name") or fname
            name = title if _ext(title) == _ext(fname) else f"{title}.{_ext(fname)}"
            name = name.replace("/", "∕")
            size = _cell(file_objs.get(fuid), "byte_size", "int") or _cell(file_objs.get(fuid), "file_size", "int") or 0
            entries.append({"id": f"dataset:{uid}", "name": name, "path": f"{owner}/{name}" if owner else name,
                            "size": size, "modified_at": _cell(obj, "last_mod_date", "datetime"),
                            "mime_type": mimetypes.guess_type(name)[0], "is_folder": False, "kind": "document",
                            "dataset_type": _text(obj, "object_type")})
        return entries

    def search_files(self, query, limit=50, deadline=None, cancel_check=None, **_):
        """Documents for the words of a question, best matches first.

        Teamcenter can only search document names (the dataset saved query), so a
        question is searched three ways and the hits merged: the words as one
        name pattern, each distinctive word on its own, and — for every word that
        looks like an item ID (``PRT-1000``) — the documents attached to that
        item's newest revision. Hits are ranked by how many of the words their
        name and item carry.
        """
        self._require_docs()
        try:
            cap = min(int(50 if limit is None else limit), self.max_rows)
        except (TypeError, ValueError):
            raise TeamcenterError(_BAD_LIMIT)
        raw = [w for w in re.split(r"[\s*?,;:()\"']+", str(query or "")) if w][:24]
        words = [w for w in raw if len(w) >= 3 and w.lower() not in _SEARCH_STOPWORDS]
        if not words or cap <= 0:
            return []
        item_like = [w for w in words if _ITEM_ID_LIKE.match(w)]
        distinct = sorted(dict.fromkeys(words), key=lambda w: (w not in item_like, -len(w)))[:_SEARCH_WORDS]
        entries = self._query_entries(self.dataset_query)
        entry = self._entry_for(entries or [], "object_name")
        found: dict[str, dict] = {}

        def stopped():
            return cancel_check is not None and cancel_check()

        try:
            patterns = (["*" + "*".join(distinct) + "*"] if len(distinct) > 1 else []) + [f"*{w}*" for w in distinct]
            for pattern in patterns if entry else []:
                if stopped():
                    raise SearchStopped("cancelled", list(found.values()))
                uids = [u for u in self._run_query(self.dataset_query, [(entry, pattern)], cap * 3,
                                                   "searching documents") if f"dataset:{u}" not in found]
                for f in self._datasets(uids):
                    found[f["id"]] = f
                if len(found) >= cap * 3:
                    break
            for word in item_like[:3]:
                if stopped():
                    raise SearchStopped("cancelled", list(found.values()))
                try:
                    attached = self.list_files(word, limit=cap)
                except TeamcenterError:
                    continue      # not an item, or one this identity cannot see
                for f in attached:
                    found.setdefault(f["id"], f)["path"] = f["path"]
        except TeamcenterHTTPError as exc:
            if exc.kind == "timeout":
                raise SearchStopped("deadline", list(found.values())[:cap])
            raise
        lowered = [w.lower() for w in distinct]

        def score(f):
            text = (f.get("name", "") + " " + f.get("path", "")).lower()
            return sum(1 for w in lowered if w in text)

        ranked = sorted(found.values(), key=lambda f: (-score(f), f.get("name", "")))
        ranked = [f for f in ranked if score(f) > 0]
        if len(ranked) > cap:
            raise SearchStopped("max_results", ranked[:cap])
        return ranked

    def list_files(self, folder_id=None, recursive=None, limit=None, progress_callback=None, **_):
        """The home folder (no folder_id), a folder (``folder:<uid>``), or one item revision's documents (``PRT-1000/B``)."""
        self._require_docs()
        try:
            cap = min(int(200 if limit is None else limit), self.max_rows)
        except (TypeError, ValueError):
            raise TeamcenterError(_BAD_LIMIT)
        folder = str(folder_id or "").strip().strip("/")
        if folder.startswith("folder:") or not folder:
            fuid = folder[7:] if folder else self._identity().get("home_folder")
            if not fuid or not _UID_RE.match(fuid):
                raise TeamcenterError(_BAD_FOLDER)
            contents = self._properties([fuid], ["contents"], "listing a folder").get(fuid)
            db, _ = _values(contents, "contents")
            children = [_uid(v) for v in db if _uid(v)][: cap * 2]
            kinds = self._properties(children, ["object_name", "object_type", "item_id", "item_revision_id",
                                                "ref_list", "contents"], "listing a folder")
            out, datasets = [], []
            for uid in children:
                obj = kinds.get(uid)
                if obj is None:
                    continue
                item_id = _text(obj, "item_id")
                if _values(obj, "ref_list")[0] or (obj.get("className") or "").lower() == "dataset":
                    datasets.append(uid)
                elif item_id:
                    rev = _text(obj, "item_revision_id")
                    ref = f"{item_id}/{rev}" if rev else item_id
                    out.append({"id": ref, "name": f"{ref} {_text(obj, 'object_name') or ''}".strip().replace("/", "∕"),
                                "path": ref, "size": 0, "modified_at": None, "mime_type": None, "is_folder": True,
                                "kind": "item"})
                elif "contents" in ((obj.get("props") or {}) if isinstance(obj.get("props"), dict) else {}):
                    out.append({"id": f"folder:{uid}", "name": (_text(obj, "object_name") or uid).replace("/", "∕"),
                                "path": _text(obj, "object_name") or uid, "size": 0, "modified_at": None,
                                "mime_type": None, "is_folder": True, "kind": "folder"})
            return (out + self._datasets(datasets))[:cap]
        rev_uid = self._find_revision(folder)
        rel = self._properties([rev_uid], list(ATTACHMENT_RELATIONS), "listing documents").get(rev_uid)
        attached = []
        for prop in ATTACHMENT_RELATIONS:
            db, _ = _values(rel, prop)
            attached += [_uid(v) for v in db if _uid(v)]
        return self._datasets(list(dict.fromkeys(attached))[:cap * 2], owner=folder)[:cap]

    def _dataset_uid(self, file_id) -> str:
        text = str(file_id or "").strip()
        if text.lower().startswith("dataset:"):
            text = text[8:]
        if not _UID_RE.match(text):
            raise TeamcenterError(_BAD_FILE_ID)
        return text

    def read_raw_bytes(self, file_id, *, max_bytes=None):
        self._require_docs()
        uid = self._dataset_uid(file_id)
        entries = self._datasets([uid])
        if not entries:
            obj = self._properties([uid], ["object_name"], "reading a document").get(uid)
            if obj is None:
                raise TeamcenterError(_NO_FILE)
            raise GlobScopeError(_FILE_TYPE.format(exts=", ".join(sorted(self.file_extensions))))
        entry = entries[0]
        limit = min(self._max_bytes, max_bytes) if max_bytes is not None else self._max_bytes
        if limit < 1 or (entry["size"] or 0) > limit:
            raise FileTooLargeError(_FILE_TOO_LARGE)
        refs = self._properties([uid], ["ref_list"], "reading a document").get(uid)
        db, _ = _values(refs, "ref_list")
        files = self._properties([_uid(v) for v in db if _uid(v)], ["original_file_name"], "reading a document")
        fuid, fname = next(((f, _text(o, "original_file_name")) for f, o in files.items()
                            if _ext(_text(o, "original_file_name")) in self.file_extensions), (None, None))
        if not fuid:
            raise TeamcenterError(_NO_FILE)
        resp = self._call("read_tickets", {"files": [_ref(fuid, "ImanFile")]}, "reading a document")
        ticket = None
        for key, value in _pairs((resp or {}).get("tickets") if isinstance(resp, dict) else None):
            if _uid(key) == fuid or key == fuid or ticket is None:
                ticket = str(value) if value else ticket
        if not ticket:
            raise TeamcenterError(_NO_FILE)
        content = self._download(fname, ticket, limit)
        return content, entry["name"], entry["mime_type"]

    def _download(self, name: str, ticket: str, limit: int) -> bytes:
        session = self._session()
        url = f"{self._fms}/fmsdownload/{quote(name or 'file')}?ticket={quote(ticket, safe='')}"
        with session.lock:
            if session.http is None or not session.logged_in:
                self._login(session)
            try:
                with session.http.get(url, timeout=(10, 120), allow_redirects=False, stream=True) as resp:
                    if resp.status_code != 200:
                        raise TeamcenterHTTPError("download", _SERVICE_ERROR.format(
                            what="downloading the document", kind=f"HTTP {resp.status_code}"), resp.status_code)
                    if int(resp.headers.get("Content-Length") or 0) > limit:
                        raise FileTooLargeError(_FILE_TOO_LARGE)
                    chunks, size = [], 0
                    for chunk in resp.iter_content(65536):
                        size += len(chunk)
                        if size > limit:
                            raise FileTooLargeError(_FILE_TOO_LARGE)
                        chunks.append(chunk)
                    return b"".join(chunks)
            except requests.exceptions.SSLError:
                raise TeamcenterHTTPError("tls", _TLS)
            except requests.exceptions.Timeout:
                raise TeamcenterHTTPError("timeout", _TIMEOUT)
            except requests.exceptions.RequestException:
                raise TeamcenterHTTPError("unreachable", _UNREACHABLE)

    def read_file(self, file_id, sheet=None, max_bytes=None, page_range=None, **_):
        from app.data_sources.clients.graph_drive_client import TEXT_EXTS, _extract_pdf_pages_from_bytes, _trim_to_data
        content, name, mime = self.read_raw_bytes(file_id, max_bytes=max_bytes)
        ext = _ext(name)
        if page_range is not None:
            if ext != "pdf" or page_range[0] < 1 or page_range[1] < page_range[0]:
                raise TeamcenterError("page_range requires a PDF and a positive, ordered page interval.")
            text, total = _extract_pdf_pages_from_bytes(content, name, *page_range)
            return {"__doc_pages__": True, "text": text, "pages_total": total,
                    "first": page_range[0], "last": min(page_range[1], total), "raw": content, "name": name}
        if ext in DOC_EXTS:
            text = extract_document_text_from_bytes(content, name)
            if doc_text_is_usable(text, ext):
                return DocumentText(text, raw=content, name=name, mime=mime)
            return NamedBytes(content, name=name, mime=mime)
        if ext in ("csv", "tsv"):
            return _trim_to_data(pd.read_csv(io.BytesIO(content), sep="\t" if ext == "tsv" else ",", header=None))
        if ext in EXCEL_EXTS:
            return read_excel_sheet(content, sheet)
        if ext == "json":
            return json.loads(content.decode("utf-8-sig"))
        if ext in TEXT_EXTS:
            return content.decode("utf-8-sig", errors="replace")
        return NamedBytes(content, name=name, mime=mime)
