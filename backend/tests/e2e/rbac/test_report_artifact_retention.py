"""Report and artifact retention: org settings that delete, for good, what
nobody has used for N days.

Contract:
  * ``report_retention_days`` and ``artifact_retention_days`` are off by
    default (0, which the settings page shows as an empty field), accept
    empty, 0 and 1..3650, and refuse anything else; empty and 0 keep
    everything. ``file_retention_days`` takes empty the same way;
  * once set, the nightly run deletes every report untouched for that long
    with **everything it owns** — every table of its tree — and leaves no
    reference anywhere pointing at a deleted row; what outlives a report
    (instructions and eval cases learned from it, usage records, data
    sources, other conversations) stays, with the link cleared;
  * it keeps reports that still work or were given to someone: scheduled,
    fed by an active webhook, shared or published, eval test reports, a run
    in progress, and any whose deletion would take a dashboard that must stay;
  * artifact retention deletes dashboards nobody edited or ran, with their
    versions and releases, and keeps published, shared, scheduled and
    embedded ones;
  * a file goes with its report only when nothing else links to it, bytes
    included; bytes a delete could not remove are retried until gone;
  * a preview tells what tonight's run would delete; each deletion is audited
    with counts.

The clock is the boundary: instead of ageing rows, the run is given ``now``
moved past the window (as in test_file_retention.py).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.dependencies import async_session_maker
from tests.fixtures.retention_tree import orphans, seed_tree, surviving

pytestmark = pytest.mark.e2e


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _report(client, admin, title="r"):
    r = client.post("/api/reports", headers=_h(admin["token"], admin["org_id"]),
                    json={"title": title, "files": [], "data_sources": []})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _upload(client, admin, report_id):
    r = client.post("/api/files", headers=_h(admin["token"], admin["org_id"]), data={"report_id": report_id},
                    files={"file": (f"{uuid.uuid4().hex[:6]}.txt", b"hello", "text/plain")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _set(client, admin, **days):
    return client.put("/api/organization/settings", headers=_h(admin["token"], admin["org_id"]),
                      json={"config": {k: {"value": v} for k, v in days.items()}})


def _run(days_later: int) -> dict:
    from app.services.retention.jobs import apply_retention

    return asyncio.run(apply_retention(now=datetime.utcnow() + timedelta(days=days_later)))


def _db(fn):
    async def _go():
        async with async_session_maker() as db:
            return await fn(db)
    return asyncio.run(_go())


def _report_exists(client, admin, report_id) -> bool:
    """The report's row is still in the database (rewound or not)."""
    from app.models.report import Report

    async def _go(db):
        return (await db.execute(select(Report.id).where(Report.id == report_id))).first() is not None
    return _db(_go)


def _api_sees(client, admin, report_id) -> bool:
    r = client.get(f"/api/reports/{report_id}", headers=_h(admin["token"], admin["org_id"]))
    assert r.status_code in (200, 404), r.text
    return r.status_code == 200


def _touch(report_id, days_later):
    """The report was active ``days_later`` days from now (on the run's clock)."""
    from app.models.report import Report

    async def _go(db):
        await db.execute(update(Report).where(Report.id == report_id)
                         .values(last_activity_at=datetime.utcnow() + timedelta(days=days_later)))
        await db.commit()
    _db(_go)


def _set_columns(model, row_id, **values):
    async def _go(db):
        await db.execute(update(model).where(model.id == row_id).values(**values))
        await db.commit()
    _db(_go)


@pytest.fixture
def admin(bootstrap_admin):
    a = bootstrap_admin("retention")
    return {"token": a["token"], "org_id": str(a["org_id"]), "user_id": str(a["user_id"])}


# -- the settings -----------------------------------------------------------

@pytest.mark.parametrize("key", ["report_retention_days", "artifact_retention_days", "file_retention_days"])
@pytest.mark.parametrize("value,ok,stored", [
    (None, True, 0), ("", True, 0), (0, True, 0), (1, True, 1), (3650, True, 3650),
    (-1, False, None), (3651, False, None), ("30", False, None), (True, False, None), (1.5, False, None),
])
def test_retention_settings_take_empty_or_whole_days(test_client, admin, key, value, ok, stored):
    r = _set(test_client, admin, **{key: value})
    assert (r.status_code == 200) is ok, r.text
    if ok:
        assert r.json()["config"][key]["value"] == stored


def test_retention_is_empty_by_default_and_then_deletes_nothing(test_client, admin):
    config = test_client.get("/api/organization/settings", headers=_h(admin["token"], admin["org_id"])).json()["config"]
    for key in ("report_retention_days", "artifact_retention_days", "file_retention_days"):
        assert config[key]["value"] == 0, key  # shown as an empty field
    rid = _report(test_client, admin)
    assert _run(days_later=5000)["reports"] == 0
    assert _api_sees(test_client, admin, rid)


def test_only_admins_with_manage_settings_can_set_or_preview_it(test_client, admin, invite_user_to_org):
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    h = _h(member["token"], admin["org_id"])
    assert test_client.put("/api/organization/settings", headers=h,
                           json={"config": {"report_retention_days": {"value": 1}}}).status_code == 403
    assert test_client.get("/api/organization/retention/preview", headers=h).status_code == 403


# -- what a report purge deletes --------------------------------------------

def test_an_expired_report_goes_with_every_table_of_its_tree(test_client, admin):
    """One row in every purgeable table hangs off the old report, and again
    off a report used yesterday. The run deletes the first tree whole, keeps
    the second whole, and leaves no reference to a deleted row anywhere."""
    from app.services.retention import purge

    old = _report(test_client, admin, "old")
    recent = _report(test_client, admin, "recent")
    old_tree = _db(lambda db: seed_tree(db, org_id=admin["org_id"], user_id=admin["user_id"], report_id=old))
    recent_tree = _db(lambda db: seed_tree(db, org_id=admin["org_id"], user_id=admin["user_id"], report_id=recent))
    _touch(recent, days_later=30)
    assert _db(orphans) == {}

    assert _set(test_client, admin, report_retention_days=30).status_code == 200
    assert _run(days_later=29)["reports"] == 0  # inside the window nothing goes
    totals = _run(days_later=31)

    assert totals["reports"] == 1
    tree = purge.deletable_tables()
    after_old = _db(lambda db: surviving(db, old_tree))
    after_recent = _db(lambda db: surviving(db, recent_tree))
    left_behind = sorted(t for t, alive in after_old.items() if t in tree and alive)
    assert left_behind == [], f"rows of the deleted report's tree still there: {left_behind}"
    assert all(after_recent.values()), sorted(t for t, alive in after_recent.items() if not alive)
    # What outlives a report stays: every seeded row outside the tree — but
    # its file, which only the deleted tree used, went with it.
    outside = sorted(t for t, alive in after_old.items() if t not in tree and t != "files" and not alive)
    assert outside == [], f"rows outside the tree were deleted: {outside}"
    assert after_old["files"] is False and after_recent["files"] is True
    assert _db(orphans) == {}
    assert not _report_exists(test_client, admin, old)
    assert _report_exists(test_client, admin, recent)


def test_what_outlives_a_report_lets_go_of_it(test_client, admin):
    from app.models.eval import TestCase
    from app.models.instruction import Instruction
    from app.models.llm_usage_record import LLMUsageRecord

    rid = _report(test_client, admin)
    tree = _db(lambda db: seed_tree(db, org_id=admin["org_id"], user_id=admin["user_id"], report_id=rid))
    assert _set(test_client, admin, report_retention_days=7).status_code == 200
    _run(days_later=8)

    async def _links(db):
        return {
            "instruction": (await db.execute(select(Instruction.agent_execution_id)
                                             .where(Instruction.id == tree["instructions"]))).scalar_one(),
            "usage": (await db.execute(select(LLMUsageRecord.report_id)
                                       .where(LLMUsageRecord.id == tree["llm_usage_records"]))).scalar_one(),
            "eval_case": (await db.execute(select(TestCase.source_completion_id)
                                           .where(TestCase.id == tree["test_cases"]))).scalar_one(),
        }
    assert _db(_links) == {"instruction": None, "usage": None, "eval_case": None}


def test_rewound_turns_are_deleted_too(test_client, admin):
    from app.models.completion import Completion

    rid = _report(test_client, admin)
    tree = _db(lambda db: seed_tree(db, org_id=admin["org_id"], user_id=admin["user_id"], report_id=rid))
    _set_columns(Completion, tree["completions"], deleted_at=datetime.utcnow())  # a rewind hides it
    assert _set(test_client, admin, report_retention_days=7).status_code == 200
    _run(days_later=8)

    async def _count(db):
        return (await db.execute(select(Completion.id).where(Completion.id == tree["completions"])
                                 .execution_options(include_rewound=True))).all()
    assert _db(_count) == []
    assert _db(orphans) == {}


# -- what keeps a report ----------------------------------------------------

def _keep_cases():
    from app.models.agent_execution import AgentExecution
    from app.models.report import Report
    from app.models.webhook import Webhook

    return {
        "cron schedule": lambda t: _set_columns(Report, t["reports"], cron_schedule="0 9 * * *"),
        "active webhook": lambda t: _set_columns(Webhook, t["webhooks"], is_active=True),
        "published": lambda t: _set_columns(Report, t["reports"], status="published"),
        "dashboard shared": lambda t: _set_columns(Report, t["reports"], artifact_visibility="public"),
        "conversation shared": lambda t: _set_columns(Report, t["reports"], conversation_visibility="public"),
        "share link": lambda t: _set_columns(Report, t["reports"], conversation_share_enabled=True),
        "eval test report": lambda t: _set_columns(Report, t["reports"], report_type="test"),
        "run in progress": lambda t: _set_columns(AgentExecution, t["agent_executions"], status="in_progress"),
    }


@pytest.mark.parametrize("case", list(_keep_cases()))
def test_reports_that_still_work_or_were_given_to_someone_are_kept(test_client, admin, case):
    rid = _report(test_client, admin)
    tree = _db(lambda db: seed_tree(db, org_id=admin["org_id"], user_id=admin["user_id"], report_id=rid))
    _keep_cases()[case](tree)
    assert _set(test_client, admin, report_retention_days=7).status_code == 200
    assert _run(days_later=400)["reports"] == 0
    assert all(_db(lambda db: surviving(db, tree)).values())


def test_an_active_scheduled_prompt_keeps_its_report(test_client, admin, create_scheduled_prompt):
    rid = _report(test_client, admin)
    create_scheduled_prompt(rid, user_token=admin["token"], org_id=admin["org_id"])
    assert _set(test_client, admin, report_retention_days=7).status_code == 200
    assert _run(days_later=400)["reports"] == 0
    assert _report_exists(test_client, admin, rid)


def test_a_report_shared_with_a_user_is_kept(test_client, admin):
    from app.models.report_share import ReportShare

    rid = _report(test_client, admin)
    tree = _db(lambda db: seed_tree(db, org_id=admin["org_id"], user_id=admin["user_id"], report_id=rid))
    _set_columns(ReportShare, tree["report_shares"], deleted_at=None)
    assert _set(test_client, admin, report_retention_days=7).status_code == 200
    assert _run(days_later=400)["reports"] == 0


@pytest.mark.parametrize("how", ["published", "shared", "edited inside the window"])
def test_a_report_whose_dashboard_must_stay_is_kept_with_it(test_client, admin, how):
    from app.models.artifact import Artifact

    rid = _report(test_client, admin)
    tree = _db(lambda db: seed_tree(db, org_id=admin["org_id"], user_id=admin["user_id"], report_id=rid))
    values = {"published": {"published_release_id": tree["artifact_releases"]},
              "shared": {"visibility": "shared"},
              "edited inside the window": {"content_updated_at": datetime.utcnow() + timedelta(days=399)}}[how]
    _set_columns(Artifact, tree["artifacts"], **values)
    assert _set(test_client, admin, report_retention_days=7).status_code == 200
    assert _run(days_later=400)["reports"] == 0
    assert _report_exists(test_client, admin, rid)
    assert _db(lambda db: surviving(db, {"artifacts": tree["artifacts"]})) == {"artifacts": True}


def test_one_kept_report_does_not_stop_the_others(test_client, admin):
    from app.models.artifact import Artifact

    kept, gone = _report(test_client, admin), _report(test_client, admin)
    kept_tree = _db(lambda db: seed_tree(db, org_id=admin["org_id"], user_id=admin["user_id"], report_id=kept))
    _db(lambda db: seed_tree(db, org_id=admin["org_id"], user_id=admin["user_id"], report_id=gone))
    _set_columns(Artifact, kept_tree["artifacts"], visibility="shared")
    assert _set(test_client, admin, report_retention_days=7).status_code == 200
    assert _run(days_later=400)["reports"] == 1
    assert _report_exists(test_client, admin, kept)
    assert not _report_exists(test_client, admin, gone)
    assert _db(orphans) == {}


# -- files ------------------------------------------------------------------

def test_a_reports_files_go_with_it_unless_something_else_uses_them(test_client, admin):
    from app.models.file import File, report_file_association

    old, recent = _report(test_client, admin), _report(test_client, admin)
    own = _upload(test_client, admin, old)
    shared = _upload(test_client, admin, old)

    async def _link_and_path(db):
        await db.execute(report_file_association.insert().values(report_id=recent, file_id=shared))
        await db.commit()
        return (await db.execute(select(File.path).where(File.id == own))).scalar_one()
    own_path = _db(_link_and_path)
    assert os.path.isfile(own_path)
    _touch(recent, days_later=30)

    assert _set(test_client, admin, report_retention_days=7).status_code == 200
    totals = _run(days_later=31)
    assert totals["reports"] == 1 and totals["files"] == 1
    listed = {f["id"] for f in test_client.get("/api/files", headers=_h(admin["token"], admin["org_id"])).json()}
    assert own not in listed and shared in listed
    assert not os.path.exists(own_path), "the file's bytes are still on disk"


def test_bytes_a_delete_could_not_remove_are_retried_until_gone(test_client, admin, monkeypatch):
    from app.models.file import File
    from app.models.pending_blob_delete import PendingBlobDelete
    from app.services import storage

    rid = _report(test_client, admin)
    fid = _upload(test_client, admin, rid)
    path = _db(lambda db: _scalar(db, select(File.path).where(File.id == fid)))

    async def _refuse(_path):
        return False
    monkeypatch.setattr(storage, "adiscard", _refuse)
    assert _set(test_client, admin, report_retention_days=7).status_code == 200
    assert _run(days_later=8)["files"] == 1
    pending = _db(lambda db: _scalars(db, select(PendingBlobDelete.path)))
    assert pending == [path]

    monkeypatch.undo()
    blobs = _run(days_later=9)["blobs"]
    assert blobs == {"deleted": 1, "still_pending": 0}
    assert _db(lambda db: _scalars(db, select(PendingBlobDelete.path))) == []


async def _scalar(db, stmt):
    return (await db.execute(stmt)).scalar_one()


async def _scalars(db, stmt):
    return [r for (r,) in (await db.execute(stmt)).all()]


# -- artifacts --------------------------------------------------------------

def _standalone_dashboard(test_client, admin):
    """A dashboard with its whole tree (report retention stays off)."""
    rid = _report(test_client, admin)
    tree = _db(lambda db: seed_tree(db, org_id=admin["org_id"], user_id=admin["user_id"], report_id=rid))
    return rid, tree


def test_artifact_retention_deletes_an_untouched_dashboard_and_all_it_owns(test_client, admin):
    from app.models.artifact import Artifact
    from app.services.retention import purge

    rid, tree = _standalone_dashboard(test_client, admin)
    _set_columns(Artifact, tree["artifacts"], report_id=None)  # it left its conversation
    assert _set(test_client, admin, artifact_retention_days=30).status_code == 200
    totals = _run(days_later=400)
    assert totals["artifacts"] == 1 and totals["reports"] == 0

    async def _artifact_tree(db):
        return await purge.closure(db, {"artifacts": [tree["artifacts"]]})
    # Every row the artifact owned is gone (its tree is empty but for itself).
    assert {t: ids for t, ids in _db(_artifact_tree).ids.items() if ids} == {"artifacts": {tree["artifacts"]}}
    assert _db(lambda db: surviving(db, {"artifacts": tree["artifacts"], "artifact_versions": tree["artifact_versions"],
                                         "artifact_releases": tree["artifact_releases"]})) == {
        "artifacts": False, "artifact_versions": False, "artifact_releases": False}
    assert _report_exists(test_client, admin, rid)  # the conversation is not artifact retention's to delete
    assert _db(orphans) == {}


@pytest.mark.parametrize("how", ["published", "shared", "scheduled", "embedded", "conversation active"])
def test_dashboards_people_rely_on_are_kept(test_client, admin, how):
    from app.models.artifact import Artifact
    from app.models.host_application import ArtifactHostApplication

    rid, tree = _standalone_dashboard(test_client, admin)
    aid = tree["artifacts"]
    if how == "conversation active":
        _touch(rid, days_later=399)
    else:
        _set_columns(Artifact, aid, report_id=None)
    if how == "published":
        _set_columns(Artifact, aid, published_release_id=tree["artifact_releases"])
    elif how == "shared":
        _set_columns(Artifact, aid, visibility="public")
    elif how == "scheduled":
        _set_columns(Artifact, aid, cron_schedule="0 9 * * *")
    elif how == "embedded":
        _set_columns(ArtifactHostApplication, tree["artifact_host_applications"], deleted_at=None)
    assert _set(test_client, admin, artifact_retention_days=30).status_code == 200
    assert _run(days_later=400)["artifacts"] == 0
    assert _db(lambda db: surviving(db, {"artifacts": aid})) == {"artifacts": True}


# -- preview and audit ------------------------------------------------------

def test_preview_counts_what_tonight_would_delete_and_the_run_is_audited(test_client, admin):
    from app.ee.audit.models import AuditLog
    from app.models.report import Report

    old = [_report(test_client, admin) for _ in range(2)]
    _report(test_client, admin)  # new: not counted
    two_days_ago = datetime.utcnow() - timedelta(days=2)
    for rid in old:
        _set_columns(Report, rid, created_at=two_days_ago, last_activity_at=two_days_ago)
    h = _h(admin["token"], admin["org_id"])
    off = test_client.get("/api/organization/retention/preview", headers=h)
    assert off.status_code == 200, off.text
    assert off.json()["reports"] == 0 and off.json()["report_retention_days"] is None

    assert _set(test_client, admin, report_retention_days=1).status_code == 200
    preview = test_client.get("/api/organization/retention/preview", headers=h).json()
    assert preview["reports"] == 2 and preview["report_retention_days"] == 1

    assert _run(days_later=0)["reports"] == 2
    assert test_client.get("/api/organization/retention/preview", headers=h).json()["reports"] == 0

    async def _audits(db):
        rows = (await db.execute(select(AuditLog).where(AuditLog.organization_id == admin["org_id"],
                                                        AuditLog.action == "retention.reports_deleted"))).scalars().all()
        return [r.details for r in rows]
    details = _db(_audits)
    assert len(details) == 1
    assert details[0]["reports"] == 2 and details[0]["retention_days"] == 1
