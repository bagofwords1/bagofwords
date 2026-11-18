"""Confinement for connectors that run SQL in an engine inside the API process.

DuckDB (duckdb / csv / qvd / pbix / iceberg / Power BI Report Server models)
and SQLite execute in-process, so a statement can reach whatever the app user
can: other files, the network, the app's own database. Two rules keep a
connection to the data it was configured with:

  * **Path policy** (`check_local_path`): a configured local path must not
    resolve into the app's own files or a system / secret location.
  * **Session lock** (`lock_connection`): once a connection's own views exist,
    the DuckDB session may only touch that connection's files or URI prefixes,
    cannot load extensions, and cannot change its configuration back.

`security.unsafe_allow_unrestricted_file_connectors` turns both off.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parents[3]
_INSTALL_DIR = _BACKEND_DIR.parent

# System locations that hold process state, host configuration or mounted
# secrets (Docker / Kubernetes secrets, service-account tokens). Ordinary data
# locations (/data, /mnt, /srv, /var/lib/<x>, /opt, /home/<user>, /root/<x>)
# are not here; credential stores inside them are refused by name below.
_SYSTEM_DIRS = ("/proc", "/sys", "/dev", "/etc", "/run/secrets", "/var/run/secrets")

# The app's own code and logs, relative to the backend directory. A directory
# the operator mounts next to them (backend/<their-data>) is not listed, and
# neither are backend/db and backend/data as a whole: only the app database
# and the encryption key inside them are protected, as files.
_APP_SUBDIRS = ("app", "alembic", "tests", "branding_uploads", "logs")
# Next to the backend directory: config files and TLS material.
_INSTALL_SUBDIRS = ("configs", "certs")
# What the app itself keeps under uploads/ (the data volume in the shipped
# compose file and chart). Anything else an operator puts there is theirs.
_UPLOADS_SUBDIRS = (
    "files", "pptx", "pptx_previews", "thumbnails", "pdfs", "branding", "avatars",
    "artifact_files", "artifact_js", "git_state", "repo_cache", "filecache", "fast",
    "qvd_cache", "pbix_file_cache", "pbix_data_cache", "pbirs_schema_cache",
    "network_dir_tables", "system-logs", ".storage-tmp", "demo_data",
)

# Sample data the app ships inside its own tree: bundled demo databases and
# the test fixtures. Open to every organization.
_SHARED_APP_DATA = ("demo-datasources", "tests/config")
# Generated demo datasets live in uploads/demo_data/<organization id>/.
_DEMO_DATA = ("uploads", "demo_data")

# Per-user credential stores, wherever the home directory is.
_SECRET_DIR_NAMES = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure"})
_SECRET_DIR_PAIRS = frozenset({(".config", "gcloud")})
_SECRET_FILE_PREFIXES = (".env", ".netrc", ".pgpass", ".git-credentials", ".encryption.key", "known_hosts")
_CONFIG_FILE_NAME = re.compile(r"^bow-config.*\.ya?ml$", re.IGNORECASE)

_GLOB_CHARS = re.compile(r"[*?\[]")
_URI_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://")

_REFUSED = "points at a protected system or application location"


_settings_unavailable = False


def _bow_config():
    """The loaded config, or None where the app settings cannot be built
    (a bare script). A failed import is not retried: it re-reads the config
    files every time."""
    global _settings_unavailable
    if _settings_unavailable:
        return None
    try:
        from app.settings.config import settings

        return settings.bow_config
    except Exception:
        _settings_unavailable = True
        return None


def _security_setting(name: str):
    cfg = _bow_config()
    return getattr(getattr(cfg, "security", None), name, None)


def unrestricted() -> bool:
    """True when the operator restored the pre-hardening behaviour."""
    return bool(_security_setting("unsafe_allow_unrestricted_file_connectors"))


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _real(path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def _app_database_file() -> Optional[Path]:
    cfg = _bow_config()
    url = (getattr(getattr(cfg, "database", None), "url", None) if cfg else None) or os.environ.get(
        "BOW_DATABASE_URL", ""
    )
    if not url.startswith("sqlite"):
        return None
    raw = url.split("///", 1)[1] if "///" in url else ""
    raw = raw.split("?", 1)[0]
    return _real(raw) if raw and raw != ":memory:" else None


def _protected_app_files() -> List[Path]:
    """Single files that hold app state or secrets, wherever they live: the
    SQLite app database with its sidecars, the loaded config file and the
    encryption key. Their folders may hold customer data, so only the files
    are protected."""
    out: List[Path] = []
    db_file = _app_database_file()
    if db_file is not None:
        out += [db_file.with_name(db_file.name + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    config_path = os.environ.get("BOW_CONFIG_PATH")
    out.append(_real(config_path) if config_path else _real(_INSTALL_DIR / "bow-config.yaml"))
    out.append(_real(os.environ.get("BOW_ENCRYPTION_KEY_FILE") or _BACKEND_DIR / "data" / "encryption.key"))
    return out


def pattern_reaches_protected_file(pattern: str) -> bool:
    """Whether a glob pattern can match one of the app's protected files."""
    base, has_glob = pattern_base(pattern)
    if not has_glob:
        return False
    real_base = _real(base)
    tail = pattern[len(base):].lstrip("/") if base not in (".", "") and pattern.startswith(base) else pattern
    for f in _protected_app_files():
        if _is_within(f, real_base) and fnmatch.fnmatch(str(f.relative_to(real_base)), tail):
            return True
    return False


def organization_roots(organization_id: Optional[str] = None) -> Optional[List[Path]]:
    """The roots the operator assigned to this organization, or None when it
    has no entry (or the organization is not known)."""
    mapping = _security_setting("organization_file_roots")
    if not mapping:
        return None
    org = _organization_id(organization_id)
    if not org or org not in mapping:
        return None
    return [_real(r) for r in mapping[org] if r]


def check_organization_roots(real: Path, organization_id: Optional[str] = None) -> None:
    """``security.organization_file_roots``: a listed root is reserved for the
    organizations it is listed for, and an organization with roots stays
    inside them. Raises ValueError. ``real`` is already resolved."""
    mapping = _security_setting("organization_file_roots")
    if not mapping:
        return
    org = _organization_id(organization_id)
    own = [_real(r) for r in mapping.get(org, []) if r] if org and org in mapping else None
    if own is not None:
        if not any(_is_within(real, r) for r in own):
            raise ValueError("Path is outside the directories assigned to this organization")
        return
    for owner, roots in mapping.items():
        if owner != org and any(_is_within(real, _real(r)) for r in roots if r):
            raise ValueError("Path is inside a directory assigned to another organization")


def _protected_app_locations() -> List[Path]:
    """Directories where this deployment keeps its own code, state and logs."""
    from app.services.storage import keys as storage_keys

    out = [_BACKEND_DIR / d for d in _APP_SUBDIRS]
    out += [_INSTALL_DIR / d for d in _INSTALL_SUBDIRS]
    for root in storage_keys._roots():
        out += [Path(root) / d for d in _UPLOADS_SUBDIRS]
    out += [Path(root) for root, _area in storage_keys._mapped_roots()]
    cfg = _bow_config()
    logs_dir = getattr(getattr(cfg, "system_logs", None), "directory", None) or os.environ.get("BOW_SYSTEM_LOGS_DIR")
    if logs_dir:
        out.append(Path(logs_dir))
    return list(dict.fromkeys(_real(p) for p in out))


def organization_of(client) -> Optional[str]:
    """The organization a client was built for (see ``bind_organization``)."""
    return getattr(client, "_bow_organization_id", None)


def _organization_id(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return str(explicit)
    try:
        from app.core.request_org import current_request_organization_id

        return current_request_organization_id()
    except Exception:
        return None


def bind_organization(client, organization_id):
    """Record the organization that owns the connection a client was built
    for, so connect-time checks hold outside a request too. Returns client."""
    if organization_id:
        try:
            client._bow_organization_id = str(organization_id)
        except Exception:
            pass
    return client


def _is_app_data(real: Path, organization_id: Optional[str]) -> bool:
    """Sample data inside the app tree that a connection may read: the shared
    samples, and the generated datasets of the connection's own organization."""
    if any(_is_within(real, _real(_BACKEND_DIR / d)) for d in _SHARED_APP_DATA):
        return True
    org = _organization_id(organization_id)
    if not org or not re.fullmatch(r"[A-Za-z0-9_\-]+", org):
        return False
    roots = {_real(_BACKEND_DIR.joinpath(*_DEMO_DATA)), _real(os.path.join(os.getcwd(), *_DEMO_DATA))}
    return any(real != r / org and _is_within(real, r / org) for r in roots)


def check_local_path(path: str, *, contains: bool = False, organization_id: Optional[str] = None) -> Path:
    """Resolve ``path`` (symlinks followed) and refuse protected locations.

    Raises ValueError. ``contains=True`` is for a directory the engine will be
    allowed to read as a whole: it is also refused when a protected location
    sits below it. ``organization_id`` is the organization that owns the
    connection (default: the one the current request was authorized against);
    generated demo data is only reachable by its own organization.

    With ``security.file_connector_allowed_roots`` set the path must also be
    inside one listed root.
    """
    real = _real(path)
    if unrestricted() or _is_app_data(real, organization_id):
        return real

    def hit(base: Path) -> bool:
        return real == base or _is_within(real, base) or (contains and _is_within(base, real))

    allowed = _security_setting("file_connector_allowed_roots")
    if allowed and not any(_is_within(real, _real(a)) for a in allowed if a):
        raise ValueError("Path is outside the allowed file connector roots")
    check_organization_roots(real, organization_id)
    if real in _protected_app_files():
        raise ValueError(f"Path {_REFUSED}")

    for d in _SYSTEM_DIRS:
        if hit(Path(d).resolve()) or hit(Path(d)):
            raise ValueError(f"Path {_REFUSED}")
    for base in _protected_app_locations():
        if hit(base):
            raise ValueError(f"Path {_REFUSED}")

    parts = real.parts
    if any(p in _SECRET_DIR_NAMES for p in parts) or any(
        pair in _SECRET_DIR_PAIRS for pair in zip(parts, parts[1:])
    ):
        raise ValueError(f"Path {_REFUSED}")
    if real.name.startswith(_SECRET_FILE_PREFIXES) or _CONFIG_FILE_NAME.match(real.name):
        raise ValueError(f"Path {_REFUSED}")
    return real


def pattern_base(pattern: str) -> Tuple[str, bool]:
    """(literal directory prefix, has_glob) of a path or glob pattern."""
    m = _GLOB_CHARS.search(pattern)
    if not m:
        return pattern, False
    head = pattern[: m.start()]
    base = head.rsplit("/", 1)[0] if "/" in head else ""
    return (base or ("/" if head.startswith("/") else ".")), True


def check_file_patterns(patterns: Iterable[str], organization_id: Optional[str] = None) -> None:
    """Config-time check for newline-separated path / glob settings whose
    matches are opened one file at a time (csv, qvd, pbix). Raises ValueError."""
    for pattern in patterns:
        base, _ = pattern_base(pattern)
        check_local_path(base, organization_id=organization_id)
        if pattern_reaches_protected_file(pattern):
            raise ValueError(f"Pattern {_REFUSED}")


def _lines(value) -> List[str]:
    return [p.strip() for p in str(value or "").splitlines() if p.strip()]


def check_connection_config(conn_type: str, config: dict) -> None:
    """Save/test-time path policy for a file-based connection's config.
    Raises ValueError naming the offending field. Enforced again at connect."""
    if unrestricted() or not isinstance(config, dict):
        return
    try:
        if conn_type == "sqlite":
            field = "database"
            database = str(config.get("database") or "").strip()
            if database and database != ":memory:":
                check_local_path(database)
        elif conn_type == "duckdb":
            field = "database"
            database = str(config.get("database") or "").strip()
            if database:
                check_local_path(database)
                return
            field = "uris"
            uris = _lines(config.get("uris"))
            db_files = [u for u in uris if u.lower().endswith((".duckdb", ".db"))]
            if db_files:
                check_local_path(local_path_of(db_files[0]))
            else:
                sources_for_patterns(uris)
        elif conn_type in ("csv", "qvd", "pbix"):
            field = "file_paths"
            check_file_patterns(_lines(config.get("file_paths")))
    except ValueError as e:
        raise ValueError(f"{field}: {e}") from e


def filter_files(files: Iterable[str], kind: str, organization_id: Optional[str] = None) -> List[str]:
    """Drop resolved files the policy refuses (a broad glob can sweep in a
    protected file, or a symlink can point at one)."""
    kept = []
    for f in files:
        try:
            check_local_path(f, organization_id=organization_id)
        except ValueError:
            logger.warning("%s: skipping a file in a protected location", kind)
            continue
        kept.append(f)
    return kept


def is_remote_uri(pattern: str) -> bool:
    m = _URI_SCHEME.match(pattern)
    return bool(m) and m.group(1).lower() != "file"


# The engine compares a URL with its allowed prefixes as plain text, so a
# `..` segment (or an encoded dot / separator the server decodes) would step
# out of the prefix on the remote side.
_URL_TRAVERSAL = re.compile(r"(?:^|[/\\])\.\.(?:[/\\?#]|$)|%2e|%2f|%5c|%25", re.IGNORECASE)


def has_url_traversal(url: str) -> bool:
    return bool(_URL_TRAVERSAL.search(str(url)))


def local_path_of(pattern: str) -> str:
    """Filesystem path of a local pattern (`file://` and `file:` stripped)."""
    lower = pattern.lower()
    if lower.startswith("file://"):
        return pattern[7:]
    if lower.startswith("file:"):
        return pattern[5:]
    return pattern


# DuckDB reads `?s3_endpoint=...&s3_use_ssl=...` on an object-store URL as
# per-request overrides of where and how the request is sent.
_URL_QUERY = re.compile(r"[?#][^/]*=")
_URL_S3_OVERRIDE = re.compile(r"[?&#]s3_\w+=", re.IGNORECASE)


def has_url_override(url: str) -> bool:
    """A query string that redirects an object-store request. Plain http(s)
    URLs may carry one (presigned URLs, SAS tokens) as long as it sets no
    `s3_*` parameter; `?` on its own stays a glob character."""
    m = _URI_SCHEME.match(str(url))
    if not m:
        return False
    if m.group(1).lower() in ("http", "https"):
        return bool(_URL_S3_OVERRIDE.search(url))
    return bool(_URL_QUERY.search(url))


def sources_for_patterns(
    patterns: Iterable[str], organization_id: Optional[str] = None
) -> Tuple[List[str], List[str]]:
    """(allowed_paths, allowed_directories) for DuckDB URI patterns the engine
    expands itself. Local ones pass the path policy first (ValueError)."""
    paths: List[str] = []
    dirs: List[str] = []
    for pattern in patterns:
        if is_remote_uri(pattern):
            if has_url_traversal(pattern):
                raise ValueError("A remote URI may not contain '..' segments or encoded path separators")
            if has_url_override(pattern):
                raise ValueError("A remote URI may not carry endpoint or connection parameters in a query string")
            base, has_glob = pattern_base(pattern)
            if has_glob:
                dirs.append(base.rstrip("/") + "/")
            else:
                paths.append(pattern)
            continue
        local = local_path_of(pattern)
        base, has_glob = pattern_base(local)
        if has_glob:
            real = check_local_path(base, contains=True, organization_id=organization_id)
            if pattern_reaches_protected_file(local):
                raise ValueError(f"Pattern {_REFUSED}")
            dirs.extend({os.path.abspath(base), str(real)})
        else:
            real = check_local_path(local, organization_id=organization_id)
            paths.extend({pattern, local, os.path.abspath(local), str(real)})
    return paths, dirs


def database_file_sources(db_file: str, extra_patterns: Iterable[str] = (),
                          organization_id: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """What a session opened on a database file may read besides the file:
    the file's own folder (views in it commonly read files kept next to it)
    and any further locations the connection lists. Raises ValueError when
    the database file itself is refused."""
    real = check_local_path(db_file, organization_id=organization_id)
    paths: List[str] = []
    dirs: List[str] = []
    try:
        folder = check_local_path(str(real.parent), contains=True, organization_id=organization_id)
        if not any(_is_within(f, folder) for f in _protected_app_files() if f != real):
            dirs += [str(folder), os.path.dirname(os.path.abspath(db_file))]
    except ValueError:
        pass  # the folder holds something protected: the file alone is served
    try:
        more_paths, more_dirs = sources_for_patterns(extra_patterns, organization_id)
        paths += more_paths
        dirs += more_dirs
    except ValueError:
        logger.warning("duckdb: an additional location on this connection is refused by the path policy")
    return paths, dirs


# Opening a database file: these can only be given at open time for the
# settings to hold for every handle on that file.
FILE_DATABASE_CONFIG = {"enable_external_access": False, "lock_configuration": True}

ACCESS_HELP = (
    "This connection can only read the files or locations it is configured with. "
    "For a .duckdb file that is the file's own folder plus any location listed in the connection's URIs; "
    "add the location there (it must pass the path policy), or set "
    "security.unsafe_allow_unrestricted_file_connectors to lift the restriction for the whole instance."
)


def explain_access_error(exc: Exception) -> Optional[str]:
    """A message for the engine's refusal that says what was refused and how
    to allow it; None when ``exc`` is some other error."""
    text = str(exc)
    if unrestricted() or "disabled by configuration" not in text and "disabled through configuration" not in text:
        return None
    return f"{text.splitlines()[0]} {ACCESS_HELP}"


def _sql_list(values: Iterable[str]) -> str:
    return "[" + ", ".join("'" + str(v).replace("'", "''") + "'" for v in values) + "]"


def lock_connection(con, *, paths: Iterable[str] = (), directories: Iterable[str] = ()) -> None:
    """Seal a DuckDB session after trusted code created its views.

    Only ``paths`` (exact files / URLs) and ``directories`` (path or URL
    prefixes) stay reachable; extensions can no longer be installed or loaded
    and the settings cannot be changed back.
    """
    if unrestricted():
        return
    try:
        # Several handles on one database file share its settings: a sibling
        # that got here first has sealed them for this one too.
        if con.execute("SELECT current_setting('lock_configuration')").fetchone()[0]:
            return
    except Exception:
        pass
    paths = list(dict.fromkeys(str(p) for p in paths if p))
    directories = list(dict.fromkeys(str(d) for d in directories if d))
    try:
        # The secret manager reads its on-disk store the first time a remote
        # file is opened; that must happen before file access is closed.
        con.execute("SELECT count(*) FROM duckdb_secrets()").fetchall()
    except Exception:
        pass
    if paths:
        con.execute(f"SET allowed_paths={_sql_list(paths)}")
    if directories:
        con.execute(f"SET allowed_directories={_sql_list(directories)}")
    con.execute("SET python_enable_replacements=false")
    con.execute("SET allow_unredacted_secrets=false")
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")


# -- what a statement may name, by the engine's own parser ---------------------

_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_PATH_LIKE = re.compile(
    r"[/\\:]|\.(?:csv|tsv|txt|parquet|pq|json|jsonl|ndjson|gz|zst|bz2|db|duckdb|sqlite|sqlite3|xlsx?|arrow|avro|orc)$",
    re.IGNORECASE,
)
# Built-in table functions that read nothing outside the session.
TABLE_FUNCTIONS_ALLOWED = frozenset({
    "range", "generate_series", "unnest", "generate_subscripts", "repeat", "repeat_row",
    "json_each", "json_tree",
    "duckdb_tables", "duckdb_columns", "duckdb_schemas", "duckdb_types",
    "pragma_table_info", "pragma_show",
})
_CATALOG_RELATIONS_ALLOWED = frozenset({"duckdb_tables", "duckdb_columns", "duckdb_schemas", "duckdb_types"})
# Functions that report session settings, secrets or the process environment.
_STATE_FUNCTIONS = frozenset({
    "current_setting", "which_secret", "getenv", "getvariable", "duckdb_settings", "duckdb_secrets",
})
_SHOW_ALLOWED = frozenset({'"tables"', "tables", "__show_tables_expanded"})
_PRAGMA_NO_ARG = re.compile(r"^\s*PRAGMA\s+(?:show_tables|show_tables_expanded)\s*;?\s*$", re.IGNORECASE)
_PRAGMA_ONE_ARG = re.compile(
    r"^\s*PRAGMA\s+(?:table_info|show|storage_info)\s*\(\s*(['\"]?)([^'\"()]+)\1\s*\)\s*;?\s*$", re.IGNORECASE
)
_EXPLAIN_PREFIX = re.compile(r"^\s*EXPLAIN\s+(?:ANALYZE\s+)?", re.IGNORECASE)
_STRING_SYNTAX_SQLGLOT_MISREADS = re.compile(r"\$[A-Za-z_]*\$|\b[eE]'")

_NOT_A_TABLE = "Query the connection's tables by name; files, URLs and system state are not available."
_scratch = None
_scratch_lock = threading.Lock()


def _refuse(message: str = _NOT_A_TABLE):
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    raise ReadOnlyViolation(message)


def _serialized(sql: str) -> dict:
    """DuckDB's parse tree for one SELECT-type statement, without running it."""
    import duckdb

    global _scratch
    with _scratch_lock:
        if _scratch is None:
            _scratch = duckdb.connect(":memory:")
        raw = _scratch.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
    return json.loads(raw)


def session_relations(con) -> frozenset:
    """Names of the tables and views a session serves, lowercased."""
    rows = con.execute("SELECT table_name FROM information_schema.tables").fetchall()
    return frozenset(str(r[0]).lower() for r in rows)


def _check_relation(name: str, schema: str, catalog: str, relations, ctes) -> None:
    """A relation is one of the session's own, a CTE of the statement, or a
    plain identifier (which the engine resolves in its catalog and never as a
    file). A string, path, URL or file name is not a relation."""
    lowered = (name or "").lower()
    qualifiers_plain = all(not q or _PLAIN_NAME.match(q) for q in (schema, catalog))
    if not name or not qualifiers_plain:
        _refuse()
    if not schema and not catalog and lowered in ctes:
        return
    if relations is not None and lowered in relations:
        return
    if _PLAIN_NAME.match(name):
        if lowered.startswith("duckdb_") and lowered not in _CATALOG_RELATIONS_ALLOWED:
            _refuse()
        return
    # Not plain and not a known relation. Without the session's list (the
    # check ran before connecting) an unusual but path-free name is left for
    # the session check to decide.
    if relations is None and not _PATH_LIKE.search(name):
        return
    _refuse()


def _check_function(name: str, table_function: bool) -> None:
    from app.ai.code_execution.sql_guard import _DUCKDB_FILE_FUNCTIONS

    lowered = (name or "").lower()
    if table_function and lowered not in TABLE_FUNCTIONS_ALLOWED:
        _refuse()
    if lowered in _STATE_FUNCTIONS or _DUCKDB_FILE_FUNCTIONS.match(lowered):
        _refuse()


def _walk_serialized(node, relations, ctes) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_serialized(item, relations, ctes)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    if kind == "BASE_TABLE":
        _check_relation(node.get("table_name") or "", node.get("schema_name") or "",
                        node.get("catalog_name") or "", relations, ctes)
    elif kind == "TABLE_FUNCTION":
        fn = node.get("function")
        if not isinstance(fn, dict) or fn.get("class") != "FUNCTION":
            _refuse()
        _check_function(fn.get("function_name"), table_function=True)
    elif kind == "SHOW_REF":
        if node.get("show_type") == "SHOW_UNQUALIFIED" and (node.get("table_name") or "") not in _SHOW_ALLOWED:
            _refuse()
    if node.get("class") == "FUNCTION":
        _check_function(node.get("function_name"), table_function=False)
    for value in node.values():
        _walk_serialized(value, relations, ctes)


def _cte_names(node, out: set) -> set:
    if isinstance(node, list):
        for item in node:
            _cte_names(item, out)
    elif isinstance(node, dict):
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map") or []:
                if isinstance(entry, dict) and entry.get("key"):
                    out.add(str(entry["key"]).lower())
        for value in node.values():
            _cte_names(value, out)
    return out


def _check_pivot_statement(sql: str, relations) -> None:
    """A dynamic PIVOT cannot be serialized by the engine. Its relations are
    read from the SQL guard's tree instead, and the string syntaxes that tree
    does not read the engine's way are refused outright."""
    import sqlglot
    from sqlglot import exp

    if _STRING_SYNTAX_SQLGLOT_MISREADS.search(sql):
        _refuse()
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        _refuse()
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for node in tree.walk():
        if isinstance(node, exp.Table):
            if isinstance(node.this, exp.Func):
                name = node.this.name if isinstance(node.this, exp.Anonymous) else node.this.sql_name()
                _check_function(name, table_function=True)
            else:
                _check_relation(node.name, node.db or "", node.catalog or "", relations, ctes)
        elif isinstance(node, exp.Func):
            _check_function(node.name if isinstance(node, exp.Anonymous) else node.sql_name(), table_function=False)


def engine_tree_check(sql: str, relations=None) -> None:
    """Decide, from DuckDB's own reading of the statement, that it is one
    read whose relations and functions stay inside the session. ``relations``
    is the session's table/view names when known. Raises ReadOnlyViolation;
    fails closed on anything it cannot read."""
    import duckdb

    try:
        kinds = [s.type for s in duckdb.extract_statements(sql)]
    except duckdb.Error:
        return  # not runnable: the engine reports the syntax error itself
    if (
        kinds == [duckdb.StatementType.CREATE, duckdb.StatementType.SELECT]
        and re.search(r"\bPIVOT\b", sql, re.IGNORECASE)
    ):
        # One statement that DuckDB expands into a temporary enum plus a SELECT.
        _check_pivot_statement(sql, relations)
        return
    if len(kinds) != 1:
        _refuse("Only a single read-only SELECT statement is allowed.")
    if kinds[0] == duckdb.StatementType.EXPLAIN:
        m = _EXPLAIN_PREFIX.match(sql)
        if not m:
            _refuse("Only a single read-only SELECT statement is allowed.")
        inner = sql[m.end():]
        try:
            inner_kinds = [s.type for s in duckdb.extract_statements(inner)]
        except duckdb.Error:
            inner_kinds = []
        if inner_kinds != [duckdb.StatementType.SELECT]:
            _refuse("Only a single read-only SELECT statement is allowed.")
        engine_tree_check(inner, relations)
        return
    if kinds[0] != duckdb.StatementType.SELECT:
        _refuse("Only a single read-only SELECT statement is allowed.")
    tree = _serialized(sql)
    if tree.get("error"):
        if _PRAGMA_NO_ARG.match(sql):
            return
        m = _PRAGMA_ONE_ARG.match(sql)
        if m:
            _check_relation(m.group(2).strip(), "", "", relations, frozenset())
            return
        _refuse("The statement could not be verified as a read. Only SELECT queries are allowed.")
    _walk_serialized(tree, relations, _cte_names(tree, set()))


def check_user_sql(sql: str, *, hide_settings: bool = False, relations=None) -> None:
    """Read-only check on SQL a connector is about to run in-process, for
    callers that do not come through the code-execution wrapper. With
    ``relations`` (see ``session_relations``) every relation the statement
    names must be one of the session's own."""
    if unrestricted() or not isinstance(sql, str) or not sql.strip():
        return
    from app.ai.code_execution.sql_guard import check_sql

    check_sql(sql, "duckdb")
    if relations is not None:
        engine_tree_check(sql, relations)


def check_session_sql(con, sql: str) -> None:
    """``check_user_sql`` against the relations this session actually serves."""
    if unrestricted():
        return
    check_user_sql(sql, relations=session_relations(con))
