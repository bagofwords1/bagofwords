"""Who is editing a dashboard right now (collab01).

One `artifact_presence` row per person and conversation:

- the browser creates it when an editor opens the dashboard in a
  conversation, and beats it every ~15 s (`heartbeat`); closing the tab
  deletes it (`leave`);
- the agent's run moves it: `agent_editing` with its completion while it
  runs, the charts its tools touch, back to `viewing` when it ends;
- a row not beaten for PRESENCE_TTL_SECONDS counts as gone (a crash, a lost
  network) and is deleted later.

Hooks write the state; the table shares it across servers (the hook bus is
per process); editors' browsers read it (`entries`).
"""
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update

from app.models.artifact_release import ArtifactPresence

logger = logging.getLogger(__name__)

PRESENCE_TTL_SECONDS = 30
_PURGE_AFTER = timedelta(hours=1)
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


async def _row(db, artifact_id: str, user_id: str, report_id: str | None):
    return (await db.execute(
        select(ArtifactPresence).where(
            ArtifactPresence.artifact_id == str(artifact_id), ArtifactPresence.user_id == str(user_id),
            ArtifactPresence.report_id.is_(None) if report_id is None else ArtifactPresence.report_id == str(report_id),
        ).limit(1)
    )).scalars().first()


async def heartbeat(db, artifact, user, report_id: str | None) -> None:
    now = datetime.utcnow()
    row = await _row(db, str(artifact.id), str(user.id), report_id)
    if row is None:
        db.add(ArtifactPresence(artifact_id=str(artifact.id), user_id=str(user.id), report_id=report_id,
                                state="viewing", element_keys=[], seen_at=now))
    else:
        row.seen_at = now
    await db.commit()


async def leave(db, artifact, user, report_id: str | None) -> None:
    await db.execute(delete(ArtifactPresence).where(
        ArtifactPresence.artifact_id == str(artifact.id), ArtifactPresence.user_id == str(user.id),
        ArtifactPresence.state == "viewing",
        ArtifactPresence.report_id.is_(None) if report_id is None else ArtifactPresence.report_id == str(report_id)))
    await db.commit()


async def entries(db, artifact, user) -> list[dict]:
    """Fresh rows, newest first; stale ones older than an hour are purged."""
    from app.services.dashboard_collab import person

    now = datetime.utcnow()
    purged = await db.execute(delete(ArtifactPresence).where(ArtifactPresence.artifact_id == str(artifact.id),
                                                             ArtifactPresence.seen_at < now - _PURGE_AFTER))
    if purged.rowcount:
        await db.commit()
    rows = (await db.execute(
        select(ArtifactPresence).where(
            ArtifactPresence.artifact_id == str(artifact.id),
            ArtifactPresence.seen_at >= now - timedelta(seconds=PRESENCE_TTL_SECONDS),
        ).order_by(ArtifactPresence.seen_at.desc())
    )).scalars().all()
    out, seen = [], set()
    for r in rows:
        key = (str(r.user_id), str(r.report_id))
        if key in seen:
            continue
        seen.add(key)
        who = await person(db, r.user_id)
        if who is None:
            continue
        out.append({"user": who, "report_id": str(r.report_id) if r.report_id else None, "state": r.state,
                    "element_keys": list(r.element_keys or []), "seen_at": r.seen_at,
                    "is_me": user is not None and str(r.user_id) == str(user.id)})
    return out


# ── the agent's run ────────────────────────────────────────────────────────

async def _dashboards_of(db, report_id: str) -> list:
    from app.services.dashboard_collab import dashboards_of_conversations

    return await dashboards_of_conversations(db, [str(report_id)])


async def agent_started(report_id: str, user_id: str, completion_id: str | None) -> None:
    """Best effort, on its own short session: presence never fails a run."""
    from app.dependencies import async_session_maker

    try:
        async with async_session_maker() as db:
            now = datetime.utcnow()
            for artifact in await _dashboards_of(db, report_id):
                row = await _row(db, str(artifact.id), str(user_id), str(report_id))
                if row is None:
                    db.add(ArtifactPresence(artifact_id=str(artifact.id), user_id=str(user_id),
                                            report_id=str(report_id), state="agent_editing", element_keys=[],
                                            completion_id=completion_id, seen_at=now))
                else:
                    row.state, row.completion_id, row.element_keys, row.seen_at = (
                        "agent_editing", completion_id, [], now)
            await db.commit()
    except Exception:
        logger.debug("presence: agent start not recorded for %s", report_id, exc_info=True)


async def agent_touched(report_id: str, tool_input) -> None:
    """The charts a tool is about to change: any chart of this conversation,
    or of a query of it, whose id the tool's input names."""
    from app.dependencies import async_session_maker
    from app.models.visualization import Visualization

    ids = list(dict.fromkeys(m.group(0).lower() for m in _UUID.finditer(str(tool_input or ""))))[:200]
    if not ids:
        return
    try:
        async with async_session_maker() as db:
            vizs = (await db.execute(
                select(Visualization.id, Visualization.element_key).where(
                    Visualization.report_id == str(report_id),
                    or_(Visualization.id.in_(ids), Visualization.query_id.in_(ids)))
            )).all()
            keys = [str(k or i) for i, k in vizs]
            if not keys:
                return
            rows = (await db.execute(select(ArtifactPresence).where(
                ArtifactPresence.report_id == str(report_id), ArtifactPresence.state == "agent_editing"))).scalars().all()
            now = datetime.utcnow()
            for r in rows:
                r.element_keys = list(dict.fromkeys([*(r.element_keys or []), *keys]))[-50:]
                r.seen_at = now
            await db.commit()
    except Exception:
        logger.debug("presence: tool touch not recorded for %s", report_id, exc_info=True)


def changed_keys(elements: dict | None, base_elements: dict | None) -> list[str]:
    """The charts a version added, changed or removed against the one it
    grew from. A removed one is often replaced (the agent makes a new query
    for "top 6 instead of 8"): it is the chart the other editors see on
    screen, so it is outlined too."""
    new, base = elements or {}, base_elements or {}
    touched = [str(k) for k, el in new.items()
               if k not in base or (base.get(k) or {}).get("fp") != (el or {}).get("fp")]
    return touched + [str(k) for k in base if k not in new]


async def agent_changed_version(report_id: str, version_id: str) -> None:
    """After the agent saved a version: outline the charts it changed. A
    tool's input rarely names them (an edit is find/replace over the page
    code), the saved version always does -- its chart fingerprints against
    the version it grew from."""
    from app.dependencies import async_session_maker
    from app.models.artifact import ArtifactVersion

    try:
        async with async_session_maker() as db:
            version = await db.get(ArtifactVersion, str(version_id))
            if version is None or str(version.report_id) != str(report_id):
                return
            base = await db.get(ArtifactVersion, str(version.base_version_id)) if version.base_version_id else None
            keys = changed_keys(version.elements, base.elements if base is not None else None)
            if not keys:
                return
            rows = (await db.execute(select(ArtifactPresence).where(
                ArtifactPresence.report_id == str(report_id), ArtifactPresence.state == "agent_editing"))).scalars().all()
            now = datetime.utcnow()
            for r in rows:
                r.element_keys = list(dict.fromkeys([*(r.element_keys or []), *keys]))[-50:]
                r.seen_at = now
            await db.commit()
    except Exception:
        logger.debug("presence: saved version not recorded for %s", report_id, exc_info=True)


async def agent_finished(report_id: str, completion_id: str | None) -> None:
    from app.dependencies import async_session_maker

    try:
        async with async_session_maker() as db:
            await db.execute(update(ArtifactPresence).where(
                ArtifactPresence.report_id == str(report_id), ArtifactPresence.state == "agent_editing",
            ).values(state="viewing", element_keys=[], completion_id=None, seen_at=datetime.utcnow()))
            await db.commit()
    except Exception:
        logger.debug("presence: agent end not recorded for %s", report_id, exc_info=True)
