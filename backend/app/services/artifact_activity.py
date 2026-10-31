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
* **Payloads** (what a request carried, what came back, a stream's events)
  are a separate, optional row per event (``ArtifactActivityPayload``):
  private to the person whose session produced it, encrypted at rest,
  PII-redacted on the way in, each part bounded to ``MAX_PAYLOAD_CHARS``,
  kept ``PAYLOAD_RETENTION_HOURS``. Inspect shows a payload to its owner
  only; the agent gets it through the org's data gate: the full (redacted)
  payload when the model may see data and the event is the current user's,
  otherwise its ``shape`` (keys, types, counts) -- as step results do.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import delete, or_, select, update

from app.models.artifact_activity import KINDS, SOURCES, ArtifactActivityEvent, ArtifactActivityPayload

RETENTION_DAYS = 7
KEEP_PER_REPORT = 500
MAX_BATCH = 50
MAX_DETAIL_CHARS = 2000
MAX_TEXT = 300
CONTEXT_LINES = 10
STEPS_BEFORE = 3
PAYLOAD_RETENTION_HOURS = 24
MAX_PAYLOAD_CHARS = 32_000   # request, response and stream, each, serialized
MAX_STREAM_EVENTS = 200
MAX_STREAM_TEXT = 500
PAYLOAD_KINDS = {"request", "stream"}

# Detail keys a client may send, per kind. Anything else is dropped.
DETAIL_KEYS = {
    "request": {"resource", "action", "agents", "message", "http_status", "bytes", "rows", "simulated"},
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


# ── payloads ───────────────────────────────────────────────────────────────

def _size(value: Any) -> int:
    return len(json.dumps(value, default=str, ensure_ascii=False))


def _bound(value: Any, limit: int = MAX_PAYLOAD_CHARS) -> Any:
    """``value`` within ``limit`` serialized characters. A grid loses rows
    from the end and says so; anything else is replaced by an honest stub."""
    if _size(value) <= limit:
        return value
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        rows = value["rows"]
        kept = len(rows)
        while kept > 0:
            kept = kept // 2
            trial = {**value, "rows": rows[:kept], "truncated": {"rows_kept": kept, "rows_total": len(rows)}}
            if _size(trial) <= limit:
                return trial
    text = json.dumps(value, default=str, ensure_ascii=False)
    return {"truncated": True, "chars": len(text), "preview": text[: max(0, limit - 200)]}


def _stream_event(e: dict) -> dict:
    """One stream event, kept small: its type and scalar fields, text cut
    to MAX_STREAM_TEXT, a result reduced to its columns and row count."""
    out: dict = {}
    for k, v in e.items():
        if k in ("rows",):
            out["rows"] = len(v) if isinstance(v, list) else v
        elif isinstance(v, str):
            out[k] = v[:MAX_STREAM_TEXT] + ("…" if len(v) > MAX_STREAM_TEXT else "")
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list) and k == "columns":
            out[k] = [str(c.get("field") if isinstance(c, dict) else c)[:64] for c in v[:40]]
        else:
            out[k] = _bound(v, 1000)
    return out


def clean_payload(raw: Any) -> Optional[dict]:
    """A client payload reduced to request / response / stream, each
    bounded; None when there is nothing usable."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for key in ("request", "response", "stream"):
        value = raw.get(key)
        if value is None:
            continue
        if key == "stream":
            if not isinstance(value, list):
                continue
            events = [_stream_event(e) for e in value[:MAX_STREAM_EVENTS] if isinstance(e, dict)]
            value = events
            if len(raw["stream"]) > MAX_STREAM_EVENTS:
                value = events + [{"type": "truncated", "events_total": len(raw["stream"])}]
        out[key] = _bound(value)
    return out or None


def _shape_value(value: Any, depth: int = 0) -> Any:
    """Keys, types and counts: what a payload looks like without its data."""
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        if isinstance(value.get("rows"), list):
            rows = value["rows"]
            columns = value.get("columns")
            names = [str(c.get("field") if isinstance(c, dict) else c) for c in columns[:40]] if isinstance(columns, list) \
                else (sorted(rows[0].keys())[:40] if rows and isinstance(rows[0], dict) else [])
            return {"rows": len(rows), "columns": names, **({"truncated": value["truncated"]} if value.get("truncated") else {})}
        return {str(k)[:64]: _shape_value(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return {"list": len(value), "item": _shape_value(value[0], depth + 1) if value else None}
    if isinstance(value, str):
        return f"str({len(value)})"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return None if value is None else type(value).__name__


def shape(payload: dict) -> dict:
    """The data-free view of a payload: what the agent gets when the org
    hides data from the model, or when the payload is someone else's."""
    out: dict = {}
    if payload.get("request") is not None:
        out["request"] = _shape_value(payload["request"])
    if payload.get("response") is not None:
        out["response"] = _shape_value(payload["response"])
    if isinstance(payload.get("stream"), list):
        events = payload["stream"]
        types: dict[str, int] = {}
        text_chars = 0
        for e in events:
            t = str(e.get("type") or "?")
            types[t] = types.get(t, 0) + 1
            if t in ("text_delta", "text") and isinstance(e.get("text"), str):
                text_chars += len(e["text"])
        out["stream"] = {"events": len(events), "types": types, "text_chars": text_chars,
                         "ended": str(events[-1].get("type")) if events else None}
    return out


async def payload_for(db, event_id: str) -> Optional[ArtifactActivityPayload]:
    return (await db.execute(select(ArtifactActivityPayload).where(
        ArtifactActivityPayload.event_id == str(event_id)))).scalar_one_or_none()


def payload_view(p: ArtifactActivityPayload, *, user_id: Optional[str], allow_data: bool) -> dict:
    """What a reader may see of a payload: the data when it is theirs and
    the gate allows, else its shape -- and that it is someone else's."""
    full = {"request": p.request, "response": p.response, "stream": p.stream}
    mine = user_id is not None and str(p.user_id) == str(user_id)
    if mine and allow_data:
        return {"payload": full}
    return {"payload_shape": shape(full), **({} if mine else {"payload_private": True})}


async def _redactor(organization_id: str) -> tuple:
    """(redactor, ok). ``redactor`` None with ``ok`` means nothing to redact
    (feature off, no rules); ``ok`` False means the loader failed, and
    payloads are then dropped rather than stored unredacted. (When the
    checked loader lands -- ``load_redactor_for_org_checked`` -- use it here:
    it also reports rules that exist but could not be read.)"""
    from app.ai.llm.pii.loader import load_redactor_for_org
    from app.dependencies import async_session_maker

    try:
        return await load_redactor_for_org(organization_id, async_session_maker), True
    except Exception:
        return None, False


async def record(db, *, organization_id: str, artifact_id: str, report_id: Optional[str],
                 user_id: Optional[str], session_id: str, source: str, events: Iterable[dict],
                 artifact_version: Optional[int] = None, commit: bool = True) -> int:
    """Store cleaned events and prune. Returns how many were stored."""
    assert source in SOURCES
    raw_events = list(events)[:MAX_BATCH]
    rows = [(clean_event(e), clean_payload(e.get("payload")) if isinstance(e, dict) else None) for e in raw_events]
    rows = [(row, payload) for row, payload in rows if row]
    if not rows:
        return 0
    redactor, redaction_ok = await _redactor(organization_id)
    stored = []
    for row, payload in rows:
        if redactor is not None and row["detail"]:
            row["detail"] = redactor.redact_deep(row["detail"])
        event = ArtifactActivityEvent(
            organization_id=str(organization_id), artifact_id=str(artifact_id),
            artifact_version=artifact_version, report_id=str(report_id) if report_id else None,
            user_id=str(user_id) if user_id else None, session_id=str(session_id)[:64],
            source=source, **row,
        )
        db.add(event)
        # A payload needs an owner to be private to, and the PII rules to have
        # loaded: without either it is dropped, never stored unredacted.
        if payload and user_id and redaction_ok and row["kind"] in PAYLOAD_KINDS:
            stored.append((event, {k: (redactor.redact_deep(v) if redactor is not None else v)
                                   for k, v in payload.items()}))
    await db.flush()
    for event, payload in stored:
        db.add(ArtifactActivityPayload(event_id=str(event.id), organization_id=str(organization_id),
                                       user_id=str(user_id), request=payload.get("request"),
                                       response=payload.get("response"), stream=payload.get("stream")))
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
    # Payloads go first and sooner: their own window, and with their event
    # (SQLite does not always honor ON DELETE CASCADE).
    await db.execute(delete(ArtifactActivityPayload).where(
        ArtifactActivityPayload.created_at < now - timedelta(hours=PAYLOAD_RETENTION_HOURS)))
    expired = ArtifactActivityEvent.created_at < now - timedelta(days=RETENTION_DAYS)
    await db.execute(delete(ArtifactActivityPayload).where(ArtifactActivityPayload.event_id.in_(
        select(ArtifactActivityEvent.id).where(expired))))
    await db.execute(delete(ArtifactActivityEvent).where(expired))
    column, value = (ArtifactActivityEvent.report_id, report_id) if report_id else \
        (ArtifactActivityEvent.artifact_id, artifact_id)
    if value is None:
        return
    cutoff = (await db.execute(
        select(ArtifactActivityEvent.created_at).where(column == str(value))
        .order_by(ArtifactActivityEvent.created_at.desc()).offset(KEEP_PER_REPORT).limit(1)
    )).scalar_one_or_none()
    if cutoff is not None:
        beyond = (column == str(value)) & (ArtifactActivityEvent.created_at <= cutoff)
        await db.execute(delete(ArtifactActivityPayload).where(ArtifactActivityPayload.event_id.in_(
            select(ArtifactActivityEvent.id).where(beyond))))
        await db.execute(delete(ArtifactActivityEvent).where(beyond))


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
    """One compact, readable line for the agent: when, who, which version, what."""
    when = e.created_at.strftime("%H:%M:%S") if e.created_at else ""
    who = "preview" if e.source == "preview" else "editor"
    version = f" v{e.artifact_version}" if e.artifact_version is not None else ""
    return f"{when} {who}{version} {describe(e)}".strip()


def describe(e: ArtifactActivityEvent) -> str:
    """What happened, in a few words (the part of ``line`` after its prefix)."""
    d = e.detail or {}
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
        if d.get("rows") is not None:
            text += f", {d['rows']} rows"
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
    return text


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
               event_id: Optional[str] = None, limit: int = 40, user_id: Optional[str] = None,
               allow_data: bool = True) -> dict:
    """What the ``read_artifact_activity`` tool returns. ``user_id`` is who
    asks (payloads are private to their owner); ``allow_data`` is the org's
    "the model may see data" switch: off, a payload comes back as its shape."""
    if event_id:
        e = await db.get(ArtifactActivityEvent, str(event_id))
        if e is None or str(e.report_id) != str(report_id):
            return {"events": [], "error": "Event not found in this report"}
        event = {"id": e.id, "line": line(e), "kind": e.kind, "op": e.op, "status": e.status,
                 "error_code": e.error_code, "duration_ms": e.duration_ms, "source": e.source,
                 "artifact_id": e.artifact_id, "artifact_version": e.artifact_version,
                 "created_at": e.created_at.isoformat() if e.created_at else None,
                 "detail": e.detail or {}}
        p = await payload_for(db, e.id)
        if p is not None:
            event.update(payload_view(p, user_id=user_id, allow_data=allow_data))
        return {"event": event}
    since = datetime.utcnow() - timedelta(minutes=since_minutes) if since_minutes else None
    rows = await recent(db, report_id=report_id, artifact_id=artifact_id, since=since, kinds=kinds,
                        only_errors=only_errors, source=source, limit=max(1, min(limit, 100)))
    with_payload = await payload_event_ids(db, [e.id for e in rows])
    return {
        # artifact_version is the version number (v1, v2...), as in each line;
        # artifact_id is the app, shared by all its versions.
        "events": [{"id": e.id, "line": line(e), "failed": is_failure(e), "artifact_id": e.artifact_id,
                    "artifact_version": e.artifact_version,
                    **({"has_payload": True} if e.id in with_payload else {})} for e in rows],
        "failures": sum(1 for e in rows if is_failure(e)),
        "retention": f"last {RETENTION_DAYS} days, newest {KEEP_PER_REPORT} per conversation; "
                     f"request/response/stream payloads {PAYLOAD_RETENTION_HOURS} hours (pass event_id)",
    }


async def payload_event_ids(db, event_ids: list[str]) -> set[str]:
    """Which of these events have a payload row."""
    if not event_ids:
        return set()
    return {str(x) for x in (await db.execute(select(ArtifactActivityPayload.event_id).where(
        ArtifactActivityPayload.event_id.in_([str(i) for i in event_ids])))).scalars()}


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
    with_payload = await payload_event_ids(db, [rows[j].id for j in keep])
    lines, last_session = [], None
    for j in keep:
        e = rows[j]
        if last_session is not None and e.session_id != last_session:
            lines.append("  ...")
        last_session = e.session_id
        mark_ = "!" if is_failure(e) else " "
        # "+payload": read_artifact_activity(event_id) has what it carried.
        lines.append(f"{mark_} {line(e)}  (event {e.id[:8]}{', +payload' if e.id in with_payload else ''})")
    text = "\n".join(lines)
    if len(text) > 6000:
        text = text[-6000:]
    if mark and completion_id:
        ids = list({rows[j].id for j in keep} | {rows[i].id for i in unseen})
        await db.execute(update(ArtifactActivityEvent).where(ArtifactActivityEvent.id.in_(ids))
                         .values(shown_in_completion_id=str(completion_id)))
        await db.commit()
    return text
