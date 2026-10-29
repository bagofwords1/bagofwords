"""users.last_login is TIMESTAMP WITHOUT TIME ZONE. asyncpg rejects an aware
datetime for it ("can't subtract offset-naive and offset-aware datetimes"), so
every login writer must bind a naive UTC value. SQLite accepts either, which
is why this is asserted on the bound parameter rather than on a round trip."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest

import app.dependencies as deps


def _capture(monkeypatch):
    params = []

    class _Session:
        async def execute(self, stmt):
            params.extend(stmt.compile().params.values())

        async def commit(self):
            pass

    @asynccontextmanager
    async def maker():
        yield _Session()

    monkeypatch.setattr(deps, "async_session_maker", maker)
    return params


def _assert_naive_now(params):
    stamps = [p for p in params if isinstance(p, datetime)]
    assert stamps, "last_login was not written"
    assert all(s.tzinfo is None for s in stamps)
    assert abs(stamps[0] - datetime.utcnow()) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_oidc_login_writes_naive_utc_last_login(monkeypatch):
    from app.services.auth_providers import _record_login

    params = _capture(monkeypatch)
    await _record_login(type("U", (), {"id": "u1"})())
    _assert_naive_now(params)


@pytest.mark.asyncio
async def test_password_login_writes_naive_utc_last_login(monkeypatch):
    from app.core.auth import UserManager

    params = _capture(monkeypatch)
    await UserManager.on_after_login(object.__new__(UserManager), type("U", (), {"id": "u1"})())
    _assert_naive_now(params)
