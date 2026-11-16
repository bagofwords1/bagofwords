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
paths or directories, a temp directory, external access on) is refused.
The callers that open a database file all lock it with no paths of their
own, so their connections to one file agree.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

import duckdb

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


def _check_read_back(con, paths: set[str], directories: set[str]) -> None:
    """Raise unless the engine of `con` reads back as locked to exactly
    `paths` (plus the files DuckDB adds for its own databases) and
    `directories`, with no temp directory and external access off."""
    locked, external, temp, got_paths, got_directories = con.execute(
        "SELECT current_setting('lock_configuration'), current_setting('enable_external_access'), "
        "current_setting('temp_directory'), current_setting('allowed_paths'), "
        "current_setting('allowed_directories')").fetchone()
    databases = con.execute(
        "SELECT path FROM duckdb_databases() WHERE NOT internal AND path IS NOT NULL").fetchall()
    paths = paths | {path + suffix for (path,) in databases for suffix in _DATABASE_FILE_SUFFIXES}
    for setting, ok, value in (
        ("lock_configuration", locked is True, locked),
        ("enable_external_access", external is False, external),
        ("temp_directory", temp == "", temp),
        ("allowed_paths", set(got_paths) == paths, got_paths),
        ("allowed_directories", set(got_directories) == directories, got_directories),
    ):
        if not ok:
            raise _refused(setting, f"the engine reads back {value!r}")


def lock_down(con, *, allowed_paths: Iterable[str] = (), allowed_directories: Iterable[str] = ()) -> None:
    """Restrict `con` to `allowed_paths` (exact files) and, when given,
    `allowed_directories`; turn external access off; lock the configuration.
    Raises RuntimeError if any step fails or the engine does not read back
    exactly those settings, so no caller is left holding an unlocked
    connection, or one locked more widely than it asked.

    `allowed_paths` lets the connection write those files as well as read
    them: the engine alone does not keep a statement from overwriting the
    connection's own files. The query check (`check_duckdb_file_query`)
    is what refuses anything but a SELECT."""
    paths = [str(p) for p in allowed_paths]
    directories = [str(d) for d in allowed_directories]
    with _LOCKING:
        if con.execute("SELECT current_setting('lock_configuration')").fetchone()[0]:
            # The engine of a database file open elsewhere, locked already:
            # its settings cannot be set here, only compared.
            expected_paths = _resolved_on_scratch("allowed_paths", paths)
            expected_directories = _resolved_on_scratch("allowed_directories", directories)
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
                expected_directories = _resolved(con, "allowed_directories", directories)
            except Exception as err:
                raise _refused("allowed_paths", err) from err
            for setting, statement in steps:
                try:
                    con.execute(statement)
                except Exception as err:
                    raise _refused(setting, err) from err
        _check_read_back(con, expected_paths, expected_directories)
