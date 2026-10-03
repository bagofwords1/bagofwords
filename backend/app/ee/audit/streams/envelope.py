# Audit event envelope (v1) — the stable public shape every log stream and the
# export endpoint emit. Built from an AuditLog row; independent of the table
# schema so the table can evolve without breaking SIEM parsers.
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

from datetime import datetime, timezone
from typing import Any, Optional

ENVELOPE_VERSION = 1


def actor_type(user_id: Optional[str], details: Optional[dict]) -> str:
    """user | agent | system — shared with the UI's actor marker."""
    if isinstance(details, dict) and details.get("agent_execution_id"):
        return "agent"
    if user_id:
        return "user"
    return "system"


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_envelope(
    log: Any,
    *,
    user_email: Optional[str] = None,
    organization_name: Optional[str] = None,
) -> dict:
    """Map an AuditLog (or anything with its attributes) to envelope v1."""
    details = log.details if isinstance(log.details, dict) else ({} if log.details is None else {"value": log.details})
    actor = {"type": actor_type(log.user_id, details), "id": log.user_id, "email": user_email}
    targets = []
    if log.resource_type or log.resource_id:
        target = {"type": log.resource_type, "id": log.resource_id}
        if details.get("title"):
            target["name"] = details["title"]
        targets.append(target)
    return {
        "id": log.id,
        "version": ENVELOPE_VERSION,
        "action": log.action,
        "occurred_at": iso_utc(log.created_at),
        "organization": {"id": log.organization_id, "name": organization_name},
        "actor": actor,
        "targets": targets,
        "context": {"ip_address": log.ip_address, "user_agent": log.user_agent},
        "metadata": details,
    }
