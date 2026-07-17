"""Machine-initiated turns — the webhook three-completion idiom, generalized.

A machine wake (eval run finished, wait elapsed, ...) should not impersonate
the user. This helper reproduces the pattern ``WebhookService`` established:

1. a **visible event entry** — ``role='external'``, rendered by the UI as a
   compact event strip (👀 while the follow-up runs, ✅/❌ when it ends);
2. a **hidden trigger** — the ``role='user'`` prompt the agent actually
   answers, stamped with ``trigger_source`` so ``get_completions_v2``
   filters it from the timeline;
3. the agent's normal ``role='system'`` reply.

Callers run in background tasks / scheduler jobs and pass their own session.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from app.models.completion import Completion

logger = logging.getLogger(__name__)


async def run_machine_turn(
    session,
    *,
    report,
    user,
    organization,
    summary: str,
    trigger_source: str,
    message_type: str,
    instruction: str,
    details: Optional[str] = None,
    mode: Optional[str] = None,
) -> None:
    """Post a visible machine event on ``report`` and run the agent on
    ``instruction`` as a hidden trigger. Blocks until the agent turn ends;
    the event entry's status mirrors the outcome."""
    from app.schemas.completion_schema import PromptSchema
    from app.schemas.completion_v2_schema import CompletionCreate
    from app.services.completion_service import CompletionService

    last = (
        await session.execute(
            select(Completion)
            .where(Completion.report_id == str(report.id))
            .order_by(Completion.turn_index.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    turn = (last.turn_index + 1) if last else 0

    event = Completion(
        prompt={"content": summary, "summary": summary, **({"details": details} if details else {})},
        completion={"content": ""},
        model=trigger_source,
        report_id=str(report.id),
        turn_index=turn,
        message_type=message_type,
        role="external",
        status="in_progress",
        user_id=str(user.id),
        # Drives the event-strip icon in the UI (same field webhooks use for
        # their source icon); no external system is actually involved.
        external_platform=trigger_source,
        trigger_source=trigger_source,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)

    try:
        await CompletionService().create_completion(
            db=session,
            report_id=str(report.id),
            completion_data=CompletionCreate(
                prompt=PromptSchema(content=instruction, mode=mode)
            ),
            current_user=user,
            organization=organization,
            background=False,
            trigger_source=trigger_source,
        )
        event.status = "success"
        await session.commit()
    except Exception as e:
        logger.error(f"machine turn ({trigger_source}) on report {report.id} failed: {e}")
        try:
            event.status = "error"
            event.completion = {"content": f"Follow-up failed: {e}"}
            await session.commit()
        except Exception:
            pass
        raise
