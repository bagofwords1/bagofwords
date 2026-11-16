"""System logs service: log files, retention, size cap, reads and stream copy.

Lines are produced through the capture path itself (log records and stream
writes, flushed by the service) into a per-test directory. Ages are set with
explicit timestamps and file modification times, so nothing here depends on
the wall clock. No database is involved.
"""
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import app_log_service as svc

log = logging.getLogger("tests.app_log_service")
log.setLevel(logging.INFO)

NOW = datetime(2026, 10, 6, 9, 30)


def _config(directory, max_bytes=10 ** 9, retention_days=7):
    return svc.LogConfig(directory=directory, retention=timedelta(days=retention_days), max_bytes=max_bytes)


@pytest.fixture
def cfg(tmp_path):
    # Building a test database elsewhere runs alembic's fileConfig, which
    # resets the root logger's handlers; attach ours again.
    svc.install_log_handler()
    config = _config(tmp_path / "logs")
    svc.reset()
    svc.configure(config)
    yield config
    svc.reset()
    svc.uninstall_stream_capture()


def _emit_at(message, when, level=logging.INFO):
    record = log.makeRecord(log.name, level, __file__, 0, message, (), None)
    record.created = when.replace(tzinfo=timezone.utc).timestamp()
    log.handle(record)


def _query(config, **filters):
    svc.flush()
    rows, _ = svc.query_logs(config=config, **filters)
    return rows


def _messages(config, **filters):
    return {r["message"] for r in _query(config, **filters)}


def _files(config):
    return sorted(p for p in config.directory.iterdir() if p.suffix == ".jsonl")


def _age_files(config, when):
    ts = when.replace(tzinfo=timezone.utc).timestamp()
    for p in _files(config):
        os.utime(p, (ts, ts))


def _as_another_worker(config):
    # A worker process that starts with nothing of this one's state: an empty
    # buffer, its own new file, no cached "Delete all" time.
    svc.reset()
    svc.configure(config)


def test_nothing_is_captured_before_the_app_configures_it(tmp_path):
    svc.install_log_handler()
    svc.reset()
    marker = f"early-{uuid.uuid4().hex}"
    log.warning(marker)
    config = _config(tmp_path / "logs")
    svc.configure(config)
    try:
        assert svc.flush() == 0
        assert _messages(config, search=marker) == set()
    finally:
        svc.reset()


def test_lines_are_written_as_json_to_this_processes_own_file(cfg):
    tag = uuid.uuid4().hex
    log.warning(f"{tag} hello")

    assert svc.flush() == 1
    (path,) = _files(cfg)
    instance, pid, _ = path.stem.rsplit("__", 2)
    assert instance == svc.INSTANCE_ID and pid == str(os.getpid())
    rec = json.loads(path.read_text().strip())
    assert rec["m"] == f"{tag} hello"
    assert rec["l"] == "WARNING" and rec["g"] == log.name and rec["s"] == "stdout"

    (row,) = _query(cfg, search=tag)
    assert row["instance_id"] == svc.INSTANCE_ID
    assert row["id"] == f"{path.name}:0"


def test_reads_merge_every_file_newest_first(cfg):
    tag = uuid.uuid4().hex
    _emit_at(f"{tag} a", NOW - timedelta(minutes=3))
    _emit_at(f"{tag} c", NOW - timedelta(minutes=1))
    svc.flush()
    _as_another_worker(cfg)
    _emit_at(f"{tag} b", NOW - timedelta(minutes=2))
    _emit_at(f"{tag} d", NOW)
    svc.flush()
    assert len(_files(cfg)) == 2

    rows = _query(cfg, search=tag)
    assert [r["message"] for r in rows] == [f"{tag} d", f"{tag} c", f"{tag} b", f"{tag} a"]

    rows, has_more = svc.query_logs(search=tag, limit=3, config=cfg)
    assert [r["message"] for r in rows] == [f"{tag} d", f"{tag} c", f"{tag} b"]
    assert has_more is True


def test_filters_time_range_errors_and_search(cfg):
    tag = uuid.uuid4().hex
    _emit_at(f"{tag} early info", NOW - timedelta(hours=2))
    _emit_at(f"{tag} mid ERROR", NOW - timedelta(hours=1), level=logging.ERROR)
    _emit_at(f"{tag} late Info", NOW)

    assert _messages(cfg, search=tag, start=NOW - timedelta(hours=1)) == {f"{tag} mid ERROR", f"{tag} late Info"}
    assert _messages(cfg, search=tag, end=NOW - timedelta(hours=1)) == {f"{tag} early info", f"{tag} mid ERROR"}
    assert _messages(cfg, search=tag, errors_only=True) == {f"{tag} mid ERROR"}
    # Case-insensitive, and text JSON escapes on disk is still found.
    assert _messages(cfg, search=f"{tag} LATE info") == {f"{tag} late Info"}
    log.info(f'{tag} quoted "value" \\ path')
    assert _messages(cfg, search='"value" \\') == {f'{tag} quoted "value" \\ path'}


def test_a_file_that_reaches_the_segment_size_is_closed(tmp_path):
    svc.install_log_handler()
    svc.reset()
    config = _config(tmp_path / "logs", max_bytes=16 * 1024 ** 2)
    svc.configure(config)
    try:
        segment = config.segment_bytes
        big = "z" * 30_000
        for i in range(segment // 30_000 + 2):
            log.info(f"{i} {big}")
            svc.flush()
        assert len(_files(config)) == 2
        assert all(p.stat().st_size <= segment + 31_000 for p in _files(config))
    finally:
        svc.reset()


@pytest.mark.parametrize("age_days, kept", [(8, False), (7.01, False), (6.9, True), (1, True)])
def test_cleanup_deletes_files_older_than_the_retention(cfg, age_days, kept):
    tag = uuid.uuid4().hex
    log.info(f"{tag} aged")
    svc.flush()
    _age_files(cfg, NOW - timedelta(days=age_days))
    _as_another_worker(cfg)
    log.info(f"{tag} fresh")
    svc.flush()
    fresh = max(_files(cfg), key=lambda p: p.stat().st_mtime)
    os.utime(fresh, ((NOW - timedelta(minutes=1)).replace(tzinfo=timezone.utc).timestamp(),) * 2)

    svc.cleanup(cfg, now=NOW)

    remaining = _messages(cfg, search=tag)
    assert f"{tag} fresh" in remaining
    assert (f"{tag} aged" in remaining) is kept


def test_cleanup_over_the_size_cap_deletes_the_oldest_files_first(tmp_path):
    svc.install_log_handler()
    svc.reset()
    config = _config(tmp_path / "logs")
    svc.configure(config)
    try:
        tag = uuid.uuid4().hex
        for i in range(6):
            _as_another_worker(config)
            log.info(f"{tag} {i} " + "y" * 10_000)
            svc.flush()
        files = _files(config)
        base = NOW.replace(tzinfo=timezone.utc).timestamp()
        # Oldest first by modification time.
        for i, p in enumerate(sorted(files, key=lambda p: json.loads(p.read_text())["m"])):
            os.utime(p, (base - 600 + i * 60,) * 2)
        total = sum(p.stat().st_size for p in files)

        capped = _config(config.directory, max_bytes=total // 2)
        result = svc.cleanup(capped, now=NOW)

        kept = sorted(int(m.split()[1]) for m in _messages(capped, search=tag))
        assert result["trimmed"] == 6 - len(kept)
        assert 0 < len(kept) < 6
        assert kept == list(range(6 - len(kept), 6))  # the newest survive
        assert svc.size_bytes(capped) <= capped.max_bytes
    finally:
        svc.reset()


def test_a_failed_write_keeps_the_lines_for_the_next_flush(cfg):
    tag = uuid.uuid4().hex
    cfg.directory.rmdir()
    cfg.directory.write_text("not a directory")  # a real write failure
    try:
        for i in range(3):
            log.info(f"{tag} {i}")
        assert svc.flush() == 0
        assert svc.writer_status()["write_error"] is True
    finally:
        cfg.directory.unlink()
        cfg.directory.mkdir()

    assert _messages(cfg, search=tag) == {f"{tag} {i}" for i in range(3)}
    assert svc.writer_status() == {"write_error": False, "dropped_lines": 0}


def test_delete_all_removes_every_file_and_other_workers_drop_older_buffered_lines(cfg):
    tag = uuid.uuid4().hex
    log.info(f"{tag} before")
    svc.flush()
    _as_another_worker(cfg)
    log.info(f"{tag} other worker")
    svc.flush()

    assert svc.delete_all(cfg) == 2
    assert _files(cfg) == []

    # Another worker that buffered lines around the delete.
    _as_another_worker(cfg)
    real_now = datetime.now(timezone.utc).replace(tzinfo=None)
    _emit_at(f"{tag} buffered-before-delete", real_now - timedelta(seconds=3))
    _emit_at(f"{tag} after-delete", real_now + timedelta(seconds=1))

    assert _messages(cfg, search=tag) == {f"{tag} after-delete"}
    # Not hidden but gone: what was deleted is not written back to disk.
    assert not any("buffered-before-delete" in p.read_text() for p in _files(cfg))


def test_requests_for_the_system_logs_page_are_not_recorded(cfg):
    access = logging.getLogger("uvicorn.access")
    saved = (access.disabled, access.level)
    access.disabled = False
    access.setLevel(logging.INFO)
    tag = uuid.uuid4().hex
    try:
        fmt = '%s - "%s %s HTTP/%s" %d'
        access.info(fmt, "127.0.0.1:1", "GET", f"/api/system-logs?search={tag}", "1.1", 200)
        access.info(fmt, "127.0.0.1:1", "GET", "/api/system-logs/status", "1.1", 200)
        access.info(fmt, "127.0.0.1:1", "GET", f"/api/reports?tag={tag}", "1.1", 200)
    finally:
        access.disabled, access.level = saved
    assert _messages(cfg, search=tag) == {f'127.0.0.1:1 - "GET /api/reports?tag={tag} HTTP/1.1" 200'}
    assert _messages(cfg, search="/api/system-logs") == set()


def test_stream_capture_wraps_a_callhandlers_patch_made_after_import(cfg):
    original = logging.Logger.callHandlers
    seen = []

    def other_library(self, record):
        seen.append(record.getMessage())
        return original(self, record)

    logging.Logger.callHandlers = other_library
    try:
        svc.install_stream_capture()
        marker = f"patched-{uuid.uuid4().hex}"
        log.warning(marker)
        assert marker in seen  # still called
        assert _messages(cfg, search=marker) == {marker}  # and still captured once
        svc.uninstall_stream_capture()
        assert logging.Logger.callHandlers is other_library
    finally:
        logging.Logger.callHandlers = original


def test_direct_stdout_and_stderr_writes_are_captured(cfg):
    svc.install_stream_capture()
    tag = uuid.uuid4().hex

    print(f"{tag} printed")
    sys.stderr.write(f"{tag} raw stderr part one, ")
    sys.stderr.write("part two\n")
    sys.stdout.write(f"{tag} unterminated")  # not a line yet

    rows = _query(cfg, search=tag)
    by_message = {r["message"]: r for r in rows}
    assert set(by_message) == {f"{tag} printed", f"{tag} raw stderr part one, part two"}
    assert by_message[f"{tag} printed"]["stream"] == "stdout"
    assert by_message[f"{tag} printed"]["level"] is None
    errors = _query(cfg, search=tag, errors_only=True)
    assert [e["message"] for e in errors] == [f"{tag} raw stderr part one, part two"]


def test_stream_capture_does_not_duplicate_logged_lines(cfg):
    svc.install_stream_capture()
    # A console handler created after the tee is installed writes through it.
    handler = logging.StreamHandler(sys.stderr)
    log.addHandler(handler)
    try:
        marker = f"once-{uuid.uuid4().hex}"
        log.warning(marker)
    finally:
        log.removeHandler(handler)
    assert [r["message"] for r in _query(cfg, search=marker)] == [marker]


def test_stopping_the_writer_thread_writes_what_is_buffered(cfg):
    svc.start(cfg)
    marker = f"thread-{uuid.uuid4().hex}"
    try:
        log.info(marker)
    finally:
        svc.stop()
    # Read the files directly: a read would flush and hide a missing write.
    assert any(marker in p.read_text() for p in _files(cfg))
