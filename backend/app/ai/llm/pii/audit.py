"""Audit a prompt that PII protection refused to send.

Called from the LLM chokepoint, which has no request or DB session of its own:
the event goes on the durable audit queue (app.ee.audit.tool_audit), the same
path AI tools use. Only rule ids/names, counts, the provider, model and policy
are recorded — never the matched values.
"""

from __future__ import annotations

import logging
import types
from typing import Optional

from app.ai.llm.usage_attribution import get_usage_attribution

logger = logging.getLogger(__name__)

ACTION_PROMPT_BLOCKED = "pii_protection.prompt_blocked"


async def record_prompt_blocked(
    *,
    organization_id: Optional[str],
    provider_id: Optional[str],
    provider_type: Optional[str],
    model_id: Optional[str],
    error,
) -> None:
    try:
        from app.ee.audit.tool_audit import _build_event, enqueue_event

        attribution = get_usage_attribution()
        org_id = organization_id or attribution.get("organization_id")
        if not org_id:
            return
        user_id = attribution.get("user_id")
        runtime_ctx = {
            "organization": types.SimpleNamespace(id=str(org_id)),
            "user": types.SimpleNamespace(id=str(user_id)) if user_id else None,
            "agent_execution_id": attribution.get("agent_execution_id"),
        }
        event = _build_event(
            runtime_ctx,
            ACTION_PROMPT_BLOCKED,
            "llm_provider",
            provider_id,
            {
                "policy_id": getattr(error, "policy_id", None),
                "policy_name": getattr(error, "policy_name", None),
                "rules": list(getattr(error, "rule_names", []) or []),
                "provider_type": provider_type,
                "model_id": model_id,
                "report_id": attribution.get("report_id"),
            },
            outcome="denied",
            failure_reason="pii_blocked",
        )
        if event is not None:
            await enqueue_event(event)
    except Exception:  # auditing must never change the outcome of the call
        logger.warning("PII block audit failed", exc_info=True)
