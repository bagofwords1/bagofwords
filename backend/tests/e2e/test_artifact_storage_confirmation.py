"""Destructive storage-declaration changes need the run user's approval.

Contract (spec 12, plan P6, RD4):
- When create_artifact (rebuild) or edit_artifact would change an artifact's
  effective storage declaration in a way that hides or exposes stored records
  (collection removed, field removed, field type changed, scope changed,
  create rule changed, field made required without a default), the tool
  pauses on a durable builtin confirmation (`tool.confirmation` with
  kind == "builtin_tool" and the per-change record/user impact). Every
  detected change asks, even when the collection is empty (impact 0).
- Approve -> the new version is persisted. Deny, timeout, a non-interactive
  run, or too little tool budget left to ask -> nothing becomes effective:
  edit persists nothing, a rebuild ends `failed` (PP11).
- Additive changes (new optional field, new field with a default) never ask.
- The legacy, unauthenticated /api/artifacts/confirm route cannot answer a
  builtin confirmation (404, the wait stays pending).

Render validation is stubbed at the Playwright boundary (same fixtures as
test_artifact_storage_tools.py). Approvals resolve through the same-process
public API: `resolve_confirmation` (in-memory future) or
`ToolConfirmationService.resolve` (durable row, picked up by the poll).

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_artifact_storage_confirmation.py --db=sqlite
"""
import asyncio
import copy
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.app_record import AppRecord
from app.models.artifact import ArtifactVersion
from app.models.report import Report
from app.models.user import User
from tests.e2e.test_artifact_storage_tools import (  # noqa: F401  (fixtures)
    NOTES_DECL,
    PREFS,
    _content,
    _create,
    _page_code,
    _runtime_ctx,
    _version_count,
    page,
    stub_render,
)

pytestmark = pytest.mark.e2e


def _run(coro):
    return asyncio.run(coro)


def _create_notes(page, storage=None):
    result = _create(page.report_id, {
        "title": "Notes", "code": _page_code(page.viz_id, "notes"),
        "visualization_ids": [page.viz_id], "storage": storage or NOTES_DECL,
    })
    assert result["output"].get("success", True) is not False, result["observation"]
    return result["output"]["artifact_id"]


async def _parent_id(version_id: str) -> str:
    async with async_session_maker() as db:
        return str((await db.get(ArtifactVersion, version_id)).artifact_id)


async def _seed_records(version_id: str, collection: str, user_ids):
    async with async_session_maker() as db:
        version = await db.get(ArtifactVersion, version_id)
        report = await db.get(Report, version.report_id)
        for uid in user_ids:
            db.add(AppRecord(
                organization_id=report.organization_id, report_id=report.id,
                artifact_id=version.artifact_id, collection=collection,
                user_id=uid, version=1, data={"country": "PT", "text": "hi"},
            ))
        await db.commit()


async def _owner_and_other(report_id: str):
    """The report owner plus a second record author (a bare users row: the
    impact count only needs distinct author ids)."""
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        other = User(
            email=f"author-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x",
            name="Second author", is_active=True, is_verified=True,
        )
        db.add(other)
        await db.commit()
        return str(report.user_id), str(other.id)


async def _drive(tool, report_id, tool_input, *, answer=None, via="future", ctx_extra=None, stop=False):
    """Run a tool; answer its storage confirmation (if any) when it appears,
    or press Stop (the run's sigkill event) while it waits."""
    from app.ai.tools.confirmation import resolve_confirmation
    from app.services.tool_confirmation_service import ToolConfirmationService

    async with async_session_maker() as db:
        runtime_ctx = await _runtime_ctx(db, report_id)
        runtime_ctx.update(ctx_extra or {})
        if stop:
            runtime_ctx["sigkill_event"] = asyncio.Event()
        events = []
        async for evt in tool.run_stream(tool_input, runtime_ctx):
            events.append(evt)
            if evt.type == "tool.confirmation" and stop:
                runtime_ctx["sigkill_event"].set()
            if evt.type == "tool.confirmation" and answer is not None:
                cid = evt.payload["confirmation_id"]
                if via == "future":
                    assert resolve_confirmation(cid, {"approved": answer})
                else:
                    async with async_session_maker() as other:
                        await ToolConfirmationService().resolve(
                            other, confirmation_id=cid, approved=answer, remember=False,
                            user_id=str(runtime_ctx["user"].id),
                        )
    ends = [e for e in events if e.type == "tool.end"]
    assert ends, f"no tool.end in {[e.type for e in events]}"
    confirmations = [e.payload for e in events if e.type == "tool.confirmation"]
    return ends[-1].payload, confirmations


def _edit(report_id, tool_input, **kw):
    from app.ai.tools.implementations.edit_artifact import EditArtifactTool
    return _run(_drive(EditArtifactTool(), report_id, tool_input, **kw))


def _rebuild(report_id, tool_input, **kw):
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool
    return _run(_drive(CreateArtifactTool(), report_id, {"prompt": "rebuild", "mode": "page", **tool_input}, **kw))


def _drop_notes_edit(v1):
    """A storage edit that removes the `notes` collection (and its use)."""
    return {
        "artifact_id": v1,
        "storage": {"collections": {"prefs": PREFS}},
        "edits": [{"find": 'const c0 = useCollection("notes");', "replace": 'const c0 = useCollection("prefs");'}],
    }


async def _effective_storage(parent_id: str):
    from app.services.app_data_service import app_data_service
    async with async_session_maker() as db:
        decl = await app_data_service.effective_declaration(db, parent_id)
        return decl.model_dump(mode="json", exclude_unset=True) if decl is not None else None


# ---------------------------------------------------------------------------
# edit_artifact
# ---------------------------------------------------------------------------

def test_removing_a_collection_with_records_asks_and_approve_persists(page):
    v1 = _create_notes(page)
    owner_id, other_id = _run(_owner_and_other(page.report_id))
    _run(_seed_records(v1, "notes", [owner_id, owner_id, other_id]))

    end, confirmations = _edit(page.report_id, _drop_notes_edit(v1), answer=True)

    assert len(confirmations) == 1, "a destructive change must pause for approval"
    conf = confirmations[0]
    assert conf["kind"] == "builtin_tool"
    assert conf["tool_name"] == "edit_artifact"
    assert conf["confirmation_id"] and conf["timeout_seconds"] > 0
    assert conf["storage_changes"] == [{
        "kind": "collection_removed", "collection": "notes", "field": None, "principal": None, "capability": None,
        "before": None, "after": None, "records": 3, "users": 2,
    }]
    assert "affects 3 record(s) from 2 user(s)" in conf["summary"]
    assert end["output"]["success"] is True, end["observation"]
    assert _run(_content(end["output"]["artifact_id"]))["storage"] == {"collections": {"prefs": PREFS}}


def test_denied_edit_persists_nothing(page):
    v1 = _create_notes(page)
    before = _run(_version_count(page.report_id))

    end, confirmations = _edit(page.report_id, _drop_notes_edit(v1), answer=False, via="row")

    assert len(confirmations) == 1
    assert end["output"]["success"] is False
    assert end["observation"]["error"]["type"] == "storage_change_not_confirmed"
    assert end["observation"]["error"]["reason"] == "declined"
    assert _run(_version_count(page.report_id)) == before, "a declined change must persist nothing"
    assert _run(_effective_storage(_run(_parent_id(v1)))) == NOTES_DECL


def test_unanswered_confirmation_times_out_and_persists_nothing(page, monkeypatch):
    from app.ai.tools.implementations import _artifact_storage
    monkeypatch.setattr(_artifact_storage, "STORAGE_CONFIRM_TIMEOUT_S", 1.0)
    v1 = _create_notes(page)
    before = _run(_version_count(page.report_id))

    end, confirmations = _edit(page.report_id, _drop_notes_edit(v1))

    assert len(confirmations) == 1
    assert confirmations[0]["timeout_seconds"] == 1.0
    assert end["output"]["success"] is False
    assert end["observation"]["error"]["reason"] == "timed_out"
    assert _run(_version_count(page.report_id)) == before


def test_non_interactive_run_fails_closed_without_asking(page):
    v1 = _create_notes(page)
    before = _run(_version_count(page.report_id))

    end, confirmations = _edit(page.report_id, _drop_notes_edit(v1), ctx_extra={"platform": "slack"})

    assert confirmations == [], "nobody can answer in a platform run: no card"
    assert end["output"]["success"] is False
    assert end["observation"]["error"]["type"] == "storage_change_not_confirmed"
    assert end["observation"]["error"]["reason"] == "non_interactive"
    assert _run(_version_count(page.report_id)) == before


def test_too_little_tool_budget_left_fails_closed_without_asking(page, monkeypatch):
    # A3: the wait is capped by the runner's hard budget; when too little
    # remains the tool refuses instead of being killed mid-wait.
    from app.ai.tools.implementations import _artifact_storage
    monkeypatch.setattr(_artifact_storage, "TOOL_HARD_TIMEOUT_S", 25.0)
    v1 = _create_notes(page)
    before = _run(_version_count(page.report_id))

    end, confirmations = _edit(page.report_id, _drop_notes_edit(v1))

    assert confirmations == []
    assert end["output"]["success"] is False
    assert end["observation"]["error"]["reason"] == "insufficient_time"
    assert _run(_version_count(page.report_id)) == before


def test_wait_is_capped_by_the_remaining_tool_budget(page, monkeypatch):
    from app.ai.tools.implementations import _artifact_storage
    monkeypatch.setattr(_artifact_storage, "TOOL_HARD_TIMEOUT_S", 100.0)
    v1 = _create_notes(page)

    end, confirmations = _edit(page.report_id, _drop_notes_edit(v1), answer=False)

    assert len(confirmations) == 1
    budget = 100.0 - _artifact_storage.STORAGE_CONFIRM_SAFETY_MARGIN_S
    assert 0 < confirmations[0]["timeout_seconds"] <= budget


def test_removing_an_empty_collection_still_asks_with_zero_impact(page):
    v1 = _create_notes(page)

    end, confirmations = _edit(page.report_id, _drop_notes_edit(v1), answer=False)

    assert len(confirmations) == 1, "spec 12: every detected change asks, even with no records"
    assert confirmations[0]["storage_changes"][0]["kind"] == "collection_removed"
    assert confirmations[0]["storage_changes"][0]["records"] == 0
    assert confirmations[0]["storage_changes"][0]["users"] == 0


def _variant(kind):
    base = copy.deepcopy(NOTES_DECL)
    notes = base["collections"]["notes"]
    if kind == "field_removed":
        del notes["fields"]["text"]
        return NOTES_DECL, base
    if kind == "field_type_changed":
        notes["fields"]["text"] = {"type": "json"}
        return NOTES_DECL, base
    if kind == "create_members_to_owner":
        notes["create"] = "owner"
        return NOTES_DECL, base
    if kind == "per_user_to_shared":
        # per_user -> shared: every user's private rows become visible to all.
        private = {"collections": {"notes": {"scope": "per_user", "fields": notes["fields"]}}}
        return private, base
    if kind == "modify_owner_to_author":
        # Codex #2: members gain edit/delete of their existing rows.
        moderated = copy.deepcopy(NOTES_DECL)
        moderated["collections"]["notes"]["modify"] = "owner"
        return moderated, base
    if kind == "field_made_required":
        notes["fields"]["text"]["required"] = True
        return NOTES_DECL, base
    raise AssertionError(kind)


# (kind, principal, capability, before, after) the spec's rule tables promise.
EXPECTED_CHANGES = {
    "field_removed": [("field_removed", None, None, "string", None)],
    "field_type_changed": [("field_type_changed", None, None, "string", "json")],
    "field_made_required": [("field_made_required", None, None, None, None)],
    "create_members_to_owner": [
        ("access_changed", "member", "create", "yes", "no"),
        ("access_changed", "member", "modify_own", "yes", "no"),
    ],
    "per_user_to_shared": [
        ("access_changed", "owner", "read", "own", "all"),
        ("access_changed", "owner", "modify_others", "no", "yes"),
        ("access_changed", "member", "read", "own", "all"),
    ],
    "modify_owner_to_author": [("access_changed", "member", "modify_own", "no", "yes")],
}


@pytest.mark.parametrize("kind", sorted(EXPECTED_CHANGES))
def test_each_detected_change_asks_even_on_an_empty_collection(page, kind):
    before_decl, after_decl = _variant(kind)
    v1 = _create_notes(page, storage=before_decl)
    before = _run(_version_count(page.report_id))

    end, confirmations = _edit(page.report_id, {"artifact_id": v1, "storage": after_decl}, answer=False)

    assert len(confirmations) == 1, f"{kind} must ask (RD4 / spec 12)"
    got = [(c["kind"], c["principal"], c["capability"], c["before"], c["after"])
           for c in confirmations[0]["storage_changes"]]
    assert got == EXPECTED_CHANGES[kind]
    assert all(c["records"] == 0 for c in confirmations[0]["storage_changes"])
    assert end["output"]["success"] is False
    assert _run(_version_count(page.report_id)) == before


@pytest.mark.parametrize("field", [
    {"type": "string"},
    {"type": "boolean", "default": False},
    {"type": "number", "required": True, "default": 0},
])
def test_additive_changes_do_not_ask(page, field):
    v1 = _create_notes(page)
    storage = copy.deepcopy(NOTES_DECL)
    storage["collections"]["notes"]["fields"]["extra"] = field

    end, confirmations = _edit(page.report_id, {"artifact_id": v1, "storage": storage})

    assert confirmations == []
    assert end["output"]["success"] is True, end["observation"]
    assert _run(_content(end["output"]["artifact_id"]))["storage"] == storage


def test_optional_to_required_with_a_default_does_not_ask(page):
    v1 = _create_notes(page)
    storage = copy.deepcopy(NOTES_DECL)
    storage["collections"]["notes"]["fields"]["text"].update(required=True, default="")

    end, confirmations = _edit(page.report_id, {"artifact_id": v1, "storage": storage})

    assert confirmations == []
    assert end["output"]["success"] is True, end["observation"]


# ---------------------------------------------------------------------------
# create_artifact (rebuild with replaces_artifact_id)
# ---------------------------------------------------------------------------

def _rebuild_without_notes(page, v1):
    return {
        "title": "Rebuilt", "code": _page_code(page.viz_id), "visualization_ids": [page.viz_id],
        "replaces_artifact_id": v1, "storage": {"collections": {}},
    }


def test_rebuild_removing_a_collection_asks_and_approve_completes(page):
    v1 = _create_notes(page)
    owner_id, _ = _run(_owner_and_other(page.report_id))
    _run(_seed_records(v1, "notes", [owner_id]))

    end, confirmations = _rebuild(page.report_id, _rebuild_without_notes(page, v1), answer=True)

    assert len(confirmations) == 1
    assert confirmations[0]["kind"] == "builtin_tool"
    assert confirmations[0]["tool_name"] == "create_artifact"
    assert confirmations[0]["storage_changes"][0]["records"] == 1
    assert end["output"].get("success", True) is not False, end["observation"]
    assert _run(_effective_storage(_run(_parent_id(v1)))) == {"collections": {}}


def test_denied_rebuild_ends_failed_and_keeps_the_effective_declaration(page):
    v1 = _create_notes(page)

    end, confirmations = _rebuild(page.report_id, _rebuild_without_notes(page, v1), answer=False)

    assert len(confirmations) == 1
    assert end["output"]["success"] is False
    assert end["observation"]["error"]["type"] == "storage_change_not_confirmed"
    assert end["observation"]["error"]["reason"] == "declined"
    new_row = _run(_row(end["output"]["artifact_id"]))
    assert new_row["status"] == "failed"
    assert _run(_effective_storage(_run(_parent_id(v1)))) == NOTES_DECL


def test_rebuild_in_a_non_interactive_run_ends_failed_without_asking(page):
    v1 = _create_notes(page)

    end, confirmations = _rebuild(page.report_id, _rebuild_without_notes(page, v1), ctx_extra={"is_eval_run": True})

    assert confirmations == []
    assert end["output"]["success"] is False
    assert end["observation"]["error"]["reason"] == "non_interactive"
    assert _run(_effective_storage(_run(_parent_id(v1)))) == NOTES_DECL


def test_rebuild_carrying_the_declaration_does_not_ask(page):
    v1 = _create_notes(page)

    end, confirmations = _rebuild(page.report_id, {
        "code": _page_code(page.viz_id, "notes", label="Again"), "visualization_ids": [page.viz_id],
        "replaces_artifact_id": v1,
    })

    assert confirmations == []
    assert end["output"].get("success", True) is not False, end["observation"]


async def _row(version_id: str):
    async with async_session_maker() as db:
        row = await db.get(ArtifactVersion, version_id)
        return {"status": row.status}


# ---------------------------------------------------------------------------
# legacy confirm route
# ---------------------------------------------------------------------------

def test_legacy_confirm_route_refuses_builtin_confirmation_ids(test_client):
    from app.ai.tools.confirmation import (
        KIND_BUILTIN_TOOL,
        PENDING_CONFIRMATIONS,
        discard_confirmation,
        register_confirmation,
    )

    async def scenario():
        cid = str(uuid.uuid4())
        future = register_confirmation(cid, meta={"kind": KIND_BUILTIN_TOOL, "user_id": "u"})
        try:
            res = test_client.post(f"/api/artifacts/confirm/{cid}", json={"approved": True})
            return res.status_code, future.done(), cid in PENDING_CONFIRMATIONS
        finally:
            discard_confirmation(cid)

    status, done, still_pending = _run(scenario())
    assert status == 404
    assert not done, "the builtin confirmation must stay pending"
    assert still_pending


# ---------------------------------------------------------------------------
# W3 remediation
# ---------------------------------------------------------------------------

import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from tests.e2e.test_artifact_storage_tools import _FakeModel, _diff  # noqa: E402

PER_USER_NOTES = {"collections": {"notes": {"scope": "per_user", "fields": NOTES_DECL["collections"]["notes"]["fields"]}}}
OWNER_NOTES = {**NOTES_DECL["collections"]["notes"], "create": "owner"}


def _add_prefs(v1):
    """Storage-only edit that adds `prefs` (additive: never asks)."""
    end, confirmations = _edit(_report_of(v1), {
        "artifact_id": v1, "storage": {"collections": {**NOTES_DECL["collections"], "prefs": PREFS}},
    })
    assert confirmations == [] and end["output"]["success"] is True, end["observation"]
    return end["output"]["artifact_id"]


def _report_of(version_id):
    async def go():
        async with async_session_maker() as db:
            return str((await db.get(ArtifactVersion, version_id)).report_id)
    return _run(go())


def test_readding_a_removed_collection_with_orphaned_rows_asks(page):
    # notes was per_user (private rows), removed with approval (rows kept),
    # then re-declared as shared/owner-create: anonymous visitors of a public
    # artifact could read the old private rows. That must ask first.
    v1 = _create_notes(page, storage=PER_USER_NOTES)
    owner_id, other_id = _run(_owner_and_other(page.report_id))
    _run(_seed_records(v1, "notes", [owner_id, owner_id, other_id]))
    end, _ = _edit(page.report_id, _drop_notes_edit(v1), answer=True)
    v2 = end["output"]["artifact_id"]
    before = _run(_version_count(page.report_id))

    end, confirmations = _edit(page.report_id, {
        "artifact_id": v2, "storage": {"collections": {"prefs": PREFS, "notes": OWNER_NOTES}},
    }, answer=False)

    assert len(confirmations) == 1, "re-adding a collection with orphaned rows must ask"
    assert confirmations[0]["storage_changes"] == [{
        "kind": "collection_readded", "collection": "notes", "field": None, "principal": None, "capability": None,
        "before": "orphaned records", "after": "shared/owner", "records": 3, "users": 2,
    }]
    assert end["output"].get("success") is False
    assert _run(_version_count(page.report_id)) == before


def test_orphaned_rows_ask_even_without_a_previous_declaration(page):
    created = _create(page.report_id, {
        "title": "Plain", "code": _page_code(page.viz_id), "visualization_ids": [page.viz_id],
    })
    v1 = created["output"]["artifact_id"]
    owner_id, _ = _run(_owner_and_other(page.report_id))
    _run(_seed_records(v1, "notes", [owner_id]))
    before = _run(_version_count(page.report_id))

    end, confirmations = _edit(page.report_id, {"artifact_id": v1, "storage": NOTES_DECL}, answer=False)

    assert len(confirmations) == 1
    assert [c["kind"] for c in confirmations[0]["storage_changes"]] == ["collection_readded"]
    assert confirmations[0]["storage_changes"][0]["records"] == 1
    assert _run(_version_count(page.report_id)) == before

    end, confirmations = _rebuild(page.report_id, {
        "code": _page_code(page.viz_id, "notes"), "visualization_ids": [page.viz_id],
        "replaces_artifact_id": v1, "storage": NOTES_DECL,
    }, answer=False)
    assert [c["kind"] for c in confirmations[0]["storage_changes"]] == ["collection_readded"]
    assert _run(_row(end["output"]["artifact_id"]))["status"] == "failed"


def test_editing_an_older_version_without_storage_asks_about_newer_collections(page):
    # R2: v1 declares notes, v2 adds prefs; editing v1 carries v1's
    # declaration, which removes prefs from the effective declaration.
    v1 = _create_notes(page)
    _add_prefs(v1)
    before = _run(_version_count(page.report_id))

    end, confirmations = _edit(page.report_id, {
        "artifact_id": v1, "edits": [{"find": "Notes</div>", "replace": "Old notes</div>"}],
    }, answer=False)

    assert len(confirmations) == 1
    assert [(c["kind"], c["collection"]) for c in confirmations[0]["storage_changes"]] == [("collection_removed", "prefs")]
    assert end["output"].get("success") is False
    assert _run(_version_count(page.report_id)) == before


def _mcp_edit(page, monkeypatch, artifact_id):
    from app.ai.tools.mcp import edit_artifact as mcp_edit

    class _FakeLLM:  # LLM provider boundary
        def __init__(self, *args, **kwargs):
            pass

        def inference(self, *args, **kwargs):
            return _diff("Notes</div>", "MCP notes</div>")

    async def _fake_rich_context(**kwargs):
        return SimpleNamespace(model=_FakeModel(), instructions_text="", org_settings=None)

    monkeypatch.setattr(mcp_edit, "LLM", _FakeLLM)
    monkeypatch.setattr(mcp_edit, "build_rich_context", _fake_rich_context)

    async def go():
        async with async_session_maker() as db:
            ctx = await _runtime_ctx(db, page.report_id)
            return await mcp_edit.EditArtifactMCPTool().execute(
                {"report_id": page.report_id, "artifact_id": artifact_id, "edit_instruction": "rename"},
                db, ctx["user"], ctx["organization"],
            )
    return _run(go())


def test_mcp_edit_of_an_older_version_fails_closed(page, monkeypatch):
    v1 = _create_notes(page)
    _add_prefs(v1)
    before = _run(_version_count(page.report_id))

    result = _mcp_edit(page, monkeypatch, v1)

    assert result["success"] is False, result
    assert "collection_removed" in result["error_message"] and "prefs" in result["error_message"]
    assert "latest version" in result["error_message"]
    assert _run(_version_count(page.report_id)) == before


async def _failed_version_without_storage(v1):
    from app.services.artifact_service import new_version
    async with async_session_maker() as db:
        source = await db.get(ArtifactVersion, v1)
        content = {**copy.deepcopy(source.content), "storage": {"collections": {}}}
        # No API mints a failed version with content without an LLM run.
        row = await new_version(db, source, content=content, status="failed")
        await db.commit()
        return str(row.id)


def test_mcp_edit_of_a_failed_version_fails_closed(page, monkeypatch):
    v1 = _create_notes(page)
    failed = _run(_failed_version_without_storage(v1))
    before = _run(_version_count(page.report_id))

    result = _mcp_edit(page, monkeypatch, failed)

    assert result["success"] is False, result
    assert "collection_removed" in result["error_message"] and "notes" in result["error_message"]
    assert _run(_version_count(page.report_id)) == before
    assert _run(_effective_storage(_run(_parent_id(v1)))) == NOTES_DECL


def test_legacy_edit_of_an_older_version_fails_closed(page, monkeypatch):
    from app.ai.llm.types import TextDeltaEvent
    from app.ai.tools.implementations import edit_artifact_legacy

    class _FakeLLM:  # LLM provider boundary
        def __init__(self, *args, **kwargs):
            pass

        async def inference_stream_v2(self, *args, **kwargs):
            yield TextDeltaEvent(text=_diff("Notes</div>", "Legacy notes</div>"))

    monkeypatch.setattr(edit_artifact_legacy, "LLM", _FakeLLM)
    v1 = _create_notes(page)
    _add_prefs(v1)
    before = _run(_version_count(page.report_id))

    end, confirmations = _run(_drive(edit_artifact_legacy.EditArtifactTool(), page.report_id,
                                     {"artifact_id": v1, "edit_prompt": "rename"},
                                     ctx_extra={"model": _FakeModel()}))

    assert confirmations == []
    assert end["output"].get("success") is False
    assert "prefs" in end["observation"]["summary"]
    assert _run(_version_count(page.report_id)) == before


async def _latest_row(report_id):
    from sqlalchemy import select
    async with async_session_maker() as db:
        row = (await db.execute(
            select(ArtifactVersion).where(ArtifactVersion.report_id == report_id)
            .order_by(ArtifactVersion.created_at.desc()).limit(1)
        )).scalar_one()
        return {"id": str(row.id), "status": row.status}


@pytest.mark.parametrize("exc", [RuntimeError("storage step broke"), asyncio.CancelledError()])
def test_rebuild_exception_after_the_pending_row_marks_it_failed(page, monkeypatch, exc):
    from app.ai.tools.implementations import _artifact_storage
    v1 = _create_notes(page)

    async def _boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(_artifact_storage, "destructive_storage_changes", _boom)
    with pytest.raises(type(exc)):
        _rebuild(page.report_id, _rebuild_without_notes(page, v1))

    latest = _run(_latest_row(page.report_id))
    assert latest["id"] != v1
    assert latest["status"] == "failed", "a crashed rebuild must not stay pending forever"


def test_stop_during_the_approval_wait_is_reported_as_stopped(page):
    v1 = _create_notes(page)
    before = _run(_version_count(page.report_id))

    end, confirmations = _edit(page.report_id, _drop_notes_edit(v1), stop=True)

    assert len(confirmations) == 1
    assert end["output"].get("success") is False
    assert end["observation"]["error"]["reason"] == "stopped"
    assert _run(_version_count(page.report_id)) == before

    end, confirmations = _rebuild(page.report_id, _rebuild_without_notes(page, v1), stop=True)
    assert end["observation"]["error"]["reason"] == "stopped"
    assert _run(_row(end["output"]["artifact_id"]))["status"] == "stopped"
    assert _run(_effective_storage(_run(_parent_id(v1)))) == NOTES_DECL


def test_generated_rebuild_code_is_checked_against_the_declaration(page, monkeypatch):
    # M4: a rebuild without `code` generates the page with the inner model;
    # the generated code must pass the reference gate like authored code.
    from app.ai.tools.implementations import create_artifact as create_mod

    generated = "```jsx\n" + _page_code(page.viz_id, "ghost") + "\n```"

    class _FakeLLM:  # LLM provider boundary
        def __init__(self, *args, **kwargs):
            pass

        async def inference_stream_v2(self, *args, **kwargs):
            from app.ai.llm.types import TextDeltaEvent
            yield TextDeltaEvent(text=generated)

    monkeypatch.setattr(create_mod, "LLM", _FakeLLM)
    v1 = _create_notes(page)

    end, confirmations = _rebuild(page.report_id, {
        "visualization_ids": [page.viz_id], "replaces_artifact_id": v1,
    }, ctx_extra={"model": _FakeModel()})

    assert confirmations == []
    assert end["output"].get("success") is False, end["observation"]
    assert end["observation"]["error"]["type"] == "storage_errors"
    assert "ghost" in end["observation"]["error"]["message"]
    assert _run(_row(end["output"]["artifact_id"]))["status"] == "failed"
    assert _run(_effective_storage(_run(_parent_id(v1)))) == NOTES_DECL


async def _corrupt_storage(version_id):
    async with async_session_maker() as db:
        row = await db.get(ArtifactVersion, version_id)
        row.content = {**copy.deepcopy(row.content), "storage": {"collections": {"Bad Name": {"scope": "nope"}}}}
        await db.commit()


def test_rebuild_of_an_artifact_with_an_invalid_declaration_fails_closed(page):
    v1 = _create_notes(page)
    _run(_corrupt_storage(v1))
    before = _run(_version_count(page.report_id))

    end, confirmations = _rebuild(page.report_id, {
        "code": _page_code(page.viz_id), "visualization_ids": [page.viz_id], "replaces_artifact_id": v1,
    })

    assert confirmations == []
    assert end["output"].get("success") is False
    assert end["observation"]["error"]["type"] == "storage_errors"
    assert "invalid" in end["observation"]["error"]["message"]
    assert _run(_version_count(page.report_id)) == before


def test_runner_deadline_in_the_runtime_context_caps_the_wait(page):
    # H4: the runner's hard deadline covers every retry attempt; a late
    # attempt must see the real remaining budget, not a fresh 300 s.
    v1 = _create_notes(page)
    before = _run(_version_count(page.report_id))

    end, confirmations = _edit(page.report_id, _drop_notes_edit(v1),
                               ctx_extra={"tool_deadline_monotonic": time.monotonic() + 25})

    assert confirmations == []
    assert end["observation"]["error"]["reason"] == "insufficient_time"
    assert _run(_version_count(page.report_id)) == before


# ---------------------------------------------------------------------------
# W6-A: public reads are an explicit decision; access diff; re-added fields
# ---------------------------------------------------------------------------

async def _make_public(report_id: str):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        report.artifact_visibility = "public"
        await db.commit()


async def _anonymous_list(parent_id: str, collection: str):
    """(status, author ids) of an anonymous list call through the service."""
    from app.errors import AppError
    from app.services.app_data_service import app_data_service

    async with async_session_maker() as db:
        try:
            listed = await app_data_service.list_records(db, artifact_id=parent_id, collection=collection, user=None)
        except AppError as exc:
            return exc.status_code, None
        return 200, sorted(item.user.id for item in listed.items)


def test_regression_create_members_to_owner_on_a_public_artifact_does_not_publish(page):
    # Codex #1: switching who may add used to publish every member-written row.
    v1 = _create_notes(page)
    owner_id, other_id = _run(_owner_and_other(page.report_id))
    _run(_seed_records(v1, "notes", [owner_id, other_id]))
    _run(_make_public(page.report_id))
    parent = _run(_parent_id(v1))
    assert _run(_anonymous_list(parent, "notes"))[0] == 401

    storage = copy.deepcopy(NOTES_DECL)
    storage["collections"]["notes"]["create"] = "owner"
    end, confirmations = _edit(page.report_id, {"artifact_id": v1, "storage": storage}, answer=True)

    assert end["output"]["success"] is True, end["observation"]
    changes = confirmations[0]["storage_changes"]
    assert not any(c["principal"] in ("public", "outsider", "anonymous") for c in changes)
    assert _run(_anonymous_list(parent, "notes"))[0] == 401, "create owner alone never publishes"


def test_public_read_on_with_member_rows_asks_and_publishes_owner_rows_only(page):
    v1 = _create_notes(page)
    owner_id, other_id = _run(_owner_and_other(page.report_id))
    _run(_seed_records(v1, "notes", [owner_id, owner_id, other_id]))
    _run(_make_public(page.report_id))
    parent = _run(_parent_id(v1))

    storage = copy.deepcopy(NOTES_DECL)
    storage["collections"]["notes"].update(create="owner", modify="owner", public_read=True)
    end, confirmations = _edit(page.report_id, {"artifact_id": v1, "storage": storage}, answer=True)

    assert len(confirmations) == 1, "publishing records must ask first"
    public = [c for c in confirmations[0]["storage_changes"] if c["principal"] == "public"]
    assert public == [{
        "kind": "access_changed", "collection": "notes", "field": None, "principal": "public", "capability": "read",
        "before": "none", "after": "owner", "records": 2, "users": 1,
    }]
    assert ("Anyone with the public link will be able to read 2 existing record(s) written by the owner in 'notes'"
            in confirmations[0]["summary"])
    assert end["output"]["success"] is True, end["observation"]
    assert _run(_effective_storage(parent))["collections"]["notes"]["public_read"] is True
    # The member-written row stays private: anonymous readers get the owner's rows only.
    assert _run(_anonymous_list(parent, "notes")) == (200, [owner_id, owner_id])


def test_declined_public_read_publishes_nothing(page):
    v1 = _create_notes(page)
    owner_id, _ = _run(_owner_and_other(page.report_id))
    _run(_seed_records(v1, "notes", [owner_id]))
    _run(_make_public(page.report_id))
    parent = _run(_parent_id(v1))
    owner_notes = copy.deepcopy(NOTES_DECL)
    owner_notes["collections"]["notes"].update(create="owner", modify="owner")
    end, _ = _edit(page.report_id, {"artifact_id": v1, "storage": owner_notes}, answer=True)
    v2 = end["output"]["artifact_id"]

    published = copy.deepcopy(owner_notes)
    published["collections"]["notes"]["public_read"] = True
    end, confirmations = _edit(page.report_id, {"artifact_id": v2, "storage": published}, answer=False)

    assert [(c["kind"], c["principal"], c["after"], c["records"]) for c in confirmations[0]["storage_changes"]] == [
        ("access_changed", "public", "owner", 1),
    ]
    assert end["output"]["success"] is False
    assert _run(_anonymous_list(parent, "notes"))[0] == 401


def test_regression_readding_a_removed_field_with_the_same_type_asks(page):
    # Codex #3: re-adding `text` (same type) would return the values kept
    # since it was removed.
    v1 = _create_notes(page)
    owner_id, _ = _run(_owner_and_other(page.report_id))
    _run(_seed_records(v1, "notes", [owner_id]))
    without_text = copy.deepcopy(NOTES_DECL)
    del without_text["collections"]["notes"]["fields"]["text"]
    end, _ = _edit(page.report_id, {"artifact_id": v1, "storage": without_text}, answer=True)
    v2 = end["output"]["artifact_id"]
    before = _run(_version_count(page.report_id))

    end, confirmations = _edit(page.report_id, {"artifact_id": v2, "storage": NOTES_DECL}, answer=False)

    assert len(confirmations) == 1
    assert [(c["kind"], c["field"], c["before"], c["after"], c["records"])
            for c in confirmations[0]["storage_changes"]] == [("field_readded", "text", "string", "string", 1)]
    assert end["output"]["success"] is False
    assert _run(_version_count(page.report_id)) == before


def test_regression_a_300kb_default_is_rejected_before_anything_persists(page):
    # Codex #6: one default would make every record exceed the limits.
    storage = copy.deepcopy(NOTES_DECL)
    storage["collections"]["notes"]["fields"]["snapshot"] = {"type": "json", "default": {"blob": "x" * 300_000}}
    result = _create(page.report_id, {
        "title": "Big", "code": _page_code(page.viz_id, "notes"), "visualization_ids": [page.viz_id],
        "storage": storage,
    })

    assert result["output"]["success"] is False
    assert result["observation"]["error"]["type"] == "storage_errors"
    assert "default" in result["observation"]["error"]["message"]
    assert _run(_version_count(page.report_id)) == 0
