import json
from typing import Optional

from .generic_adapter import GenericAdapter


class JiraAdapter(GenericAdapter):
    """Jira webhooks. Jira typically can't HMAC-sign, so these usually run in
    ``token`` (Jira Cloud, custom header) or ``url_token`` (Jira Server) mode.
    HMAC verification falls back to the generic BOW scheme when configured.
    """

    source = "jira"

    def event_id(self, headers: dict, payload: dict) -> Optional[str]:
        return (
            headers.get("x-bow-delivery")
            or headers.get("x-atlassian-webhook-identifier")
            or str(payload.get("timestamp") or "")
            or None
        )

    def normalize(self, headers: dict, payload: dict) -> dict:
        event_key = payload.get("webhookEvent") or payload.get("issue_event_type_name") or "jira.event"
        issue = payload.get("issue") or {}
        fields = issue.get("fields") or {}
        key = issue.get("key") or ""
        title = fields.get("summary") or ""
        reporter = (fields.get("reporter") or {}).get("displayName") or "?"
        status = ((fields.get("status") or {}).get("name")) or ""

        summary = f"Jira {event_key}: {key} {title}".strip()
        details_lines = [
            f"event: {event_key}",
            f"issue: {key}",
            f"summary: {title}",
            f"reporter: {reporter}",
            f"status: {status}",
            f"priority: {(fields.get('priority') or {}).get('name') or ''}",
            f"description: {str(fields.get('description') or '')[:1500]}",
        ]
        if not issue:
            try:
                details_lines = [f"event: {event_key}", json.dumps(payload, indent=1, default=str)[:1800]]
            except Exception:
                details_lines = [f"event: {event_key}", str(payload)[:1800]]

        return {
            "summary": summary,
            "details": "\n".join(details_lines),
            "raw": payload,
            "event_key": str(event_key),
        }
