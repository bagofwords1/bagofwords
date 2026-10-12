"""Per-recipient ("bursting") delivery of report emails.

A report email used to be rendered ONCE, from the owner's stored rows, and the
same message (and PDF) went to every recipient. For an identity-scoped query
("my accounts", ``source: identity`` params, or anything that consumes one —
see identity_taint.step_identity_scope) those rows are the OWNER's slice, so
every subscriber received the owner's private rows.

For a report that contains identity-scoped content, delivery is now per
recipient, through the same pipeline the live app uses for a signed-in viewer:

* the owner gets exactly what they got before (the stored snapshot);
* an org member is checked against the report's share gate and agent access
  (ReportService.viewer_rerun_report_steps — the /r/{id}/run path), their
  identity-scoped steps are executed AS THEM into their own step_user_results
  (non-identity steps are not re-run; they serve the shared snapshot exactly as
  the app does), and their copy is rendered with ``viewer=<member>`` so every
  query resolves through viewer_data_policy.resolve_step_data;
* an address with no account in the org is skipped — there is no identity to
  render it as — and so is a member who cannot open the report or its agents.

A report with NO identity-scoped content never reaches this module: callers
keep their original single-render, single-send path, byte for byte.

Cost is bounded: recipients are processed sequentially (each one boots a
headless browser for its PDF), at most BURST_MAX_RECIPIENTS per run, each
under BURST_RECIPIENT_TIMEOUT_S, and one recipient's failure never stops the
next. Skips and failures are logged and summarised to the owner in-app.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Iterable, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import lazyload

logger = logging.getLogger(__name__)

# One headless render per member; keep a run well inside the scheduler's
# cadence. NotifyRequest already caps a share at 20 addresses.
BURST_MAX_RECIPIENTS = 50
# Above ReportPdfService.RENDER_TIMEOUT_SECONDS (150s) plus the member's
# identity-scoped query run.
BURST_RECIPIENT_TIMEOUT_S = 300

SKIP_EXTERNAL = "external_recipient"
SKIP_NO_ACCESS = "no_report_access"
SKIP_NO_AGENT_ACCESS = "no_agent_access"
SKIP_CAP = "recipient_cap"
FAIL_TIMEOUT = "timeout"
FAIL_ERROR = "error"

SKIP_MESSAGES = {
    SKIP_EXTERNAL: "not sent: the report contains data scoped to each viewer, and this address has no account in the organization",
    SKIP_NO_ACCESS: "not sent: this member cannot open the report",
    SKIP_NO_AGENT_ACCESS: "not sent: this member has no access to the report's agent",
    SKIP_CAP: f"not sent: more than {BURST_MAX_RECIPIENTS} personalized recipients in one run",
}


# ── identity detection ────────────────────────────────────────────────────────

async def identity_scoped_step_ids(db, step_ids: Iterable[str]) -> set[str]:
    """The subset of `step_ids` whose rows depend on who runs them."""
    from app.models.step import Step
    from app.services.identity_taint import step_identity_scope

    ids = [str(s) for s in step_ids if s]
    if not ids:
        return set()
    rows = (await db.execute(
        select(Step.id, Step.query_id, Step.code).where(Step.id.in_(ids))
    )).all()
    out: set[str] = set()
    for sid, qid, code in rows:
        tainted, _ = await step_identity_scope(db, SimpleNamespace(query_id=qid, code=code))
        if tainted:
            out.add(str(sid))
    return out


async def report_identity_scoped(db, report_id: str) -> bool:
    """Whether any query of the report renders identity-scoped rows.

    Report-wide (every query's rendered step), not only the latest artifact's:
    over-detecting costs a per-recipient render; under-detecting leaks."""
    from app.models.query import Query
    from app.models.step import Step

    qrows = (await db.execute(
        select(Query.id, Query.default_step_id).where(Query.report_id == str(report_id))
    )).all()
    step_ids: list[str] = []
    for qid, default_step_id in qrows:
        if default_step_id:
            step_ids.append(str(default_step_id))
            continue
        latest = (await db.execute(
            select(Step.id).where(Step.query_id == str(qid))
            .order_by(Step.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if latest:
            step_ids.append(str(latest))
    return bool(await identity_scoped_step_ids(db, step_ids))


# ── recipients ────────────────────────────────────────────────────────────────

@dataclass
class Recipient:
    email: str
    user_id: Optional[str] = None   # None → no account in the org (external)
    is_owner: bool = False


async def classify_recipients(
    db, report, *, emails: Iterable[str] = (), user_ids: Iterable[str] = (),
) -> List[Recipient]:
    """Resolve addresses / user ids to org members, in order, deduped by email.

    Only an ACTIVE membership of the report's organization counts — a pending
    invite has no account to render as, and a user from another org has no
    identity here."""
    from app.models.membership import Membership
    from app.models.user import User

    org_id = str(report.organization_id)
    owner_id = str(report.user_id) if report.user_id else None
    out: List[Recipient] = []
    seen: set[str] = set()

    def _add(email: str, uid: Optional[str]) -> None:
        key = (email or "").strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        out.append(Recipient(email=email.strip(), user_id=uid,
                             is_owner=bool(uid and owner_id and uid == owner_id)))

    member_q = (
        select(User.id, User.email)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.organization_id == org_id)
    )
    uids = [str(u) for u in user_ids if u]
    if uids:
        by_id = {str(r[0]): r[1] for r in (await db.execute(member_q.where(User.id.in_(uids)))).all()}
        for uid in uids:
            if uid in by_id and by_id[uid]:
                _add(by_id[uid], uid)
            elif uid not in by_id:
                # A subscribed user who has since left the org: no identity
                # here. Keep their address (if any) so the skip is reported.
                user = await db.get(User, uid)
                if user is not None and user.email:
                    _add(user.email, None)
    addrs = [e for e in emails if e]
    if addrs:
        lowered = [a.strip().lower() for a in addrs]
        by_email = {
            (r[1] or "").strip().lower(): str(r[0])
            for r in (await db.execute(member_q.where(func.lower(User.email).in_(lowered)))).all()
        }
        for addr in addrs:
            _add(addr, by_email.get(addr.strip().lower()))
    return out


# ── one recipient's copy ──────────────────────────────────────────────────────

@dataclass
class PreparedCopy:
    skip_reason: Optional[str] = None
    # False when one of the member's identity-scoped runs failed: their copy
    # would print those charts empty, so it goes out link-only instead.
    rows_ready: bool = True


async def prepare_recipient_copy(db, report_id: str, user) -> PreparedCopy:
    """Gate the member and run their identity-scoped steps as them.

    Goes through ReportService.viewer_rerun_report_steps — the same entry the
    dashboard uses when a signed-in member opens it — so the share gate,
    agent access, credentials and identity binding are the live app's own."""
    from fastapi import HTTPException

    from app.errors.app_error import AppError
    from app.services.report_service import ReportService

    try:
        result = await ReportService().viewer_rerun_report_steps(
            db, str(report_id), user, only_identity_scoped=True,
        )
    except (HTTPException, AppError) as e:
        status = getattr(e, "status_code", None)
        logger.info("bursting: %s cannot open report %s (%s)", user.id, report_id, status)
        return PreparedCopy(skip_reason=SKIP_NO_ACCESS)

    if any(err.get("code") == "no_access" for err in result.get("data_source_errors") or []):
        return PreparedCopy(skip_reason=SKIP_NO_AGENT_ACCESS)
    return PreparedCopy(rows_ready=int(result.get("steps_failed") or 0) == 0)


# ── delivery ──────────────────────────────────────────────────────────────────

@dataclass
class Delivery:
    email: str
    status: str                       # "sent" | "skipped" | "failed"
    reason: Optional[str] = None
    personal: bool = False            # rendered as this recipient
    attachment: bool = False
    error: Optional[str] = None
    source: Optional[str] = None      # transport that carried it (SendOutcome.source)


# send(recipients, pdf_path_or_None, personal) -> object with .ok / .error
SendFn = Callable[[List[str], Optional[str], bool], Awaitable[Any]]


@dataclass
class BurstResult:
    deliveries: List[Delivery] = field(default_factory=list)

    @property
    def sent(self) -> List[Delivery]:
        return [d for d in self.deliveries if d.status == "sent"]

    @property
    def skipped(self) -> List[Delivery]:
        return [d for d in self.deliveries if d.status == "skipped"]

    @property
    def failed(self) -> List[Delivery]:
        return [d for d in self.deliveries if d.status == "failed"]


def _unlink(path: Optional[str]) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


async def deliver_burst(
    *, report_id: str, recipients: List[Recipient], want_pdf: bool, send: SendFn,
    pdf_renderer: Any = None,
) -> BurstResult:
    """Deliver one report email per recipient (see module docstring)."""
    from app.dependencies import async_session_maker
    from app.models.user import User
    from app.services.report_pdf_service import ReportPdfService

    pdfs = pdf_renderer or ReportPdfService()
    result = BurstResult()

    owners = [r for r in recipients if r.is_owner]
    externals = [r for r in recipients if not r.user_id]
    members = [r for r in recipients if r.user_id and not r.is_owner]

    # Owner: unchanged — the stored snapshot, as before bursting existed.
    if owners:
        pdf = await pdfs.generate_for_report(str(report_id)) if want_pdf else None
        try:
            outcome = await send([r.email for r in owners], pdf, False)
            ok = bool(getattr(outcome, "ok", False))
            for r in owners:
                result.deliveries.append(Delivery(
                    email=r.email, status="sent" if ok else "failed",
                    attachment=bool(pdf), error=None if ok else getattr(outcome, "error", None),
                    source=getattr(outcome, "source", None),
                ))
        except Exception as e:  # noqa: BLE001
            logger.exception("bursting: owner send failed for report %s", report_id)
            for r in owners:
                result.deliveries.append(Delivery(email=r.email, status="failed", reason=FAIL_ERROR, error=str(e)))

    for r in externals:
        logger.info("bursting: skipping %s for identity-scoped report %s (no account)", r.email, report_id)
        result.deliveries.append(Delivery(email=r.email, status="skipped", reason=SKIP_EXTERNAL))

    for r in members[BURST_MAX_RECIPIENTS:]:
        logger.warning("bursting: recipient cap reached for report %s; skipping %s", report_id, r.email)
        result.deliveries.append(Delivery(email=r.email, status="skipped", reason=SKIP_CAP))

    async def _one(r: Recipient) -> Delivery:
        pdf_path: Optional[str] = None
        try:
            async with async_session_maker() as db:
                user = await db.get(User, r.user_id)
                if user is None:
                    return Delivery(email=r.email, status="skipped", reason=SKIP_EXTERNAL)
                prep = await prepare_recipient_copy(db, str(report_id), user)
                if prep.skip_reason:
                    return Delivery(email=r.email, status="skipped", reason=prep.skip_reason)
                if want_pdf and prep.rows_ready:
                    key = f"burst-{uuid.uuid4().hex}"
                    pdf_path = await pdfs.generate_for_report(str(report_id), viewer=user, out_key=key)
            outcome = await send([r.email], pdf_path, True)
            ok = bool(getattr(outcome, "ok", False))
            return Delivery(
                email=r.email, status="sent" if ok else "failed", personal=True,
                attachment=bool(pdf_path), error=None if ok else getattr(outcome, "error", None),
                source=getattr(outcome, "source", None),
            )
        finally:
            _unlink(pdf_path)

    for r in members[:BURST_MAX_RECIPIENTS]:
        try:
            result.deliveries.append(await asyncio.wait_for(_one(r), timeout=BURST_RECIPIENT_TIMEOUT_S))
        except asyncio.TimeoutError:
            logger.error("bursting: recipient %s timed out for report %s", r.email, report_id)
            result.deliveries.append(Delivery(email=r.email, status="failed", reason=FAIL_TIMEOUT))
        except Exception as e:  # noqa: BLE001 — one recipient never stops the next
            logger.exception("bursting: recipient %s failed for report %s", r.email, report_id)
            result.deliveries.append(Delivery(email=r.email, status="failed", reason=FAIL_ERROR, error=str(e)))

    logger.info(
        "bursting: report %s — %d sent, %d skipped, %d failed",
        report_id, len(result.sent), len(result.skipped), len(result.failed),
    )
    return result


async def notify_owner_of_undelivered(report_id: str, result: BurstResult, *, source: str) -> None:
    """In-app summary to the report owner of who did not get a copy."""
    undelivered = result.skipped + result.failed
    if not undelivered:
        return
    try:
        from app.dependencies import async_session_maker
        from app.models.notification import SEVERITY_WARNING
        from app.models.report import Report
        from app.services.inbox_service import inbox_service

        async with async_session_maker() as db:
            report = (await db.execute(
                select(Report).options(lazyload("*")).where(Report.id == str(report_id))
            )).scalar_one_or_none()
            if report is None or not report.user_id:
                return
            title = report.title or "Untitled"
            emails = ", ".join(d.email for d in undelivered[:10])
            if len(undelivered) > 10:
                emails += f" (+{len(undelivered) - 10})"
            await inbox_service.notify_users(
                db, organization_id=str(report.organization_id), user_ids=[str(report.user_id)],
                source=source, type="delivery_skipped", severity=SEVERITY_WARNING,
                title=f'Some recipients did not get "{title}"',
                body=(f"{len(undelivered)} recipient(s) were not sent this report, which "
                      f"contains data scoped to each viewer: {emails}"),
                link=f"/reports/{report_id}",
                subject={"kind": "report", "report_id": str(report_id),
                         "i18n": {"report": title, "count": len(undelivered), "recipients": emails},
                         "undelivered": [{"email": d.email, "status": d.status, "reason": d.reason}
                                         for d in undelivered]},
                dedup=False,
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — the summary must never break delivery
        logger.warning("bursting: owner summary failed for report %s", report_id, exc_info=True)
