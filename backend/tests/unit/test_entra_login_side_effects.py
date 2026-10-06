"""Side effects of an Entra/OIDC login must actually land in a pooled Postgres
deployment (see docs/feedback-loops/entra-login-cross-loop-and-last-login.md).

- users.last_login is TIMESTAMP WITHOUT TIME ZONE: asyncpg rejects an aware
  datetime for it, so every login writer must pass a naive UTC value.
- OBO auto-provisioning runs on the background-loop thread and must not borrow
  the request engine, whose pooled asyncpg connections belong to another loop.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest

import app.dependencies as deps


class _CaptureSession:
    def __init__(self, sink):
        self.sink = sink

    async def execute(self, stmt):
        self.sink.extend(stmt.compile().params.values())

    async def commit(self):
        pass


def _capturing_maker(sink):
    @asynccontextmanager
    async def maker():
        yield _CaptureSession(sink)
    return maker


@pytest.mark.asyncio
async def test_oidc_login_writes_naive_utc_last_login(monkeypatch):
    from app.services.auth_providers import _record_login

    params = []
    monkeypatch.setattr(deps, "async_session_maker", _capturing_maker(params))
    await _record_login(type("U", (), {"id": "u1"})())

    stamps = [p for p in params if isinstance(p, datetime)]
    assert stamps, "last_login was not written"
    assert all(s.tzinfo is None for s in stamps)
    assert abs(stamps[0] - datetime.utcnow()) < timedelta(minutes=1)


def test_background_auto_provision_does_not_use_request_engine(monkeypatch):
    from app.services import connection_oauth_service as cos

    used_request_engine = []

    def forbidden():
        used_request_engine.append(True)
        raise AssertionError("request-loop engine used from background loop")

    monkeypatch.setattr(deps, "async_session_maker", forbidden)

    provisioned = []

    async def fake_provision(db, user, token):
        provisioned.append((user.id if user else None, token))
        return {}

    monkeypatch.setattr(cos, "auto_provision_connection_credentials", fake_provision)

    from app.services.connection_indexing_service import _get_background_loop
    fut = asyncio.run_coroutine_threadsafe(
        cos._auto_provision_in_background("missing-user", "tok"), _get_background_loop()
    )
    fut.result(timeout=30)

    assert not used_request_engine
