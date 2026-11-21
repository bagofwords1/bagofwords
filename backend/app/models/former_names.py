"""Names an agent (DataSource) or a Connection used to have.

Saved step and entity code reaches a source by name:
`ds_clients["<agent>:<connection>"]`. A rename would strand every saved query
that spells the old name, so the row keeps its former names in
`previous_names` and the run resolves an old key to the same client
(app/services/client_keys.py, DataSourceService.construct_clients).

Recorded by a mapper hook, so every ORM write of `name` is covered -- the
settings page, the YAML import, a script -- without each caller remembering.

An entry is `{"name": <old name>, "connection_id": <id or None>}`. For an
agent, `connection_id` is the connection that answered to the bare
`ds_clients["<agent>"]` key when the name was given up: the agent's only
active connection then, None when it had several (the bare key did not
exist). The bare old key is that connection's and no other's -- replacing
the agent's connection later must not hand saved code a different database.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import event, inspect, select

# A row renamed more often than this forgets its oldest names.
MAX_FORMER_NAMES = 20


def _entry_name(entry) -> Optional[str]:
    name = entry.get("name") if isinstance(entry, dict) else entry
    return name if isinstance(name, str) and name else None


def names_of(previous_names, current: Optional[str] = None) -> list[str]:
    """The names in a stored `previous_names` value, oldest first, without
    `current`."""
    if not isinstance(previous_names, (list, tuple)):
        return []
    out: list[str] = []
    for entry in previous_names:
        name = _entry_name(entry)
        if name and name != current and name not in out:
            out.append(name)
    return out


def former_names(row) -> list[str]:
    """The names `row` used to have, oldest first; never its current one."""
    return names_of(getattr(row, "previous_names", None), getattr(row, "name", None))


def former_bare_connections(data_source) -> dict[str, str]:
    """{former agent name: id of the connection its bare key belonged to}.
    Names given up while the agent had several connections are left out."""
    raw = getattr(data_source, "previous_names", None)
    out: dict[str, str] = {}
    for entry in raw if isinstance(raw, (list, tuple)) else ():
        name = _entry_name(entry)
        owner = entry.get("connection_id") if isinstance(entry, dict) else None
        if name and name != getattr(data_source, "name", None) and isinstance(owner, str) and owner:
            out[name] = owner
    return out


def _sole_active_connection_id(connection, data_source_id) -> Optional[str]:
    """The id of the agent's only active connection, read on the flush's own
    connection; None when it has none or several."""
    from app.models.connection import Connection
    from app.models.domain_connection import domain_connection

    ids = [str(r[0]) for r in connection.execute(
        select(Connection.id)
        .join(domain_connection, domain_connection.c.connection_id == Connection.id)
        .where(domain_connection.c.data_source_id == str(data_source_id), Connection.is_active.is_(True))
    ).all()]
    return ids[0] if len(ids) == 1 else None


def _record(mapper, connection, target, bare_owner) -> None:
    history = inspect(target).attrs.name.history
    if not history.has_changes():
        return
    stored = target.previous_names if isinstance(target.previous_names, (list, tuple)) else []
    entries = [e if isinstance(e, dict) else {"name": e, "connection_id": None}
               for e in stored if _entry_name(e)]
    for old in history.deleted or ():
        if isinstance(old, str) and old:
            entries = [e for e in entries if e["name"] != old]
            entries.append({"name": old, "connection_id": bare_owner(connection, target)})
    # Renamed back: the name is current again, not a former one.
    entries = [e for e in entries if e["name"] != target.name][-MAX_FORMER_NAMES:]
    target.previous_names = entries or None


def _record_agent_name(mapper, connection, target) -> None:
    def owner(conn, row):
        try:
            return _sole_active_connection_id(conn, row.id)
        except Exception:
            return None  # the bare old key is then simply not answered
    _record(mapper, connection, target, owner)


def _record_connection_name(mapper, connection, target) -> None:
    _record(mapper, connection, target, lambda conn, row: None)


def track_former_names(model) -> None:
    hook = _record_agent_name if model.__tablename__ == "data_sources" else _record_connection_name
    event.listen(model, "before_update", hook)
