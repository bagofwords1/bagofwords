"""Confinement for connectors that run SQL in an engine inside the API process.

DuckDB (duckdb / csv / qvd / pbix / iceberg / Power BI Report Server models)
and SQLite execute in-process, so a statement can reach whatever the app user
can: other files, the network, the app's own database. Two rules keep a
connection to the data it was configured with:

  * **Path policy** (`check_local_path`): a configured local path must not
    resolve into the app's own files or a system / secret location.
  * **Session lock** (`lock_connection`): before agent SQL reaches the
    session, it may only touch that connection's files or URI prefixes (no
    temp directory either: spilling to disk is off), cannot load extensions,
    and cannot change its configuration back. The lock is read back from the
    engine and refused unless it holds exactly what was asked.

`security.unsafe_allow_unrestricted_file_connectors` turns the path policy,
the session lock and the file connections' statement checks off. The
code-execution wrapper still checks what DuckDB connections and Iceberg are
sent (`sql_guard`), and the accelerated tables and the network directory,
which are not file connectors, keep their own locks whatever it says.

Untrusted text is only ever parsed by DuckDB on a locked engine (the query
guard's inspector, `duckdb_guard`, or the locked session itself): DuckDB opens
files and addresses some statements name while it parses them.
"""

from __future__ import annotations

import fnmatch
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


def opened_path(path: str) -> str:
    """``path`` as a process opens it with a plain file call: relative to
    the working directory, with a leading `~` or `file:` taken as part of
    a name. Absolute; its links and `..` are left for the path policy to
    resolve, so the result names the same file under its own name."""
    path = str(path)
    return path if os.path.isabs(path) else os.path.join(os.getcwd(), path)


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


def check_organization_roots(real: Path, organization_id: Optional[str] = None, contains: bool = False) -> None:
    """``security.organization_file_roots``: a listed root is reserved for the
    organizations it is listed for, and an organization with roots stays
    inside them. Raises ValueError. ``real`` is already resolved.
    ``contains=True`` is for a directory read as a whole: one that holds
    another organization's root is refused too."""
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
        if owner == org:
            continue
        for root in (_real(r) for r in roots if r):
            if _is_within(real, root):
                raise ValueError("Path is inside a directory assigned to another organization")
            if contains and _is_within(root, real):
                raise ValueError("Path holds a directory assigned to another organization")


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
    check_organization_roots(real, organization_id, contains=contains)
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
    matches are opened one file at a time (csv, qvd, pbix), as the glob
    reads them (`opened_path`). Raises ValueError."""
    for pattern in patterns:
        pattern = opened_path(pattern)
        base, has_glob = pattern_base(pattern)
        real = check_local_path(base, organization_id=organization_id)
        if has_glob:
            # A glob over a directory holding another organization's root.
            check_organization_roots(real, organization_id, contains=True)
        if pattern_reaches_protected_file(pattern):
            raise ValueError(f"Pattern {_REFUSED}")


def _lines(value) -> List[str]:
    return [p.strip() for p in str(value or "").splitlines() if p.strip()]


def check_connection_config(conn_type: str, config: dict) -> None:
    """Save/test-time path policy for a file-based connection's config.
    Raises ValueError naming the offending field. Enforced again at connect."""
    if unrestricted() or not isinstance(config, dict):
        return
    if conn_type == "iceberg":
        from app.data_sources.clients.iceberg_client import IcebergClient

        IcebergClient(**config).check_locations()
        return
    try:
        if conn_type == "sqlite":
            field = "database"
            database = str(config.get("database") or "").strip()
            if database and database != ":memory:":
                check_local_path(opened_path(database))
        elif conn_type == "duckdb":
            field = "database"
            database = str(config.get("database") or "").strip()
            if database:
                database_file(database)
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
    """The files a glob found, each as the absolute path the policy checked
    (`opened_path`), which is what the connection then reads; files the
    policy refuses are dropped (a broad glob can sweep in a protected
    file, or a symlink can point at one)."""
    kept = []
    for f in files:
        f = opened_path(f)
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


_FILE_URI_HOST = ("A file:// URI names a host. Write a local file as file:///path or "
                  "file://localhost/path.")
FILE_SCHEME_CASE = "Write the file: prefix of a URI in lower case, as file:///path."


def local_path_of(pattern: str) -> str:
    """Filesystem path of a local pattern (`file:` stripped, and `file://`
    with its host). The host of a `file://` URI must be empty or
    `localhost`, the two DuckDB reads as this machine: `file://localhost/x`
    and `file:///x` are both `/x`. Any other host, or no path after it,
    raises ValueError, as does a `file:` prefix not written in lower case
    (DuckDB reads that text as a path relative to the working directory)."""
    lower = pattern.lower()
    if lower.startswith("file:") and not pattern.startswith("file:"):
        raise ValueError(FILE_SCHEME_CASE)
    if lower.startswith("file://"):
        host, slash, rest = pattern[7:].partition("/")
        if host not in ("", "localhost") or not slash:
            raise ValueError(_FILE_URI_HOST)
        return "/" + rest
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
    expands itself. Local ones pass the path policy first (ValueError), and
    are listed only in the form it checked (links followed, then `..`)."""
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
        if pattern.startswith("~"):
            # DuckDB reads a leading `~` as the home directory's text,
            # followed by the rest as written: `~ogs` is `<home>ogs`.
            local = os.path.expanduser("~") + local[1:]
        base, has_glob = pattern_base(local)
        if has_glob:
            real = check_local_path(base, contains=True, organization_id=organization_id)
            if pattern_reaches_protected_file(local):
                raise ValueError(f"Pattern {_REFUSED}")
            dirs.append(str(real))
        else:
            # A directory, with or without its '/', is read as a whole.
            whole = local.endswith("/") or os.path.isdir(_real(local))
            real = check_local_path(local, contains=whole, organization_id=organization_id)
            if whole and _reaches_protected_file(real):
                raise ValueError(f"Path {_REFUSED}")
            paths.append(str(real))
    return paths, dirs


def _reaches_protected_file(directory: Path) -> bool:
    """Whether one of the app's protected files sits anywhere under
    ``directory`` (already resolved)."""
    return any(_is_within(f, directory) for f in _protected_app_files())


def check_lock_targets(paths: Iterable[str], directories: Iterable[str],
                       organization_id: Optional[str] = None) -> None:
    """The path policy on what a session is about to be locked to, in the
    form the engine resolved each entry to: every local directory as a
    whole (no protected location and no other organization's root inside
    it), every local file on its own. A directory also listed as a path is
    one the connection reads whole (a URI naming the directory itself), so
    no protected file may sit anywhere under it; under a glob's directory,
    what the glob can match is checked where the directory is resolved
    (`pattern_reaches_protected_file`). Object-store and http(s) entries
    are not local paths. Raises ValueError."""
    paths = list(paths)
    for directory in directories:
        if not is_remote_uri(directory):
            real = check_local_path(local_path_of(directory), contains=True, organization_id=organization_id)
            if directory in paths and _reaches_protected_file(real):
                raise ValueError(f"Path {_REFUSED}")
    for path in paths:
        if not is_remote_uri(path):
            check_local_path(local_path_of(path), organization_id=organization_id)


def database_file(value: str, organization_id: Optional[str] = None) -> str:
    """The database file a DuckDB `database` value names, as the file to
    open: a `file:` prefix read as DuckDB reads it (`local_path_of`), then
    links followed and `..` taken, and the path policy applied to that.
    Raises ValueError."""
    return str(check_local_path(local_path_of(value), organization_id=organization_id))


def engine_resolved(paths: Iterable[str], directories: Iterable[str]) -> Tuple[List[str], List[str]]:
    """(paths, directories) as DuckDB resolves them when a session is
    locked to them: the form `check_lock_targets` checks."""
    return (sorted(_resolved_on_scratch("allowed_paths", list(paths))),
            sorted(_resolved_on_scratch("allowed_directories", list(directories))))


def database_file_sources(db_file: str, extra_patterns: Iterable[str] = (),
                          organization_id: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """What a session opened on a database file may read besides the file:
    the file's own folder (views in it commonly read files kept next to it)
    and any further locations the connection lists, each in the form the
    path policy checked. Raises ValueError when the database file itself is
    refused."""
    real = check_local_path(db_file, organization_id=organization_id)
    paths: List[str] = []
    dirs: List[str] = []
    try:
        folder = check_local_path(str(real.parent), contains=True, organization_id=organization_id)
        if not any(_is_within(f, folder) for f in _protected_app_files() if f != real):
            dirs.append(str(folder))
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


def _list_literal(values: Iterable[str]) -> str:
    return "[" + ", ".join("'" + str(v).replace("'", "''") + "'" for v in values) + "]"


# Locks taken in this process do not interleave on a shared engine.
_LOCKING = threading.Lock()

# What DuckDB adds to allowed_paths for each database attached to the
# engine when external access is turned off.
_DATABASE_FILE_SUFFIXES = ("", ".wal", ".wal.checkpoint", ".wal.recovery")


# A database file's engine is shared by every connection to it in the
# process, and keeps the locations the first one locked it to.
_OTHER_CONNECTION_OTHER_LOCATIONS = (
    "another open connection to the same database file uses different additional locations; "
    "use the same additional locations on both connections, or let the other connection's "
    "queries finish and try again")


def _refused(setting: str, detail) -> RuntimeError:
    return RuntimeError(f"Refusing to query files without the engine locked ({setting}): {detail}")


def _resolved(con, setting: str, values: List[str]) -> set:
    """`values` as DuckDB resolves them in `setting` (allowed_paths or
    allowed_directories): set on `con`, then read back."""
    if not values:
        return set()
    con.execute(f"SET {setting} = {_list_literal(values)}")
    return set(con.execute(f"SELECT current_setting('{setting}')").fetchone()[0])


def _resolved_on_scratch(setting: str, values: List[str]) -> set:
    import duckdb

    if not values:
        return set()
    with duckdb.connect(":memory:") as scratch:
        return _resolved(scratch, setting, values)


def _check_read_back(con, paths: set, directories: set, database_path: Optional[str],
                     session_set: bool = True) -> None:
    """Raise unless the engine of `con` reads back as locked to exactly
    `paths` (plus the files DuckDB adds for the connection's own database)
    and `directories`, with no temp directory, external access off,
    replacement scans of Python objects off, secrets shown redacted only
    and no other database file attached; with `database_path` (as DuckDB
    stores it), the connection's own database must be that file. What the
    engine read back names server files: it goes to the server log, not
    into the refusal."""
    try:
        locked, external, temp, got_paths, got_directories, replacements, unredacted = con.execute(
            "SELECT current_setting('lock_configuration'), current_setting('enable_external_access'), "
            "current_setting('temp_directory'), current_setting('allowed_paths'), "
            "current_setting('allowed_directories'), current_setting('python_enable_replacements'), "
            "current_setting('allow_unredacted_secrets')").fetchone()
        databases = con.execute(
            "SELECT database_name = current_database(), path FROM duckdb_databases() "
            "WHERE NOT internal AND path IS NOT NULL").fetchall()
    except Exception as err:
        raise _refused("read back", err) from err
    own = [path for is_own, path in databases if is_own]
    others = [path for is_own, path in databases if not is_own]
    paths = paths | {path + suffix for path in own for suffix in _DATABASE_FILE_SUFFIXES}
    for setting, ok, value in (
        # Its name can be re-pointed at another file in this process.
        ("own database", database_path is None or own == [database_path], own),
        # DuckDB lets the engine read every database file attached to it.
        ("attached databases", not others, others),
        ("lock_configuration", locked is True, locked),
        ("enable_external_access", external is False, external),
        ("temp_directory", temp == "", temp),
        ("python_enable_replacements", replacements is False or not session_set, replacements),
        ("allow_unredacted_secrets", unredacted is False, unredacted),
        ("allowed_paths", got_paths is not None and set(got_paths) == paths, got_paths),
        ("allowed_directories", got_directories is not None and set(got_directories) == directories,
         got_directories),
    ):
        if not ok:
            logger.warning("duckdb_lockdown.refused %s: the engine reads back %r", setting, value)
            if not session_set and setting in ("allowed_paths", "allowed_directories"):
                raise _refused(setting, _OTHER_CONNECTION_OTHER_LOCATIONS)
            raise _refused(setting, "the engine reads back other settings than this connection's")


def lock_connection(con, *, paths: Iterable[str] = (), directories: Iterable[str] = (),
                    database_path: Optional[str] = None) -> None:
    """Restrict the DuckDB session `con` to `paths` (exact files or URLs)
    and, when given, `directories` (path or URL prefixes); turn external
    access, replacement scans of Python objects and unredacted secrets off;
    keep no temp directory; lock the configuration. Called by trusted code
    before agent SQL reaches the session, and before DuckDB parses any of
    it there.

    Raises RuntimeError if any step fails or the engine does not read back
    exactly those settings, so no caller is left holding an unlocked
    connection, or one locked more widely than it asked. A connection
    opened on a database file passes that file as `database_path`: its own
    database must then be that file, not another one put under its name.

    DuckDB keeps one engine per database file in a process, and these
    settings belong to the engine: a second connection to a file still open
    elsewhere finds them already locked. That connection is taken only when
    the engine reads back exactly what this call would have set; any other
    lock (other paths or directories, a temp directory, external access on,
    another database file attached, its own database name on another file)
    is refused. A database file is locked to its own folder and the
    connection's URIs (`database_file_sources`), so connections to one file
    configured with the same URIs agree; one configured with other URIs is
    refused while the file is open elsewhere.

    `paths` lets the connection write those files as well as read them: the
    engine alone does not keep a statement from overwriting the
    connection's own files. The query checks are what refuse anything but
    a SELECT. With `security.unsafe_allow_unrestricted_file_connectors`
    on, nothing is locked."""
    if unrestricted():
        return
    paths = list(dict.fromkeys(str(p) for p in paths if p))
    directories = list(dict.fromkeys(str(d) for d in directories if d))
    own_database = None
    if database_path is not None:
        # The opened file in the form DuckDB stores a database's path
        # (absolute, normalized, links resolved, no file: prefix).
        try:
            resolved = _resolved_on_scratch("allowed_paths", [str(database_path)])
        except Exception as err:
            raise _refused("own database", err) from err
        if len(resolved) != 1:
            raise _refused("own database", "the database path does not resolve to one file")
        (own_database,) = resolved
    with _LOCKING:
        try:
            locked_already = con.execute("SELECT current_setting('lock_configuration')").fetchone()[0]
        except Exception as err:
            raise _refused("lock_configuration", err) from err
        if locked_already:
            # The engine of a database file open elsewhere, locked already:
            # its settings cannot be set here, only compared.
            try:
                expected_paths = _resolved_on_scratch("allowed_paths", paths)
            except Exception as err:
                raise _refused("allowed_paths", err) from err
            try:
                expected_directories = _resolved_on_scratch("allowed_directories", directories)
            except Exception as err:
                raise _refused("allowed_directories", err) from err
        else:
            if any(is_remote_uri(p) for p in paths + directories):
                try:
                    # The secret manager reads its on-disk store the first
                    # time a remote file is opened; that must happen before
                    # file access is closed. Trusted text, on a session agent
                    # SQL has not reached yet.
                    con.execute("SELECT count(*) FROM duckdb_secrets()").fetchall()
                except Exception:
                    pass
            # DuckDB allows a connection its own temp directory under the
            # lock; with none set, spilling is off and no temp directory is
            # reachable.
            steps = [
                ("temp_directory", "SET temp_directory = ''"),
                ("allowed_paths", f"SET allowed_paths = {_list_literal(paths)}"),
            ]
            if directories:
                steps.append(("allowed_directories", f"SET allowed_directories = {_list_literal(directories)}"))
            steps += [
                ("python_enable_replacements", "SET python_enable_replacements = false"),
                ("allow_unredacted_secrets", "SET allow_unredacted_secrets = false"),
                ("enable_external_access", "SET enable_external_access = false"),
                ("lock_configuration", "SET lock_configuration = true"),
            ]
            try:
                expected_paths = _resolved(con, "allowed_paths", paths)
            except Exception as err:
                raise _refused("allowed_paths", err) from err
            try:
                expected_directories = _resolved(con, "allowed_directories", directories)
            except Exception as err:
                raise _refused("allowed_directories", err) from err
            for setting, statement in steps:
                try:
                    con.execute(statement)
                except Exception as err:
                    raise _refused(setting, err) from err
        _check_read_back(con, expected_paths, expected_directories, own_database,
                         session_set=not locked_already)


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

# A string constant where a PIVOT statement takes its relation.
_STRING_RELATION_IN_PIVOT = re.compile(
    r"\b(?:PIVOT|UNPIVOT|FROM|JOIN)\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*)*'", re.IGNORECASE | re.DOTALL
)

_NOT_A_TABLE = "Query the connection's tables by name; files, URLs and system state are not available."


def _refuse(message: str = _NOT_A_TABLE):
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    raise ReadOnlyViolation(message)


def _serialized(sql: str) -> dict:
    """DuckDB's parse tree for one SELECT-type statement, without running
    it, read on the query guard's locked inspector connection."""
    from app.ai.code_execution.duckdb_guard import serialize_sql

    return serialize_sql(sql)


def _statement_types(sql: str) -> list:
    """The statement types DuckDB splits `sql` into, read on the query
    guard's locked inspector connection (`duckdb_guard.statement_types`).
    Text DuckDB cannot parse gives None; anything else the engine refuses
    while it parses (a statement that would open a file or an address) is
    refused."""
    import duckdb

    from app.ai.code_execution.duckdb_guard import statement_types

    try:
        return statement_types(sql)
    except duckdb.ParserException:
        return None
    except Exception as err:
        logger.warning("duckdb_lockdown.parse_refused %s", type(err).__name__)
        _refuse("The statement could not be verified as a read. Only SELECT queries are allowed.")


def _fold(name) -> str:
    """`name` folded as DuckDB folds an identifier it compares: ASCII letters
    only (`duckdb_guard.ascii_lower`)."""
    from app.ai.code_execution.duckdb_guard import ascii_lower

    return ascii_lower(str(name or ""))


class SessionCatalog:
    """The tables and views a session really has, fully qualified, and the
    database and schema an unqualified name resolves in. Identifiers compare
    case-insensitively, as DuckDB's do: only their ASCII letters fold."""

    def __init__(self, relations, database: str = "memory", schema: str = "main"):
        self.relations = frozenset(tuple(_fold(p) for p in r) for r in relations)
        self.database = _fold(database or "memory")
        self.schema = _fold(schema or "main")

    def has(self, catalog: str, schema: str, name: str) -> bool:
        catalog, schema, name = _fold(catalog), _fold(schema), _fold(name)
        if catalog:
            return (catalog, schema or "main", name) in self.relations
        if schema:
            # `a.b` is schema a in the current database, or database a's main schema.
            return (self.database, schema, name) in self.relations or (schema, "main", name) in self.relations
        return (self.database, self.schema, name) in self.relations or ("temp", "main", name) in self.relations


def session_relations(con) -> SessionCatalog:
    """Ask the engine what the session serves."""
    rows = con.execute(
        "SELECT database_name, schema_name, table_name FROM duckdb_tables() "
        "UNION ALL SELECT database_name, schema_name, view_name FROM duckdb_views()"
    ).fetchall()
    database, schema = con.execute("SELECT current_database(), current_schema()").fetchone()
    return SessionCatalog(rows, database, schema)


def _is_plain(*parts: str) -> bool:
    return all(not p or _PLAIN_NAME.match(p) for p in parts)


def _check_relation(name: str, schema: str, catalog: str, relations, ctes,
                    is_string: bool = False, deferred: bool = False) -> None:
    """Whether a statement may name this relation.

    With the session's catalog (``relations``): any relation the session
    really has, whatever characters its name, schema or database contain
    ("Sales Ops".orders, a table called "export.csv"). A name is a file path
    or URL only when it is not one of those. Anything else must be a plain
    identifier, which the engine resolves in its catalog and never as a file.

    A relation written as a string constant is always refused. Without the
    catalog, a non-plain identifier is left to the client's own check against
    its catalog (``deferred``), or refused when it looks like a path.
    """
    lowered = _fold(name)
    if not name:
        _refuse()
    if is_string:
        # 'x', E'x', $$x$$ in a relation position: how a file is named. A
        # table is always nameable as an identifier ("x").
        _refuse()
    if not schema and not catalog and lowered in ctes:
        return
    if isinstance(relations, SessionCatalog) and relations.has(catalog, schema, name):
        return
    if _is_plain(name, schema, catalog):
        if lowered.startswith("duckdb_") and lowered not in _CATALOG_RELATIONS_ALLOWED:
            _refuse()
        return
    if relations is None:
        if deferred:
            return
        if not any(_PATH_LIKE.search(p) for p in (name, schema, catalog) if p):
            return
    _refuse()


def _written_as_string(sql: str, location) -> bool:
    """Whether the token at ``location`` is a string constant ('…', E'…', $$…$$)
    rather than an identifier."""
    # The engine reports the location as a byte offset into the UTF-8 text,
    # not a character index: the two differ after any non-ASCII character.
    raw = sql.encode("utf-8")
    if not isinstance(location, int) or location < 0 or location >= len(raw):
        return False
    head = raw[location:location + 2]
    return head[:1] in (b"'", b"$") or head.lower() == b"e'"


def _check_function(name: str, table_function: bool) -> None:
    from app.ai.code_execution.sql_guard import _DUCKDB_FILE_FUNCTIONS

    lowered = (name or "").lower()
    if table_function and lowered not in TABLE_FUNCTIONS_ALLOWED:
        _refuse()
    if lowered in _STATE_FUNCTIONS or _DUCKDB_FILE_FUNCTIONS.match(lowered):
        _refuse()


def _walk_serialized(node, relations, ctes, sql: str = "", deferred: bool = False) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_serialized(item, relations, ctes, sql, deferred)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    if kind == "BASE_TABLE":
        _check_relation(node.get("table_name") or "", node.get("schema_name") or "",
                        node.get("catalog_name") or "", relations, ctes,
                        is_string=_written_as_string(sql, node.get("query_location")), deferred=deferred)
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
        _walk_serialized(value, relations, ctes, sql, deferred)


def _cte_names(node, out: set) -> set:
    """The names of every CTE the statement defines, as written."""
    if isinstance(node, list):
        for item in node:
            _cte_names(item, out)
    elif isinstance(node, dict):
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map") or []:
                if isinstance(entry, dict) and entry.get("key"):
                    out.add(str(entry["key"]))
        for value in node.values():
            _cte_names(value, out)
    return out


def _plain_ctes(names) -> set:
    """`names`, the CTEs a statement defines, folded; refused unless every
    one is plain (`duckdb_guard.is_plain_cte_name`, the wrapper's rule)."""
    from app.ai.code_execution.duckdb_guard import CTE_NAME_NOT_PLAIN, is_plain_cte_name

    if not all(is_plain_cte_name(name) for name in names):
        _refuse(CTE_NAME_NOT_PLAIN)
    return {_fold(name) for name in names}


def _check_pivot_statement(sql: str, relations, deferred: bool = False) -> None:
    """A dynamic PIVOT cannot be serialized by the engine. Its relations are
    read from the SQL guard's tree instead, and the string syntaxes that tree
    does not read the engine's way are refused outright."""
    import sqlglot
    from sqlglot import exp

    if _STRING_SYNTAX_SQLGLOT_MISREADS.search(sql) or _STRING_RELATION_IN_PIVOT.search(sql):
        _refuse()
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        _refuse()
    ctes = _plain_ctes({c.alias_or_name for c in tree.find_all(exp.CTE)})
    for node in tree.walk():
        if isinstance(node, exp.Table):
            if isinstance(node.this, exp.Func):
                name = node.this.name if isinstance(node.this, exp.Anonymous) else node.this.sql_name()
                _check_function(name, table_function=True)
            else:
                _check_relation(node.name, node.db or "", node.catalog or "", relations, ctes, deferred=deferred)
        elif isinstance(node, exp.Func):
            _check_function(node.name if isinstance(node, exp.Anonymous) else node.sql_name(), table_function=False)


def engine_tree_check(sql: str, relations=None, deferred: bool = False) -> None:
    """Decide, from DuckDB's own reading of the statement, that it is one
    read whose relations and functions stay inside the session. ``relations``
    is the session's catalog (``session_relations``) when known. ``deferred``
    says the caller will run this check again with the catalog before the
    statement executes. A CTE must have a plain name, as on the wrapper
    (`duckdb_guard.is_plain_cte_name`). Raises ReadOnlyViolation; fails
    closed on anything it cannot read."""
    import duckdb

    kinds = _statement_types(sql)
    if kinds is None:
        return  # not runnable: the engine reports the syntax error itself
    if (
        kinds == [duckdb.StatementType.CREATE, duckdb.StatementType.SELECT]
        and re.search(r"\bPIVOT\b", sql, re.IGNORECASE)
    ):
        # One statement that DuckDB expands into a temporary enum plus a SELECT.
        _check_pivot_statement(sql, relations, deferred)
        return
    if len(kinds) != 1:
        _refuse("Only a single read-only SELECT statement is allowed.")
    if kinds[0] == duckdb.StatementType.EXPLAIN:
        m = _EXPLAIN_PREFIX.match(sql)
        if not m:
            _refuse("Only a single read-only SELECT statement is allowed.")
        inner = sql[m.end():]
        inner_kinds = _statement_types(inner) or []
        if inner_kinds != [duckdb.StatementType.SELECT]:
            _refuse("Only a single read-only SELECT statement is allowed.")
        engine_tree_check(inner, relations, deferred)
        return
    if kinds[0] != duckdb.StatementType.SELECT:
        _refuse("Only a single read-only SELECT statement is allowed.")
    tree = _serialized(sql)
    if tree.get("error"):
        if _PRAGMA_NO_ARG.match(sql):
            return
        m = _PRAGMA_ONE_ARG.match(sql)
        if m:
            _check_relation(m.group(2).strip(), "", "", relations, frozenset(), deferred=deferred)
            return
        _refuse("The statement could not be verified as a read. Only SELECT queries are allowed.")
    _walk_serialized(tree, relations, _plain_ctes(_cte_names(tree, set())), sql, deferred)


def check_user_sql(sql: str, *, hide_settings: bool = False, relations=None, deferred: bool = False) -> None:
    """Read-only check on SQL a connector is about to run in-process, for
    callers that do not come through the code-execution wrapper. With
    ``relations`` (see ``session_relations``) every relation the statement
    names is checked against what the session really has. ``deferred``: the
    caller has no session yet and will call again with one."""
    if unrestricted() or not isinstance(sql, str) or not sql.strip():
        return
    from app.ai.code_execution.sql_guard import check_sql

    check_sql(sql, "duckdb", relations_checked_by_client=deferred or relations is not None)
    if relations is not None:
        engine_tree_check(sql, relations)


def check_session_sql(con, sql: str) -> None:
    """``check_user_sql`` against the relations this session actually serves."""
    if unrestricted():
        return
    check_user_sql(sql, relations=session_relations(con))
