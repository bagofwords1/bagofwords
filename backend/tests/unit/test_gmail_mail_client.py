"""Contract tests for the native Gmail API mail connector.

The connector is intentionally mail-shaped (list/read/search email capabilities)
while reusing the existing file-payload transport used by the mail tools.  All
Google HTTP calls are exercised through ``httpx.MockTransport`` so the tests are
deterministic and never require live credentials.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import httpx
import pytest

from app.data_sources.clients.base import Capability
from app.data_sources.clients.gmail_mail_client import GmailMailClient


def _b64url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _headers(subject: str = "Quarterly update") -> list[dict]:
    return [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": "Sender <sender@example.com>"},
        {"name": "To", "value": "reader@example.com"},
        {"name": "Date", "value": "Fri, 18 Jul 2026 09:30:00 +0000"},
    ]


def _metadata(message_id: str, subject: str = "Quarterly update") -> dict:
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "internalDate": "1784367000000",
        "snippet": "A short message preview",
        "payload": {"headers": _headers(subject)},
    }


def _client(handler) -> GmailMailClient:
    return GmailMailClient(
        access_token="user-token",
        transport=httpx.MockTransport(handler),
    )


def test_gmail_advertises_only_mail_capabilities():
    assert GmailMailClient.capabilities == {
        Capability.LIST_EMAILS,
        Capability.READ_EMAIL,
        Capability.SEARCH_EMAILS,
    }
    assert Capability.LIST_FILES not in GmailMailClient.capabilities
    assert Capability.READ_FILE not in GmailMailClient.capabilities


def test_list_emails_returns_canonical_message_entries_in_api_order():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me/messages"):
            assert request.url.params["maxResults"] == "25"
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"id": "newer", "threadId": "thread-newer"},
                        {"id": "older", "threadId": "thread-older"},
                    ]
                },
            )
        message_id = request.url.path.rsplit("/", 1)[-1]
        assert request.url.params["format"] == "metadata"
        return httpx.Response(200, json=_metadata(message_id, f"Subject {message_id}"))

    rows = _client(handler).list_files()

    assert [row["id"] for row in rows] == ["newer", "older"]
    assert rows[0]["name"] == "Subject newer"
    assert rows[0]["path"] == "Subject newer"
    assert rows[0]["mime_type"] == "message/rfc822"
    assert rows[0]["modified_at"].endswith("Z")
    assert rows[0]["web_url"].endswith("thread-newer")


def test_search_emails_forwards_native_gmail_query_unchanged():
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me/messages"):
            seen_queries.append(request.url.params["q"])
            assert request.url.params["includeSpamTrash"] == "false"
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json=_metadata("m1"))

    query = "from:finance@example.com newer_than:30d has:attachment"
    rows = _client(handler).search_files(query)

    assert seen_queries == [query]
    assert [row["id"] for row in rows] == ["m1"]


def test_read_email_prefers_plain_text_across_nested_multipart_payloads():
    message = _metadata("m1")
    message["payload"].update(
        {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/html", "body": {"data": _b64url("<p>HTML version</p>")}},
                        {"mimeType": "text/plain", "body": {"data": _b64url("Plain version\nSecond line")}},
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "body": {"attachmentId": "attachment-1"},
                },
            ],
        }
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=message)

    content = _client(handler).read_file("m1")

    assert "Subject: Quarterly update" in content
    assert "From: Sender <sender@example.com>" in content
    assert "Plain version\nSecond line" in content
    assert "HTML version" not in content


def test_read_email_uses_clean_html_fallback_when_plain_text_is_absent():
    message = _metadata("m2", "HTML only")
    message["payload"].update(
        {
            "mimeType": "text/html",
            "body": {"data": _b64url("<div>Hello&nbsp;<strong>team</strong><br>Next line</div>")},
        }
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=message)

    content = _client(handler).read_file("m2")

    assert "Subject: HTML only" in content
    assert "Hello team" in content
    assert "<strong>" not in content


def test_profile_test_reports_the_connected_google_account():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/users/me/profile")
        return httpx.Response(200, json={"emailAddress": "reader@example.com"})

    result = _client(handler).test_connection()

    assert result["success"] is True
    assert "reader@example.com" in result["message"]


def test_admin_save_without_user_token_is_non_destructive_and_does_not_call_google():
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    client = GmailMailClient(transport=httpx.MockTransport(handler))

    assert client.list_files() == []
    assert client.get_schemas() == []
    assert client.test_connection()["success"] is True
    assert called is False


@pytest.mark.asyncio
async def test_admin_oauth_app_validation_accepts_mail_inventory_without_user_token():
    from app.services.connection_service import ConnectionService

    result = await ConnectionService().test_connection_params(
        data_source_type="gmail_mail",
        config={},
        credentials={
            "oauth_client_id": "client-id",
            "oauth_client_secret": "client-secret",
        },
    )

    assert result["success"] is True
    assert result["connectivity"] is True
    assert result["table_count"] == 0
    assert "signing in" in result["message"]


def test_native_gmail_visible_and_gmail_mcp_preset_removed():
    from app.schemas.data_source_registry import (
        REGISTRY,
        list_available_data_sources,
        mcp_preset,
        resolve_client_class,
    )

    visible = {item["type"] for item in list_available_data_sources()}
    assert {"gmail_mail", "google_drive"} <= visible
    assert REGISTRY["gmail_mail"].data_shape == "files"
    assert REGISTRY["gmail_mail"].catalog_ownership == "per_user"
    assert resolve_client_class("gmail_mail") is GmailMailClient
    # Gmail is now served solely by the native connector — the MCP preview preset
    # was removed once it shipped. Drive keeps its MCP preview alongside native.
    assert mcp_preset("gmail") is None
    assert mcp_preset("google_drive").title.endswith("(MCP Preview)")


def test_native_gmail_oauth_uses_readonly_api_scope_without_mcp_audience():
    from app.services.connection_oauth_service import get_oauth_params

    connection = MagicMock()
    connection.id = "gmail-connection"
    connection.type = "gmail_mail"
    connection.decrypt_credentials.return_value = {
        "oauth_client_id": "client-id",
        "oauth_client_secret": "client-secret",
    }

    params = get_oauth_params(connection)

    assert params["provider_name"] == "google"
    assert "gmail.readonly" in params["scopes"]
    assert "drive.readonly" not in params["scopes"]
    assert not params.get("audience")


def test_mail_tool_metadata_covers_both_supported_providers():
    from app.ai.tools.implementations.email_tools import (
        ListEmailsTool,
        ReadEmailTool,
        SearchEmailsTool,
    )

    descriptions = " ".join(
        [
            ListEmailsTool().metadata.description,
            ReadEmailTool().metadata.description,
            SearchEmailsTool().metadata.description,
        ]
    )
    assert "Gmail" in descriptions
    assert "Outlook" in descriptions

    # The shared file-tool execution paths must also stay mail-shaped in the
    # live tool timeline and model-facing observations, not just in metadata.
    assert ListEmailsTool._start_title == "Listing emails"
    assert ListEmailsTool._item_noun == "email"
    assert ReadEmailTool._start_noun == "email"
    assert ReadEmailTool._operation_name == "read_email"
    assert SearchEmailsTool._start_noun == "emails"
    assert SearchEmailsTool._item_noun == "email"
    assert SearchEmailsTool._operation_name == "search_email"
