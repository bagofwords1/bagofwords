"""System logs service: retention, size cap, timed capture and stream copy.

Rows are produced through the capture path itself (log records and stream
writes, flushed by the service), with explicit timestamps and ``now`` values,
so nothing here depends on the wall clock or seeds the table by hand.
"""
import asyncio
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.dependencies import async_session_maker
from app.services import app_log_service as svc

pytestmark = pytest.mark.db

log = logging.getLogger("tests.app_log_service")
log.setLevel(logging.INFO)

# A Tuesday mid-morning, far enough in the past that a timed capture set
# relative to it has already expired by the real clock.
NOW = datetime(2026, 10, 6, 9, 30)


@pytest.fixture(autouse=True)
def _isolated_state():
    # Building the test database runs alembic's fileConfig, which disables
    # loggers that already exist (this module's included) and resets root's
    # handlers; production runs migrations in a separate process.
    log.disabled = False
    svc.install_log_handler()
    svc.reset()
    yield
    svc.reset()
    svc.uninstall_stream_capture()


def _run(coro):
    return asyncio.run(coro)


def _capture_on():
    async def go():
        async with async_session_maker() as db:
            await svc.set_enabled(db, True)
    _run(go())


def _emit_at(message, when, level=logging.INFO):
    record = log.makeRecord(log.name, level, __file__, 0, message, (), None)
    record.created = when.replace(tzinfo=timezone.utc).timestamp()
    log.handle(record)


def _messages(**filters):
    async def go():
        await svc.flush()
        async with async_session_maker() as db:
            rows, _ = await svc.query_logs(db, **filters)
            return {r.message for r in rows}
    return _run(go())


def _rotate(**kw):
    async def go():
        await svc.flush()  # rotation applies to rows already written
        async with async_session_maker() as db:
            return await svc.rotate(db, **kw)
    return _run(go())


@pytest.mark.parametrize("age_days, kept", [(8, False), (7.01, False), (6.9, True), (1, True), (0, True)])
def test_rotation_drops_lines_older_than_a_week(age_days, kept):
    _capture_on()
    tag = uuid.uuid4().hex
    _emit_at(f"{tag} aged", NOW - timedelta(days=age_days))
    _emit_at(f"{tag} fresh", NOW - timedelta(minutes=1))

    _rotate(now=NOW)

    remaining = _messages(search=tag)
    assert f"{tag} fresh" in remaining
    assert (f"{tag} aged" in remaining) is kept


def test_rotation_over_size_cap_keeps_only_todays_lines():
    _capture_on()
    tag = uuid.uuid4().hex
    start_of_today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    _emit_at(f"{tag} today-early", start_of_today + timedelta(seconds=1))
    _emit_at(f"{tag} today-late", NOW - timedelta(seconds=1))
    _emit_at(f"{tag} yesterday", start_of_today - timedelta(seconds=1))
    _emit_at(f"{tag} three-days", NOW - timedelta(days=3))

    # Under the cap nothing within the week is touched.
    result = _rotate(now=NOW, max_bytes=10 ** 9)
    assert result["trimmed"] == 0
    assert len(_messages(search=tag)) == 4

    # Over the cap only today's lines survive.
    result = _rotate(now=NOW, max_bytes=1)
    assert result["size_bytes"] > 1
    assert result["trimmed"] >= 2
    assert _messages(search=tag) == {f"{tag} today-early", f"{tag} today-late"}


def test_timed_capture_is_off_once_its_period_passes():
    async def go():
        async with async_session_maker() as db:
            return await svc.set_enabled(db, True, duration_minutes=30, now=NOW)
    status = _run(go())
    assert status["enabled"] is True
    assert status["expires_at"] == NOW + timedelta(minutes=30)

    assert svc.capture_status(now=NOW + timedelta(minutes=29))["enabled"] is True
    assert svc.capture_status(now=NOW + timedelta(minutes=31))["enabled"] is False

    # NOW + 30 min is long past by the real clock, so nothing is captured.
    marker = f"expired-{uuid.uuid4().hex}"
    log.error(marker)
    assert _messages(search=marker) == set()


def test_direct_stdout_and_stderr_writes_are_captured():
    svc.install_stream_capture()
    _capture_on()
    tag = uuid.uuid4().hex

    print(f"{tag} printed")
    sys.stderr.write(f"{tag} raw stderr part one, ")
    sys.stderr.write("part two\n")
    sys.stdout.write(f"{tag} unterminated")  # not a line yet

    async def go():
        await svc.flush()
        async with async_session_maker() as db:
            rows, _ = await svc.query_logs(db, search=tag)
            errors, _ = await svc.query_logs(db, search=tag, errors_only=True)
            return rows, errors
    rows, errors = _run(go())

    by_message = {r.message: r for r in rows}
    assert set(by_message) == {f"{tag} printed", f"{tag} raw stderr part one, part two"}
    assert by_message[f"{tag} printed"].stream == "stdout"
    assert by_message[f"{tag} printed"].level is None
    assert [e.message for e in errors] == [f"{tag} raw stderr part one, part two"]


def test_stream_capture_does_not_duplicate_logged_lines():
    svc.install_stream_capture()
    _capture_on()
    # A console handler created after the tee is installed writes through it.
    handler = logging.StreamHandler(sys.stderr)
    log.addHandler(handler)
    try:
        marker = f"once-{uuid.uuid4().hex}"
        log.warning(marker)
    finally:
        log.removeHandler(handler)
    async def go():
        await svc.flush()
        async with async_session_maker() as db:
            rows, _ = await svc.query_logs(db, search=marker)
            return [r.message for r in rows]
    assert _run(go()) == [marker]
