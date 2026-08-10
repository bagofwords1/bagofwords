"""
users.last_login / users.last_seen are TIMESTAMP WITHOUT TIME ZONE.

asyncpg's naive-timestamp codec subtracts the value from a naive epoch, so an
offset-aware datetime raises DataError ("can't subtract offset-naive and
offset-aware datetimes") and the write is lost. These tests pin every login
timestamp write to naive UTC. No database — the UPDATE statement is captured
and its bound parameters inspected.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _captured_values(session: AsyncMock) -> dict:
    """Bound column values of the UPDATE the session executed."""
    session.execute.assert_awaited_once()
    stmt = session.execute.await_args.args[0]
    return stmt.compile().params


def _fake_session_maker(session):
    """Stand-in for async_session_maker() used as an async context manager."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


@pytest.mark.asyncio
async def test_record_login_writes_naive_utc():
    from app.services.auth_providers import _record_login

    session = AsyncMock()

    with patch("app.dependencies.async_session_maker", _fake_session_maker(session)):
        await _record_login(MagicMock(id="u1"))

    written = _captured_values(session)["last_login"]
    assert written.tzinfo is None
    # Naive *UTC*, not naive local time — a local-time write would be off by the
    # host's offset and silently corrupt the "last seen N minutes ago" reads.
    assert abs(written - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(minutes=1)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_last_seen_writes_naive_utc():
    from app.core.auth import _update_last_seen

    session = AsyncMock()
    user = MagicMock(id="u1")
    user.last_seen = None

    await _update_last_seen(user, session)

    written = _captured_values(session)["last_seen"]
    assert written.tzinfo is None
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_last_seen_debounce_compares_stored_naive_value_as_utc():
    """A recent naive last_seen suppresses the write; a stale one lets it through."""
    from app.core.auth import _update_last_seen

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    recent = AsyncMock()
    user = MagicMock(id="u1")
    user.last_seen = now - timedelta(minutes=5)
    await _update_last_seen(user, recent)
    recent.execute.assert_not_awaited()

    stale = AsyncMock()
    user.last_seen = now - timedelta(hours=2)
    await _update_last_seen(user, stale)
    assert _captured_values(stale)["last_seen"].tzinfo is None


@pytest.mark.asyncio
async def test_on_after_login_writes_naive_utc():
    from app.core.auth import UserManager

    session = AsyncMock()

    with patch("app.dependencies.async_session_maker", _fake_session_maker(session)):
        # Unbound call: on_after_login touches only its arguments on this path.
        await UserManager.on_after_login(MagicMock(), MagicMock(id="u1"), request=None)

    assert _captured_values(session)["last_login"].tzinfo is None
