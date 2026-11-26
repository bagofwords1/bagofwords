"""Audit entries for agent sandboxes.

Every entry goes through the existing audit service, which records regardless
of licence (viewing the audit log is what is gated). The sandbox or report
name goes in ``details.title`` so audit search finds it.

Details are built by the callers from names, ids, counts and hashes only.
Secret values, tokens, keys, passwords and run output never go in.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def audit(db: AsyncSession, organization_id: str, action: str, actor_id: Optional[str],
                resource_type: str, resource_id: Optional[str], details: Dict[str, Any], *,
                outcome: Optional[str] = None) -> None:
    """Record one event in the caller's transaction. Never raises: a failed
    audit write is logged, not turned into a failed sandbox operation."""
    try:
        from app.ee.audit.service import audit_service

        await audit_service.log(
            db=db, organization_id=str(organization_id), action=action,
            user_id=str(actor_id) if actor_id else None,
            resource_type=resource_type, resource_id=str(resource_id) if resource_id else None,
            details=details, commit=False, outcome=outcome,
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not write sandbox audit entry %s", action)
