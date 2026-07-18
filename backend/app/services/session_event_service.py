"""SessionEventService — emit silent session-event completions.

A session event is a ``role='event'`` :class:`Completion` row (see
``app.ai.context.session_events``). Emitting one is a plain insert + the
existing ``after_insert_completion`` websocket broadcast — no LLM, no agent
run, and it must never fail the user action that triggered it. Use
:meth:`emit_safe` from fire-and-forget hook points (feedback service, share
routes, file up/remove) exactly like the audit/telemetry calls already scattered
through those services.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.completion import Completion
from app.ai.context.session_events import EVENT_ROLE, default_event_content

logger = logging.getLogger(__name__)


class SessionEventService:

    @staticmethod
    async def emit(
        db: AsyncSession,
        *,
        report,
        kind: str,
        user=None,
        user_id: Optional[str] = None,
        actor_name: Optional[str] = None,
        content: Optional[str] = None,
        meta: Optional[dict] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        commit: bool = True,
    ) -> Completion:
        """Insert a silent ``role='event'`` completion on ``report``.

        turn_index deliberately does NOT increment — a silent event does not
        start a turn, so it carries the last turn's index and is ordered purely
        by ``created_at`` (which is what both the context builder and the UI
        sort on). ``content`` is the human/LLM fallback string; ``meta`` carries
        the structured, per-kind payload consumed by the UI component and by the
        builder's displaced-target reference. ``actor_name`` (or the passed
        ``user``'s name) is baked into the default text — "Dana thumbed down…"
        rather than the ambiguous "user thumbed down…" in a shared report.
        """
        uid = user_id or (str(user.id) if user is not None else None)
        actor = (
            actor_name
            or getattr(user, "name", None)
            or getattr(user, "email", None)
            or "user"
        )

        last_turn = (
            await db.execute(
                select(Completion.turn_index)
                .where(Completion.report_id == str(report.id))
                .order_by(Completion.turn_index.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        turn = int(last_turn) if last_turn is not None else 0

        meta = dict(meta or {})
        if target_type is not None:
            meta.setdefault("target_type", target_type)
        if target_id is not None:
            meta.setdefault("target_id", target_id)
        # Record the actor so the UI can render a name/avatar without re-deriving.
        meta.setdefault("actor", actor)

        text = content or default_event_content(kind, meta, actor=actor)

        prompt: dict[str, Any] = {"content": text, "summary": text, "meta": meta}
        if target_type is not None:
            prompt["target_type"] = target_type
        if target_id is not None:
            prompt["target_id"] = target_id

        event = Completion(
            prompt=prompt,
            completion={"content": ""},
            model=EVENT_ROLE,
            report_id=str(report.id),
            turn_index=turn,
            message_type=kind,
            role=EVENT_ROLE,
            status="success",
            user_id=uid,
        )
        db.add(event)
        if commit:
            await db.commit()
            await db.refresh(event)
        return event

    @staticmethod
    async def emit_safe(db: AsyncSession, **kwargs) -> Optional[Completion]:
        """Fire-and-forget wrapper: never raises. Use at hook points where a
        failed event write must not break the user action."""
        try:
            return await SessionEventService.emit(db, **kwargs)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("SessionEventService.emit_safe swallowed error: %s", e)
            try:
                await db.rollback()
            except Exception:
                pass
            return None
