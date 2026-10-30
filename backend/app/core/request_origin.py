"""Which outside application a request came through: a host application
(an embedded artifact) or an OAuth app (an MCP client or app-scope token).

Set once by the authentication that recognised it (core/embed_auth.py,
core/auth.py, routes/mcp.py). Every conversation (Report) and agent run
(AgentExecution) inserted while handling that request -- including work it
starts in the background, which copies the context -- is stamped with it, so
the reports list and Monitoring can name the app. The app's name is looked
up when shown, so renaming it renames it everywhere.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Iterable, Optional

from sqlalchemy import event, select

HOST_APP = "host_app"
OAUTH_APP = "oauth_app"
KINDS = (HOST_APP, OAUTH_APP)

_origin: ContextVar[Optional[tuple[str, str]]] = ContextVar("bow_request_origin", default=None)


def set_origin(kind: str, app_id: str) -> None:
    if kind in KINDS and app_id:
        _origin.set((kind, str(app_id)))


def current() -> Optional[tuple[str, str]]:
    return _origin.get()


def _stamp(_mapper, _connection, target) -> None:
    origin = _origin.get()
    if origin is not None and getattr(target, "origin_app_type", None) is None:
        target.origin_app_type, target.origin_app_id = origin


def install() -> None:
    """Stamp new conversations and agent runs (idempotent)."""
    from app.models.agent_execution import AgentExecution
    from app.models.report import Report

    for model in (Report, AgentExecution):
        if not event.contains(model, "before_insert", _stamp):
            event.listen(model, "before_insert", _stamp)


async def names(db, pairs: Iterable[tuple[Optional[str], Optional[str]]]) -> dict[tuple[str, str], str]:
    """{(kind, id): current name} for the given origins."""
    from app.models.host_application import HostApplication
    from app.models.oauth_server import OAuthClient

    wanted = {(k, str(i)) for k, i in pairs if k in KINDS and i}
    out: dict[tuple[str, str], str] = {}
    for kind, model in ((HOST_APP, HostApplication), (OAUTH_APP, OAuthClient)):
        ids = [i for k, i in wanted if k == kind]
        if ids:
            for app_id, name in (await db.execute(select(model.id, model.name).where(model.id.in_(ids)))).all():
                out[(kind, str(app_id))] = name
    return out


def describe(kind: Optional[str], app_id: Optional[str], known: dict) -> Optional[dict]:
    """{type, id, name} for an API response, or None (made in BOW itself)."""
    if kind not in KINDS or not app_id:
        return None
    return {"type": kind, "id": str(app_id), "name": known.get((kind, str(app_id)))}
