import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pydantic import ValidationError

from app.schemas.notification_schema import (
    NotifyRequest,
    NotificationSubscriber,
    ScheduleRequest,
    NotificationType,
    NotificationChannel,
)
from app.services.notification_service import NotificationService


# ---- Schema validation tests ----


class TestNotificationSubscriberSchema:
    def test_valid_user_subscriber(self):
        sub = NotificationSubscriber(type="user", id="abc-123")
        assert sub.type == "user"
        assert sub.id == "abc-123"

    def test_valid_email_subscriber(self):
        sub = NotificationSubscriber(type="email", address="test@example.com")
        assert sub.type == "email"
        assert sub.address == "test@example.com"

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            NotificationSubscriber(type="slack", address="#channel")

    def test_user_without_id_allowed(self):
        # Schema allows it; business logic handles missing id
        sub = NotificationSubscriber(type="user")
        assert sub.id is None

    def test_email_without_address_allowed(self):
        sub = NotificationSubscriber(type="email")
        assert sub.address is None


class TestNotifyRequestSchema:
    def test_valid_request(self):
        req = NotifyRequest(
            type="share_dashboard",
            channels=["email"],
            recipients=["user@example.com"],
            share_url="https://example.com/r/123",
        )
        assert req.type == NotificationType.SHARE_DASHBOARD
        assert len(req.recipients) == 1

    def test_email_validation_rejects_invalid(self):
        with pytest.raises(ValidationError):
            NotifyRequest(
                type="share_dashboard",
                channels=["email"],
                recipients=["not-an-email"],
            )

    def test_email_lowercased(self):
        req = NotifyRequest(
            type="share_dashboard",
            channels=["email"],
            recipients=["USER@Example.COM"],
        )
        assert req.recipients[0] == "user@example.com"

    def test_deduplication(self):
        req = NotifyRequest(
            type="share_dashboard",
            channels=["email"],
            recipients=["a@b.com", "a@b.com", "c@d.com"],
        )
        assert len(req.recipients) == 2


class TestScheduleRequestSchema:
    def test_valid_with_subscribers(self):
        req = ScheduleRequest(
            cron_expression="0 * * * *",
            notification_subscribers=[
                NotificationSubscriber(type="user", id="u1"),
                NotificationSubscriber(type="email", address="a@b.com"),
            ],
        )
        assert len(req.notification_subscribers) == 2

    def test_valid_without_subscribers(self):
        req = ScheduleRequest(cron_expression="None")
        assert req.notification_subscribers is None


# ---- NotificationService unit tests ----


class TestNotificationServiceDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_email_calls_send(self):
        svc = NotificationService()
        mock_fm = AsyncMock()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.email_client = mock_fm

            result = await svc.dispatch(
                notification_type=NotificationType.SHARE_DASHBOARD,
                channels=[NotificationChannel.EMAIL],
                recipients=["user@test.com"],
                share_url="https://example.com/r/1",
                report_title="Test Report",
                sender_name="Test User",
            )

        assert len(result.dispatched) == 1
        assert result.dispatched[0].channel == "email"
        assert result.dispatched[0].status == "sent"

    @pytest.mark.asyncio
    async def test_dispatch_no_smtp_returns_error(self):
        svc = NotificationService()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.email_client = None

            result = await svc.dispatch(
                notification_type=NotificationType.SHARE_DASHBOARD,
                channels=[NotificationChannel.EMAIL],
                recipients=["user@test.com"],
                share_url="https://example.com/r/1",
                report_title="Test Report",
                sender_name="Test User",
            )

        assert len(result.errors) == 1
        assert "SMTP" in result.errors[0].error

    def test_build_subject_share_dashboard(self):
        svc = NotificationService()
        subject = svc._build_subject(NotificationType.SHARE_DASHBOARD, "My Report")
        assert "My Report" in subject
        assert "shared" in subject.lower()

    def test_build_subject_schedule_report(self):
        svc = NotificationService()
        subject = svc._build_subject(NotificationType.SCHEDULE_REPORT, "Weekly KPIs")
        assert "Weekly KPIs" in subject

    def test_build_html_contains_url(self):
        svc = NotificationService()
        html = svc._build_html({
            "notification_type": NotificationType.SHARE_DASHBOARD,
            "share_url": "https://example.com/r/123",
            "report_title": "Test",
            "sender_name": "Alice",
            "message": None,
        })
        assert "https://example.com/r/123" in html
        assert "Alice" in html

    def test_build_html_escapes_message(self):
        svc = NotificationService()
        html = svc._build_html({
            "notification_type": NotificationType.SHARE_DASHBOARD,
            "share_url": "https://example.com",
            "report_title": "Test",
            "sender_name": "Alice",
            "message": "<script>alert('xss')</script>",
        })
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_build_results_html_contains_url(self):
        svc = NotificationService()
        html = svc._build_results_html("My Report", "https://example.com/r/1")
        assert "https://example.com/r/1" in html
        assert "My Report" in html
        assert "Scheduled report completed" in html


class TestSendScheduledReportResults:
    @pytest.mark.asyncio
    async def test_skips_when_no_smtp(self):
        svc = NotificationService()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.email_client = None

            # Should return without error
            await svc.send_scheduled_report_results(
                report_id="r1",
                report_title="Test",
                subscribers=[{"type": "email", "address": "a@b.com"}],
                report_url="https://example.com",
            )

    @pytest.mark.asyncio
    async def test_skips_when_no_subscribers(self):
        svc = NotificationService()

        with patch("app.services.notification_service.settings") as mock_settings:
            mock_settings.email_client = AsyncMock()

            await svc.send_scheduled_report_results(
                report_id="r1",
                report_title="Test",
                subscribers=[],
                report_url="https://example.com",
            )

            # send_message should never be called
            mock_settings.email_client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolves_email_subscribers_and_sends(self):
        svc = NotificationService()
        mock_fm = AsyncMock()

        mock_db = AsyncMock()
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("app.services.notification_service.settings") as mock_settings,
            patch("app.dependencies.async_session_maker", return_value=mock_session_ctx),
            patch("app.services.report_pdf_service.ReportPdfService") as mock_pdf_cls,
        ):
            mock_settings.email_client = mock_fm
            mock_pdf_cls.return_value.generate_for_report = AsyncMock(return_value=None)

            await svc.send_scheduled_report_results(
                report_id="r1",
                report_title="Test Report",
                subscribers=[{"type": "email", "address": "notify@example.com"}],
                report_url="https://example.com/r/r1",
            )

            mock_fm.send_message.assert_called_once()
            call_args = mock_fm.send_message.call_args
            msg = call_args[0][0]
            assert "notify@example.com" in msg.recipients
