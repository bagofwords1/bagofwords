"""Distinguish app conversations from the read-only dashboard chat surface."""


def is_app_conversation(platform_context) -> bool:
    return isinstance(platform_context, dict) and platform_context.get("surface") == "app"


def is_dashboard_chat(report, platform_context=None) -> bool:
    # Both surfaces share the artifact_chat report type. Missing/unknown context
    # retains the restrictive dashboard behavior; permissions are checked separately.
    return getattr(report, "report_type", "regular") == "artifact_chat" and not is_app_conversation(platform_context)


async def parent_app_surface(db, system_completion_id):
    """Carry a turn's app surface into its children and continuations.

    Resolve the exact parent turn, not the latest message: a report may also
    receive dashboard-bubble turns, whose read-only policy must stay separate.
    """
    from app.models.completion import Completion

    system = await db.get(Completion, system_completion_id) if system_completion_id else None
    head = await db.get(Completion, system.parent_id) if system and system.parent_id else None
    prompt = head.prompt if head and isinstance(head.prompt, dict) else {}
    context = prompt.get("platform_context")
    if is_app_conversation(context):
        return {"platform": prompt.get("platform"), "platform_context": context}
    return {}
