"""Unit tests for the post-registration welcome email."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services import welcome_email as mod


@pytest.mark.asyncio
async def test_noop_when_smtp_not_configured():
    """No SMTP → no send, no raise (registration must never be blocked)."""
    with patch("app.settings.config.settings") as settings, \
         patch("app.services.notification_service.notification_service.send_custom_email", new=AsyncMock()) as send:
        settings.email_client = None
        await mod.send_welcome_email("user-1")
        send.assert_not_awaited()


@pytest.mark.asyncio
async def test_swallows_errors(monkeypatch):
    """Any internal failure is swallowed — never propagates into registration."""
    # email_client present so we get past the early return, but session maker blows up.
    import app.settings.config as cfg
    monkeypatch.setattr(cfg.settings, "email_client", object(), raising=False)
    with patch("app.dependencies.async_session_maker", side_effect=RuntimeError("boom")):
        # Should not raise.
        await mod.send_welcome_email("user-1")


# -- which organization the email speaks for ---------------------------------


@pytest.fixture
def sent(monkeypatch):
    """Capture the outgoing email at the SMTP boundary."""
    from types import SimpleNamespace

    from app.services import email_client_resolver
    from app.services.notification_service import notification_service

    calls = []

    async def fake_send(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="sent", source="test", error=None)

    async def available(db, organization_id, purpose="system"):
        return True

    monkeypatch.setattr(notification_service, "send_custom_email", fake_send)
    monkeypatch.setattr(email_client_resolver, "is_outbound_available", available)
    return calls


async def _person_in(orgs):
    """A user who is a member of each (label, joined_at, live) organization.

    Written straight to the database: the sign-up API attaches the memberships
    of one request in a single transaction, and the point here is to control
    their order and timestamps. Each organization has one public agent named
    after its label. Returns (user_id, {label: organization_id}).
    """
    import uuid
    from datetime import datetime

    from app.dependencies import async_session_maker
    from app.models.data_source import DataSource
    from app.models.membership import Membership
    from app.models.organization import Organization
    from app.models.user import User

    tag = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        user = User(id=str(uuid.uuid4()), email=f"new-{tag}@example.com", name="New Person",
                    hashed_password="x", is_active=True, is_verified=True, is_superuser=False)
        db.add(user)
        ids = {}
        for label, joined_at, live in orgs:
            org = Organization(id=str(uuid.uuid4()), name=f"{label}-{tag}")
            db.add(org)
            await db.flush()
            ids[label] = org.id
            db.add(DataSource(name=f"agent-of-{label}", organization_id=org.id, is_public=True))
            db.add(Membership(user_id=user.id, organization_id=org.id, role="member", created_at=joined_at,
                              deleted_at=None if live else datetime(2024, 6, 1)))
        await db.commit()
        return user.id, ids


def _organization_and_agents(call):
    return call["organization_id"], [line.strip(" -") for line in call["body"].splitlines() if "agent-of-" in line]


@pytest.mark.db
@pytest.mark.asyncio
async def test_welcome_email_uses_the_organization_the_user_just_joined(sent):
    from datetime import datetime

    user_id, orgs = await _person_in([
        ("sales", datetime(2024, 1, 1), True),
        ("finance", datetime(2024, 3, 1), True),
    ])
    for label in ("sales", "finance"):
        sent.clear()
        await mod.send_welcome_email(user_id, orgs[label])
        assert len(sent) == 1, "one sign-up, one email"
        assert _organization_and_agents(sent[0]) == (orgs[label], [f"agent-of-{label}"])


@pytest.mark.db
@pytest.mark.asyncio
async def test_welcome_email_without_a_named_organization_uses_the_latest_live_membership(sent):
    from datetime import datetime

    user_id, orgs = await _person_in([
        ("newest-but-left", datetime(2024, 5, 1), False),
        ("older", datetime(2024, 1, 1), True),
        ("latest", datetime(2024, 3, 1), True),
    ])
    await mod.send_welcome_email(user_id)
    assert len(sent) == 1
    assert _organization_and_agents(sent[0]) == (orgs["latest"], ["agent-of-latest"])

    # An organization the user is not a live member of is never used.
    sent.clear()
    await mod.send_welcome_email(user_id, orgs["newest-but-left"])
    assert _organization_and_agents(sent[0]) == (orgs["latest"], ["agent-of-latest"])


@pytest.mark.db
@pytest.mark.asyncio
async def test_welcome_email_for_organizations_joined_together_is_one_stable_choice(sent):
    from datetime import datetime

    together = datetime(2024, 2, 2)
    user_id, orgs = await _person_in([("a", together, True), ("b", together, True), ("c", together, True)])
    expected = min(orgs.values())
    for _ in range(3):
        sent.clear()
        await mod.send_welcome_email(user_id)
        assert len(sent) == 1
        assert sent[0]["organization_id"] == expected


@pytest.mark.db
@pytest.mark.asyncio
async def test_welcome_email_with_one_organization_is_unchanged(sent):
    from datetime import datetime

    user_id, orgs = await _person_in([("only", datetime(2024, 1, 1), True)])
    await mod.send_welcome_email(user_id)
    assert _organization_and_agents(sent[0]) == (orgs["only"], ["agent-of-only"])
    assert sent[0]["recipients"] and sent[0]["purpose"] == "system"
