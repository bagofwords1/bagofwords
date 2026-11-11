"""E2E: Settings → System logs (the app's own log lines, kept in app_logs).

The contract the UI depends on: capture is off until an admin switches it on,
log records written while it is on come back through the API with their level,
stream and instance, the read filters (time range, instance, errors only,
search, limit) narrow the result, clearing empties it, and only an instance
admin can use any of it.

Lines are produced through Python's ``logging`` exactly as app code does. The
background writer does not run under the test client (startup events do not
fire), so the read endpoint's own flush is what puts them in the table.
"""
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
def _fresh_capture_state():
    # The switch and buffer are per process; each test gets a new database,
    # so start (and leave) the process with nothing captured.
    # Building the test database runs alembic's fileConfig, which disables
    # loggers that already exist and resets root's handlers; production runs
    # migrations in a separate process, so the app never sees that.
    log.disabled = False
    app_log_service.install_log_handler()
    app_log_service.reset()
    yield
    app_log_service.reset()


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def admin(create_user, login_user, whoami):
    email = f"syslogs_{uuid.uuid4().hex[:10]}@test.com"
    create_user(email=email, password="Test1234!")
    token = login_user(email, "Test1234!")
    org_id = whoami(token)["organizations"][0]["id"]
    return {"token": token, "org_id": org_id, "email": email, "headers": _h(token, org_id)}


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


def _enable(test_client, headers, **body):
    resp = test_client.put("/api/system-logs/settings", json={"enabled": True, **body}, headers=headers)
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


def test_capture_is_off_by_default_and_records_nothing(test_client, admin):
    settings = test_client.get("/api/system-logs/settings", headers=admin["headers"]).json()
    assert settings["enabled"] is False
    assert settings["expires_at"] is None

    marker = f"off-{uuid.uuid4().hex}"
    log.warning(marker)

    assert _logs(test_client, admin["headers"], search=marker)["items"] == []


def test_captured_lines_carry_level_stream_and_instance(test_client, admin):
    settings = _enable(test_client, admin["headers"])
    assert settings["enabled"] is True

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

    # The instance shows up in the filter list once it has rows.
    settings = test_client.get("/api/system-logs/settings", headers=admin["headers"]).json()
    assert settings["instance_id"] in settings["instances"]


def test_filters_errors_only_search_and_instance(test_client, admin):
    settings = _enable(test_client, admin["headers"])
    tag = uuid.uuid4().hex
    log.info(f"Alpha {tag} request served")
    log.warning(f"Beta {tag} slow query")
    log.error(f"Gamma {tag} connection refused")

    errors = _logs(test_client, admin["headers"], errors_only=True, search=tag)["items"]
    assert [e["message"] for e in errors] == [f"Gamma {tag} connection refused"]

    # Search is a case-insensitive substring match.
    found = _logs(test_client, admin["headers"], search=f"BETA {tag.upper()}")["items"]
    assert [e["message"] for e in found] == [f"Beta {tag} slow query"]

    # LIKE wildcards in the search are literal text.
    assert _logs(test_client, admin["headers"], search=f"{tag}%served")["items"] == []

    mine = _logs(test_client, admin["headers"], search=tag, instance_id=settings["instance_id"])["items"]
    assert len(mine) == 3
    assert _logs(test_client, admin["headers"], search=tag, instance_id=f"other-{tag}")["items"] == []


def test_time_range_filter_is_inclusive_and_validated(test_client, admin):
    _enable(test_client, admin["headers"])
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
    _enable(test_client, admin["headers"])
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


def test_turning_capture_off_stops_recording(test_client, admin):
    _enable(test_client, admin["headers"])
    before = f"before-{uuid.uuid4().hex}"
    log.info(before)

    resp = test_client.put("/api/system-logs/settings", json={"enabled": False}, headers=admin["headers"])
    assert resp.status_code == 200 and resp.json()["enabled"] is False

    after = f"after-{uuid.uuid4().hex}"
    log.info(after)
    # Lines captured while it was on are kept.
    assert len(_logs(test_client, admin["headers"], search=before)["items"]) == 1
    assert _logs(test_client, admin["headers"], search=after)["items"] == []


def test_timed_capture_reports_when_it_switches_off(test_client, admin):
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    settings = _enable(test_client, admin["headers"], duration_minutes=90)
    assert settings["enabled"] is True
    expires = datetime.fromisoformat(settings["expires_at"]).replace(tzinfo=None)
    assert before + timedelta(minutes=89) < expires < before + timedelta(minutes=91)

    for bad in (0, -5, 7 * 24 * 60 + 1):
        resp = test_client.put(
            "/api/system-logs/settings",
            json={"enabled": True, "duration_minutes": bad},
            headers=admin["headers"],
        )
        assert resp.status_code == 422, bad


def test_switch_is_stored_for_every_worker(test_client, admin):
    """Another worker process (simulated by forgetting this one's state)
    learns the switch from the database, not from the worker that set it."""
    _enable(test_client, admin["headers"])
    app_log_service.reset()

    settings = test_client.get("/api/system-logs/settings", headers=admin["headers"]).json()
    assert settings["enabled"] is True
    marker = f"worker-{uuid.uuid4().hex}"
    log.info(marker)
    assert len(_logs(test_client, admin["headers"], search=marker)["items"]) == 1


def test_delete_all_empties_the_table(test_client, admin):
    _enable(test_client, admin["headers"])
    tag = uuid.uuid4().hex
    for i in range(3):
        log.info(f"{tag} {i}")
    assert len(_logs(test_client, admin["headers"], search=tag)["items"]) == 3

    resp = test_client.delete("/api/system-logs", headers=admin["headers"])
    assert resp.status_code == 200
    assert resp.json()["deleted"] >= 3

    assert _logs(test_client, admin["headers"], search=tag)["items"] == []
    # Clearing does not switch capture off.
    later = f"later-{uuid.uuid4().hex}"
    log.info(later)
    assert len(_logs(test_client, admin["headers"], search=later)["items"]) == 1


def test_member_cannot_read_clear_or_switch(test_client, admin, member):
    _enable(test_client, admin["headers"])
    log.info(f"secret-{uuid.uuid4().hex}")

    calls = [
        ("get", "/api/system-logs", {}),
        ("get", "/api/system-logs/settings", {}),
        ("put", "/api/system-logs/settings", {"json": {"enabled": False}}),
        ("delete", "/api/system-logs", {}),
    ]
    for method, url, kw in calls:
        resp = getattr(test_client, method)(url, headers=member["headers"], **kw)
        assert resp.status_code == 403, (method, url, resp.status_code)

    # The member's attempts changed nothing.
    settings = test_client.get("/api/system-logs/settings", headers=admin["headers"]).json()
    assert settings["enabled"] is True
    assert _logs(test_client, admin["headers"])["items"]


def test_multi_org_instance_requires_instance_superuser(test_client, admin, monkeypatch):
    monkeypatch.setattr(bow_settings.bow_config.features, "allow_multiple_organizations", True)

    for method, url, kw in [
        ("get", "/api/system-logs", {}),
        ("get", "/api/system-logs/settings", {}),
        ("put", "/api/system-logs/settings", {"json": {"enabled": True}}),
        ("delete", "/api/system-logs", {}),
    ]:
        resp = getattr(test_client, method)(url, headers=admin["headers"], **kw)
        assert resp.status_code == 403, (method, url)
        assert resp.json()["error_code"] == "system_logs.instance_admin_required"

    # No API grants instance superuser (it is set when the instance is
    # provisioned), so this one flag is set directly.
    import asyncio
    from sqlalchemy import update
    from app.dependencies import async_session_maker
    from app.models.user import User

    async def _make_superuser():
        async with async_session_maker() as db:
            await db.execute(update(User).where(User.email == admin["email"]).values(is_superuser=True))
            await db.commit()

    asyncio.run(_make_superuser())
    resp = test_client.get("/api/system-logs/settings", headers=admin["headers"])
    assert resp.status_code == 200, resp.json()
