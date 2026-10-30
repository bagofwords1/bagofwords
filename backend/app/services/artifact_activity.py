"""The app activity log: what happened in an app while its editor (or BOW's
own preview) used it, so the agent can see "it doesn't work" for itself.

* Written by the editor's page broker in batches
  (``POST /api/artifacts/{id}/runtime/activity``, routes/artifact_activity.py)
  and by the agent's preview browser (``source="preview"``,
  ai/tools/implementations/_artifact_browser.py).
* Never for viewers, embed sessions or host applications: the route takes
  only an app session that can edit the artifact.
* Values people type and message text are never sent; strings that are
  stored are bounded and pass the organization's PII rules.
* Read by the agent automatically (``context_section``: only errors not yet
  shown, with the steps before them) and on demand (``read`` behind the
  ``read_artifact_activity`` tool).
* Retention: ``RETENTION_DAYS`` and the newest ``KEEP_PER_REPORT`` events per
  report, pruned on every write.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import delete, or_, select, update

from app.models.artifact_activity import KINDS, SOURCES, ArtifactActivityEvent

RETENTION_DAYS = 7
KEEP_PER_REPORT = 500
MAX_BATCH = 50
MAX_DETAIL_CHARS = 2000
MAX_TEXT = 300
CONTEXT_LINES = 10
STEPS_BEFORE = 3

# Detail keys a client may send, per kind. Anything else is dropped.
DETAIL_KEYS = {
    "request": {"resource", "action", "agents", "message", "http_status", "bytes", "simulated"},
    "stream": {"message", "events", "resource", "simulated"},
    "filter": {"params", "queries", "rows", "withheld", "message"},
    "error": {"message", "stack", "where"},
    "console": {"message"},
    "click": {"target"},
}
STATUSES = {"ok", "error", "completed", "interrupted", "cancelled", "aborted"}


def _clip(value: Any, limit: int = MAX_TEXT) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_clip(v, limit) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k)[:64]: _clip(v, limit) for k, v in list(value.items())[:20]}
    return str(value)[:limit]


def clean_event(raw: dict) -> Optional[dict]:
    """One client event, reduced to known fields and bounded; None if invalid."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind not in KINDS:
        return None
    status = raw.get("status")
    detail = {k: _clip(v, 1200 if k == "stack" else MAX_TEXT)
              for k, v in (raw.get("detail") or {}).items() if k in DETAIL_KEYS[kind]} \
        if isinstance(raw.get("detail"), dict) else {}
    if len(json.dumps(detail, default=str)) > MAX_DETAIL_CHARS:
        detail = {"message": "detail exceeded size limit", "truncated": True}
    duration = raw.get("duration_ms")
    seq = raw.get("seq")
    return {
        "kind": kind,
        "op": _clip(raw.get("op"), 64) if isinstance(raw.get("op"), str) else None,
        "status": status if status in STATUSES else None,
        "error_code": _clip(raw.get("error_code"), 80) if isinstance(raw.get("error_code"), str) else None,
        "duration_ms": int(duration) if isinstance(duration, (int, float)) and 0 <= duration < 3_600_000 else None,
        "seq": int(seq) if isinstance(seq, int) and 0 <= seq < 10_000_000 else 0,
        "detail": detail or None,
    }


async def _redactor(organization_id: str):
    from app.ai.llm.pii.loader import load_redactor_for_org
    from app.dependencies import async_session_maker

    try:
        return await load_redactor_for_org(organization_id, async_session_maker)
    except Exception:
        return None


async def record(db, *, organization_id: str, artifact_id: str, report_id: Optional[str],
                 user_id: Optional[str], session_id: str, source: str, events: Iterable[dict],
                 artifact_version: Optional[int] = None, commit: bool = True) -> int:
    """Store cleaned events and prune. Returns how many were stored."""
    assert source in SOURCES
    rows = [e for e in (clean_event(e) for e in list(events)[:MAX_BATCH]) if e]
    if not rows:
        return 0
    redactor = await _redactor(organization_id)
    for row in rows:
        if redactor is not None and row["detail"]:
            row["detail"] = redactor.redact_deep(row["detail"])
        db.add(ArtifactActivityEvent(
            organization_id=str(organization_id), artifact_id=str(artifact_id),
            artifact_version=artifact_version, report_id=str(report_id) if report_id else None,
            user_id=str(user_id) if user_id else None, session_id=str(session_id)[:64],
            source=source, **row,
        ))
    await db.flush()
    await prune(db, report_id=report_id, artifact_id=artifact_id)
    if commit:
        await db.commit()
    return len(rows)


async def prune(db, *, report_id: Optional[str] = None, artifact_id: Optional[str] = None,
                now: Optional[datetime] = None) -> None:
    """Drop events past retention, and all but the newest KEEP_PER_REPORT
    for this report (or artifact, when the event has no report)."""
    now = now or datetime.utcnow()
    await db.execute(delete(ArtifactActivityEvent).where(
        ArtifactActivityEvent.created_at < now - timedelta(days=RETENTION_DAYS)))
    column, value = (ArtifactActivityEvent.report_id, report_id) if report_id else \
        (ArtifactActivityEvent.artifact_id, artifact_id)
    if value is None:
        return
    cutoff = (await db.execute(
        select(ArtifactActivityEvent.created_at).where(column == str(value))
        .order_by(ArtifactActivityEvent.created_at.desc()).offset(KEEP_PER_REPORT).limit(1)
    )).scalar_one_or_none()
    if cutoff is not None:
        await db.execute(delete(ArtifactActivityEvent).where(
            column == str(value), ArtifactActivityEvent.created_at <= cutoff))


async def mark_shown(db, *, session_id: str, completion_id: str, seqs: Optional[Iterable[int]] = None,
                     match: Optional[Callable[[Optional[dict]], bool]] = None) -> None:
    """Stamp one session's unstamped events as shown in ``completion_id`` (only
    those with a ``seq`` in ``seqs``, or all of them), so ``context_section``
    skips them. With ``seqs``, ``match`` also selects error/console events
    without a seq (0) by their ``detail``. One UPDATE."""
    unstamped = (ArtifactActivityEvent.session_id == str(session_id)[:64],
                 ArtifactActivityEvent.shown_in_completion_id.is_(None))
    q = update(ArtifactActivityEvent).where(*unstamped)
    if seqs is not None:
        seqs = sorted({int(s) for s in seqs if s})
        ids = []
        if match is not None:
            rows = (await db.execute(select(ArtifactActivityEvent.id, ArtifactActivityEvent.detail).where(
                *unstamped, ArtifactActivityEvent.seq == 0,
                ArtifactActivityEvent.kind.in_(("error", "console"))))).all()
            ids = [row.id for row in rows if match(row.detail)]
        if not seqs and not ids:
            return
        q = q.where(or_(ArtifactActivityEvent.seq.in_(seqs), ArtifactActivityEvent.id.in_(ids)))
    await db.execute(q.values(shown_in_completion_id=str(completion_id)))
    await db.commit()


# ── reading ────────────────────────────────────────────────────────────────

def line(e: ArtifactActivityEvent) -> str:
    """One compact, readable line for the agent."""
    d = e.detail or {}
    when = e.created_at.strftime("%H:%M:%S") if e.created_at else ""
    who = "preview" if e.source == "preview" else "editor"
    if e.kind == "click":
        text = f"clicked {d.get('target') or 'an element'}"
    elif e.kind == "request":
        what = e.op or "request"
        extra = ", ".join(str(x) for x in (d.get("resource"), d.get("action")) if x)
        text = f"{what}{f' ({extra})' if extra else ''} -> {e.status or '?'}"
        if e.error_code:
            text += f" {e.error_code}"
        if d.get("message") and e.status == "error":
            text += f": {d['message']}"
        if e.duration_ms is not None:
            text += f" [{e.duration_ms} ms]"
    elif e.kind == "stream":
        text = f"{e.op or 'stream'} ended {e.status or '?'}"
        if e.error_code:
            text += f" {e.error_code}"
        if d.get("message"):
            text += f": {d['message']}"
    elif e.kind == "filter":
        text = f"filter {json.dumps(d.get('params'), default=str)[:160]}"
        if d.get("queries") is not None:
            text += f" -> {d.get('queries')} queries"
        if d.get("rows") is not None:
            text += f", {d.get('rows')} rows"
        if e.status == "error":
            text += f" failed{': ' + d['message'] if d.get('message') else ''}"
    else:  # error / console
        text = f"{e.kind}: {d.get('message') or e.error_code or 'unknown'}"
    version = f" v{e.artifact_version}" if e.artifact_version is not None else ""
    return f"{when} {who}{version} {text}".strip()


def is_failure(e: ArtifactActivityEvent) -> bool:
    return e.kind in {"error", "console"} or e.status in {"error", "interrupted"}


async def recent(db, *, report_id: str, artifact_id: Optional[str] = None, since: Optional[datetime] = None,
                 kinds: Optional[list[str]] = None, only_errors: bool = False, source: Optional[str] = None,
                 limit: int = 60) -> list[ArtifactActivityEvent]:
    """Events for a report (or one of its artifacts), oldest first."""
    q = select(ArtifactActivityEvent).where(ArtifactActivityEvent.report_id == str(report_id))
    if artifact_id:
        q = q.where(ArtifactActivityEvent.artifact_id == str(artifact_id))
    if since:
        q = q.where(ArtifactActivityEvent.created_at >= since)
    if kinds:
        q = q.where(ArtifactActivityEvent.kind.in_([k for k in kinds if k in KINDS]))
    if source in SOURCES:
        q = q.where(ArtifactActivityEvent.source == source)
    rows = list((await db.execute(
        q.order_by(ArtifactActivityEvent.created_at.desc(), ArtifactActivityEvent.seq.desc()).limit(500)
    )).scalars())
    rows.reverse()
    if only_errors:
        rows = [r for r in rows if is_failure(r)]
    return rows[-limit:]


async def read(db, *, report_id: str, artifact_id: Optional[str] = None, since_minutes: Optional[int] = None,
               kinds: Optional[list[str]] = None, only_errors: bool = False, source: Optional[str] = None,
               event_id: Optional[str] = None, limit: int = 40) -> dict:
    """What the ``read_artifact_activity`` tool returns."""
    if event_id:
        e = await db.get(ArtifactActivityEvent, str(event_id))
        if e is None or str(e.report_id) != str(report_id):
            return {"events": [], "error": "Event not found in this report"}
        return {"event": {"id": e.id, "line": line(e), "kind": e.kind, "op": e.op, "status": e.status,
                          "error_code": e.error_code, "duration_ms": e.duration_ms, "source": e.source,
                          "artifact_id": e.artifact_id, "artifact_version": e.artifact_version,
                          "created_at": e.created_at.isoformat() if e.created_at else None,
                          "detail": e.detail or {}}}
    since = datetime.utcnow() - timedelta(minutes=since_minutes) if since_minutes else None
    rows = await recent(db, report_id=report_id, artifact_id=artifact_id, since=since, kinds=kinds,
                        only_errors=only_errors, source=source, limit=max(1, min(limit, 100)))
    return {
        # artifact_version is the version number (v1, v2...), as in each line;
        # artifact_id is the app, shared by all its versions.
        "events": [{"id": e.id, "line": line(e), "failed": is_failure(e), "artifact_id": e.artifact_id,
                    "artifact_version": e.artifact_version} for e in rows],
        "failures": sum(1 for e in rows if is_failure(e)),
        "retention": f"last {RETENTION_DAYS} days, newest {KEEP_PER_REPORT} per conversation",
    }


async def context_section(db, *, report_id: str, completion_id: Optional[str] = None,
                          mark: bool = True) -> Optional[str]:
    """The automatic per-turn section: failures the agent has not seen yet,
    each with the few steps before it. None when there are none.
    With ``mark``, stamps them (and their steps) with ``completion_id``."""
    rows = await recent(db, report_id=report_id, limit=200)
    unseen = [i for i, e in enumerate(rows) if e.shown_in_completion_id is None and is_failure(e)]
    if not unseen:
        return None
    keep: list[int] = []
    for i in unseen[-CONTEXT_LINES:]:
        for j in range(max(0, i - STEPS_BEFORE), i + 1):
            if j not in keep and (j == i or rows[j].shown_in_completion_id is None):
                keep.append(j)
    keep.sort()
    keep = keep[-(CONTEXT_LINES * 2):]
    lines, last_session = [], None
    for j in keep:
        e = rows[j]
        if last_session is not None and e.session_id != last_session:
            lines.append("  ...")
        last_session = e.session_id
        mark_ = "!" if is_failure(e) else " "
        lines.append(f"{mark_} {line(e)}  (event {e.id[:8]})")
    text = "\n".join(lines)
    if len(text) > 6000:
        text = text[-6000:]
    if mark and completion_id:
        ids = list({rows[j].id for j in keep} | {rows[i].id for i in unseen})
        await db.execute(update(ArtifactActivityEvent).where(ArtifactActivityEvent.id.in_(ids))
                         .values(shown_in_completion_id=str(completion_id)))
        await db.commit()
    return text
