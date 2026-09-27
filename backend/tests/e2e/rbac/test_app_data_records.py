"""Artifact app data: record semantics behind the four data endpoints.

Invariants under test (design spec sections 9, 11, 12; plan E1-E3):
  * Declaration changes apply at READ time: missing fields come back with
    their declared default, removed fields are hidden but kept, retyped
    values come back as stored. Reads never rewrite a row (PP7).
  * Records are validated against the effective declaration over HTTP:
    unknown keys, wrong types, missing required fields, size limits, and the
    per-collection record limit.
  * Optimistic concurrency: a stale version is 409 and changes nothing;
    every successful write bumps the version by exactly one (PP6). Deletes
    are soft; a deleted record is 404 for further writes.
  * The effective declaration is the latest COMPLETED version (PP8); an
    artifact without storage has no collections (PP9).
  * Records survive new versions and an owner rerun (PP13).
  * Writes are audited without record content.
  * `user` and `mine` are computed server-side for every reader (PP5);
    unverified users cannot write when email verification is on (RD11).
"""
import asyncio
import json
import logging
import uuid

import pytest
from sqlalchemy import func, select, text

from app.dependencies import async_session_maker
from app.models.app_record import AppRecord
from app.models.artifact import ArtifactVersion
from app.services.artifact_service import new_version

pytestmark = pytest.mark.e2e


def _org_headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _content(storage, code='function App() { useCollection("tasks"); return null; }'):
    content = {"code": code, "visualization_ids": [], "runtime_version": 11}
    if storage is not None:
        content["storage"] = storage
    return content


def _owner_and_report(bootstrap_admin, create_report):
    owner = bootstrap_admin("appowner")
    report = create_report(title=f"App {uuid.uuid4().hex[:6]}", user_token=owner["token"],
                           org_id=owner["org_id"], data_sources=[])
    return owner, report


def _create_artifact(test_client, owner, report_id, storage):
    resp = test_client.post(
        "/api/artifacts",
        json={"report_id": report_id, "title": "Tracker", "mode": "page", "content": _content(storage)},
        headers=_org_headers(owner["token"], owner["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["id"], body["artifact_id"]  # (version id, parent artifact id)


def _redeclare(test_client, owner, version_id, storage):
    """Owner edits the version in place (PATCH /api/artifacts/{version_id})."""
    resp = test_client.patch(
        f"/api/artifacts/{version_id}",
        json={"content": _content(storage)},
        headers=_org_headers(owner["token"], owner["org_id"]),
    )
    assert resp.status_code == 200, resp.text


def _set_internal(test_client, owner, report_id):
    resp = test_client.put(
        f"/api/reports/{report_id}/visibility/artifact",
        json={"visibility": "internal"},
        headers=_org_headers(owner["token"], owner["org_id"]),
    )
    assert resp.status_code == 200, resp.text


class _Api:
    def __init__(self, test_client, artifact_id, token):
        self.c, self.token = test_client, token
        self.base = f"/api/artifacts/{artifact_id}/data"

    def list(self, coll, token=None):
        return self.c.get(f"{self.base}/{coll}", headers=_auth(token or self.token))

    def create(self, coll, data, token=None):
        return self.c.post(f"{self.base}/{coll}", json={"data": data}, headers=_auth(token or self.token))

    def update(self, coll, rid, data, version, token=None):
        return self.c.patch(f"{self.base}/{coll}/{rid}", json={"data": data, "version": version},
                            headers=_auth(token or self.token))

    def delete(self, coll, rid, version, token=None):
        return self.c.request("DELETE", f"{self.base}/{coll}/{rid}", json={"version": version},
                              headers=_auth(token or self.token))


def _error(resp, status, code):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error_code"] == code, body
    return body


# Direct DB reads below only ASSERT stored row state (raw `data`, `version`,
# `updated_at`, `deleted_at`): the API deliberately never exposes raw rows.
def _row(record_id):
    async def _q():
        async with async_session_maker() as db:
            row = await db.get(AppRecord, record_id)
            if row is None:
                return None
            return {"data": dict(row.data), "version": row.version,
                    "updated_at": row.updated_at, "deleted_at": row.deleted_at}
    return asyncio.run(_q())


def _count_rows(artifact_id, live_only=True):
    async def _q():
        async with async_session_maker() as db:
            stmt = select(func.count(AppRecord.id)).where(AppRecord.artifact_id == artifact_id)
            if live_only:
                stmt = stmt.where(AppRecord.deleted_at.is_(None))
            return (await db.execute(stmt)).scalar_one()
    return asyncio.run(_q())


TASKS_V1 = {"collections": {"tasks": {"scope": "shared", "create": "members", "modify": "author", "fields": {
    "title": {"type": "string", "required": True},
    "note": {"type": "string"},
    "score": {"type": "number"},
}}}}


def test_declaration_changes_apply_at_read_time_without_rewriting_rows(test_client, bootstrap_admin, create_report):
    owner, report = _owner_and_report(bootstrap_admin, create_report)
    version_id, artifact_id = _create_artifact(test_client, owner, report["id"], TASKS_V1)
    api = _Api(test_client, artifact_id, owner["token"])

    created = api.create("tasks", {"title": "a", "note": "n", "score": 5})
    assert created.status_code == 201, created.text
    rid = created.json()["id"]
    assert created.json()["data"] == {"title": "a", "note": "n", "score": 5}
    stored_before = _row(rid)

    # v2: `note` removed, `score` retyped, new fields with defaults (incl. an
    # explicit null default and a required field satisfied by its default),
    # plus a new optional field without default.
    _redeclare(test_client, owner, version_id, {"collections": {"tasks": {
        "scope": "shared", "create": "members", "modify": "author", "fields": {
            "title": {"type": "string", "required": True},
            "score": {"type": "string"},
            "done": {"type": "boolean", "default": False},
            "tag": {"type": "string", "default": None},
            "status": {"type": "string", "required": True, "default": "open"},
            "priority": {"type": "number"},
        }}}})

    listed = api.list("tasks")
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["items"]
    assert item["data"] == {"title": "a", "score": 5, "done": False, "tag": None, "status": "open"}
    assert item["version"] == 1
    # PP7: reading changed nothing in the stored row.
    assert _row(rid) == stored_before

    # A PATCH of another field works on the old row: the new required field is
    # satisfied by its default and the retyped value is not re-checked.
    patched = api.update("tasks", rid, {"done": True}, version=1)
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"] == {"title": "a", "score": 5, "done": True, "tag": None, "status": "open"}
    assert patched.json()["version"] == 2
    # The hidden `note` survives the merge; defaults are never written.
    assert _row(rid)["data"] == {"title": "a", "note": "n", "score": 5, "done": True}

    # Only patched keys are type-checked against the new declaration.
    body = _error(api.update("tasks", rid, {"score": 7}, version=2), 422, "app_data.validation")
    assert body["params"]["field"] == "score"

    # Reverting the declaration makes the preserved value visible again.
    _redeclare(test_client, owner, version_id, TASKS_V1)
    [item] = api.list("tasks").json()["items"]
    assert item["data"] == {"title": "a", "note": "n", "score": 5}


FORM = {"collections": {"form": {"scope": "per_user", "fields": {
    "r": {"type": "string", "required": True},
    "s": {"type": "string", "max_length": 5},
    "n": {"type": "number"},
    "b": {"type": "boolean"},
    "d": {"type": "date"},
    "j": {"type": "json"},
}}}}


def test_record_validation_and_size_limits(test_client, bootstrap_admin, create_report):
    owner, report = _owner_and_report(bootstrap_admin, create_report)
    _, artifact_id = _create_artifact(test_client, owner, report["id"], FORM)
    api = _Api(test_client, artifact_id, owner["token"])

    invalid = [
        ({"r": "x", "zzz": 1}, "zzz"),            # unknown field
        ({"r": "x", "user_id": "u"}, "user_id"),  # envelope names are just undeclared fields
        ({"r": "x", "version": 3}, "version"),
        ({"r": "x", "n": True}, "n"),             # bool is not a number
        ({"r": "x", "n": "5"}, "n"),
        ({"r": "x", "b": "yes"}, "b"),
        ({"r": "x", "d": "27/09/2026"}, "d"),
        ({"r": "x", "s": "toolong"}, "s"),        # max_length in characters
        ({"s": "ok"}, "r"),                       # required missing
        ({"r": None}, "r"),                       # required null
    ]
    for data, field in invalid:
        body = _error(api.create("form", data), 422, "app_data.validation")
        assert body["params"]["field"] == field, (data, body)

    # Non-finite numbers are rejected (the JSON body carries a bare NaN token).
    resp = test_client.post(f"/api/artifacts/{artifact_id}/data/form",
                            content=b'{"data": {"r": "x", "n": NaN}}',
                            headers={**_auth(owner["token"]), "Content-Type": "application/json"})
    assert resp.status_code == 422, resp.text
    assert _count_rows(artifact_id) == 0

    ok = api.create("form", {"r": "x", "s": "héllo", "n": 1.5, "b": False, "d": "2026-09-27",
                             "j": {"any": [1, "two", None]}})
    assert ok.status_code == 201, ok.text
    ok_dt = api.create("form", {"r": "x", "d": "2026-09-27T10:30:00"})
    assert ok_dt.status_code == 201, ok_dt.text

    # Non-json part: compact UTF-8 JSON of {"r":"xxx..."} is len + 8 bytes.
    at_limit = api.create("form", {"r": "x" * (65_536 - 8)})
    assert at_limit.status_code == 201, at_limit.status_code
    _error(api.create("form", {"r": "x" * (65_536 - 7)}), 413, "app_data.too_large")
    # One json field above 256 KB.
    body = _error(api.create("form", {"r": "x", "j": "y" * 262_144}), 413, "app_data.too_large")
    assert body["params"].get("field") == "j"
    # Whole record above 256 KB although each part is within its own limit.
    _error(api.create("form", {"r": "x" * 60_000, "j": "y" * 210_000}), 413, "app_data.too_large")
    assert _count_rows(artifact_id) == 3

    # PATCH is validated too, and a rejected PATCH changes nothing.
    rid = ok.json()["id"]
    body = _error(api.update("form", rid, {"b": 1}, version=1), 422, "app_data.validation")
    assert body["params"]["field"] == "b"
    _error(api.update("form", rid, {"nope": 1}, version=1), 422, "app_data.validation")
    _error(api.update("form", rid, {"r": None}, version=1), 422, "app_data.validation")
    assert _row(rid)["version"] == 1


def test_record_limit_per_collection_counts_live_rows(test_client, bootstrap_admin, create_report, monkeypatch):
    import app.schemas.app_storage as app_storage
    monkeypatch.setattr(app_storage, "MAX_RECORDS_PER_COLLECTION", 3)

    owner, report = _owner_and_report(bootstrap_admin, create_report)
    _, artifact_id = _create_artifact(test_client, owner, report["id"], TASKS_V1)
    api = _Api(test_client, artifact_id, owner["token"])

    ids = []
    for i in range(3):
        resp = api.create("tasks", {"title": f"t{i}"})
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])
    body = _error(api.create("tasks", {"title": "over"}), 422, "app_data.limit_reached")
    assert body["params"]["limit"] == 3
    assert _count_rows(artifact_id) == 3

    # Soft-deleted rows do not count.
    assert api.delete("tasks", ids[0], version=1).status_code == 200
    assert api.create("tasks", {"title": "again"}).status_code == 201


def test_optimistic_concurrency_and_soft_delete(test_client, bootstrap_admin, create_report):
    owner, report = _owner_and_report(bootstrap_admin, create_report)
    _, artifact_id = _create_artifact(test_client, owner, report["id"], TASKS_V1)
    api = _Api(test_client, artifact_id, owner["token"])

    rid = api.create("tasks", {"title": "a"}).json()["id"]

    # Two writers on the same base version: exactly one wins (PP6).
    first = api.update("tasks", rid, {"title": "first"}, version=1)
    assert first.status_code == 200 and first.json()["version"] == 2, first.text
    stale = _error(api.update("tasks", rid, {"title": "second"}, version=1), 409, "app_data.conflict")
    assert stale["params"]["current_version"] == 2
    assert _row(rid)["data"] == {"title": "first"} and _row(rid)["version"] == 2

    # Retry on the fresh version succeeds; an empty patch is a valid write.
    assert api.update("tasks", rid, {"title": "second"}, version=2).json()["version"] == 3
    empty = api.update("tasks", rid, {}, version=3)
    assert empty.status_code == 200 and empty.json()["version"] == 4, empty.text
    assert empty.json()["data"]["title"] == "second"

    # Delete is version-checked too, then soft.
    _error(api.delete("tasks", rid, version=3), 409, "app_data.conflict")
    deleted = api.delete("tasks", rid, version=4)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"id": rid, "deleted": True}
    assert api.list("tasks").json()["items"] == []
    row = _row(rid)
    assert row is not None and row["deleted_at"] is not None  # row kept

    # A deleted record is gone for every further write.
    _error(api.update("tasks", rid, {"title": "late"}, version=4), 404, "app_data.record_not_found")
    _error(api.delete("tasks", rid, version=4), 404, "app_data.record_not_found")
    _error(api.update("tasks", str(uuid.uuid4()), {"title": "x"}, version=1), 404, "app_data.record_not_found")


def test_effective_declaration_is_latest_completed_version(test_client, bootstrap_admin, create_report, caplog,
                                                           monkeypatch):
    owner, report = _owner_and_report(bootstrap_admin, create_report)
    version_id, artifact_id = _create_artifact(test_client, owner, report["id"], TASKS_V1)
    assert version_id != artifact_id
    api = _Api(test_client, artifact_id, owner["token"])
    assert api.create("tasks", {"title": "a"}).status_code == 201

    # No API mints a pending or failed version without an LLM run (the agent
    # tools do that), so the newer versions are appended through the same
    # factory the tools use.
    async def _append():
        async with async_session_maker() as db:
            v1 = await db.get(ArtifactVersion, version_id)
            drafts = {"collections": {"drafts": TASKS_V1["collections"]["tasks"]}}
            pending = await new_version(db, v1, content=_content(drafts), status="pending")
            failed = await new_version(db, v1, content=_content(drafts), status="failed")
            await db.commit()
            return str(pending.id), str(failed.id)
    pending_id, failed_id = asyncio.run(_append())

    assert api.list("tasks").status_code == 200  # PP8
    _error(api.list("drafts"), 404, "app_data.collection_not_declared")
    for bad in ("Tasks", "no-tes", "tasks_"):
        _error(api.list(bad), 404, "app_data.collection_not_declared")

    # A version id is not an artifact id.
    _error(_Api(test_client, version_id, owner["token"]).list("tasks"), 404, "artifact.not_found")

    # PP9: an artifact without storage has no collections, for every op.
    plain_version, plain_id = _create_artifact(test_client, owner, report["id"], None)
    plain = _Api(test_client, plain_id, owner["token"])
    _error(plain.list("tasks"), 404, "app_data.collection_not_declared")
    _error(plain.create("tasks", {"title": "a"}), 404, "app_data.collection_not_declared")
    _error(plain.update("tasks", str(uuid.uuid4()), {"title": "a"}, 1), 404, "app_data.collection_not_declared")
    _error(plain.delete("tasks", str(uuid.uuid4()), 1), 404, "app_data.collection_not_declared")
    assert _count_rows(plain_id, live_only=False) == 0

    # A stored declaration that no longer parses declares nothing (fail
    # closed), and the warning names the failing field paths without logging
    # any declared value. Appended directly: the API rejects such storage.
    secret = f"SECRET-{uuid.uuid4().hex}"
    invalid = {"collections": {"tasks": {"scope": "shared", "create": "members", "modify": "author",
                                         "fields": {"title": {"type": "string", "default": 7}},
                                         "stray_key": secret}}}

    async def _append_invalid():
        async with async_session_maker() as db:
            await new_version(db, await db.get(ArtifactVersion, plain_version), content=_content(invalid))
            await db.commit()
    asyncio.run(_append_invalid())
    # The per-test alembic run (logging.config.fileConfig) disables loggers
    # that already exist; the server process never runs migrations in-process.
    monkeypatch.setattr(logging.getLogger("app.services.app_data_service"), "disabled", False)
    with caplog.at_level(logging.WARNING, logger="app.services.app_data_service"):
        _error(plain.list("tasks"), 404, "app_data.collection_not_declared")
    warnings = [r.getMessage() for r in caplog.records if plain_id in r.getMessage()]
    assert warnings, caplog.text
    assert any("stray_key" in w and "fields.title" in w for w in warnings), warnings
    assert secret not in caplog.text

    # Deleting the only completed version leaves no effective declaration;
    # deleting every version deletes the artifact.
    headers = _org_headers(owner["token"], owner["org_id"])
    assert test_client.delete(f"/api/artifacts/{version_id}", headers=headers).status_code == 200
    _error(api.list("tasks"), 404, "app_data.collection_not_declared")
    for vid in (pending_id, failed_id):
        assert test_client.delete(f"/api/artifacts/{vid}", headers=headers).status_code == 200
    _error(api.list("tasks"), 404, "artifact.not_found")
    _error(api.create("tasks", {"title": "b"}), 404, "artifact.not_found")


def test_records_survive_new_versions_and_owner_rerun(test_client, bootstrap_admin, create_report, rerun_report):
    owner, report = _owner_and_report(bootstrap_admin, create_report)
    version_id, artifact_id = _create_artifact(test_client, owner, report["id"], TASKS_V1)
    api = _Api(test_client, artifact_id, owner["token"])
    created = api.create("tasks", {"title": "keep"}).json()

    dup = test_client.post(f"/api/artifacts/{version_id}/duplicate",
                           headers=_org_headers(owner["token"], owner["org_id"]))
    assert dup.status_code == 200, dup.text
    assert dup.json()["artifact_id"] == artifact_id and dup.json()["id"] != version_id
    rerun_report(report["id"], user_token=owner["token"], org_id=owner["org_id"])

    [item] = api.list("tasks").json()["items"]
    assert (item["id"], item["data"], item["version"]) == (created["id"], {"title": "keep"}, 1)


def test_writes_are_audited_without_record_content(test_client, bootstrap_admin, create_report):
    owner, report = _owner_and_report(bootstrap_admin, create_report)
    _, artifact_id = _create_artifact(test_client, owner, report["id"], TASKS_V1)
    api = _Api(test_client, artifact_id, owner["token"])
    secret = f"secret-{uuid.uuid4().hex}"

    rid = api.create("tasks", {"title": secret}).json()["id"]
    assert api.update("tasks", rid, {"note": secret}, version=1).status_code == 200
    assert api.delete("tasks", rid, version=2).status_code == 200
    api.list("tasks")  # reads are not audited

    for action, version in (("app_data.record_created", 1), ("app_data.record_updated", 2),
                            ("app_data.record_deleted", 2)):
        resp = test_client.get("/api/enterprise/audit", headers=_org_headers(owner["token"], owner["org_id"]),
                               params={"action": action, "resource_id": rid})
        assert resp.status_code == 200, resp.text
        [entry] = resp.json()["items"]
        assert entry["resource_type"] == "app_record"
        assert entry["user_id"] == owner["user_id"]
        assert entry["details"]["artifact_id"] == artifact_id
        assert entry["details"]["collection"] == "tasks"
        assert entry["details"]["version"] >= version
        assert secret not in json.dumps(entry)


def test_author_and_mine_are_computed_per_reader(test_client, bootstrap_admin, create_report,
                                                 invite_user_to_org, whoami, monkeypatch):
    owner, report = _owner_and_report(bootstrap_admin, create_report)
    member = invite_user_to_org(org_id=owner["org_id"], admin_token=owner["token"])
    _, artifact_id = _create_artifact(test_client, owner, report["id"], TASKS_V1)
    _set_internal(test_client, owner, report["id"])
    api = _Api(test_client, artifact_id, owner["token"])

    by_owner = api.create("tasks", {"title": "owner"}).json()["id"]
    by_member = api.create("tasks", {"title": "member"}, token=member["token"]).json()["id"]
    names = {owner["user_id"]: whoami(owner["token"])["name"], member["user_id"]: whoami(member["token"])["name"]}
    authors = {by_owner: owner["user_id"], by_member: member["user_id"]}

    for reader in (owner, member):
        items = api.list("tasks", token=reader["token"]).json()["items"]
        assert {i["id"] for i in items} == {by_owner, by_member}
        for item in items:
            author = authors[item["id"]]
            assert item["user"] == {"id": author, "name": names[author]}
            assert item["mine"] is (author == reader["user_id"])

    from app.services.app_data_service import app_data_service

    async def _stats():
        async with async_session_maker() as db:
            return await app_data_service.collection_stats(db, artifact_id)
    assert asyncio.run(_stats()) == {"tasks": {"records": 2, "users": 2}}
    assert api.delete("tasks", by_owner, version=1).status_code == 200
    assert asyncio.run(_stats()) == {"tasks": {"records": 1, "users": 1}}

    # RD11: an unverified user never writes while email verification is on.
    # Invited users are verified by accepting the invite, so the unverified
    # principal here is a report owner who signed up while verification was on.
    from app.settings.config import settings
    features = settings.bow_config.features
    monkeypatch.setattr(features, "verify_emails", True)
    unverified = bootstrap_admin("unverified")
    monkeypatch.setattr(features, "verify_emails", False)
    own_report = create_report(title="Mine", user_token=unverified["token"], org_id=unverified["org_id"],
                               data_sources=[])
    _, own_artifact = _create_artifact(test_client, unverified, own_report["id"], TASKS_V1)
    own_api = _Api(test_client, own_artifact, unverified["token"])
    rid = own_api.create("tasks", {"title": "before"}).json()["id"]

    monkeypatch.setattr(features, "verify_emails", True)
    assert own_api.list("tasks").status_code == 200
    # The refusal says why, so the viewer is told to verify rather than
    # that they lack access.
    for resp in (own_api.create("tasks", {"title": "x"}), own_api.update("tasks", rid, {"title": "x"}, 1),
                 own_api.delete("tasks", rid, 1)):
        assert _error(resp, 403, "app_data.forbidden")["params"].get("reason") == "email_unverified"
    assert _count_rows(own_artifact) == 1 and _row(rid)["version"] == 1
    # Verified principals denied by the rules carry no such reason.
    monkeypatch.setattr(features, "verify_emails", False)
    owner_written = {"collections": {"news": {"scope": "shared", "create": "owner", "modify": "owner",
                                              "fields": {"title": {"type": "string"}}}}}
    _, news_artifact = _create_artifact(test_client, owner, report["id"], owner_written)
    denied = _error(_Api(test_client, news_artifact, member["token"]).create("news", {"title": "x"}),
                    403, "app_data.forbidden")
    assert denied["params"].get("reason") != "email_unverified"


def test_unreadable_rows_are_skipped_not_fatal(test_client, bootstrap_admin, create_report, invite_user_to_org):
    owner, report = _owner_and_report(bootstrap_admin, create_report)
    author = invite_user_to_org(org_id=owner["org_id"], admin_token=owner["token"])
    other = invite_user_to_org(org_id=owner["org_id"], admin_token=owner["token"])
    _, artifact_id = _create_artifact(test_client, owner, report["id"], TASKS_V1)
    _set_internal(test_client, owner, report["id"])
    api = _Api(test_client, artifact_id, owner["token"])
    good = api.create("tasks", {"title": "good"}).json()["id"]
    broken = api.create("tasks", {"title": "broken"}).json()["id"]
    broken_by_author = api.create("tasks", {"title": "mine"}, token=author["token"]).json()["id"]

    # Direct write: the API cannot produce an unreadable row. JSON null is what
    # the column hands back when stored ciphertext cannot be decrypted
    # (EncryptedJSON returns None after a key change).
    async def _break(record_id):
        async with async_session_maker() as db:
            await db.execute(text("UPDATE app_records SET data = 'null' WHERE id = :id"), {"id": record_id})
            await db.commit()
    asyncio.run(_break(broken))
    asyncio.run(_break(broken_by_author))

    listed = api.list("tasks")
    assert listed.status_code == 200, listed.text
    assert [i["id"] for i in listed.json()["items"]] == [good]
    # An unreadable row cannot be updated (a patch needs its data) ...
    _error(api.update("tasks", broken, {"title": "x"}, version=1), 404, "app_data.record_not_found")
    _error(api.update("tasks", broken_by_author, {"title": "x"}, version=1, token=author["token"]),
           404, "app_data.record_not_found")
    # ... but it still counts toward the collection limit, so the report owner
    # or its author can delete it (a delete needs no data); nobody else can.
    _error(api.delete("tasks", broken, version=1, token=author["token"]), 403, "app_data.forbidden")
    _error(api.delete("tasks", broken_by_author, version=1, token=other["token"]), 403, "app_data.forbidden")
    assert _count_rows(artifact_id) == 3
    assert api.delete("tasks", broken, version=1).status_code == 200
    assert api.delete("tasks", broken_by_author, version=1, token=author["token"]).status_code == 200
    assert _count_rows(artifact_id) == 1
    assert _row(good)["deleted_at"] is None
    _error(api.delete("tasks", broken, version=1), 404, "app_data.record_not_found")
    assert api.update("tasks", good, {"title": "still fine"}, version=1).status_code == 200
