import asyncio
import re
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
    render_notification_email,
    render_scheduled_prompt_email,
)

logger = getLogger(__name__)


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
    ) -> NotifyResponse:
        """Send notifications across multiple channels. Failures in one channel don't block others."""
        dispatched: list[ChannelResult] = []
        errors: list[ChannelResult] = []

        context = {
            "notification_type": notification_type,
            "share_url": share_url,
            "report_title": report_title,
            "sender_name": sender_name,
            "message": message,
            "report_id": report_id,
            "locale": _valid_locale(locale),
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

            result = await handler(recipients, context)
            if result.status == "sent":
                dispatched.append(result)
            else:
                errors.append(result)

        return NotifyResponse(dispatched=dispatched, errors=errors)

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

    # ---- email channel ----

    async def _send_email(self, recipients: List[str], context: dict) -> ChannelResult:
        fm = settings.email_client
        if not fm:
            return ChannelResult(
                channel="email",
                status="failed",
                recipients=recipients,
                error="SMTP is not configured",
            )

        subject, html = render_notification_email(
            context["notification_type"],
            context["locale"],
            share_url=context["share_url"],
            report_title=context["report_title"],
            sender_name=context["sender_name"],
            message=context.get("message"),
        )

        async def _do_send():
            try:
                # Generate PDF attachment for dashboard shares (in background)
                attachments = []
                report_id = context.get("report_id")
                if context["notification_type"] == NotificationType.SHARE_DASHBOARD and report_id:
                    try:
                        from app.services.report_pdf_service import ReportPdfService
                        from pathlib import Path

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

                if attachments:
                    message = MessageSchema(
                        subject=subject,
                        recipients=recipients,
                        body=html,
                        subtype="html",
                        attachments=attachments,
                    )
                else:
                    message = MessageSchema(
                        subject=subject,
                        recipients=recipients,
                        body=html,
                        subtype="html",
                    )

                await fm.send_message(message)
                logger.info("Notification email sent to %s", recipients)
            except Exception as e:
                logger.error("Failed to send notification email: %s", e)

        asyncio.create_task(_do_send())

        return ChannelResult(
            channel="email",
            status="sent",
            recipients=recipients,
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
        fm = settings.email_client
        if not fm or not subscribers:
            return

        # Resolve subscriber emails
        recipient_emails = []
        try:
            from app.dependencies import async_session_maker
            from app.models.user import User

            async with async_session_maker() as db:
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

        # Attach artifact PDF if artifacts were created in this execution
        attachments = []
        if exec_summary and exec_summary.get("artifacts", 0) > 0:
            try:
                from app.services.report_pdf_service import ReportPdfService
                from pathlib import Path

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

        if attachments:
            message = MessageSchema(
                subject=subject,
                recipients=recipient_emails,
                body=html,
                subtype="html",
                attachments=attachments,
            )
        else:
            message = MessageSchema(
                subject=subject,
                recipients=recipient_emails,
                body=html,
                subtype="html",
            )

        try:
            await fm.send_message(message)
            logger.info("Scheduled prompt results sent to %s for report %s", recipient_emails, report_id)
        except Exception as e:
            logger.error("Failed to send scheduled prompt results: %s", e)

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
