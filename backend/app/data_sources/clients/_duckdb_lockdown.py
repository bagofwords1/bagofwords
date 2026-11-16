"""Lock a DuckDB connection down before agent SQL runs on it.

For connections over local files: once their views exist, the connection
may read only the files those views read (`allowed_paths`, plus
`allowed_directories` when given), reaches nothing else on disk or on the
network (no temp directory either: spilling to disk is off), cannot load
extensions, and cannot change any of that again.

DuckDB keeps one engine per database file in a process, and these settings
belong to the engine: a second connection to a file still open elsewhere
finds them already locked. That connection is taken only when the engine
reads back exactly what this call would have set; any other lock (other
paths or directories, a temp directory, external access on, another
database file attached, its own database name on another file) is refused.
The callers that open a database file all lock it with no paths of their
own, so their connections to one file agree.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable

import duckdb

logger = logging.getLogger(__name__)

# Locks taken in this process do not interleave on a shared engine.
_LOCKING = threading.Lock()

# What DuckDB adds to allowed_paths for each database attached to the
# engine when external access is turned off.
_DATABASE_FILE_SUFFIXES = ("", ".wal", ".wal.checkpoint", ".wal.recovery")


def _list_literal(values: Iterable[str]) -> str:
    return "[" + ", ".join("'" + str(v).replace("'", "''") + "'" for v in values) + "]"


def _refused(setting: str, detail) -> RuntimeError:
    return RuntimeError(f"Refusing to query files without the engine locked ({setting}): {detail}")


def _resolved(con, setting: str, values: list[str]) -> set[str]:
    """`values` as DuckDB resolves them in `setting` (allowed_paths or
    allowed_directories): set on `con`, then read back."""
    if not values:
        return set()
    con.execute(f"SET {setting} = {_list_literal(values)}")
    return set(con.execute(f"SELECT current_setting('{setting}')").fetchone()[0])


def _resolved_on_scratch(setting: str, values: list[str]) -> set[str]:
    if not values:
        return set()
    with duckdb.connect(":memory:") as scratch:
        return _resolved(scratch, setting, values)


def _check_read_back(con, paths: set[str], directories: set[str], database_path: str | None) -> None:
    """Raise unless the engine of `con` reads back as locked to exactly
    `paths` (plus the files DuckDB adds for the connection's own database)
    and `directories`, with no temp directory, external access off and no
    other database file attached; with `database_path` (as DuckDB stores
    it), the connection's own database must be that file. What the engine
    read back names server files: it goes to the server log, not into the
    refusal."""
    try:
        locked, external, temp, got_paths, got_directories = con.execute(
            "SELECT current_setting('lock_configuration'), current_setting('enable_external_access'), "
            "current_setting('temp_directory'), current_setting('allowed_paths'), "
            "current_setting('allowed_directories')").fetchone()
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
        ("allowed_paths", got_paths is not None and set(got_paths) == paths, got_paths),
        ("allowed_directories", got_directories is not None and set(got_directories) == directories,
         got_directories),
    ):
        if not ok:
            logger.warning("duckdb_lockdown.refused %s: the engine reads back %r", setting, value)
            raise _refused(setting, "the engine reads back other settings than this connection's")


def lock_down(con, *, allowed_paths: Iterable[str] = (), allowed_directories: Iterable[str] = (),
              database_path: str | None = None) -> None:
    """Restrict `con` to `allowed_paths` (exact files) and, when given,
    `allowed_directories`; turn external access off; lock the configuration.
    Raises RuntimeError if any step fails or the engine does not read back
    exactly those settings, so no caller is left holding an unlocked
    connection, or one locked more widely than it asked. A connection
    opened on a database file passes that file as `database_path`: its
    own database must then be that file, not another one put under its
    name.

    `allowed_paths` lets the connection write those files as well as read
    them: the engine alone does not keep a statement from overwriting the
    connection's own files. The query check (`check_duckdb_file_query`)
    is what refuses anything but a SELECT."""
    paths = [str(p) for p in allowed_paths]
    directories = [str(d) for d in allowed_directories]
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
        _check_read_back(con, expected_paths, expected_directories, own_database)
