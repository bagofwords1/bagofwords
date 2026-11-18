"""Which outside application a request came through: a host application
(an embedded artifact) or an OAuth app (an MCP client or app-scope token).

Set once by the authentication that recognised it (core/embed_auth.py,
core/auth.py, routes/mcp.py). Every conversation (Report) and agent run
(AgentExecution) inserted while handling that request -- including work it
starts in the background, which copies the context -- is stamped with it, so
the reports list and Monitoring can name the app. The app's name is looked
up when shown, so renaming it renames it everywhere.

Separately, a request authenticated with an API key (core/auth.py) marks
every prompt (Completion, role='user') it inserts with
``started_via = 'api_key'``. The mark lives on the row, not only in the
request context, because a queued prompt is dispatched later from whichever
run finishes; the tool permission rules read it from the run's head
completion and treat such runs as unattended. A machine turn started by an
unattended run carries the same mark on its own head (``'unattended'``, or
``'api_key'`` kept from an API-key origin), so a chain of turns inherits it.
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


API_KEY_STARTED = "api_key"
# A machine turn (wait wake, check-in, sub-agent, wave or message reply)
# started by a scheduled, webhook or eval run, or by another such turn. Set by
# app.services.machine_turn.unattended_origin; an API-key origin keeps
# API_KEY_STARTED.
UNATTENDED_STARTED = "unattended"
# A machine turn started by a turn a person sent from another channel (Slack,
# Teams, email, an app surface): the same kind of run as that turn, which
# shows no approval card and gets none of the report's grants.
CHANNEL_STARTED = "channel"

_api_key_request: ContextVar[bool] = ContextVar("bow_api_key_request", default=False)


def mark_api_key_request() -> None:
    """This request is authenticated with an API key."""
    _api_key_request.set(True)


def api_key_request() -> bool:
    return _api_key_request.get()


def _stamp_completion(_mapper, _connection, target) -> None:
    if (_api_key_request.get() and getattr(target, "role", None) == "user"
            and getattr(target, "started_via", None) is None):
        target.started_via = API_KEY_STARTED


def install() -> None:
    """Stamp new conversations, agent runs and API-key prompts (idempotent)."""
    from app.models.agent_execution import AgentExecution
    from app.models.completion import Completion
    from app.models.report import Report

    for model in (Report, AgentExecution):
        if not event.contains(model, "before_insert", _stamp):
            event.listen(model, "before_insert", _stamp)
    if not event.contains(Completion, "before_insert", _stamp_completion):
        event.listen(Completion, "before_insert", _stamp_completion)


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
