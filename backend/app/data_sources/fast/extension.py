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
2. The directory (BOW_FAST_EXTENSION_DIR, `/opt/duckdb_httpfs` in the image,
   in DuckDB's own layout `<dir>/<version>/<platform>/`) holds httpfs and
   nothing else: the file, its `.info` file and the hash record written at
   image build. It is httpfs's own directory: the one the postgres multi
   engine loads its extension from is never read here.
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
"unavailable". One log line is written per change of state.

Without V (switch off, or the file not trusted), `open_with_signed_httpfs`
gives serving engines httpfs from the same directory, loaded by name with
DuckDB's signature check and no unsigned flag; the extract writer
(`artifacts.connect_encrypted`) always uses it. Neither depends on V's switch,
the owner rule or the user the process runs as. A directory whose httpfs
fails that load is tried again only when the file changes or after 60 s.
With no directory, engines open exactly as they always have.
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
DIRECTORY_ENV = "BOW_FAST_EXTENSION_DIR"

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


def describe_error(e: BaseException) -> str:
    """`Type: first line of the message`, short enough for one log line."""
    lines = str(e).strip().splitlines()
    first = lines[0].strip()[:240] if lines else ""
    return f"{type(e).__name__}: {first}" if first else type(e).__name__


def _owner_chain(directory: str) -> list[str]:
    """Every path the owner rule covers: the files in the platform folder,
    the folders up to the real extension directory, and all its ancestors."""
    root = os.path.realpath(directory)
    version, platform = _layout()
    platform_dir = os.path.join(root, version, platform)
    chain = [os.path.join(platform_dir, n) for n in sorted(os.listdir(platform_dir))]
    chain += [platform_dir, os.path.dirname(platform_dir), root]
    # A parent this process could write or own could swap the whole directory.
    parent = os.path.dirname(root)
    while True:
        chain.append(parent)
        up = os.path.dirname(parent)
        if up == parent:
            return chain
        parent = up


def _check_chain(paths: list[str]) -> None:
    me = os.geteuid()
    if me == 0:
        raise _Refused("the process runs as root")
    for p in paths:
        if os.stat(p).st_uid == me:
            raise _Refused(f"{p} is owned by the process's own user")
        if os.access(p, os.W_OK):
            raise _Refused(f"{p} is writable by the process")


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
        # Resolved once: every check and the returned path use the real directory.
        root = os.path.realpath(directory)
        version, platform = _layout()
        platform_dir = os.path.join(root, version, platform)
        path = os.path.join(platform_dir, EXTENSION_NAME)
        if not os.path.isfile(path):
            raise _Refused(f"no httpfs for DuckDB {version} {platform} in the extension directory")
        _check_contents(root, platform_dir)
        if require_foreign_owner:
            _check_chain(_owner_chain(root))
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
            raise _Refused(f"the signature-checked load failed ({describe_error(e)})")
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
        _LAST.reason = f"the extension directory could not be read ({describe_error(e)})"
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
    """BOW_FAST_EXTENSION_DIR when it names an existing directory."""
    d = os.environ.get(DIRECTORY_ENV, "").strip()
    return d if d and os.path.isdir(d) else None


_layout_failure_logged = False


def installed_httpfs(d: Optional[str]) -> Optional[str]:
    """The httpfs file for this DuckDB build inside extension directory `d`, if present."""
    global _layout_failure_logged
    if not d:
        return None
    try:
        version, platform = _layout()
    except Exception:
        if not _layout_failure_logged:
            _layout_failure_logged = True
            logger.warning("FAST: could not read DuckDB's version and platform", exc_info=True)
        return None
    path = os.path.join(d, version, platform, EXTENSION_NAME)
    return path if os.path.isfile(path) else None


# A directory whose httpfs failed the signed load: directory -> (the file's
# stat then, when to try again). Skipped until the file changes or
# RETRY_SECONDS pass, so a broken directory costs one attempt a minute.
_SIGNED_FAILURES: dict[str, tuple[tuple, float]] = {}
_signed_lock = threading.Lock()


def _stat_key(path: str) -> Optional[tuple]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def open_with_signed_httpfs(config: Optional[dict] = None, *, purpose: str):
    """An in-memory engine that loaded httpfs by name from `directory()`,
    with DuckDB's signature check, or None when there is no httpfs there or
    the load fails (logged at WARNING once per change). The caller then
    opens as before.

    Used by the extract writer always and by serving engines when the
    verified load is off or unavailable, so neither depends on a download.
    """
    d = directory()
    if not d:
        return None
    path = installed_httpfs(d)
    if not path:
        if purpose == "writer":
            logger.warning(f"FAST {purpose}: no httpfs for this DuckDB in {d}; opening without it")
        return None
    key = _stat_key(path)
    now = _VERIFIER._clock()
    with _signed_lock:
        failed = _SIGNED_FAILURES.get(d)
    if failed is not None and failed[0] == key and now < failed[1]:
        return None
    con = None
    try:
        con = duckdb.connect(database=":memory:", config={**(config or {}), "extension_directory": d})
        con.execute("LOAD httpfs")
    except Exception as e:
        if con is not None:
            con.close()
        with _signed_lock:
            _SIGNED_FAILURES[d] = (key, now + RETRY_SECONDS)
        if failed is None or failed[0] != key:
            logger.warning(
                f"FAST {purpose}: broken extension directory {d}: it holds httpfs but the "
                f"signature-checked load failed ({describe_error(e)}); opening without it, "
                f"retried in {int(RETRY_SECONDS)} s or when the file changes"
            )
        return None
    if failed is not None:
        with _signed_lock:
            _SIGNED_FAILURES.pop(d, None)
        logger.info(f"FAST {purpose}: httpfs loads from {d} again")
    return con


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
        # Bumped by mark_unavailable; a verification that saw it change while
        # it ran is discarded. `_state` guards the compare-and-publish only.
        self._generation = 0
        self._state = threading.Lock()

    def current(self) -> Optional[str]:
        # Fields are published before `_path` (see `_verify`); a hash time
        # not seen yet only means a re-hash is due.
        path = self._path
        hashed_at = self._hashed_at
        now = self._clock()
        if path is not None:
            if not unchanged(path):
                due = "verify"
            elif hashed_at is None or now - hashed_at >= REHASH_SECONDS:
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
        generation = self._generation
        exc_info = None
        try:
            path = self._check()
            reason = getattr(_LAST, "reason", None) or "not trusted"
        except Exception as e:  # a query must never see this
            path, reason, exc_info = None, describe_error(e), e
        with self._state:
            if self._generation != generation:
                return  # marked unavailable meanwhile: that decision stands
            if path:
                self._status, self._reason = "trusted", ""
                self._hashed_at = now
                self._next_check = None
                self._path = path  # last: readers see the rest first
                self._log()
            else:
                self._set_unavailable(reason, now, exc_info)

    def _rehash(self, path: str, now: float) -> None:
        generation = self._generation
        record = _RECORDS.get(path)
        try:
            ok = record is not None and _sha256(path) == record.sha256
        except OSError:
            ok = False
        with self._state:
            if self._generation != generation:
                return
            if ok:
                self._hashed_at = now
            else:
                self._set_unavailable("the file's hash changed since it was verified", now)

    def mark_unavailable(self, reason: str) -> None:
        # Never waits for a verification in flight; that one is discarded.
        with self._state:
            self._generation += 1
            self._set_unavailable(reason, self._clock())

    def _set_unavailable(self, reason: str, now: float, exc_info=None) -> None:
        self._path = None  # first: trust is withdrawn before anything else
        self._status, self._reason = "unavailable", reason
        self._next_check = now + RETRY_SECONDS
        self._log(exc_info)

    def note_off(self) -> None:
        if self._logged != ("off", ""):
            self._logged = ("off", "")
            logger.info(f"FAST verified extension load: off ({SWITCH_ENV} is not set)")

    def _log(self, exc_info=None) -> None:
        key = (self._status, self._reason)
        if key == self._logged:
            return
        self._logged = key
        if self._status == "trusted":
            logger.info(f"FAST verified extension load: trusted ({self._path})")
        else:
            logger.warning(
                f"FAST verified extension load: unavailable ({self._reason}); "
                f"engines load httpfs signature-checked, re-checked in {int(RETRY_SECONDS)} s",
                exc_info=exc_info,
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
            "next_check": self._next_check,
        }


_VERIFIER = _Verifier()


def current() -> Optional[str]:
    """The trusted httpfs file for a serving engine to load, or None.

    Read once per query; the answer is handed down, never asked again.
    """
    if not _enabled():
        _VERIFIER.note_off()
        return None
    try:
        return _VERIFIER.current()
    except Exception:
        logger.exception("FAST verified extension load: the check failed; engines open without it")
        return None


def mark_unavailable(reason: str) -> None:
    """An explicit load failed: engines open as today for the next 60 seconds."""
    _VERIFIER.mark_unavailable(reason)


def state() -> dict:
    return _VERIFIER.state()
