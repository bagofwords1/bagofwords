"""E2E: Settings → System logs (the app's own log lines, kept as files).

The contract the UI depends on: log records come back through the API with
their level, stream and instance, lines from every instance writing to the
shared directory are shown, the read filters (time range, instance, errors
only, search, limit) narrow the result, clearing empties it, nothing touches
the database, and only an instance admin can use any of it. The feature is
off unless the deployment turns it on, so every test here turns it on except
the ones about it being off.

Lines are produced through Python's ``logging`` exactly as app code does. The
writer thread does not run under the test client (startup events do not
fire), so the read endpoint's own flush is what writes them.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import app_log_service
from app.settings.config import settings as bow_settings

pytestmark = pytest.mark.e2e

log = logging.getLogger("tests.system_logs")
log.setLevel(logging.INFO)


@pytest.fixture(autouse=True)
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bow_settings.bow_config.system_logs, "enabled", True)
    # Each test gets its own log directory and an empty buffer.
    # Building the test database runs alembic's fileConfig, which resets the
    # root logger's handlers; attach ours again.
    app_log_service.install_log_handler()
    app_log_service.reset()
    config = app_log_service.LogConfig(
        directory=tmp_path / "system-logs",
        retention=timedelta(days=7),
        max_bytes=1024 ** 3,
    )
    app_log_service.configure(config)
    yield config.directory
    app_log_service.reset()


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def org_admin(create_user, login_user, whoami):
    email = f"syslogs_{uuid.uuid4().hex[:10]}@test.com"
    create_user(email=email, password="Test1234!")
    token = login_user(email, "Test1234!")
    org_id = whoami(token)["organizations"][0]["id"]
    return {"token": token, "org_id": org_id, "email": email, "headers": _h(token, org_id)}


@pytest.fixture
def admin(org_admin, login_user, monkeypatch):
    # Signing in applies the configured instance administrator list.
    monkeypatch.setattr(bow_settings.bow_config.auth, "superuser_emails", [org_admin["email"]])
    token = login_user(org_admin["email"], "Test1234!")
    return {**org_admin, "headers": _h(token, org_admin["org_id"])}


@pytest.fixture
def member(test_client, admin, create_user, login_user):
    email = f"syslogs_member_{uuid.uuid4().hex[:10]}@test.com"
    resp = test_client.post(
        f"/api/organizations/{admin['org_id']}/members",
        json={"organization_id": admin["org_id"], "email": email, "role": "member"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.json()
    create_user(email=email, password="Test1234!")
    token = login_user(email, "Test1234!")
    return {"token": token, "headers": _h(token, admin["org_id"])}


def _status(test_client, headers):
    resp = test_client.get("/api/system-logs/status", headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _logs(test_client, headers, **params):
    resp = test_client.get("/api/system-logs", params=params, headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _emit_at(level, message, when):
    """Log a record stamped ``when`` (naive UTC), through the normal logging path."""
    record = log.makeRecord(log.name, level, __file__, 0, message, (), None)
    record.created = when.replace(tzinfo=timezone.utc).timestamp()
    log.handle(record)


def test_lines_are_recorded_without_any_switch_and_status_describes_the_files(test_client, admin, log_dir):
    marker = f"always-{uuid.uuid4().hex}"
    log.warning(marker)

    items = _logs(test_client, admin["headers"], search=marker)["items"]
    assert [i["message"] for i in items] == [marker]

    status = _status(test_client, admin["headers"])
    assert status["directory"] == str(log_dir)
    assert status["retention_days"] == 7
    assert status["max_size_bytes"] == 1024 ** 3
    assert status["size_bytes"] > 0
    assert status["write_error"] is False
    assert status["dropped_lines"] == 0
    assert any(p.suffix == ".jsonl" for p in log_dir.iterdir())


def test_captured_lines_carry_level_stream_and_instance(test_client, admin):
    settings = _status(test_client, admin["headers"])

    info_marker = f"info-{uuid.uuid4().hex}"
    error_marker = f"error-{uuid.uuid4().hex}"
    log.info(info_marker)
    try:
        raise ValueError("boom-for-system-logs")
    except ValueError:
        log.exception(error_marker)

    items = {i["message"].split("\n")[0]: i for i in _logs(test_client, admin["headers"])["items"]}
    assert info_marker in items and error_marker in items

    info, error = items[info_marker], items[error_marker]
    assert info["level"] == "INFO" and info["stream"] == "stdout"
    assert error["level"] == "ERROR" and error["stream"] == "stderr"
    # The traceback is part of the entry, not lost.
    assert "boom-for-system-logs" in error["message"]
    assert info["instance_id"] == settings["instance_id"]
    assert info["logger"] == log.name

    # The instance shows up in the filter list once it has files.
    settings = _status(test_client, admin["headers"])
    assert settings["instance_id"] in settings["instances"]


def test_lines_from_other_instances_on_the_shared_directory_are_shown(test_client, admin, log_dir):
    tag = uuid.uuid4().hex
    log.info(f"{tag} from this instance")
    # Another pod writing to the same shared directory.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    (log_dir / "other-pod-7f9c__12__1700000000000-0.jsonl").write_text(
        json.dumps({"t": now.isoformat(), "l": "ERROR", "g": "app.x", "s": "stderr", "m": f"{tag} from the other pod"}) + "\n"
    )

    items = _logs(test_client, admin["headers"], search=tag)["items"]
    assert {i["instance_id"]: i["message"] for i in items} == {
        app_log_service.INSTANCE_ID: f"{tag} from this instance",
        "other-pod-7f9c": f"{tag} from the other pod",
    }
    assert set(_status(test_client, admin["headers"])["instances"]) == {app_log_service.INSTANCE_ID, "other-pod-7f9c"}
    only_other = _logs(test_client, admin["headers"], search=tag, instance_id="other-pod-7f9c")["items"]
    assert [i["message"] for i in only_other] == [f"{tag} from the other pod"]


def test_filters_errors_only_search_and_instance(test_client, admin):
    settings = _status(test_client, admin["headers"])
    tag = uuid.uuid4().hex
    log.info(f"Alpha {tag} request served")
    log.warning(f"Beta {tag} slow query")
    log.error(f"Gamma {tag} connection refused")

    errors = _logs(test_client, admin["headers"], errors_only=True, search=tag)["items"]
    assert [e["message"] for e in errors] == [f"Gamma {tag} connection refused"]

    # Search is a case-insensitive substring match.
    found = _logs(test_client, admin["headers"], search=f"BETA {tag.upper()}")["items"]
    assert [e["message"] for e in found] == [f"Beta {tag} slow query"]

    # Wildcards in the search are literal text.
    assert _logs(test_client, admin["headers"], search=f"{tag}%served")["items"] == []

    mine = _logs(test_client, admin["headers"], search=tag, instance_id=settings["instance_id"])["items"]
    assert len(mine) == 3
    assert _logs(test_client, admin["headers"], search=tag, instance_id=f"other-{tag}")["items"] == []


def test_time_range_filter_is_inclusive_and_validated(test_client, admin):
    tag = uuid.uuid4().hex
    base = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) - timedelta(days=2)
    stamps = [base + timedelta(hours=h) for h in (0, 5, 9, 13)]
    for i, ts in enumerate(stamps):
        _emit_at(logging.INFO, f"{tag} line {i}", ts)

    # Bounds given with a UTC offset are compared in UTC.
    start = (stamps[1] + timedelta(hours=2)).replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=2)))
    items = _logs(
        test_client, admin["headers"], search=tag,
        start=start.isoformat(), end=stamps[2].isoformat(),
    )["items"]
    assert [i["message"] for i in items] == [f"{tag} line 2"]

    items = _logs(test_client, admin["headers"], search=tag, start=stamps[1].isoformat())["items"]
    # Newest first, start bound inclusive.
    assert [i["message"] for i in items] == [f"{tag} line 3", f"{tag} line 2", f"{tag} line 1"]

    resp = test_client.get(
        "/api/system-logs",
        params={"start": stamps[3].isoformat(), "end": stamps[0].isoformat()},
        headers=admin["headers"],
    )
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "system_logs.invalid_range"


def test_default_fetch_is_newest_1000_lines(test_client, admin):
    tag = uuid.uuid4().hex
    base = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    total = 1007
    for i in range(total):
        _emit_at(logging.INFO, f"{tag} #{i}", base + timedelta(milliseconds=i))

    page = _logs(test_client, admin["headers"], search=tag)
    assert page["limit"] == 1000
    assert len(page["items"]) == 1000
    assert page["has_more"] is True
    assert page["items"][0]["message"] == f"{tag} #{total - 1}"
    stamps = [i["timestamp"] for i in page["items"]]
    assert stamps == sorted(stamps, reverse=True)

    small = _logs(test_client, admin["headers"], search=tag, limit=5)
    assert len(small["items"]) == 5 and small["has_more"] is True


def test_delete_all_removes_every_file(test_client, admin, log_dir):
    tag = uuid.uuid4().hex
    for i in range(3):
        log.info(f"{tag} {i}")
    assert len(_logs(test_client, admin["headers"], search=tag)["items"]) == 3

    resp = test_client.delete("/api/system-logs", headers=admin["headers"])
    assert resp.status_code == 200
    assert resp.json()["deleted_files"] >= 1
    assert not any(p.suffix == ".jsonl" for p in log_dir.iterdir())

    assert _logs(test_client, admin["headers"], search=tag)["items"] == []
    # Recording carries on after a delete.
    later = f"later-{uuid.uuid4().hex}"
    log.info(later)
    assert len(_logs(test_client, admin["headers"], search=later)["items"]) == 1


def test_delete_all_on_an_unwritable_directory_is_a_typed_error(test_client, admin, log_dir):
    log.info(f"kept-{uuid.uuid4().hex}")
    _logs(test_client, admin["headers"])
    for p in log_dir.iterdir():
        if p.is_dir():
            for q in p.iterdir():
                q.unlink()
            p.rmdir()
        else:
            p.unlink()
    log_dir.rmdir()
    log_dir.write_text("not a directory")  # a real write failure
    try:
        resp = test_client.delete("/api/system-logs", headers=admin["headers"])
        assert resp.status_code == 503, resp.text
        body = resp.json()
        assert body["error_code"] == "system_logs.delete_failed"
        assert body["params"]["directory"] == str(log_dir)
    finally:
        log_dir.unlink()
        log_dir.mkdir()


def test_member_cannot_read_or_clear(test_client, admin, member):
    log.info(f"secret-{uuid.uuid4().hex}")

    calls = [
        ("get", "/api/system-logs", {}),
        ("get", "/api/system-logs/status", {}),
        ("delete", "/api/system-logs", {}),
    ]
    for method, url, kw in calls:
        resp = getattr(test_client, method)(url, headers=member["headers"], **kw)
        assert resp.status_code == 403, (method, url, resp.status_code)

    # The member's attempts changed nothing.
    assert _logs(test_client, admin["headers"])["items"]


@pytest.mark.parametrize("multi_org", [False, True])
def test_org_admin_cannot_use_system_logs(test_client, org_admin, monkeypatch, multi_org):
    monkeypatch.setattr(bow_settings.bow_config.features, "allow_multiple_organizations", multi_org)

    for method, url, kw in [
        ("get", "/api/system-logs", {}),
        ("get", "/api/system-logs/status", {}),
        ("delete", "/api/system-logs", {}),
    ]:
        resp = getattr(test_client, method)(url, headers=org_admin["headers"], **kw)
        assert resp.status_code == 403, (method, url)
        assert resp.json()["error_code"] == "system_logs.instance_admin_required"


# ── Turned off (the default) ───────────────────────────────────────────────


_ENDPOINTS = [("get", "/api/system-logs"), ("get", "/api/system-logs/status"), ("delete", "/api/system-logs")]


def test_settings_tell_the_ui_whether_system_logs_are_offered(test_client, monkeypatch):
    for enabled in (True, False):
        monkeypatch.setattr(bow_settings.bow_config.system_logs, "enabled", enabled)
        resp = test_client.get("/api/settings")
        assert resp.status_code == 200
        assert resp.json()["system_logs"] == {"enabled": enabled}


@pytest.mark.parametrize("method, path", _ENDPOINTS)
def test_when_turned_off_an_instance_admin_is_told_so(test_client, admin, monkeypatch, method, path):
    monkeypatch.setattr(bow_settings.bow_config.system_logs, "enabled", False)
    resp = getattr(test_client, method)(path, headers=admin["headers"])
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "system_logs.disabled"


@pytest.mark.parametrize("method, path", _ENDPOINTS)
def test_when_turned_off_anyone_else_is_still_refused(test_client, member, monkeypatch, method, path):
    monkeypatch.setattr(bow_settings.bow_config.system_logs, "enabled", False)
    resp = getattr(test_client, method)(path, headers=member["headers"])
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "system_logs.instance_admin_required"


def test_turning_it_off_leaves_existing_files_alone(test_client, admin, log_dir, monkeypatch):
    tag = uuid.uuid4().hex
    log.info(f"{tag} kept")
    _logs(test_client, admin["headers"])  # flushes to disk
    before = sorted(p.name for p in log_dir.iterdir() if p.suffix == ".jsonl")
    assert before

    monkeypatch.setattr(bow_settings.bow_config.system_logs, "enabled", False)
    assert test_client.delete("/api/system-logs", headers=admin["headers"]).status_code == 404

    assert sorted(p.name for p in log_dir.iterdir() if p.suffix == ".jsonl") == before
