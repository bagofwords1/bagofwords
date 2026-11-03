"""Trusted language provenance for background conversation continuations."""
from sqlalchemy import select
from app.models.completion import Completion
from app.ai.response_language import SUPPORTED_LOCALES


async def machine_response_context(session, report_id: str, *, parent_completion_id=None,
                                   original_request=None) -> dict:
    # A delayed wave follows its originating turn, never a newer user's turn.
    query = select(Completion).where(Completion.report_id == report_id)
    if parent_completion_id:
        query = query.where(Completion.id == str(parent_completion_id))
    else:
        query = query.where(
            Completion.role == 'system',
            (Completion.trigger_source.is_(None)) | (Completion.trigger_source != 'sub_agent'),
        ).order_by(Completion.turn_index.desc()).limit(1)
    parent = (await session.execute(query)).scalars().first()
    result = {}
    if parent:
        result['response_parent_completion_id'] = str(parent.id)
        saved = (parent.prompt or {}).get('response_locale')
        if saved in SUPPORTED_LOCALES:
            result['response_locale'] = saved
        # Legacy completions: inspect the actual parent head even when the
        # caller has a cached prompt. A cached machine wrapper is not a user ask.
        if parent.parent_id:
            head = (await session.execute(select(Completion).where(
                Completion.id == parent.parent_id, Completion.report_id == report_id,
                Completion.role == 'user',
            ))).scalars().first()
            if head and not head.trigger_source:
                original_request = (head.prompt or {}).get('content')
            elif head:
                original_request = (head.prompt or {}).get('response_original_request')
                if not original_request:
                    human = (await session.execute(select(Completion).where(
                        Completion.report_id == report_id, Completion.role == 'user',
                        Completion.trigger_source.is_(None),
                        Completion.turn_index < parent.turn_index,
                    ).order_by(Completion.turn_index.desc()).limit(1))).scalars().first()
                    original_request = (human.prompt or {}).get('content') if human else None
    if original_request:
        result['response_original_request'] = original_request
    return result


def completion_response_metadata(prompt: dict | None, trigger_source: str | None) -> dict | None:
    """Seed a machine answer before it becomes visible; ignore user-supplied hints."""
    if not trigger_source or not prompt:
        return None
    locale = prompt.get('response_locale')
    if locale not in SUPPORTED_LOCALES:
        return None
    return {'response_locale': locale}
