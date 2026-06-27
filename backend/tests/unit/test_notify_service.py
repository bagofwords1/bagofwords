"""Unit tests for NotifyService — recipient resolution + channel fan-out.

The membership/security boundary and the channel selection are covered here with
the DB and downstream senders (inbox, email, chat adapters) mocked. The notify
*tool* wrapper is thin over this service.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.notify_service import NotifyService, _snippet


def _db_with_members(rows):
    """A mock AsyncSession whose execute().all() returns the given (membership, user) rows."""
    db = MagicMock()
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    db.execute = AsyncMock(return_value=result)
    return db


SENDER = SimpleNamespace(id="u-self", email="me@acme.com", name="Me")
ORG = SimpleNamespace(id="org-1")
MEMBER_ROWS = [
    (SimpleNamespace(user_id="u-bob", email=None), SimpleNamespace(id="u-bob", email="bob@acme.com", name="Bob")),
    (SimpleNamespace(user_id=None, email="invite@acme.com"), None),
]


# ---- snippet helper --------------------------------------------------------


def test_snippet_strips_html_and_truncates():
    assert _snippet("<p>Hello <b>world</b></p>", "html") == "Hello world"
    long = "x" * 400
    out = _snippet(long, "text", limit=280)
    assert len(out) == 281 and out.endswith("…")


# ---- recipient resolution (the security boundary) --------------------------


@pytest.mark.asyncio
async def test_resolve_always_includes_self():
    db = _db_with_members(MEMBER_ROWS)
    resolved, rejected = await NotifyService().resolve_recipients(db, ORG, SENDER, [])
    assert [r.email for r in resolved] == ["me@acme.com"]
    assert resolved[0].is_self is True
    assert rejected == []


@pytest.mark.asyncio
async def test_resolve_accepts_member_and_invite_rejects_outsider():
    db = _db_with_members(MEMBER_ROWS)
    resolved, rejected = await NotifyService().resolve_recipients(
        db, ORG, SENDER, ["bob@acme.com", "OUTSIDER@evil.com", "invite@acme.com"]
    )
    emails = [r.email for r in resolved]
    assert emails == ["me@acme.com", "bob@acme.com", "invite@acme.com"]
    # Active member resolves to a user; pending invite has no account.
    by_email = {r.email: r for r in resolved}
    assert by_email["bob@acme.com"].user_id == "u-bob"
    assert by_email["invite@acme.com"].user_id is None
    assert rejected == ["outsider@evil.com"]


@pytest.mark.asyncio
async def test_resolve_dedupes_self_and_repeats():
    db = _db_with_members(MEMBER_ROWS)
    resolved, _ = await NotifyService().resolve_recipients(
        db, ORG, SENDER, ["me@acme.com", "bob@acme.com", "bob@acme.com"]
    )
    assert [r.email for r in resolved] == ["me@acme.com", "bob@acme.com"]


# ---- fan-out: in-app always + email fallback (no chat configured) ----------


@pytest.mark.asyncio
async def test_notify_inapp_plus_email_and_threading_is_self_only():
    db = _db_with_members(MEMBER_ROWS)
    svc = NotifyService()
    sc = SimpleNamespace(id="sc-1")
    report = SimpleNamespace(id="r-1")

    with patch("app.services.inbox_service.inbox_service.notify_users", new=AsyncMock()) as mock_inbox, \
         patch("app.services.external_platform_service.ExternalPlatformService.get_platform_by_type",
               new=AsyncMock(return_value=None)), \
         patch("app.services.email_send_service.EmailSendService.send",
               new=AsyncMock(return_value=SimpleNamespace(success=True, error=None))) as mock_email:
        result = await svc.notify(
            db,
            sender=SENDER,
            organization=ORG,
            report=report,
            subject="Q2 done",
            body="All set.",
            recipient_emails=["bob@acme.com"],
            system_completion=sc,
        )

    # In-app: one call, both users with accounts, actor None so self is included.
    mock_inbox.assert_awaited_once()
    kw = mock_inbox.await_args.kwargs
    assert set(kw["user_ids"]) == {"u-self", "u-bob"}
    assert kw["actor_user_id"] is None
    assert kw["link"] == "/reports/r-1"

    # Email: sent to both; only the self send carries system_completion (one-way
    # reply-threading stays owner-only).
    sent = {c.kwargs["recipient"]: c.kwargs for c in mock_email.await_args_list}
    assert set(sent) == {"me@acme.com", "bob@acme.com"}
    assert sent["me@acme.com"]["system_completion"] is sc
    assert sent["bob@acme.com"]["system_completion"] is None

    assert result["success"] is True
    by_email = {r["email"]: r for r in result["results"]}
    assert by_email["me@acme.com"]["delivered"] == ["in_app", "email"]
    assert by_email["bob@acme.com"]["delivered"] == ["in_app", "email"]
    assert result["rejected"] == []


# ---- fan-out: verified Teams mapping wins over email -----------------------


@pytest.mark.asyncio
async def test_notify_prefers_verified_teams_over_email():
    db = _db_with_members(MEMBER_ROWS)
    svc = NotifyService()
    teams_platform = SimpleNamespace(platform_type="teams", is_active=True)
    teams_mapping = SimpleNamespace(is_verified=True, external_user_id="teams-bob")

    async def _platform_by_type(self, _db, _org, ptype):
        return teams_platform if ptype == "teams" else None

    async def _mapping(self, _db, _org, ptype, app_user_id):
        # Only Bob has a verified Teams identity; self does not → self falls to email.
        return teams_mapping if (ptype == "teams" and app_user_id == "u-bob") else None

    adapter = MagicMock()
    adapter.send_dm = AsyncMock(return_value=True)

    with patch("app.services.inbox_service.inbox_service.notify_users", new=AsyncMock()), \
         patch("app.services.external_platform_service.ExternalPlatformService.get_platform_by_type", new=_platform_by_type), \
         patch("app.services.external_user_mapping_service.ExternalUserMappingService.get_mapping_by_app_user", new=_mapping), \
         patch("app.services.platform_adapters.adapter_factory.PlatformAdapterFactory.create_adapter", return_value=adapter), \
         patch("app.services.email_send_service.EmailSendService.send",
               new=AsyncMock(return_value=SimpleNamespace(success=True, error=None))) as mock_email:
        result = await svc.notify(
            db, sender=SENDER, organization=ORG, report=SimpleNamespace(id="r-1"),
            subject="Hi", body="ping", recipient_emails=["bob@acme.com"],
        )

    # Bob reached on Teams (a DM), not email; self has no Teams mapping → email.
    adapter.send_dm.assert_awaited_once()
    assert adapter.send_dm.await_args.args[0] == "teams-bob"
    emailed = {c.kwargs["recipient"] for c in mock_email.await_args_list}
    assert emailed == {"me@acme.com"}  # only self fell back to email
    by_email = {r["email"]: r for r in result["results"]}
    assert "teams" in by_email["bob@acme.com"]["delivered"]
    assert "email" in by_email["me@acme.com"]["delivered"]
