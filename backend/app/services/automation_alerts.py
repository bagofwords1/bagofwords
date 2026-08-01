"""Failure detection and owner alerting for unattended automation runs.

Two facts about the agent shape this module.

**The common failure does not raise.** When the planner exhausts its retries
``agent_v2`` flips the system completion to ``status='error'``, writes a
human-readable message, and returns normally. A caller that only watches for
exceptions sees a successful-looking response with an empty summary — which is
why scheduled tasks used to email subscribers "ran — 0 steps, 0 queries, 0
artifacts" after a failed run. Detection therefore reads the completion's
status, not the absence of an exception.

**Everything has already been tried by the time we get here.** A run reaches
the terminal error path only after provider-level retry (2 sync / 1 streaming),
the planner's in-loop replan, and — where licensed — the fallback chain swapping
models. A run the fallback rescued ends ``success`` and never reaches this
module, so anything reported here is a genuine dead end worth a human's
attention rather than noise.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.completion import Completion
from app.models.organization import Organization
from app.models.user import User

logger = logging.getLogger(__name__)


# Classified causes the owner can actually do something about. Anything else
# gets the generic "the run failed" treatment plus the provider's own words.
_ACTIONABLE_HINTS = {
    "auth": "The model provider rejected our credentials — check the API key on the provider.",
    "quota": "The organization's usage limit was reached.",
    "context_length": "The request was too large for the model's context window — shorten the task or narrow the agents.",
    "rate_limit": "The model provider rate-limited the run. It will be retried on the next scheduled fire.",
    "network": "The model provider could not be reached.",
    "provider_error": "The model provider returned an error.",
}


@dataclass
class RunOutcome:
    """What actually happened in an unattended run."""
    ok: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def hint(self) -> Optional[str]:
        return _ACTIONABLE_HINTS.get(self.error_code or "")


async def run_outcome(db: AsyncSession, report_id: str) -> RunOutcome:
    """Read the outcome of the run that just finished in ``report_id``.

    Reads the newest system completion rather than the response object: the
    status is the authoritative signal, and the classified error the agent
    stashed there survives regardless of what the caller was handed back.
    """
    try:
        completion = (await db.execute(
            select(Completion)
            .where(Completion.report_id == str(report_id), Completion.role == "system")
            .order_by(Completion.created_at.desc())
            .limit(1)
        )).scalars().first()
    except Exception:
        logger.warning("automation: could not read run outcome for report %s", report_id, exc_info=True)
        return RunOutcome(ok=True)  # fail open — never invent a failure

    if completion is None:
        # Nothing ran at all (the agent never got as far as a system turn).
        return RunOutcome(ok=False, error_code="unknown", error_message="The run produced no response.")

    if completion.status != "error":
        return RunOutcome(ok=True)

    code, message = "unknown", None
    payload = completion.completion if isinstance(completion.completion, dict) else {}
    err = payload.get("error") if isinstance(payload.get("error"), dict) else None
    if err:
        code = err.get("code") or "unknown"
        message = err.get("message") or err.get("provider_message") or err.get("summary")
    if not message:
        message = payload.get("content") or "The run failed."
    return RunOutcome(ok=False, error_code=code, error_message=str(message)[:1000])


async def last_run_statuses(db: AsyncSession, report_ids: list[str]) -> dict[str, str]:
    """Newest system-completion status per report — ``success``/``error``/``in_progress``.

    The run-history lists render a whole page at once, so this resolves them in
    one query rather than one `run_outcome` call per row. Same source of truth:
    the last system turn is the run's verdict.
    """
    ids = [str(r) for r in report_ids if r]
    if not ids:
        return {}
    try:
        rows = (await db.execute(
            select(Completion.report_id, Completion.status)
            .where(Completion.report_id.in_(ids), Completion.role == "system")
            .order_by(Completion.created_at.asc())
        )).all()
    except Exception:
        logger.warning("automation: could not read run statuses", exc_info=True)
        return {}
    # Ascending order means the last write per report wins — the newest turn.
    return {str(rid): status for rid, status in rows if status}


async def notify_owner_of_failure(
    db: AsyncSession,
    *,
    kind: str,                      # 'task' | 'trigger'
    automation_id: str,
    automation_name: str,
    owner: User,
    organization: Organization,
    outcome: RunOutcome,
    report_id: Optional[str] = None,
    next_run_at: Optional[str] = None,
) -> None:
    """Tell the owner their automation failed — inbox always, email when SMTP
    is configured.

    The owner, deliberately, and not the schedule's notification subscribers:
    that list means "send me the results", and a provider error is not a result.
    Best-effort throughout — a failed run must never be made worse by a failing
    alert.
    """
    from app.services.inbox_service import inbox_service
    from app.settings.config import settings

    base_url = "http://localhost:3000"
    try:
        base_url = getattr(settings.bow_config, "base_url", base_url) or base_url
    except Exception:
        pass
    link = f"/reports/{report_id}" if report_id else "/automations"

    title = f'"{automation_name}" failed'
    body = outcome.hint or outcome.error_message or "The run failed."
    # The inbox stores rendered text, so there is no locale at read time —
    # every row in it is English today and this one matches. The email, which
    # IS rendered per recipient, localizes the same sentence below.
    if next_run_at:
        body = f"{body} The schedule will run again at {next_run_at}."

    try:
        await inbox_service.notify_users(
            db,
            organization_id=str(organization.id),
            user_ids=[str(owner.id)],
            source="trigger" if kind == "trigger" else "schedule",
            type="automation_failed",
            title=title,
            body=body,
            severity="error",
            link=link,
            subject={"kind": kind, "automation_id": str(automation_id),
                     **({"report_id": str(report_id)} if report_id else {})},
            # One live row per automation: a schedule failing every morning
            # refreshes this instead of stacking a week of identical rows.
            group_key=f"automation_failed:{kind}:{automation_id}",
        )
    except Exception:
        logger.warning("automation: in-app failure notification failed for %s", automation_id, exc_info=True)

    try:
        from app.dependencies import _locale_from_org
        from app.services.notification_service import notification_service

        await notification_service.send_automation_failure(
            recipient_email=getattr(owner, "email", None),
            automation_name=automation_name,
            error_message=outcome.error_message or "",
            hint=outcome.hint,
            link=f"{base_url.rstrip('/')}{link}",
            next_run_at=next_run_at,
            organization_id=str(organization.id),
            locale=_locale_from_org(organization),
        )
    except Exception:
        logger.warning("automation: failure email failed for %s", automation_id, exc_info=True)
