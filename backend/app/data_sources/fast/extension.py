"""Verified-once httpfs load for FAST serving engines.

Every FAST query opens a fresh DuckDB engine, and the encrypted ATTACH makes
that engine load `httpfs` with a signature check that costs far more than the
rest of the session build. With BOW_FAST_VERIFIED_EXTENSION_LOAD on, the
process verifies one `httpfs` file once and serving engines then load that
file by path with signature checks off (`FastQueryClient._open_engine`).

What makes that acceptable is checked here, once, before the file is trusted:

1. The file and its directories cannot be written by this process: they are
   owned by another user and grant this process no write access. A process
   running as root fails this rule and never trusts the file.
2. The directory (BOW_DUCKDB_EXTENSION_DIR, in DuckDB's own layout
   `<dir>/<version>/<platform>/`) holds httpfs and nothing else: the file, its
   `.info` file and the hash record written at image build.
3. The file's SHA-256 equals the hash record, when there is one.
4. A scratch engine with default settings loads the file, so DuckDB checks its
   signature.

The stat taken then (device, inode, size, modification and change time) is
compared before every explicit load. That catches a redeploy, a remount or a
truncated copy under a running process; it is not tamper detection, which is
why a writable location is refused outright.

The decision is process-wide and never blocks a query. `current()` returns the
last decision; the first verification, the 60-second re-check after a refusal
and the five-minute re-hash each run in one thread while the others keep the
last decision. Before the first verification has finished the decision is
"unavailable", and engines open as they always have. One log line is written
per change of state.

The extract writer (`artifacts.connect_encrypted`) uses `directory()` only, to
load httpfs from the same place with signature checks on.
"""

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Optional

import duckdb

logger = logging.getLogger(__name__)

SWITCH_ENV = "BOW_FAST_VERIFIED_EXTENSION_LOAD"
DIRECTORY_ENV = "BOW_DUCKDB_EXTENSION_DIR"

EXTENSION_NAME = "httpfs.duckdb_extension"
_INFO_SUFFIX = ".info"
_HASH_SUFFIX = ".sha256"

RETRY_SECONDS = 60.0
REHASH_SECONDS = 300.0


@dataclass(frozen=True)
class _Record:
    path: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


# The stat and hash of every file `trusted_httpfs` accepted, by path.
_RECORDS: dict[str, _Record] = {}

# Why the last `trusted_httpfs` call in this thread refused, for the state.
_LAST = threading.local()


class _Refused(Exception):
    pass


@lru_cache(maxsize=1)
def _layout() -> tuple[str, str]:
    """The `<version>/<platform>` sub-folders DuckDB uses for this build."""
    con = duckdb.connect(database=":memory:")
    try:
        version = con.execute("SELECT version()").fetchone()[0]
        platform = con.execute("SELECT platform FROM pragma_platform()").fetchone()[0]
    finally:
        con.close()
    return version, platform


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _matches(record: _Record, st: os.stat_result) -> bool:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) == (
        record.dev, record.ino, record.size, record.mtime_ns, record.ctime_ns
    )


def _only_entry(folder: str, expected: str, is_dir: bool) -> None:
    for entry in os.scandir(folder):
        if entry.name != expected or entry.is_symlink():
            raise _Refused(f"the extension directory holds more than httpfs ({entry.name})")
        if entry.is_dir(follow_symlinks=False) != is_dir:
            raise _Refused(f"unexpected entry type in the extension directory ({entry.name})")


def _check_contents(root: str, platform_dir: str) -> None:
    version, platform = _layout()
    _only_entry(root, version, True)
    _only_entry(os.path.join(root, version), platform, True)
    allowed = {EXTENSION_NAME, EXTENSION_NAME + _INFO_SUFFIX, EXTENSION_NAME + _HASH_SUFFIX}
    for entry in os.scandir(platform_dir):
        if entry.name not in allowed or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            raise _Refused(f"the extension directory holds more than httpfs ({entry.name})")


def _check_not_writable(root: str, platform_dir: str, path: str) -> None:
    if os.geteuid() == 0:
        raise _Refused("the process runs as root")
    me = os.geteuid()
    owned = [path, platform_dir, os.path.dirname(platform_dir), root]
    owned += [os.path.join(platform_dir, n) for n in os.listdir(platform_dir)]
    for p in owned:
        if os.stat(p).st_uid == me:
            raise _Refused(f"{p} is owned by the process's own user")
        if os.access(p, os.W_OK):
            raise _Refused(f"{p} is writable by the process")
    # A writable parent could swap the whole directory.
    parent = os.path.dirname(os.path.realpath(root))
    while True:
        if os.access(parent, os.W_OK):
            raise _Refused(f"{parent} is writable by the process")
        up = os.path.dirname(parent)
        if up == parent:
            break
        parent = up


def trusted_httpfs(directory: Optional[str], *, require_foreign_owner: bool = True) -> Optional[str]:
    """The path of the httpfs file in `directory` if it passes every check, else None.

    On success the file's stat and hash are recorded for `unchanged`. On a
    refusal the reason is kept for this thread (see `_Verifier`) and logged by
    the caller's state change, not here. `require_foreign_owner` is turned off
    only by tests, whose files cannot be owned by another user.
    """
    _LAST.reason = None
    try:
        if not directory or not os.path.isdir(directory):
            raise _Refused(f"no extension directory ({DIRECTORY_ENV})")
        root = os.path.abspath(directory)
        version, platform = _layout()
        platform_dir = os.path.join(root, version, platform)
        path = os.path.join(platform_dir, EXTENSION_NAME)
        if not os.path.isfile(path):
            raise _Refused(f"no httpfs for DuckDB {version} {platform} in the extension directory")
        _check_contents(root, platform_dir)
        if require_foreign_owner:
            _check_not_writable(root, platform_dir, path)
        before = os.stat(path)
        digest = _sha256(path)
        if os.path.isfile(path + _HASH_SUFFIX):
            with open(path + _HASH_SUFFIX, encoding="ascii") as fh:
                recorded = (fh.read().split() or [""])[0].lower()
            if recorded != digest:
                raise _Refused("the file's hash does not match the hash record")
        scratch = duckdb.connect(database=":memory:")
        try:
            scratch.execute("LOAD '" + path.replace("'", "''") + "'")
        except Exception as e:
            raise _Refused(f"the signature-checked load failed ({type(e).__name__})")
        finally:
            scratch.close()
        after = os.stat(path)
        record = _Record(path, before.st_dev, before.st_ino, before.st_size,
                         before.st_mtime_ns, before.st_ctime_ns, digest)
        if not _matches(record, after):
            raise _Refused("the file changed while it was verified")
    except _Refused as e:
        _LAST.reason = str(e)
        return None
    except (OSError, UnicodeDecodeError) as e:
        _LAST.reason = f"the extension directory could not be read ({type(e).__name__})"
        return None
    _RECORDS[path] = record
    return path


def unchanged(path: str) -> bool:
    """Whether `path` still has the stat it had when it was verified."""
    record = _RECORDS.get(path)
    if record is None:
        return False
    try:
        return _matches(record, os.stat(path))
    except OSError:
        return False


def _enabled() -> bool:
    return os.environ.get(SWITCH_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def directory() -> Optional[str]:
    """BOW_DUCKDB_EXTENSION_DIR when it names an existing directory."""
    d = os.environ.get(DIRECTORY_ENV, "").strip()
    return d if d and os.path.isdir(d) else None


class _Verifier:
    """The process's decision about the httpfs file, kept fresh without blocking."""

    def __init__(self, check: Optional[Callable[[], Optional[str]]] = None,
                 clock: Optional[Callable[[], float]] = None):
        self._check = check or (lambda: trusted_httpfs(directory()))
        self._clock = clock or time.monotonic
        self._flight = threading.Lock()
        self._path: Optional[str] = None
        self._status = "unavailable"
        self._reason = "not verified yet"
        self._next_check: Optional[float] = None
        self._checked_at: Optional[float] = None
        self._hashed_at: Optional[float] = None
        self._verifications = 0
        self._logged = ("unavailable", "not verified yet")

    def current(self) -> Optional[str]:
        path = self._path
        now = self._clock()
        if path is not None:
            if not unchanged(path):
                due = "verify"
            elif now - self._hashed_at >= REHASH_SECONDS:
                due = "rehash"
            else:
                return path
        elif self._next_check is not None and now < self._next_check:
            return None
        else:
            due = "verify"
        if not self._flight.acquire(blocking=False):
            # Another thread is at it: keep the last decision, unless the
            # file is known to have changed.
            return path if due == "rehash" else None
        try:
            if due == "rehash":
                self._rehash(path, now)
            elif self._path is not None or self._next_check is None or now >= self._next_check:
                self._verify(now)
            return self._path
        finally:
            self._flight.release()

    def _verify(self, now: float) -> None:
        self._verifications += 1
        self._checked_at = now
        path = self._check()
        if path:
            self._path, self._status, self._reason = path, "trusted", ""
            self._hashed_at = now
            self._next_check = None
            self._log()
        else:
            self._set_unavailable(getattr(_LAST, "reason", None) or "not trusted", now)

    def _rehash(self, path: str, now: float) -> None:
        record = _RECORDS.get(path)
        try:
            ok = record is not None and _sha256(path) == record.sha256
        except OSError:
            ok = False
        if ok:
            self._hashed_at = now
        else:
            self._set_unavailable("the file's hash changed since it was verified", now)

    def mark_unavailable(self, reason: str) -> None:
        self._set_unavailable(reason, self._clock())

    def _set_unavailable(self, reason: str, now: float) -> None:
        self._path, self._status, self._reason = None, "unavailable", reason
        self._next_check = now + RETRY_SECONDS
        self._log()

    def note_off(self) -> None:
        if self._logged != ("off", ""):
            self._logged = ("off", "")
            logger.info(f"FAST verified extension load: off ({SWITCH_ENV} is not set)")

    def _log(self) -> None:
        key = (self._status, self._reason)
        if key == self._logged:
            return
        self._logged = key
        if self._status == "trusted":
            logger.info(f"FAST verified extension load: trusted ({self._path})")
        else:
            logger.warning(
                f"FAST verified extension load: unavailable ({self._reason}); "
                f"engines load httpfs signature-checked, re-checked in {int(RETRY_SECONDS)} s"
            )

    def state(self) -> dict:
        enabled = _enabled()
        return {
            "enabled": enabled,
            "status": self._status if enabled else "off",
            "reason": self._reason if enabled else "",
            "path": self._path,
            "verifications": self._verifications,
            "checked_at": self._checked_at,
            "hashed_at": self._hashed_at,
        }


_VERIFIER = _Verifier()


def current() -> Optional[str]:
    """The trusted httpfs file for a serving engine to load, or None.

    Read once per query; the answer is handed down, never asked again.
    """
    if not _enabled():
        _VERIFIER.note_off()
        return None
    return _VERIFIER.current()


def mark_unavailable(reason: str) -> None:
    """An explicit load failed: engines open as today for the next 60 seconds."""
    _VERIFIER.mark_unavailable(reason)


def state() -> dict:
    return _VERIFIER.state()
