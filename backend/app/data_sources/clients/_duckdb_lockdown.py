"""Lock a DuckDB connection down before agent SQL runs on it.

For connections over local files: once their views exist, the connection
may read only the files those views read (`allowed_paths`, plus
`allowed_directories` when given), reaches nothing else on disk or on the
network, cannot load extensions, and cannot change any of that again.
"""

from __future__ import annotations

from collections.abc import Iterable


def _list_literal(values: Iterable[str]) -> str:
    return "[" + ", ".join("'" + str(v).replace("'", "''") + "'" for v in values) + "]"


def lock_down(con, *, allowed_paths: Iterable[str] = (), allowed_directories: Iterable[str] = ()) -> None:
    """Restrict `con` to `allowed_paths` (exact files) and, when given,
    `allowed_directories`; turn external access off; lock the configuration.
    Raises RuntimeError if any step fails, so no caller is left holding an
    unlocked connection."""
    steps = [("allowed_paths", f"SET allowed_paths = {_list_literal(allowed_paths)}")]
    directories = list(allowed_directories)
    if directories:
        steps.append(("allowed_directories", f"SET allowed_directories = {_list_literal(directories)}"))
    steps += [
        ("enable_external_access", "SET enable_external_access = false"),
        ("lock_configuration", "SET lock_configuration = true"),
    ]
    for setting, statement in steps:
        try:
            con.execute(statement)
        except Exception as err:
            raise RuntimeError(f"Refusing to query files without the engine locked ({setting}): {err}") from err
