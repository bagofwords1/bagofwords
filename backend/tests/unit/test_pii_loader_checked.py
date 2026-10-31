"""The PII redactor loader, as seen by a caller that must know a failed load.

``load_redactor_for_org`` answers None both for "do not redact" and for "the
settings could not be read" (it never breaks inference). Display surfaces that
would rather show nothing than show raw values use
``load_redactor_for_org_checked``, which tells the two apart. The licence gate
(``has_feature``) and the ruleset build (``build_redactor``) are the loader's
boundaries patched here; the settings row is real.
"""
from __future__ import annotations

import contextlib
import logging
import time
import uuid
from types import SimpleNamespace

import pytest

import app.ai.llm.pii.loader as loader
from app.ai.llm.pii.redactor import build_redactor
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings

pytestmark = pytest.mark.db  # opens real sessions — see tests/unit/conftest.py

EMAIL = "ann@example.com"


async def _org_with_pii() -> str:
    async with async_session_maker() as db:
        org = Organization(name=f"PII org {uuid.uuid4().hex}")
        db.add(org)
        await db.flush()
        db.add(OrganizationSettings(organization_id=str(org.id),
                                    config={"pii_protection": {"enabled": True, "mode": "replace"}}))
        await db.commit()
        return str(org.id)


@pytest.fixture
def licensed(monkeypatch):
    monkeypatch.setattr(loader, "has_feature", lambda feature: True)
    loader.invalidate()
    yield
    loader.invalidate()


def _broken(_config):
    raise RuntimeError("ruleset failed to compile")


@pytest.mark.asyncio
async def test_a_failed_load_is_reported_and_the_old_loader_is_unchanged(licensed, monkeypatch):
    org = await _org_with_pii()
    monkeypatch.setattr(loader, "build_redactor", _broken)
    assert await loader.load_redactor_for_org(org, async_session_maker) is None  # never raises
    assert await loader.load_redactor_for_org_checked(org, async_session_maker) == (None, False)


@pytest.mark.asyncio
async def test_a_cached_failure_is_retried_by_the_checked_loader(licensed, monkeypatch):
    org = await _org_with_pii()
    monkeypatch.setattr(loader, "build_redactor", _broken)
    assert await loader.load_redactor_for_org(org, async_session_maker) is None  # caches the failure
    monkeypatch.setattr(loader, "build_redactor", build_redactor)               # the settings read recovers
    redactor, ok = await loader.load_redactor_for_org_checked(org, async_session_maker)
    assert ok is True and redactor is not None and EMAIL not in redactor.redact_display(f"mail {EMAIL}")
    # Once loaded, both loaders share it.
    assert await loader.load_redactor_for_org(org, async_session_maker) is redactor


@pytest.mark.asyncio
async def test_unlicensed_or_disabled_is_a_successful_load_of_no_redactor(monkeypatch):
    org = await _org_with_pii()
    loader.invalidate()
    monkeypatch.setattr(loader, "has_feature", lambda feature: False)
    assert await loader.load_redactor_for_org_checked(org, async_session_maker) == (None, True)
    monkeypatch.setattr(loader, "has_feature", lambda feature: True)
    async with async_session_maker() as db:
        org2 = Organization(name=f"Plain org {uuid.uuid4().hex}")
        db.add(org2)
        await db.commit()
        plain = str(org2.id)
    try:
        assert await loader.load_redactor_for_org_checked(plain, async_session_maker) == (None, True)
        # Licensed, but nothing to read the settings with: cannot tell, so not a success.
        assert await loader.load_redactor_for_org_checked(plain, None) == (None, False)
    finally:
        loader.invalidate()


@pytest.fixture
def clock(monkeypatch):
    """The loader's clock (its cache TTL and failure backoff)."""
    now = [time.monotonic()]
    monkeypatch.setattr(loader, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
def warnings_logged(caplog, monkeypatch):
    # The per-test migration run (alembic's fileConfig) disables existing loggers.
    for name in ("app.ai.llm.pii.loader", "app.services.artifact_conversation_results"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    caplog.set_level(logging.WARNING)
    return lambda: [r for r in caplog.records if r.levelno >= logging.WARNING
                    and r.name in ("app.ai.llm.pii.loader", "app.services.artifact_conversation_results")]


@pytest.mark.asyncio
async def test_an_unreadable_org_is_retried_at_most_once_per_backoff_window_and_warned_once(
        licensed, monkeypatch, clock, warnings_logged):
    from app.services.artifact_conversation_results import load_redaction

    org = await _org_with_pii()
    builds = []

    def broken(config):
        builds.append(config)
        raise RuntimeError("ruleset failed to compile")

    monkeypatch.setattr(loader, "build_redactor", broken)
    for _ in range(5):  # a tool line, lookups, 250 ms polls: within one second
        assert (await load_redaction(org)).ok is False
        clock[0] += 0.2
    assert len(builds) == 1
    logged = warnings_logged()
    assert len(logged) == 1 and isinstance(logged[0].exc_info[1], RuntimeError)  # the cause is recorded
    clock[0] += 0.5  # the window has passed
    monkeypatch.setattr(loader, "build_redactor", build_redactor)  # the settings read recovers
    recovered = await load_redaction(org)
    assert recovered.ok is True and EMAIL not in recovered.redact(f"mail {EMAIL}")



@pytest.mark.asyncio
async def test_the_backoff_window_starts_when_a_slow_failed_load_ends(licensed, monkeypatch, clock):
    org = await _org_with_pii()
    builds = []

    def slow_broken(config):
        builds.append(config)
        clock[0] += 3.0  # the failing read took 3 s (a database timeout): longer than the window
        raise RuntimeError("ruleset failed to compile")

    monkeypatch.setattr(loader, "build_redactor", slow_broken)
    for _ in range(3):  # back to back, no time passing between them
        assert await loader.load_redactor_for_org_checked(org, async_session_maker) == (None, False)
    assert len(builds) == 1


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_load_and_one_warning(licensed, monkeypatch, warnings_logged):
    import asyncio

    org = await _org_with_pii()
    monkeypatch.setattr(loader, "build_redactor", _broken)
    opened, release = [], asyncio.Event()

    @contextlib.asynccontextmanager
    async def slow_sessions():  # the settings read is slow: every caller arrives while it runs
        opened.append(1)
        await release.wait()
        async with async_session_maker() as session:
            yield session

    callers = [asyncio.create_task(loader.load_redactor_for_org_checked(org, slow_sessions)) for _ in range(5)]
    for _ in range(10):  # let each caller reach the load (no timing: the read waits on ``release``)
        await asyncio.sleep(0)
    release.set()
    assert await asyncio.wait_for(asyncio.gather(*callers), 5) == [(None, False)] * 5
    assert len(opened) == 1
    assert len(warnings_logged()) == 1
