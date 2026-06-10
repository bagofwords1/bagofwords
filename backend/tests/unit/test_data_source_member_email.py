"""Unit tests for delayed member-added data source email notifications.

Covers the two requirements:
  1. Email is only sent when SMTP is configured.
  2. The send is delayed and re-validates membership, so an accidental add that
     is undone within the window never results in an email.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import data_source_member_email as mod


class _AsyncCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *args):
        return False


def _session_maker(db):
    maker = MagicMock()
    maker.return_value = _AsyncCM(db)
    return maker


# --------------------------------------------------------------------------
# schedule_member_added_email
# --------------------------------------------------------------------------

class TestScheduleMemberAddedEmail:
    def test_noop_when_smtp_not_configured(self):
        with patch("app.settings.config.settings") as settings, \
             patch("app.core.scheduler.scheduler") as scheduler:
            settings.email_client = None
            mod.schedule_member_added_email("ds1", "user1", "admin1", "org1")
            scheduler.add_job.assert_not_called()

    def test_schedules_job_when_smtp_configured(self):
        with patch("app.settings.config.settings") as settings, \
             patch("app.core.scheduler.scheduler") as scheduler:
            settings.email_client = MagicMock()
            mod.schedule_member_added_email("ds1", "user1", "admin1", "org1")
            scheduler.add_job.assert_called_once()
            kwargs = scheduler.add_job.call_args.kwargs
            assert kwargs["trigger"] == "date"
            assert kwargs["args"][0] == "ds1"
            assert kwargs["args"][1] == "user1"

    def test_noop_when_adding_self(self):
        with patch("app.settings.config.settings") as settings, \
             patch("app.core.scheduler.scheduler") as scheduler:
            settings.email_client = MagicMock()
            # Actor adds themselves (e.g. creator/owner) — no email.
            mod.schedule_member_added_email("ds1", "user1", "user1", "org1")
            scheduler.add_job.assert_not_called()

    def test_noop_without_recipient(self):
        with patch("app.settings.config.settings") as settings, \
             patch("app.core.scheduler.scheduler") as scheduler:
            settings.email_client = MagicMock()
            mod.schedule_member_added_email("ds1", "", "admin1", "org1")
            scheduler.add_job.assert_not_called()


# --------------------------------------------------------------------------
# send_member_added_email
# --------------------------------------------------------------------------

def _make_db(membership, data_source, user, added_by):
    db = MagicMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = membership
    db.execute = AsyncMock(return_value=exec_result)
    db.get = AsyncMock(side_effect=[data_source, user, added_by])
    return db


def _ds(name="Sales DB"):
    ds = MagicMock()
    ds.name = name
    ds.deleted_at = None
    return ds


def _user(email="member@example.com", name="Member"):
    u = MagicMock()
    u.email = email
    u.name = name
    return u


class TestSendMemberAddedEmail:
    def test_sends_when_membership_present(self):
        fm = MagicMock()
        fm.send_message = AsyncMock()
        db = _make_db(membership=object(), data_source=_ds(), user=_user(),
                      added_by=_user("admin@example.com", "Admin"))

        with patch("app.core.scheduler.claim_scheduled_run", return_value=True), \
             patch("app.settings.config.settings") as settings, \
             patch("app.dependencies.async_session_maker", _session_maker(db)):
            settings.email_client = fm
            settings.bow_config = MagicMock(base_url="http://localhost:3000")
            asyncio.run(mod.send_member_added_email("ds1", "user1", "admin1", "org1"))

        fm.send_message.assert_awaited_once()
        message = fm.send_message.call_args.args[0]
        assert message.recipients == ["member@example.com"]

    def test_skips_when_membership_removed(self):
        """The mistake-undo path: membership gone before the delay elapsed."""
        fm = MagicMock()
        fm.send_message = AsyncMock()
        db = _make_db(membership=None, data_source=_ds(), user=_user(),
                      added_by=_user("admin@example.com", "Admin"))

        with patch("app.core.scheduler.claim_scheduled_run", return_value=True), \
             patch("app.settings.config.settings") as settings, \
             patch("app.dependencies.async_session_maker", _session_maker(db)):
            settings.email_client = fm
            settings.bow_config = MagicMock(base_url="http://localhost:3000")
            asyncio.run(mod.send_member_added_email("ds1", "user1", "admin1", "org1"))

        fm.send_message.assert_not_called()

    def test_skips_when_smtp_unconfigured_at_send_time(self):
        with patch("app.core.scheduler.claim_scheduled_run", return_value=True), \
             patch("app.settings.config.settings") as settings, \
             patch("app.dependencies.async_session_maker") as maker:
            settings.email_client = None
            asyncio.run(mod.send_member_added_email("ds1", "user1", "admin1", "org1"))
            maker.assert_not_called()

    def test_skips_when_not_claim_winner(self):
        with patch("app.core.scheduler.claim_scheduled_run", return_value=False), \
             patch("app.dependencies.async_session_maker") as maker:
            asyncio.run(mod.send_member_added_email("ds1", "user1", "admin1", "org1"))
            maker.assert_not_called()
