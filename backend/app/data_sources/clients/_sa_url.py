"""Build SQLAlchemy URLs from connection config without string formatting.

A URL assembled with an f-string lets one field spill into the next: a
database name carrying ``?key=value`` becomes driver options, a password
carrying ``@`` moves the host. SQLAlchemy neither encodes the database on
render nor decodes it on parse, so no rendered string can carry such a value
either. Clients therefore hand the engine pool the ``URL`` *object* — each
field then reaches the driver as its own keyword argument — and keep a
rendered string only as an identity (pool key, dialect detection).
"""
from typing import Any, Mapping, Optional

from sqlalchemy.engine import URL

# Never part of a host name; refused so a rendered URL stays unambiguous too.
_HOST_FORBIDDEN = set("/\\?#@ \t\r\n\x00")


def clean_host(host: Any) -> Optional[str]:
    if host is None:
        return None
    host = str(host).strip()
    if not host:
        return None
    if any(c in _HOST_FORBIDDEN for c in host):
        raise ValueError("The host name contains characters that are not allowed.")
    return host


def clean_port(port: Any) -> Optional[int]:
    if port is None or port == "":
        return None
    try:
        value = int(str(port).strip())
    except (TypeError, ValueError):
        raise ValueError("The port must be a number.") from None
    if not 0 < value < 65536:
        raise ValueError("The port must be between 1 and 65535.")
    return value


def build_url(
    drivername: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    host: Any = None,
    port: Any = None,
    database: Optional[str] = None,
    query: Optional[Mapping[str, str]] = None,
) -> URL:
    """The URL object for a connection. Pass it to ``get_engine`` as is."""
    return URL.create(
        drivername,
        username=None if username in (None, "") else str(username),
        password=None if password is None else str(password),
        host=clean_host(host),
        port=clean_port(port),
        database=None if database in (None, "") else str(database),
        query=dict(query or {}),
    )


def render(url: URL) -> str:
    """Identity string for a URL: pool key and dialect detection only."""
    return url.render_as_string(hide_password=False)


# ── credentials saved percent-encoded (Postgres, Oracle) ────────────────────
#
# Those two clients used to paste user and password into the URL string
# unencoded, and SQLAlchemy percent-decodes both when it parses a URL. A
# password containing ``@`` or ``/`` therefore only worked when the admin
# saved it percent-encoded, and such saved values must keep connecting. The
# literal value is tried first; when that fails and decoding user/password
# gives something different, the decoded form is tried once. A decoded form
# that works is remembered for the process, so the failing literal login is
# not repeated on every query (and the pool keeps one engine, for the form
# that works). The database name is not part of this: SQLAlchemy never
# decoded it, so its old meaning was already the literal one.

import logging
import threading
from collections import OrderedDict
from typing import Callable, TypeVar
from urllib.parse import unquote

_log = logging.getLogger(__name__)
_T = TypeVar("_T")
_decoded_lock = threading.Lock()
_decoded_ok: "OrderedDict[str, URL]" = OrderedDict()
_DECODED_MAX = 512


def legacy_decoded(url: URL) -> Optional[URL]:
    """``url`` with user and password percent-decoded, or None when decoding
    changes nothing."""
    user = None if url.username is None else unquote(url.username)
    password = None if url.password is None else unquote(str(url.password))
    if user == url.username and password == url.password:
        return None
    return url.set(username=user, password=password)


def effective_url(url: URL) -> URL:
    """The form of ``url`` known to connect: itself, unless its decoded form
    was the one that worked."""
    with _decoded_lock:
        return _decoded_ok.get(render(url), url)


def connect_with_legacy_decoding(url: URL, open_connection: Callable[[URL], _T]) -> _T:
    """``open_connection(url)``, falling back once to the percent-decoded
    credentials (see above). The original error is raised when there is
    nothing to decode or the decoded form fails too."""
    known = effective_url(url)
    if known is not url:
        return open_connection(known)
    try:
        return open_connection(url)
    except Exception as first:
        decoded = legacy_decoded(url)
        if decoded is None:
            raise
        try:
            conn = open_connection(decoded)
        except Exception:
            raise first from None
        with _decoded_lock:
            fresh = render(url) not in _decoded_ok
            _decoded_ok[render(url)] = decoded
            while len(_decoded_ok) > _DECODED_MAX:
                _decoded_ok.popitem(last=False)
        if fresh:
            _log.warning(
                "The credentials for the %s connection to %s/%s appear to be percent-encoded "
                "(they only work decoded). Re-save the connection with the user and password "
                "exactly as they are.",
                url.get_backend_name(), url.host, url.database or "",
            )
        return conn


# ── driver options written after the database name ──────────────────────────
#
# The old string-built URLs turned ``mydb?sslmode=require`` or
# ``db?charset=utf8mb4`` into driver options, and saved connections rely on
# it. That keeps working: the text after ``?`` is parsed once, here, into
# options that travel as the URL's query (never re-parsed), minus the options
# that redirect the connection or make the driver read or write local files.

from urllib.parse import parse_qsl

# libpq: where to connect, and files it would read (password, service, CRL).
POSTGRES_DENIED_OPTIONS = frozenset({
    "host", "hostaddr", "port", "dbname", "user", "password",
    "passfile", "service", "servicefile", "krbsrvname", "sslcrl", "sslcrldir", "sslpassword",
})
# TLS material is commonly a file the operator installed; it follows the
# local path policy (the app's own files are refused).
POSTGRES_PATH_OPTIONS = frozenset({"sslrootcert", "sslcert", "sslkey"})

# PyMySQL: local file loading, option files, statements run at connect,
# objects named by import path, and where to connect.
MYSQL_DENIED_OPTIONS = frozenset({
    "local_infile", "read_default_file", "read_default_group", "init_command", "unix_socket",
    "conv", "cursorclass", "plugin_dir", "defer_connect", "auth_plugin_map", "server_public_key",
    "host", "port", "user", "password", "passwd", "db", "database", "bind_address",
})
MYSQL_PATH_OPTIONS = frozenset({"ssl_ca", "ssl_capath", "ssl_cert", "ssl_key"})

_dropped_lock = threading.Lock()
_dropped_seen: set = set()


def path_allowed(path: str) -> bool:
    """Local path policy for a file an option names, where the driver opens
    it (`opened_path`: `~` is not the home directory to it); allowed when
    the policy helper is not available."""
    try:
        from app.data_sources.clients._duckdb_lockdown import check_local_path, opened_path
    except Exception:
        return True
    try:
        check_local_path(opened_path(str(path)))
        return True
    except Exception:
        return False


def warn_dropped_option(kind: str, host: Any, key: str) -> None:
    """One WARNING per connection and key; the value is never logged."""
    mark = (kind, str(host), key.lower())
    with _dropped_lock:
        if mark in _dropped_seen:
            return
        if len(_dropped_seen) > 4096:
            _dropped_seen.clear()
        _dropped_seen.add(mark)
    _log.warning(
        "%s connection to %s: ignoring option %r given after the database name "
        "(it would redirect the connection or use a local file).", kind, host, key,
    )


def split_database_options(
    database: Any, *, kind: str, host: Any, denied: frozenset, path_options: frozenset = frozenset(),
) -> "tuple[Optional[str], dict]":
    """``"name?k=v&k2=v2"`` → ``("name", {allowed options})``."""
    if database is None:
        return None, {}
    text = str(database)
    if "?" not in text:
        return text, {}
    name, _, raw = text.partition("?")
    options: dict = {}
    for key, value in parse_qsl(raw, keep_blank_values=True):
        k = key.strip()
        if not k:
            continue
        if k.lower() in denied or (k.lower() in path_options and not path_allowed(value)):
            warn_dropped_option(kind, host, k)
            continue
        if k in options:
            prev = options[k]
            options[k] = (*prev, value) if isinstance(prev, tuple) else (prev, value)
        else:
            options[k] = value
    return name, options
