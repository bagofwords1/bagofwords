# Audit event envelope — the stable public shape every log stream and the
# export endpoint emit. Built from an AuditLog row; independent of the table
# schema so the table can evolve without breaking SIEM parsers.
#
# Version 1 is additive (docs/design/audit-event-contract.md, "Envelope"):
# every key a v1 consumer parses is still there with the same meaning, and the
# contract fields are added next to them. ``schema_version`` says whether the
# *record* was written under the full contract (2) or before it (1, legacy:
# outcome inferred, event time equals recording time, no correlation).
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

from datetime import datetime, timezone
from typing import Any, Optional

from app.ee.audit.content_policy import sanitize_details
from app.ee.audit.contract import LEGACY_SCHEMA_VERSION, category_for, infer_outcome

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
    """Map an AuditLog (or anything with its attributes) to the envelope."""
    raw = log.details if isinstance(log.details, dict) else ({} if log.details is None else {"value": log.details})
    # Legacy rows were written before the content policy existed.
    details = sanitize_details(raw) or {}
    schema_version = getattr(log, "schema_version", None) or LEGACY_SCHEMA_VERSION
    legacy = schema_version < 2

    snapshot = getattr(log, "actor_snapshot", None) if not legacy else None
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    stored_type = getattr(log, "actor_type", None) if not legacy else None
    actor = {
        "type": stored_type or actor_type(log.user_id, details),
        "id": log.user_id,
        # Identity at event time wins; the live join is only a fallback for
        # legacy rows, so renames and deletions never rewrite evidence.
        "email": snapshot.get("email") or user_email,
        "name": snapshot.get("name"),
    }
    targets = []
    if log.resource_type or log.resource_id:
        target = {"type": log.resource_type, "id": log.resource_id}
        if details.get("title"):
            target["name"] = details["title"]
        targets.append(target)

    occurred = getattr(log, "occurred_at", None) or log.created_at
    outcome = getattr(log, "outcome", None) or infer_outcome(log.action)
    request_id = getattr(log, "request_id", None)
    session_id = getattr(log, "session_id", None)
    execution_id = details.get("agent_execution_id")
    return {
        "id": log.id,
        "version": ENVELOPE_VERSION,
        "schema_version": schema_version,
        "legacy": legacy,
        "action": log.action,
        "category": category_for(log.action),
        "occurred_at": iso_utc(occurred),
        "recorded_at": iso_utc(log.created_at),
        "outcome": outcome,
        "failure_reason": getattr(log, "failure_reason", None),
        "organization": {"id": log.organization_id, "name": organization_name},
        "actor": actor,
        "targets": targets,
        "context": {"ip_address": log.ip_address, "user_agent": log.user_agent},
        "correlation": {
            "request_id": request_id,
            "session_id": session_id,
            "parent_id": details.get("parent_request_id"),
            "execution_id": execution_id,
        },
        "metadata": details,
    }
