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

logger = getLogger(__name__)


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

        subject = self._build_subject(context["notification_type"], context["report_title"])
        html = self._build_html(context)

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

    # ---- email content builders ----

    def _build_subject(self, notification_type: NotificationType, report_title: str) -> str:
        subjects = {
            NotificationType.SHARE_DASHBOARD: f"{report_title} - Dashboard shared with you",
            NotificationType.SHARE_CONVERSATION: f"{report_title} - Conversation shared with you",
            NotificationType.SCHEDULE_REPORT: f"{report_title} - Report schedule notification",
        }
        return subjects.get(notification_type, f"{report_title} - Notification")

    def _build_html(self, context: dict) -> str:
        notification_type = context["notification_type"]
        share_url = context["share_url"]
        report_title = context["report_title"]
        sender_name = context["sender_name"]
        message = context.get("message")

        if notification_type == NotificationType.SHARE_DASHBOARD:
            heading = f"{sender_name} shared a dashboard with you"
            description = f'<strong>{report_title}</strong> has been shared with you.'
            cta_text = "View Dashboard"
        elif notification_type == NotificationType.SHARE_CONVERSATION:
            heading = f"{sender_name} shared a conversation with you"
            description = f'A conversation from <strong>{report_title}</strong> has been shared with you.'
            cta_text = "View Conversation"
        elif notification_type == NotificationType.SCHEDULE_REPORT:
            heading = f"Report scheduled: {report_title}"
            description = f'{sender_name} set up a schedule for <strong>{report_title}</strong>. You will receive updates when it runs.'
            cta_text = "View Report"
        else:
            heading = "Notification"
            description = ""
            cta_text = "View"

        message_block = ""
        if message:
            safe_message = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            message_block = f"""
            <tr>
              <td style="padding: 12px 0;">
                <div style="background: #f9fafb; border-left: 3px solid #d1d5db; padding: 12px 16px; border-radius: 4px; font-style: italic; color: #4b5563;">
                  &ldquo;{safe_message}&rdquo;
                </div>
              </td>
            </tr>"""

        return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0; padding:0; background:#f3f4f6; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6; padding:40px 0;">
    <tr>
      <td align="center">
        <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff; border-radius:8px; overflow:hidden;">
          <tr>
            <td style="padding:32px 40px 24px;">
              <h2 style="margin:0 0 8px; font-size:18px; color:#111827;">{heading}</h2>
              <p style="margin:0; font-size:14px; color:#6b7280; line-height:1.5;">{description}</p>
            </td>
          </tr>{message_block}
          <tr>
            <td style="padding:0 40px 32px;">
              <a href="{share_url}"
                 style="display:inline-block; background:#2563eb; color:#ffffff; text-decoration:none; padding:10px 24px; border-radius:6px; font-size:14px; font-weight:500;">
                {cta_text}
              </a>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 40px; border-top:1px solid #e5e7eb;">
              <p style="margin:0; font-size:12px; color:#9ca3af;">Sent via Bag of Words</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


    # ---- scheduled report results ----

    async def send_scheduled_report_results(
        self,
        report_id: str,
        report_title: str,
        subscribers: list,
        report_url: str,
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
        )

    async def send_scheduled_prompt_results(
        self,
        report_id: str,
        report_title: str,
        subscribers: list,
        report_url: str,
        exec_summary: Optional[dict] = None,
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

        subject = f"{report_title} - Scheduled prompt results"
        html = self._build_scheduled_prompt_html(report_title, report_url, exec_summary)

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

    def _build_scheduled_prompt_html(self, report_title: str, report_url: str, exec_summary: Optional[dict] = None) -> str:
        # Build natural stats sentence
        stats_sentence = ""
        if exec_summary:
            iters = exec_summary.get("iterations", 0)
            queries = exec_summary.get("queries", 0)
            parts = []
            if iters:
                parts.append(f"{iters} iteration{'s' if iters != 1 else ''}")
            if queries:
                parts.append(f"{queries} quer{'ies' if queries != 1 else 'y'}")
            if parts:
                stats_sentence = f" It completed {' and '.join(parts)}."

        # Build summary content
        summary_html = ""
        if exec_summary and exec_summary.get("last_content"):
            content = exec_summary["last_content"]
            if len(content) > 2000:
                content = content[:2000] + "..."
            summary_html = f"""{self._md_to_html(content)}"""

        return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0; padding:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; color:#222; font-size:14px; line-height:1.6;">
  <div style="max-width:600px; padding:20px;">
    <p>Hi,</p>
    <p>Your scheduled report &ldquo;{report_title}&rdquo; has finished running.{stats_sentence}</p>
    {summary_html}
    <p><a href="{report_url}">View the full report</a></p>
    <p style="color:#999;">&mdash; Bag of Words</p>
  </div>
</body>
</html>"""


notification_service = NotificationService()
