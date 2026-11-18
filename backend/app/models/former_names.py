"""Names an agent (DataSource) or a Connection used to have.

Saved step and entity code reaches a source by name:
`ds_clients["<agent>:<connection>"]`. A rename would strand every saved query
that spells the old name, so the row keeps its former names in
`previous_names` and the run resolves an old key to the same client
(app/services/client_keys.py, DataSourceService.construct_clients).

Recorded by a mapper hook, so every ORM write of `name` is covered -- the
settings page, the YAML import, a script -- without each caller remembering.
"""
from __future__ import annotations

from sqlalchemy import event, inspect

# A row renamed more often than this forgets its oldest names.
MAX_FORMER_NAMES = 20


def former_names(row) -> list[str]:
    """The names `row` used to have, newest last; never its current one."""
    raw = getattr(row, "previous_names", None)
    if not isinstance(raw, (list, tuple)):
        return []
    current = getattr(row, "name", None)
    return [n for n in raw if isinstance(n, str) and n and n != current]


def _record_former_name(mapper, connection, target) -> None:
    history = inspect(target).attrs.name.history
    if not history.has_changes():
        return
    names = [n for n in (target.previous_names or []) if isinstance(n, str) and n]
    for old in history.deleted or ():
        if isinstance(old, str) and old:
            names = [n for n in names if n != old] + [old]
    # Renamed back: the name is current again, not a former one.
    names = [n for n in names if n != target.name][-MAX_FORMER_NAMES:]
    target.previous_names = names or None


def track_former_names(model) -> None:
    event.listen(model, "before_update", _record_former_name)
