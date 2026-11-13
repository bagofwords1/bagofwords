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
    # Building the test database runs alembic's fileConfig, which resets the
    # root logger's handlers; attach ours again.
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


def _size():
    async def go():
        await svc.flush()  # measure what is written, not what is buffered
        async with async_session_maker() as db:
            return await svc.table_size_bytes(db)
    return _run(go())


def test_rotation_over_size_cap_first_drops_lines_before_today():
    _capture_on()
    tag = uuid.uuid4().hex
    start_of_today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    padding = "x" * 20_000  # the older lines are what makes the table big
    _emit_at(f"{tag} today-early", start_of_today + timedelta(seconds=1))
    _emit_at(f"{tag} today-late", NOW - timedelta(seconds=1))
    _emit_at(f"{tag} yesterday {padding}", start_of_today - timedelta(seconds=1))
    _emit_at(f"{tag} three-days {padding}", NOW - timedelta(days=3))

    # Under the cap nothing within the week is touched.
    result = _rotate(now=NOW, max_bytes=10 ** 9)
    assert result["trimmed"] == 0
    assert len(_messages(search=tag)) == 4

    # Over the cap, the lines from before today go and today's fit.
    cap = 5_000
    assert _size() > cap
    result = _rotate(now=NOW, max_bytes=cap)
    assert result["trimmed"] >= 2
    assert _messages(search=tag) == {f"{tag} today-early", f"{tag} today-late"}
    assert _size() <= cap


@pytest.mark.parametrize("lines, cap", [(40, 20_000), (60, 50_000)])
def test_rotation_trims_todays_oldest_lines_when_today_alone_is_over_the_cap(lines, cap):
    _capture_on()
    tag = uuid.uuid4().hex
    first = NOW - timedelta(hours=2)
    for i in range(lines):
        _emit_at(f"{tag} {i:03d} " + "y" * 1_000, first + timedelta(seconds=i))
    assert _size() > cap

    _rotate(now=NOW, max_bytes=cap)

    kept = sorted(int(m.split()[1]) for m in _messages(search=tag, limit=lines))
    # Oldest first: what survives is the newest run of lines, and it fits.
    assert 0 < len(kept) < lines
    assert kept == list(range(lines - len(kept), lines))
    assert _size() <= cap


def test_a_failed_write_keeps_the_lines_for_the_next_flush():
    _capture_on()
    tag = uuid.uuid4().hex

    from sqlalchemy import text

    async def rename(src, dst):
        # No API makes the database refuse a write; renaming the table away
        # produces a real insert failure without mocking the app.
        async with async_session_maker() as db:
            await db.execute(text(f"ALTER TABLE {src} RENAME TO {dst}"))
            await db.commit()

    _run(rename("app_logs", "app_logs_offline"))
    try:
        for i in range(3):
            log.info(f"{tag} {i}")
        assert _run(svc.flush()) == 0
    finally:
        _run(rename("app_logs_offline", "app_logs"))

    assert _messages(search=tag) == {f"{tag} {i}" for i in range(3)}
    assert svc.capture_status()["dropped_lines"] == 0


def test_delete_all_discards_lines_other_workers_buffered_before_it():
    _capture_on()
    tag = uuid.uuid4().hex
    log.info(f"{tag} before")
    _messages(search=tag)  # written

    async def clear():
        async with async_session_maker() as db:
            return await svc.delete_all(db)
    assert _run(clear()) >= 1

    # Become "another worker": no local knowledge of the delete, recording on.
    svc.reset()
    _capture_on()
    real_now = datetime.now(timezone.utc).replace(tzinfo=None)
    _emit_at(f"{tag} buffered-before-delete", real_now - timedelta(seconds=3))
    _emit_at(f"{tag} after-delete", real_now + timedelta(seconds=1))

    assert _messages(search=tag) == {f"{tag} after-delete"}


def test_requests_for_the_system_logs_page_are_not_recorded():
    _capture_on()
    access = logging.getLogger("uvicorn.access")
    saved = (access.disabled, access.level)
    access.disabled = False
    access.setLevel(logging.INFO)
    tag = uuid.uuid4().hex
    try:
        fmt = '%s - "%s %s HTTP/%s" %d'
        access.info(fmt, "127.0.0.1:1", "GET", f"/api/system-logs?search={tag}", "1.1", 200)
        access.info(fmt, "127.0.0.1:1", "PUT", "/api/system-logs/settings", "1.1", 200)
        access.info(fmt, "127.0.0.1:1", "GET", f"/api/reports?tag={tag}", "1.1", 200)
    finally:
        access.disabled, access.level = saved
    assert _messages(search=tag) == {f'127.0.0.1:1 - "GET /api/reports?tag={tag} HTTP/1.1" 200'}
    assert _messages(search="/api/system-logs") == set()


def test_stream_capture_wraps_a_callhandlers_patch_made_after_import():
    original = logging.Logger.callHandlers
    seen = []

    def other_library(self, record):
        seen.append(record.getMessage())
        return original(self, record)

    logging.Logger.callHandlers = other_library
    try:
        svc.install_stream_capture()
        _capture_on()
        marker = f"patched-{uuid.uuid4().hex}"
        log.warning(marker)
        assert marker in seen  # still called
        assert _messages(search=marker) == {marker}  # and still captured once
        svc.uninstall_stream_capture()
        assert logging.Logger.callHandlers is other_library
    finally:
        logging.Logger.callHandlers = original


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
