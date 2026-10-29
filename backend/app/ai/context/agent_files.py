"""Files in the libraries of the agents a run executes against.

An agent's Files tab (DataSource.files) used to reach a conversation only as a
snapshot copied onto the report when the report was created or its agents were
switched. Two common cases never got them:

- Auto mode: a report with no agents attached resolves to the user's
  accessible agents at run time, so nothing was ever snapshotted.
- Files uploaded to the agent after the conversation started.

Resolving them live from the run's agents (the same working set that scopes
schemas, clients and MCP tools) fixes both, the way project files are already
inherited live. The run agents are already access-filtered, so this widens
nothing: a user sees the library of exactly the agents the run uses.
"""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm.base import NO_VALUE


def _loaded_files(ds: Any) -> list:
    """`ds.files` without triggering a lazy load (unsafe under asyncio).

    DataSource.files is `lazy="selectin"`, so it is populated whenever the
    agent row itself is loaded; a plain object (tests, stubs) is read as is.
    """
    try:
        state = sa_inspect(ds)
    except Exception:
        return list(getattr(ds, "files", None) or [])
    try:
        value = state.attrs.files.loaded_value
    except Exception:
        return []
    if value is NO_VALUE or value is None:
        return []
    return list(value)


def agent_library_files(data_sources: Iterable[Any] | None, organization_id: str | None = None) -> list[Any]:
    """Distinct, non-deleted files across the agents' libraries, org-checked."""
    out: list[Any] = []
    seen: set = set()
    for ds in data_sources or []:
        for f in _loaded_files(ds):
            fid = str(getattr(f, "id", "") or "")
            if not fid or fid in seen:
                continue
            if getattr(f, "deleted_at", None) is not None:
                continue
            f_org = getattr(f, "organization_id", None)
            if organization_id is not None and f_org is not None and str(f_org) != str(organization_id):
                continue
            seen.add(fid)
            out.append(f)
    return out


def merge_files(primary: Iterable[Any] | None, extra: Iterable[Any] | None) -> list[Any]:
    """`primary` followed by the files of `extra` it does not already hold."""
    files = list(primary or [])
    seen = {str(getattr(f, "id", "")) for f in files}
    for f in extra or []:
        fid = str(getattr(f, "id", ""))
        if fid and fid not in seen:
            seen.add(fid)
            files.append(f)
    return files
