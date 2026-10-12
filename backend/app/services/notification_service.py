import asyncio
import re
from dataclasses import dataclass
from typing import List, Optional
from logging import getLogger

from fastapi_mail import MessageSchema
from app.settings.config import settings
from app.schemas.notification_schema import (
    NotificationChannel,
    NotificationType,
    ChannelResult,
    NotifyResponse,
)
from app.services.email_renderer import (
    render_automation_failure_email,
    render_notification_email,
    render_scheduled_prompt_email,
)

logger = getLogger(__name__)


@dataclass
class SendOutcome:
    """Result of one outbound send, including *which* transport carried it.

    ``source`` is the deciding piece of information: "org_smtp" means the
    organization's own relay took the message, "global" means it went out via
    the bow-config SMTP, "none" means no transport was configured at all.
    Without it, mail leaving through the wrong server is indistinguishable from
    success.
    """

    ok: bool
    source: str
    error: Optional[str] = None
    stage: Optional[str] = None


def _default_locale() -> str:
    try:
        return settings.bow_config.i18n.default_locale
    except Exception:
        return "en"


def _valid_locale(locale: Optional[str]) -> str:
    if not locale:
        return _default_locale()
    try:
        enabled = settings.bow_config.i18n.enabled_locales
        if locale in enabled:
            return locale
    except Exception:
        pass
    return _default_locale()


class NotificationService:

    # ---- public dispatcher ----

    async def dispatch(
        self,
        notification_type: NotificationType,
        channels: List[NotificationChannel],
        recipients: List[str],
        share_url: str,
        report_title: str,
        sender_name: str,
        message: Optional[str] = None,
        report_id: Optional[str] = None,
        locale: Optional[str] = None,
        db=None,
        organization_id: Optional[str] = None,
    ) -> NotifyResponse:
        """Send notifications across multiple channels. Failures in one channel don't block others.

        When ``db`` + ``organization_id`` are supplied, email goes out via the
        org's **system** transport (Org SMTP → global), not the AI mailbox.
        """
        dispatched: list[ChannelResult] = []
        errors: list[ChannelResult] = []
        skipped: list[ChannelResult] = []

        context = {
            "notification_type": notification_type,
            "share_url": share_url,
            "report_title": report_title,
            "sender_name": sender_name,
            "message": message,
            "report_id": report_id,
            "locale": _valid_locale(locale),
            "db": db,
            "organization_id": organization_id,
        }

        for channel in channels:
            handler = self._get_handler(channel)
            if handler is None:
                errors.append(ChannelResult(
                    channel=channel.value,
                    status="failed",
                    recipients=recipients,
                    error=f"Channel '{channel.value}' is not supported yet",
                ))
                continue

            outcome = await handler(recipients, context)
            # A per-recipient (bursting) send reports one result per outcome.
            for result in (outcome if isinstance(outcome, list) else [outcome]):
                if result.status == "sent":
                    dispatched.append(result)
                elif result.status == "skipped":
                    skipped.append(result)
                else:
                    errors.append(result)

        # Nothing went out at all but someone was skipped: say so as an error,
        # so a caller that only checks `errors` never reports "sent" for an
        # email nobody received.
        if skipped and not dispatched and not errors:
            errors.extend(skipped)

        return NotifyResponse(dispatched=dispatched, errors=errors, skipped=skipped)

    # ---- channel registry ----

    def _get_handler(self, channel: NotificationChannel):
        handlers = {
            NotificationChannel.EMAIL: self._send_email,
            # Future:
            # NotificationChannel.SLACK: self._send_slack,
            # NotificationChannel.TEAMS: self._send_teams,
            # NotificationChannel.IN_APP: self._send_in_app,
        }
        return handlers.get(channel)

    # ---- outbound resolution (org SMTP overrides global bow-config SMTP) ----

    async def _resolved_send(
        self,
        recipients: List[str],
        subject: str,
        body: str,
        *,
        subtype: str = "html",
        attachments: Optional[list] = None,
        db=None,
        organization_id: Optional[str] = None,
        purpose: str = "system",
        message_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[list] = None,
        retries: int = 0,
        retry_delay: float = 1.5,
        timeout: Optional[float] = None,
    ) -> "SendOutcome":
        """Send mail via the purpose-resolved transport.

        ``purpose="analyst"`` → the AI mailbox; ``purpose="system"`` → org SMTP
        (``OrganizationSettings.config.smtp``) → global ``settings.email_client``.
        Backward compatible: no org SMTP → global; org SMTP set → used even when
        the global client is empty.

        **An org that has configured and enabled its own SMTP is authoritative.**
        When that relay refuses the mail we report the failure; we do not quietly
        re-send through the global bow-config SMTP. Falling back would deliver
        the message from a From identity the admin never chose and would leave a
        broken relay looking like it worked — which is precisely how org SMTP
        came to be silently bypassed for invites and shares.

        ``message_id`` / ``in_reply_to`` / ``references`` thread the message so a
        reply can be re-attached to a report (SMTP-config path only).
        """
        resolved = None
        if db is not None and organization_id:
            try:
                from app.services.email_client_resolver import resolve_outbound

                resolved = await resolve_outbound(db, organization_id, purpose=purpose)
            except Exception as e:  # noqa: BLE001
                # Infrastructure failure (DB down), not a configuration verdict —
                # we cannot tell whether this org has its own relay, so the
                # global client is the only option left. Logged loudly because it
                # may mean mail left from the wrong identity.
                logger.warning(
                    "Email resolution failed for org %s, using global client: %s",
                    organization_id, e,
                )
                resolved = None

        if resolved and resolved.uses_smtp_config:
            return await self._send_via_smtp_config(
                resolved, recipients, subject, body,
                subtype=subtype, attachments=attachments,
                message_id=message_id, in_reply_to=in_reply_to, references=references,
                retries=retries, retry_delay=retry_delay, timeout=timeout,
            )

        return await self._send_via_global(
            recipients, subject, body,
            subtype=subtype, attachments=attachments,
            retries=retries, retry_delay=retry_delay, timeout=timeout,
        )

    @staticmethod
    def _attachment_tuples(attachments: Optional[list]) -> list:
        """Normalize fastapi-mail attachment dicts to (filename, bytes, mime)."""
        import os
        import re as _re

        out = []
        for att in attachments or []:
            try:
                path = att.get("file")
                with open(path, "rb") as f:
                    content = f.read()
                filename = att.get("filename")
                if not filename:
                    cd = (att.get("headers") or {}).get("Content-Disposition", "")
                    m = _re.search(r'filename\*?="?([^";]+)"?', cd)
                    filename = m.group(1) if m else os.path.basename(path)
                if att.get("mime_type"):
                    mime = f"{att.get('mime_type')}/{att.get('mime_subtype', 'octet-stream')}"
                else:
                    mime = f"{att.get('type', 'application')}/{att.get('subtype', 'octet-stream')}"
                out.append((filename, content, mime))
            except Exception:  # noqa: BLE001
                continue
        return out

    async def _send_via_smtp_config(
        self,
        resolved,
        recipients: List[str],
        subject: str,
        body: str,
        *,
        subtype: str,
        attachments: Optional[list],
        message_id: Optional[str],
        in_reply_to: Optional[str],
        references: Optional[list],
        retries: int,
        retry_delay: float,
        timeout: Optional[float],
    ) -> "SendOutcome":
        from app.services.email.message_builder import build_email
        from app.services.email.sender import STAGE_CONFIG, send_message_result

        if not resolved.from_address:
            # Every relay rejects a message with no envelope sender, and
            # build_email refuses to construct one — fail with the fix, not a
            # transport stack trace.
            return SendOutcome(
                ok=False, source=resolved.source, stage=STAGE_CONFIG,
                error="no From address configured for this SMTP server",
            )

        built_attachments = self._attachment_tuples(attachments)

        # Retry only the recipients that have not been accepted yet, so a
        # partial failure across a multi-recipient send cannot double-deliver.
        pending = list(recipients)
        last_error: Optional[str] = None
        last_stage: Optional[str] = None

        for attempt in range(retries + 1):
            still_pending = []
            for rcpt in pending:
                try:
                    msg = build_email(
                        from_address=resolved.from_address,
                        from_name=resolved.from_name,
                        to_address=rcpt,
                        subject=subject,
                        body=body,
                        body_subtype=subtype,
                        message_id=message_id,
                        in_reply_to=in_reply_to,
                        references=references,
                        attachments=built_attachments or None,
                    )
                except Exception as e:  # noqa: BLE001
                    # e.g. CR/LF in a configured From name: the stdlib refuses
                    # the header. A config problem, never a 500 + traceback.
                    logger.warning("Could not build email via %s: %s", resolved.source, type(e).__name__)
                    return SendOutcome(
                        ok=False, source=resolved.source, stage=STAGE_CONFIG,
                        error="message headers are invalid (check the From name and address)",
                    )
                try:
                    coro = send_message_result(resolved.smtp_config, msg)
                    if timeout is not None:
                        ok, error, stage = await asyncio.wait_for(coro, timeout=timeout)
                    else:
                        ok, error, stage = await coro
                except asyncio.TimeoutError:
                    ok, error, stage = False, f"timed out after {timeout}s", "connect"
                except Exception as e:  # noqa: BLE001
                    ok, error, stage = False, str(e) or e.__class__.__name__, "send"
                if not ok:
                    still_pending.append(rcpt)
                    last_error, last_stage = error, stage

            pending = still_pending
            if not pending:
                return SendOutcome(ok=True, source=resolved.source)
            if attempt < retries:
                await asyncio.sleep(retry_delay * (attempt + 1))

        logger.error(
            "Email via %s failed for %s at stage=%s: %s",
            resolved.source, pending, last_stage, last_error,
        )
        return SendOutcome(
            ok=False, source=resolved.source, error=last_error, stage=last_stage,
        )

    async def _send_via_global(
        self,
        recipients: List[str],
        subject: str,
        body: str,
        *,
        subtype: str,
        attachments: Optional[list],
        retries: int,
        retry_delay: float,
        timeout: Optional[float],
    ) -> "SendOutcome":
        """Send via the global bow-config fastapi-mail client."""
        fm = settings.email_client
        if not fm:
            return SendOutcome(
                ok=False, source="none", stage="config",
                error="SMTP is not configured",
            )

        message_kwargs = dict(
            subject=subject,
            recipients=recipients,
            body=body,
            subtype=subtype,
        )
        if attachments:
            message_kwargs["attachments"] = attachments
        message = MessageSchema(**message_kwargs)

        last_error: Optional[str] = None
        for attempt in range(retries + 1):
            try:
                if timeout is not None:
                    await asyncio.wait_for(fm.send_message(message), timeout=timeout)
                else:
                    await fm.send_message(message)
                return SendOutcome(ok=True, source="global")
            except Exception as e:  # noqa: BLE001
                last_error = str(e) or e.__class__.__name__
                logger.error(
                    "Failed to send email via global SMTP (attempt %d/%d): %s",
                    attempt + 1, retries + 1, last_error,
                )
                if attempt < retries:
                    await asyncio.sleep(retry_delay * (attempt + 1))

        return SendOutcome(ok=False, source="global", error=last_error, stage="send")

    # ---- email channel ----

    async def _send_email(self, recipients: List[str], context: dict) -> ChannelResult:
        db = context.get("db")
        organization_id = context.get("organization_id")
        # Availability is a per-org question: an org that configured its own
        # SMTP can send even when the global bow-config client is empty.
        if db is not None and organization_id:
            from app.services.email_client_resolver import is_outbound_available

            available = await is_outbound_available(db, organization_id, purpose="system")
        else:
            available = settings.email_client is not None  # no org in scope
        if not available:
            return ChannelResult(
                channel="email",
                status="failed",
                recipients=recipients,
                error="SMTP is not configured",
                source="none",
                stage="config",
            )

        subject, html = render_notification_email(
            context["notification_type"],
            context["locale"],
            share_url=context["share_url"],
            report_title=context["report_title"],
            sender_name=context["sender_name"],
            message=context.get("message"),
        )

        # A shared dashboard with identity-scoped content is rendered per
        # recipient (report_bursting); everything else keeps the single send.
        if (
            context["notification_type"] == NotificationType.SHARE_DASHBOARD
            and context.get("report_id") and organization_id
        ):
            burst = await self._burst_share_dashboard(recipients, subject, html, context)
            if burst is not None:
                return burst

        async def _build_attachments() -> list:
            # Generate the PDF attachment for dashboard shares.
            # Withheld in viewer-identity mode on user-scoped connections:
            # the PDF renders the creator's snapshot, which those viewers
            # must not receive (see viewer_data_policy) — email ships with
            # the link only.
            attachments = []
            report_id = context.get("report_id")
            if context["notification_type"] == NotificationType.SHARE_DASHBOARD and report_id:
                try:
                    from app.services.report_pdf_service import ReportPdfService
                    from app.services.viewer_data_policy import report_snapshot_withheld
                    from app.dependencies import async_session_maker as _asm
                    from pathlib import Path

                    async with _asm() as policy_db:
                        withheld = await report_snapshot_withheld(policy_db, report_id)
                    if withheld:
                        logger.info(
                            "Skipping PDF attachment for shared dashboard %s: "
                            "viewer-identity mode on user-scoped connections", report_id,
                        )
                    else:
                        pdf_service = ReportPdfService()
                        pdf_path = await pdf_service.generate_for_report(report_id)
                        if pdf_path:
                            pdf_file = Path(pdf_path)
                            if pdf_file.exists():
                                attachments.append({
                                    "file": str(pdf_file),
                                    "filename": f"{context['report_title'] or 'report'}.pdf",
                                    "type": "application",
                                    "subtype": "pdf",
                                })
                except Exception as e:
                    logger.warning("PDF generation failed for shared dashboard %s: %s", report_id, e)
            return attachments

        # Awaited, not fire-and-forget: this used to spawn a background task and
        # return "sent" immediately, so a share that never left the building
        # reported success to the admin. The caller's modal already shows a
        # spinner; a few honest seconds beat an instant lie. Bounded by a
        # timeout so a hung relay cannot hold the request open.
        attachments = await _build_attachments()

        # System mail → Org SMTP → global (never the AI mailbox). The request db
        # may be mid-transaction, so open a fresh session for the org lookup.
        if organization_id:
            from app.dependencies import async_session_maker
            async with async_session_maker() as send_db:
                outcome = await self._resolved_send(
                    recipients, subject, html,
                    subtype="html", attachments=attachments or None,
                    db=send_db, organization_id=organization_id, purpose="system",
                    retries=1, timeout=20,
                )
        else:
            outcome = await self._resolved_send(
                recipients, subject, html,
                subtype="html", attachments=attachments or None,
                purpose="system", retries=1, timeout=20,
            )

        if outcome.ok:
            logger.info(
                "Notification email sent to %s via %s", recipients, outcome.source
            )
            return ChannelResult(
                channel="email",
                status="sent",
                recipients=recipients,
                source=outcome.source,
            )

        logger.error(
            "Notification email to %s failed via %s: %s",
            recipients, outcome.source, outcome.error,
        )
        return ChannelResult(
            channel="email",
            status="failed",
            recipients=recipients,
            error=outcome.error or "send failed",
            source=outcome.source,
            stage=outcome.stage,
        )

    # ---- per-recipient (bursting) report emails ----

    @staticmethod
    def _pdf_attachment(pdf_path: Optional[str], title: Optional[str]) -> list:
        from pathlib import Path

        if not pdf_path or not Path(pdf_path).exists():
            return []
        return [{
            "file": str(pdf_path),
            "filename": f"{title or 'report'}.pdf",
            "type": "application",
            "subtype": "pdf",
        }]

    def _make_burst_sender(self, organization_id, subject_html_for, title, *, retries=0, timeout=None):
        """send(recipients, pdf_path, personal) for report_bursting.deliver_burst."""
        async def send(rcpts: List[str], pdf_path: Optional[str], personal: bool):
            subject, html = subject_html_for(personal)
            from app.dependencies import async_session_maker
            async with async_session_maker() as send_db:
                return await self._resolved_send(
                    rcpts, subject, html, subtype="html",
                    attachments=self._pdf_attachment(pdf_path, title) or None,
                    db=send_db, organization_id=organization_id, purpose="system",
                    retries=retries, timeout=timeout,
                )
        return send

    async def _burst_plan(self, report_id: str, *, emails=(), user_ids=()):
        """(recipients, want_pdf_allowed) when the report needs bursting, else None."""
        from app.dependencies import async_session_maker
        from app.models.report import Report
        from app.services.report_bursting import classify_recipients, report_identity_scoped
        from app.services.viewer_data_policy import report_snapshot_withheld

        async with async_session_maker() as db:
            report = await db.get(Report, str(report_id))
            if report is None or not await report_identity_scoped(db, str(report_id)):
                return None
            recipients = await classify_recipients(db, report, emails=emails, user_ids=user_ids)
            # The credential-level withholding policy still applies to every
            # copy, exactly as on the single-send path.
            pdf_allowed = not await report_snapshot_withheld(db, str(report_id))
        return recipients, pdf_allowed

    async def _burst_share_dashboard(self, recipients, subject, html, context):
        from app.models.notification import SOURCE_SHARE
        from app.services.report_bursting import SKIP_MESSAGES, deliver_burst, notify_owner_of_undelivered

        report_id = str(context["report_id"])
        try:
            plan = await self._burst_plan(report_id, emails=recipients)
        except Exception:  # noqa: BLE001
            # Cannot tell whether the report is identity-scoped: fail closed.
            logger.exception("bursting: could not plan share email for report %s", report_id)
            return ChannelResult(channel="email", status="failed", recipients=recipients,
                                 error="Could not prepare personalized copies")
        if plan is None:
            return None
        burst_recipients, pdf_allowed = plan

        send = self._make_burst_sender(
            context.get("organization_id"), lambda personal: (subject, html),
            context.get("report_title"), retries=1, timeout=20,
        )
        result = await deliver_burst(
            report_id=report_id, recipients=burst_recipients, want_pdf=pdf_allowed, send=send,
        )
        await notify_owner_of_undelivered(report_id, result, source=SOURCE_SHARE)

        out: list[ChannelResult] = []
        if result.sent:
            out.append(ChannelResult(channel="email", status="sent",
                                     recipients=[d.email for d in result.sent],
                                     source=result.sent[0].source))
        for d in result.failed:
            out.append(ChannelResult(channel="email", status="failed", recipients=[d.email],
                                     error=d.error or d.reason or "send failed"))
        for d in result.skipped:
            out.append(ChannelResult(channel="email", status="skipped", recipients=[d.email],
                                     error=f"{d.email} {SKIP_MESSAGES.get(d.reason, 'not sent')}"))
        return out

    # ---- free-form email ----

    async def send_custom_email(
        self,
        recipients: List[str],
        subject: str,
        body: str,
        subtype: str = "plain",
        attachments: Optional[list] = None,
        retries: int = 0,
        retry_delay: float = 1.5,
        timeout: Optional[float] = None,
        db=None,
        organization_id: Optional[str] = None,
        purpose: str = "system",
        message_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[list] = None,
    ) -> ChannelResult:
        """Send a free-form email with arbitrary subject/body.

        Unlike ``dispatch`` (template-driven), this sends exactly the subject
        and body provided. The send is awaited so the returned status reflects
        actual delivery to the SMTP server, not just enqueueing.

        When ``db`` + ``organization_id`` are supplied, the transport is resolved
        per organization: ``purpose="analyst"`` uses the AI mailbox,
        ``purpose="system"`` uses the org's own SMTP and falls back to the global
        bow-config client only when the org has not configured one. Callers that
        omit the org context get the global client and therefore ignore whatever
        the organization configured — always pass ``db`` and ``organization_id``
        for organization-scoped mail.

        Reliability knobs (all opt-in, defaults preserve old behaviour):
        - ``retries``: extra attempts on failure (total tries = retries + 1),
          with linear backoff (``retry_delay`` × attempt number).
        - ``timeout``: per-attempt ceiling (seconds) so a hung SMTP server
          can't block the caller indefinitely.

        ``attachments`` follows the fastapi-mail dict shape, e.g.
        ``{"file": "/abs/path", "filename": "x.csv", "type": "text", "subtype": "csv"}``.
        """
        if subtype not in ("plain", "html"):
            subtype = "plain"

        outcome = await self._resolved_send(
            recipients,
            subject,
            body,
            subtype=subtype,
            attachments=attachments,
            db=db,
            organization_id=organization_id,
            purpose=purpose,
            message_id=message_id,
            in_reply_to=in_reply_to,
            references=references,
            retries=retries,
            retry_delay=retry_delay,
            timeout=timeout,
        )

        if outcome.ok:
            logger.info("Custom email sent to %s via %s", recipients, outcome.source)
            return ChannelResult(
                channel="email",
                status="sent",
                recipients=recipients,
                source=outcome.source,
            )

        return ChannelResult(
            channel="email",
            status="failed",
            recipients=recipients,
            error=outcome.error or "send failed",
            source=outcome.source,
            stage=outcome.stage,
        )

    # ---- scheduled report results ----

    async def send_scheduled_report_results(
        self,
        report_id: str,
        report_title: str,
        subscribers: list,
        report_url: str,
        locale: Optional[str] = None,
    ):
        """Send post-rerun notification to all subscribers with optional PDF attachment.

        Called as a fire-and-forget task after rerun_report_steps completes.
        subscribers: [{"type": "user", "id": "..."}, {"type": "email", "address": "..."}]
        """
        await self.send_scheduled_prompt_results(
            report_id=report_id,
            report_title=report_title,
            subscribers=subscribers,
            report_url=report_url,
            exec_summary=None,
            locale=locale,
        )

    async def send_automation_failure(
        self,
        recipient_email: Optional[str],
        automation_name: str,
        error_message: str,
        link: str,
        hint: Optional[str] = None,
        next_run_at: Optional[str] = None,
        organization_id: Optional[str] = None,
        locale: Optional[str] = None,
    ) -> None:
        """Email the owner that an unattended run failed.

        Sent only after every retry and fallback the agent has was exhausted,
        so this is a dead end rather than a transient blip. No-ops without a
        recipient or a configured transport — the in-app notification is the
        durable record and email is the nudge.
        """
        if not recipient_email:
            return

        subject, html = render_automation_failure_email(
            _valid_locale(locale),
            automation_name=automation_name,
            link=link,
            error_message=error_message,
            hint=hint,
            next_run_at=next_run_at,
        )
        try:
            from app.dependencies import async_session_maker
            async with async_session_maker() as send_db:
                await self._resolved_send(
                    [recipient_email], subject, html, subtype="html",
                    db=send_db, organization_id=organization_id, purpose="system",
                )
            logger.info("Automation failure email sent to %s for '%s'", recipient_email, automation_name)
        except Exception as e:
            logger.error("Failed to send automation failure email: %s", e)

    async def send_scheduled_prompt_results(
        self,
        report_id: str,
        report_title: str,
        subscribers: list,
        report_url: str,
        exec_summary: Optional[dict] = None,
        locale: Optional[str] = None,
    ):
        """Send notification after a scheduled prompt execution completes.

        exec_summary: {"iterations": N, "queries": N, "artifacts": N, "last_content": "..."}
        """
        if not subscribers:
            return

        # A report with identity-scoped content is delivered per recipient
        # (report_bursting): each member gets their own rows, external
        # addresses are skipped. Everything else keeps the single send below.
        try:
            if await self._burst_scheduled_results(
                report_id=report_id, report_title=report_title, subscribers=subscribers,
                report_url=report_url, exec_summary=exec_summary, locale=locale,
            ):
                return
        except Exception:  # noqa: BLE001
            # Cannot tell whether the report is identity-scoped: fail closed
            # rather than mail the owner's rows to everyone.
            logger.exception("bursting: scheduled results for report %s not sent", report_id)
            return

        # Resolve subscriber emails + the report's org (for Org SMTP routing).
        recipient_emails = []
        organization_id = None
        try:
            from app.dependencies import async_session_maker
            from app.models.user import User
            from app.models.report import Report

            async with async_session_maker() as db:
                rep = await db.get(Report, report_id)
                organization_id = rep.organization_id if rep else None
                for sub in subscribers:
                    if sub.get("type") == "email" and sub.get("address"):
                        recipient_emails.append(sub["address"])
                    elif sub.get("type") == "user" and sub.get("id"):
                        user = await db.get(User, sub["id"])
                        if user and user.email:
                            recipient_emails.append(user.email)
        except Exception as e:
            logger.error("Failed to resolve subscriber emails: %s", e)
            return

        if not recipient_emails:
            return

        effective_locale = _valid_locale(locale)
        summary_html = ""
        if exec_summary and exec_summary.get("last_content"):
            content = exec_summary["last_content"]
            if len(content) > 2000:
                content = content[:2000] + "..."
            summary_html = self._md_to_html(content)

        subject, html = render_scheduled_prompt_email(
            effective_locale,
            report_title=report_title,
            report_url=report_url,
            exec_summary=exec_summary,
            summary_html=summary_html,
        )

        # Attach artifact PDF if artifacts were created in this execution.
        # Same withholding policy as share emails: in viewer-identity mode on
        # user-scoped connections the PDF renders creator-credential data the
        # subscribers must not receive — send link-only.
        attachments = []
        if exec_summary and exec_summary.get("artifacts", 0) > 0:
            try:
                from app.services.report_pdf_service import ReportPdfService
                from app.services.viewer_data_policy import report_snapshot_withheld
                from app.dependencies import async_session_maker as _asm
                from pathlib import Path

                async with _asm() as policy_db:
                    withheld = await report_snapshot_withheld(policy_db, report_id)
                if withheld:
                    logger.info(
                        "Skipping PDF attachment for scheduled report %s: "
                        "viewer-identity mode on user-scoped connections", report_id,
                    )
                else:
                    pdf_service = ReportPdfService()
                    pdf_path = await pdf_service.generate_for_report(report_id)
                    if pdf_path:
                        pdf_file = Path(pdf_path)
                        if pdf_file.exists():
                            attachments.append({
                                "file": str(pdf_file),
                                "filename": f"{report_title or 'report'}.pdf",
                                "type": "application",
                                "subtype": "pdf",
                            })
            except Exception as e:
                logger.warning("PDF generation failed for scheduled prompt report %s: %s", report_id, e)

        try:
            from app.dependencies import async_session_maker
            async with async_session_maker() as send_db:
                await self._resolved_send(
                    recipient_emails, subject, html,
                    subtype="html", attachments=attachments or None,
                    db=send_db, organization_id=organization_id, purpose="system",
                )
            logger.info("Scheduled prompt results sent to %s for report %s", recipient_emails, report_id)
        except Exception as e:
            logger.error("Failed to send scheduled prompt results: %s", e)

    async def _burst_scheduled_results(
        self, *, report_id, report_title, subscribers, report_url, exec_summary, locale,
    ) -> bool:
        """Per-recipient scheduled-results delivery; False when not needed."""
        from app.dependencies import async_session_maker
        from app.models.notification import SOURCE_SCHEDULE
        from app.models.report import Report
        from app.services.report_bursting import deliver_burst, notify_owner_of_undelivered

        user_ids = [str(s.get("id")) for s in subscribers if s.get("type") == "user" and s.get("id")]
        emails = [s["address"] for s in subscribers if s.get("type") == "email" and s.get("address")]
        plan = await self._burst_plan(str(report_id), emails=emails, user_ids=user_ids)
        if plan is None:
            return False
        recipients, pdf_allowed = plan
        async with async_session_maker() as db:
            rep = await db.get(Report, str(report_id))
            organization_id = rep.organization_id if rep else None

        effective_locale = _valid_locale(locale)
        owner_summary = ""
        if exec_summary and exec_summary.get("last_content"):
            content = exec_summary["last_content"]
            if len(content) > 2000:
                content = content[:2000] + "..."
            owner_summary = self._md_to_html(content)

        def subject_html_for(personal: bool):
            # The run's written summary was produced from the OWNER's rows —
            # a personalized copy carries the run's counts and the link, and
            # its own data in the attachment, never the owner's narrative.
            return render_scheduled_prompt_email(
                effective_locale, report_title=report_title, report_url=report_url,
                exec_summary=exec_summary, summary_html="" if personal else owner_summary,
            )

        want_pdf = bool(exec_summary and exec_summary.get("artifacts", 0) > 0) and pdf_allowed
        result = await deliver_burst(
            report_id=str(report_id), recipients=recipients, want_pdf=want_pdf,
            send=self._make_burst_sender(organization_id, subject_html_for, report_title),
        )
        await notify_owner_of_undelivered(str(report_id), result, source=SOURCE_SCHEDULE)
        return True

    @staticmethod
    def _md_to_html(text: str) -> str:
        """Minimal markdown-to-HTML: bold, bullet lists, and line breaks."""
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        # bold: **text**
        safe = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', safe)
        # bullet lists: lines starting with "- "
        def _replace_list(m):
            items = m.group(0).strip().split("\n")
            li = "".join(f"<li>{item.lstrip('- ').strip()}</li>" for item in items if item.strip())
            return f"<ul style=\"margin:8px 0;padding-left:20px;\">{li}</ul>"
        safe = re.sub(r'(^- .+(?:\n- .+)*)', _replace_list, safe, flags=re.MULTILINE)
        # remaining newlines → <br>
        safe = safe.replace("\n", "<br>")
        return safe


notification_service = NotificationService()
