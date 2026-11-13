"""File retention: an org setting that deletes old files every night.

Contract:
  * ``file_retention_days`` is off (0) by default, accepts 0..3650 and refuses
    anything else;
  * once set, the nightly job deletes every file older than that many days —
    gone from the Files page, its report and its content route;
  * whatever their age, it keeps files an agent or project library holds and
    uploads attached to a report that runs on a schedule; the tool output a
    scheduled report piles up still expires;
  * files younger than the window, and every file of an org with retention
    off, are kept.

The clock is the boundary: instead of ageing rows, the job is run with ``now``
moved past the window.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker

pytestmark = pytest.mark.e2e


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _report(client, token, org_id):
    r = client.post("/api/reports", headers=_h(token, org_id), json={"title": "r", "files": [], "data_sources": []})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _upload(client, token, org_id, *, report_id=None, data_source_id=None, name=None):
    url = f"/api/data_sources/{data_source_id}/files" if data_source_id else "/api/files"
    data = {"report_id": report_id} if report_id else {}
    r = client.post(url, headers=_h(token, org_id), data=data,
                    files={"file": (name or f"{uuid.uuid4().hex[:6]}.txt", b"hello", "text/plain")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _set_retention(client, token, org_id, days):
    return client.put("/api/organization/settings", headers=_h(token, org_id),
                      json={"config": {"file_retention_days": {"value": days}}})


def _run_job(days_later: int) -> int:
    from app.services.file_retention_service import apply_file_retention

    return asyncio.run(apply_file_retention(now=datetime.utcnow() + timedelta(days=days_later)))


def _exists(client, token, org_id, file_id) -> bool:
    r = client.get(f"/api/files/{file_id}/content", headers=_h(token, org_id))
    assert r.status_code in (200, 404), r.text
    return r.status_code == 200


def _seed_tool_output(report_id, user_id, org_id) -> str:
    """A file the agent saved while running the report. Producing one through
    the API needs a full agent run with a stubbed LLM and a tool server
    (covered in test_file_delete_and_generated_files.py); retention only cares
    that it is agent output attached to the report, so it is seeded directly."""
    from app.models.file import File, report_file_association

    async def _seed():
        async with async_session_maker() as db:
            f = File(filename="cost_report.json", path="", content_type="application/json",
                     source_kind="artifact", user_id=str(user_id), organization_id=str(org_id))
            db.add(f)
            await db.flush()
            await db.execute(report_file_association.insert().values(report_id=report_id, file_id=f.id))
            await db.commit()
            return str(f.id)
    return asyncio.run(_seed())


@pytest.fixture
def admin(bootstrap_admin):
    a = bootstrap_admin("retention")
    return {"token": a["token"], "org_id": str(a["org_id"]), "user_id": str(a["user_id"])}


@pytest.mark.parametrize("days,ok", [(0, True), (1, True), (90, True), (3650, True),
                                     (-1, False), (3651, False), ("30", False), (True, False)])
def test_the_setting_accepts_zero_to_ten_years(test_client, admin, days, ok):
    r = _set_retention(test_client, admin["token"], admin["org_id"], days)
    assert (r.status_code == 200) is ok, r.text
    if ok:
        assert r.json()["config"]["file_retention_days"]["value"] == days


def test_retention_is_off_by_default_and_then_keeps_everything(test_client, admin):
    settings = test_client.get("/api/organization/settings", headers=_h(admin["token"], admin["org_id"])).json()
    assert settings["config"]["file_retention_days"]["value"] == 0

    f = _upload(test_client, admin["token"], admin["org_id"], report_id=_report(test_client, admin["token"], admin["org_id"]))
    assert _run_job(days_later=5000) == 0
    assert _exists(test_client, admin["token"], admin["org_id"], f)


@pytest.mark.parametrize("days", [7, 30])
def test_old_files_are_deleted_and_protected_ones_kept(
        test_client, admin, days, sqlite_data_source, create_project, create_scheduled_prompt):
    token, org_id, user_id = admin["token"], admin["org_id"], admin["user_id"]

    plain_report = _report(test_client, token, org_id)
    plain = _upload(test_client, token, org_id, report_id=plain_report)
    plain_output = _seed_tool_output(plain_report, user_id, org_id)

    scheduled_report = _report(test_client, token, org_id)
    create_scheduled_prompt(scheduled_report, user_token=token, org_id=org_id)
    scheduled_input = _upload(test_client, token, org_id, report_id=scheduled_report)
    scheduled_output = _seed_tool_output(scheduled_report, user_id, org_id)

    ds = sqlite_data_source(name=f"agent-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)
    library = _upload(test_client, token, org_id, data_source_id=ds["id"])

    project_file = _upload(test_client, token, org_id)
    project = create_project(user_token=token, org_id=org_id)
    r = test_client.put(f"/api/projects/{project['id']}/files", json={"file_ids": [project_file]},
                        headers=_h(token, org_id))
    assert r.status_code == 200, r.text

    assert _set_retention(test_client, token, org_id, days).status_code == 200

    # Inside the window nothing goes.
    assert _run_job(days_later=days - 1) == 0

    deleted = _run_job(days_later=days + 1)
    gone = {plain, plain_output, scheduled_output}
    kept = {scheduled_input, library, project_file}
    assert deleted == len(gone)
    listed = {f["id"] for f in test_client.get("/api/files", headers=_h(token, org_id)).json()}
    assert not (gone & listed)
    assert kept <= listed
    report_files = {f["id"] for f in test_client.get(f"/api/reports/{plain_report}/files", headers=_h(token, org_id)).json()}
    assert not (gone & report_files)
    assert not _exists(test_client, token, org_id, plain)
    for fid in kept:
        assert _exists(test_client, token, org_id, fid)


def test_each_org_follows_its_own_setting(test_client, admin, bootstrap_admin):
    other = bootstrap_admin("noretention")
    mine = _upload(test_client, admin["token"], admin["org_id"],
                   report_id=_report(test_client, admin["token"], admin["org_id"]))
    theirs = _upload(test_client, other["token"], other["org_id"],
                     report_id=_report(test_client, other["token"], other["org_id"]))
    assert _set_retention(test_client, admin["token"], admin["org_id"], 10).status_code == 200

    assert _run_job(days_later=11) == 1
    assert not _exists(test_client, admin["token"], admin["org_id"], mine)
    assert _exists(test_client, other["token"], other["org_id"], theirs)
