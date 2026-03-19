# Tool-level audit logging helper
# Creates its own DB session to avoid sharing the agent's long-lived session.
# All calls are fire-and-forget: audit failures never break tool execution.

import logging
from typing import Optional, Dict, Any

from app.ee.audit.service import audit_service

logger = logging.getLogger(__name__)

# Max length for individual query strings stored in audit details
_MAX_QUERY_LEN = 500
_MAX_QUERIES = 10


def _truncate_queries(queries: list) -> list:
    """Truncate query strings to keep audit detail payload reasonable."""
    truncated = []
    for q in (queries or [])[:_MAX_QUERIES]:
        s = str(q) if q else ""
        truncated.append(s[:_MAX_QUERY_LEN] + ("..." if len(s) > _MAX_QUERY_LEN else ""))
    return truncated


async def log_tool_audit(
    runtime_ctx: Dict[str, Any],
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Fire-and-forget audit log from within an AI tool execution.

    Extracts user/org/execution metadata from runtime_ctx and writes
    an audit entry using its own short-lived DB session.
    """
    try:
        from app.dependencies import async_session_maker

        user = runtime_ctx.get("user")
        organization = runtime_ctx.get("organization")
        org_id = str(organization.id) if organization else None
        user_id = str(user.id) if user else None

        if not org_id:
            logger.debug("log_tool_audit skipped: no organization in runtime_ctx")
            return

        # Enrich details with execution context
        enriched = dict(details or {})
        agent_execution_id = runtime_ctx.get("agent_execution_id")
        mode = runtime_ctx.get("mode")
        if agent_execution_id:
            enriched.setdefault("agent_execution_id", str(agent_execution_id))
        if mode:
            enriched.setdefault("execution_mode", str(mode))

        async with async_session_maker() as session:
            await audit_service.log(
                db=session,
                organization_id=org_id,
                action=action,
                user_id=user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                details=enriched if enriched else None,
            )
    except Exception:
        logger.debug("log_tool_audit failed", exc_info=True)
