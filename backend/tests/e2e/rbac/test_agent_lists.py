"""Agent Lists — API, RBAC, agent submissions, human edits, CSV and bow.lists.

Contracts (docs/feedback-loops/agent-lists.md):
- Lists follow the owning agent: VIEW on the agent reads lists/rows/CSV,
  MANAGE (or org admin) is required for every write.
- A submission is atomic and validated against the full schema; invalid
  records write nothing and return path-qualified errors.
- Rows upsert on the key field (or row_id); human-edited fields are locked and
  never overwritten by the agent; every change writes one revision.
- CSV export streams every row, BOM-prefixed, formula-injection safe.
- bow.<agent>.lists.<list> tables are queryable by list_id for users who can
  view the agent, and the table name never grants anything.
"""
import asyncio
import csv
import io
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.e2e


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _schema(name="Contracts", key="counterparty", require_evidence=False, extra_fields=None):
    fields = [
        {"name": "counterparty", "type": "string", "required": True, "description": "Other party"},
        {"name": "annual_value", "type": "number", "description": "Per 12 months excl. VAT"},
        {"name": "currency", "type": "enum", "enum": ["USD", "EUR", "ILS"]},
        {"name": "renewal_date", "type": "date"},
        {"name": "auto_renew", "type": "boolean"},
    ] + (extra_fields or [])
    return {"name": name, "description": "Customer contracts", "fields": fields,
            "key_field": key, "require_evidence": require_evidence}


def _env(value, status="found", quote=None, ref=None, page=None, kind="file"):
    ev = [{"kind": kind, "ref": ref, "page": page, "quote": quote}] if quote else []
    return {"value": value, "status": status, "evidence": ev, "note": None}


def _record(cp="Acme Ltd", value=120000, currency="USD", renewal="2027-01-31", auto=True, row_id=None, **over):
    fields = {
        "counterparty": _env(cp),
        "annual_value": _env(value),
        "currency": _env(currency),
        "renewal_date": _env(renewal) if renewal else _env(None, "not_found"),
        "auto_renew": _env(auto),
    }
    fields.update(over)
    return {"row_id": row_id, "fields": fields}


def _db_url():
    return (os.environ["TEST_DATABASE_URL"]
            .replace("sqlite://", "sqlite+aiosqlite://", 1)
            .replace("postgresql://", "postgresql+asyncpg://", 1))


def run_async(fn):
    """Run ``fn(db)`` in a fresh engine bound to the test database."""
    async def _run():
        engine = create_async_engine(_db_url())
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                return await fn(db, maker)
        finally:
            await engine.dispose()
    return asyncio.run(_run())


def submit(world, records, *, actor="admin", report_key="report", list_id=None, tool_call_id=None):
    """Execute the submit_list gateway exactly as AgentV2 does after rewriting submit_<list>."""
    from app.ai.tools.implementations.submit_list import SubmitListTool
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    async def _fn(db, maker):
        report = await db.get(Report, world[report_key]["id"])
        user = await db.get(User, world[actor]["user_id"])
        org = await db.get(Organization, world["org_id"])
        ctx = {"db": db, "session_maker": maker, "report": report, "user": user, "organization": org,
               "tool_call_id": tool_call_id or str(uuid.uuid4())}
        events = []
        async for e in SubmitListTool().run_stream(
            {"list_id": list_id or world["list"]["id"], "records": records}, ctx
        ):
            events.append(e)
        end = [e for e in events if e.type == "tool.end"][-1]
        return end.payload
    return run_async(_fn)


@pytest.fixture
def world(bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource, create_report, test_client):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    agent = sqlite_data_source(name=f"Sales Ops {uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)
    other_agent = sqlite_data_source(name=f"Legal {uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)

    viewer = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    outsider = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    for who, perms in ((viewer, ["view"]), (manager, ["manage"])):
        r = grant_resource(resource_type="data_source", resource_id=agent["id"], principal_type="user",
                           principal_id=who["user_id"], permissions=perms, user_token=admin["token"], org_id=org_id)
        assert r.status_code in (200, 201), r.text

    resp = test_client.post(f"/api/data_sources/{agent['id']}/lists", json=_schema(), headers=_h(admin["token"], org_id))
    assert resp.status_code == 201, resp.text
    lst = resp.json()

    report = create_report(title="Extraction", user_token=admin["token"], org_id=org_id, data_sources=[agent["id"]])
    other_report = create_report(title="Other", user_token=admin["token"], org_id=org_id, data_sources=[other_agent["id"]])
    return {"org_id": org_id, "admin": admin, "viewer": viewer, "manager": manager, "outsider": outsider,
            "agent": agent, "other_agent": other_agent, "list": lst, "report": report, "other_report": other_report}


def _url(world, suffix=""):
    return f"/api/data_sources/{world['agent']['id']}/lists{suffix}"


def _rows(test_client, world, actor="admin", **params):
    r = test_client.get(_url(world, f"/{world['list']['id']}/rows"), params=params,
                        headers=_h(world[actor]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


def _fid(world, name):
    return next(f["id"] for f in world["list"]["fields"] if f["name"] == name)


# ── S1: CRUD + RBAC ────────────────────────────────────────────────────────

def test_create_list_returns_stable_names_and_ids(world):
    lst = world["list"]
    assert lst["slug"] == "contracts"
    assert lst["tool_name"] == "submit_contracts"
    assert lst["table_name"].startswith("bow.sales_ops_") and lst["table_name"].endswith(".lists.contracts")
    assert lst["key_field"] == "counterparty"
    assert all(f["id"] for f in lst["fields"]) and len({f["id"] for f in lst["fields"]}) == len(lst["fields"])
    assert lst["version"] == 1 and lst["row_count"] == 0


@pytest.mark.parametrize("payload,path_hint", [
    ({"name": "Bad", "fields": [{"name": "Not Snake", "type": "string"}]}, "name"),
    ({"name": "Bad", "fields": [{"name": "c", "type": "enum", "enum": []}]}, "enum"),
    ({"name": "Bad", "fields": [{"name": "a"}, {"name": "a"}]}, "duplicate"),
    ({"name": "Bad", "fields": [{"name": "a"}], "key_field": "zzz"}, "key_field"),
    ({"name": "Bad", "fields": []}, "fields"),
])
def test_invalid_schema_is_422(test_client, world, payload, path_hint):
    r = test_client.post(_url(world), json=payload, headers=_h(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 422
    assert path_hint in r.text


def test_duplicate_name_conflicts_and_cap_is_enforced(test_client, world):
    h = _h(world["admin"]["token"], world["org_id"])
    assert test_client.post(_url(world), json=_schema(name="contracts"), headers=h).status_code == 409
    for i in range(9):
        assert test_client.post(_url(world), json=_schema(name=f"L{i}"), headers=h).status_code == 201
    r = test_client.post(_url(world), json=_schema(name="one too many"), headers=h)
    assert r.status_code == 400


@pytest.mark.parametrize("actor,read,write", [
    ("admin", 200, True), ("manager", 200, True), ("viewer", 200, False), ("outsider", 403, False),
])
def test_access_follows_the_agent(test_client, world, actor, read, write):
    h = _h(world[actor]["token"], world["org_id"])
    lid = world["list"]["id"]
    assert test_client.get(_url(world), headers=h).status_code == read
    assert test_client.get(_url(world, f"/{lid}"), headers=h).status_code == read
    assert test_client.get(_url(world, f"/{lid}/rows"), headers=h).status_code == read
    assert test_client.get(_url(world, f"/{lid}/rows.csv"), headers=h).status_code == read
    if read == 200:
        assert test_client.get(_url(world), headers=h).json()[0]["can_manage"] is write

    created = test_client.post(_url(world), json=_schema(name=f"New {actor}"), headers=h)
    assert (created.status_code == 201) is write
    upd = test_client.put(_url(world, f"/{lid}"), json=_schema(), headers=h)
    assert (upd.status_code == 200) is write
    if not write:
        assert created.status_code == 403 and upd.status_code == 403
        assert test_client.delete(_url(world, f"/{lid}"), headers=h).status_code == 403


def test_schema_edit_reports_change_kind_and_bumps_version_only_when_breaking(test_client, world):
    h = _h(world["admin"]["token"], world["org_id"])
    lid = world["list"]["id"]
    fields = [dict(f) for f in world["list"]["fields"]]
    base = {"name": "Contracts", "description": "Customer contracts", "key_field": "counterparty"}

    same = test_client.put(_url(world, f"/{lid}"), json={**base, "fields": fields}, headers=h).json()
    assert same["change"] == "none" and same["version"] == 1

    added = fields + [{"name": "notes", "type": "string"}]
    r = test_client.put(_url(world, f"/{lid}"), json={**base, "fields": added}, headers=h).json()
    assert r["change"] == "additive" and r["version"] == 1

    retyped = [dict(f, type="string") if f["name"] == "annual_value" else f for f in r["fields"]]
    r = test_client.put(_url(world, f"/{lid}"), json={**base, "fields": retyped}, headers=h).json()
    assert r["change"] == "breaking" and r["version"] == 2


def test_deleted_list_disappears_and_its_tool_is_not_registered(test_client, world):
    from app.ai.tools.list_tool_registry import build_list_tools
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    h = _h(world["admin"]["token"], world["org_id"])
    lid = world["list"]["id"]
    assert test_client.delete(_url(world, f"/{lid}"), headers=h).status_code == 204
    assert test_client.get(_url(world, f"/{lid}"), headers=h).status_code == 404
    assert test_client.get(_url(world), headers=h).json() == []

    async def _fn(db, maker):
        report = await db.get(Report, world["report"]["id"])
        user = await db.get(User, world["admin"]["user_id"])
        org = await db.get(Organization, world["org_id"])
        return await build_list_tools(db, report, user, org)
    descriptors, routing = run_async(_fn)
    assert descriptors == [] and routing == {}


# ── S3: runtime registration ───────────────────────────────────────────────

def _build(world, actor="admin", report_key="report"):
    from app.ai.tools.list_tool_registry import build_list_tools
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    async def _fn(db, maker):
        report = await db.get(Report, world[report_key]["id"])
        user = await db.get(User, world[actor]["user_id"])
        org = await db.get(Organization, world["org_id"])
        return await build_list_tools(db, report, user, org)
    return run_async(_fn)


def test_registry_exposes_one_native_tool_per_visible_list(world):
    import json
    from app.models.agent_list import AgentList
    from app.services.agent_lists.compiler import compile_list_schema

    d1, routing = _build(world)
    assert [d["name"] for d in d1] == ["submit_contracts"]
    assert routing["submit_contracts"]["list_id"] == world["list"]["id"]
    assert d1[0]["schema"] == compile_list_schema(world["list"]["fields"], "Customer contracts")
    d2, _ = _build(world)
    assert json.dumps(d1) == json.dumps(d2)  # prompt-cache invariant: byte-identical
    assert _build(world, report_key="other_report")[0] == []
    assert _build(world, actor="outsider")[0] == []


def test_same_slug_on_two_agents_gets_distinct_names(test_client, world, create_report):
    h = _h(world["admin"]["token"], world["org_id"])
    r = test_client.post(f"/api/data_sources/{world['other_agent']['id']}/lists", json=_schema(), headers=h)
    assert r.status_code == 201
    both = create_report(title="Both", user_token=world["admin"]["token"], org_id=world["org_id"],
                         data_sources=[world["agent"]["id"], world["other_agent"]["id"]])
    world["both"] = both
    descriptors, routing = _build(world, report_key="both")
    names = [d["name"] for d in descriptors]
    assert len(names) == 2 and len(set(names)) == 2
    assert all(n.startswith("submit_contracts") and len(n) <= 64 for n in names)
    assert {v["list_id"] for v in routing.values()} == {world["list"]["id"], r.json()["id"]}


# ── S3: submissions ────────────────────────────────────────────────────────

def test_valid_submission_creates_rows_with_provenance(test_client, world):
    call_id = str(uuid.uuid4())
    out = submit(world, [_record(), _record(cp="Globex", value=5000.5, currency="EUR", renewal=None)],
                 tool_call_id=call_id)
    assert out["output"]["success"] is True
    assert out["output"]["inserted"] == 2
    page = _rows(test_client, world)
    assert page["total"] == 2
    row = next(r for r in page["rows"] if r["values"][_fid(world, "counterparty")]["value"] == "Acme Ltd")
    assert row["report_id"] == world["report"]["id"] and row["schema_version"] == 1
    assert row["values"][_fid(world, "annual_value")]["value"] == 120000
    globex = next(r for r in page["rows"] if r["key_value"] == "globex")
    assert globex["values"][_fid(world, "renewal_date")]["status"] == "not_found"


@pytest.mark.parametrize("bad,path", [
    ({"annual_value": _env("120k")}, "records.0.fields.annual_value"),
    ({"currency": _env("GBP")}, "records.0.fields.currency"),
    ({"renewal_date": _env("31/01/2027")}, "records.0.fields.renewal_date"),
    ({"annual_value": _env(None, "found")}, "records.0.fields.annual_value.status"),
    ({"annual_value": _env(10, "not_found")}, "records.0.fields.annual_value.status"),
    ({"counterparty": None}, "records.0.fields.counterparty.value"),
])
def test_invalid_submission_writes_nothing_and_names_the_path(test_client, world, bad, path):
    out = submit(world, [_record(**bad), _record(cp="Valid Co")])
    assert out["output"]["success"] is False
    assert any(e.startswith(path) for e in out["output"]["errors"]), out["output"]["errors"]
    assert _rows(test_client, world)["total"] == 0


def test_same_key_updates_and_keyless_lists_append(test_client, world):
    submit(world, [_record(value=100)])
    out = submit(world, [_record(cp="  ACME   ltd ", value=200)])
    assert out["output"]["updated"] == 1 and out["output"]["inserted"] == 0
    page = _rows(test_client, world)
    assert page["total"] == 1
    assert page["rows"][0]["values"][_fid(world, "annual_value")]["value"] == 200
    assert page["rows"][0]["row_version"] == 2

    # Duplicate key inside one call is rejected, nothing written.
    out = submit(world, [_record(cp="Dup"), _record(cp="dup")])
    assert out["output"]["success"] is False
    assert _rows(test_client, world)["total"] == 1

    h = _h(world["admin"]["token"], world["org_id"])
    keyless = test_client.post(_url(world), json=_schema(name="Mentions", key=None), headers=h).json()
    submit(world, [_record()], list_id=keyless["id"])
    submit(world, [_record()], list_id=keyless["id"])
    r = test_client.get(_url(world, f"/{keyless['id']}/rows"), headers=h).json()
    assert r["total"] == 2


def test_resubmitting_the_same_facts_refreshes_evidence_without_a_revision(test_client, world):
    submit(world, [_record(counterparty=_env("Acme Ltd", quote="between Northwind and Acme Ltd"))])
    out = submit(world, [_record(counterparty=_env("Acme Ltd", quote="Customer: Acme Ltd"))])
    assert out["output"]["updated"] == 0 and out["output"]["unchanged"] == 1
    row = _rows(test_client, world)["rows"][0]
    assert row["values"][_fid(world, "counterparty")]["evidence"][0]["quote"] == "Customer: Acme Ltd"
    h = _h(world["admin"]["token"], world["org_id"])
    revs = test_client.get(_url(world, f"/{world['list']['id']}/rows/{row['id']}/revisions"), headers=h).json()
    assert [r["action"] for r in revs] == ["insert"]


def test_row_id_update_is_partial_and_scoped_to_the_list(test_client, world):
    submit(world, [_record()])
    row = _rows(test_client, world)["rows"][0]
    out = submit(world, [{"row_id": row["id"], "fields": {"renewal_date": _env("2028-12-31"),
                                                          "counterparty": None, "annual_value": None,
                                                          "currency": None, "auto_renew": None}}])
    assert out["output"]["updated"] == 1
    after = _rows(test_client, world)["rows"][0]
    assert after["values"][_fid(world, "renewal_date")]["value"] == "2028-12-31"
    assert after["values"][_fid(world, "annual_value")]["value"] == 120000  # untouched

    h = _h(world["admin"]["token"], world["org_id"])
    other = test_client.post(_url(world), json=_schema(name="Other list"), headers=h).json()
    out = submit(world, [{"row_id": row["id"], "fields": {"counterparty": None, "annual_value": None,
                                                          "currency": None, "renewal_date": None,
                                                          "auto_renew": None}}], list_id=other["id"])
    assert out["output"]["success"] is False and any("row_id" in e for e in out["output"]["errors"])


def test_require_evidence_rejects_extracted_values_without_quotes(test_client, world):
    h = _h(world["admin"]["token"], world["org_id"])
    strict = test_client.post(_url(world), json=_schema(name="Strict", require_evidence=True), headers=h).json()
    out = submit(world, [_record()], list_id=strict["id"])
    assert out["output"]["success"] is False
    assert any(".evidence" in e for e in out["output"]["errors"])
    rec = _record(counterparty=_env("Acme Ltd", quote="Acme Ltd"), renewal_date=_env("2027-01-31", quote="31 Jan 2027"),
                  annual_value=_env(120000, "inferred"))
    # string/date fields are 'extract' by default and now carry quotes; enum/boolean are 'classify'.
    assert submit(world, [rec], list_id=strict["id"])["output"]["success"] is True


def test_list_not_attached_to_report_is_refused(world):
    out = submit(world, [_record()], report_key="other_report")
    assert out["output"]["success"] is False
    assert _rows_count_via_db(world) == 0


def _rows_count_via_db(world):
    from sqlalchemy import func, select
    from app.models.agent_list import AgentListRow

    async def _fn(db, maker):
        return (await db.execute(select(func.count()).select_from(AgentListRow)
                                 .where(AgentListRow.list_id == world["list"]["id"]))).scalar_one()
    return run_async(_fn)


def test_quotes_are_verified_against_text_read_in_the_report(test_client, world, seed_agent_executions):
    seed_agent_executions(world["org_id"], world["report"]["id"], [{
        "user_id": world["admin"]["user_id"], "prompt": "read", "status": "success",
        "tools": [{"name": "read_file", "status": "success", "arguments": {"file_id": "f-1"},
                   "result_json": {"file_id": "f-1", "file_name": "msa_acme.pdf",
                                   "text": "This Master Agreement ... an annual fee of USD 120,000\nexcluding VAT."}}],
    }])
    rec = _record(annual_value=_env(120000, quote="annual fee of USD 120,000 excluding VAT", ref="msa_acme.pdf", page=1),
                  counterparty=_env("Acme Ltd", quote="signed by the Moon Council", ref="msa_acme.pdf"))
    out = submit(world, [rec])
    assert out["output"]["success"] is True
    assert len(out["observation"]["unverified_quotes"]) == 1
    row = _rows(test_client, world)["rows"][0]
    assert row["values"][_fid(world, "annual_value")]["evidence"][0]["verified"] is True
    assert row["values"][_fid(world, "counterparty")]["evidence"][0]["verified"] is False


# ── S6: human edits, locks, revisions ──────────────────────────────────────

def _patch(test_client, world, row, fields, actor="admin", unlock=None, version=None):
    return test_client.patch(
        _url(world, f"/{world['list']['id']}/rows/{row['id']}"),
        json={"row_version": row["row_version"] if version is None else version, "fields": fields,
              "unlock": unlock or []},
        headers=_h(world[actor]["token"], world["org_id"]),
    )


def test_human_edit_locks_field_and_agent_cannot_overwrite_it(test_client, world):
    submit(world, [_record(value=120000)])
    row = _rows(test_client, world)["rows"][0]
    r = _patch(test_client, world, row, {"annual_value": "130,000"}, actor="manager")
    assert r.status_code == 200, r.text
    edited = r.json()
    fid = _fid(world, "annual_value")
    assert edited["values"][fid]["value"] == 130000 and edited["values"][fid]["source"] == "human"
    assert fid in edited["locked_fields"] and edited["row_version"] == row["row_version"] + 1

    out = submit(world, [_record(value=999, currency="EUR")])
    assert [s["field"] for s in out["observation"]["locked_fields_skipped"]] == ["annual_value"]
    after = _rows(test_client, world)["rows"][0]
    assert after["values"][fid]["value"] == 130000
    assert after["values"][_fid(world, "currency")]["value"] == "EUR"  # unlocked fields still update

    # Unlocking hands the field back to the agent.
    r = _patch(test_client, world, after, {}, unlock=["annual_value"])
    assert r.status_code == 200 and fid not in r.json()["locked_fields"]
    submit(world, [_record(value=999, currency="EUR")])
    assert _rows(test_client, world)["rows"][0]["values"][fid]["value"] == 999


def test_patch_rejects_stale_version_bad_values_and_viewers(test_client, world):
    submit(world, [_record()])
    row = _rows(test_client, world)["rows"][0]
    assert _patch(test_client, world, row, {"annual_value": 1}, version=row["row_version"] + 5).status_code == 409
    bad = _patch(test_client, world, row, {"currency": "GBP"})
    assert bad.status_code == 422 and "currency" in bad.text
    assert _patch(test_client, world, row, {"annual_value": 1}, actor="viewer").status_code == 403
    assert _rows(test_client, world)["rows"][0]["row_version"] == row["row_version"]


def test_every_change_writes_a_revision_and_revert_restores(test_client, world):
    submit(world, [_record(value=100)])
    row = _rows(test_client, world)["rows"][0]
    edited = _patch(test_client, world, row, {"annual_value": 150}).json()
    h = _h(world["admin"]["token"], world["org_id"])
    revs = test_client.get(_url(world, f"/{world['list']['id']}/rows/{row['id']}/revisions"), headers=h).json()
    assert [r["action"] for r in revs] == ["update", "insert"]
    assert revs[0]["actor_type"] == "user" and revs[1]["actor_type"] == "agent"

    viewer_revs = test_client.get(_url(world, f"/{world['list']['id']}/rows/{row['id']}/revisions"),
                                  headers=_h(world["viewer"]["token"], world["org_id"]))
    assert viewer_revs.status_code == 200
    denied = test_client.post(_url(world, f"/{world['list']['id']}/rows/{row['id']}/revisions/{revs[0]['id']}/revert"),
                              headers=_h(world["viewer"]["token"], world["org_id"]))
    assert denied.status_code == 403

    r = test_client.post(_url(world, f"/{world['list']['id']}/rows/{row['id']}/revisions/{revs[0]['id']}/revert"),
                         headers=h)
    assert r.status_code == 200
    assert r.json()["values"][_fid(world, "annual_value")]["value"] == 100
    assert r.json()["row_version"] == edited["row_version"] + 1
    revs = test_client.get(_url(world, f"/{world['list']['id']}/rows/{row['id']}/revisions"), headers=h).json()
    assert revs[0]["action"] == "revert" and len(revs) == 3


def test_delete_row_requires_manage(test_client, world):
    submit(world, [_record()])
    row = _rows(test_client, world)["rows"][0]
    u = _url(world, f"/{world['list']['id']}/rows/{row['id']}")
    assert test_client.delete(u, headers=_h(world["viewer"]["token"], world["org_id"])).status_code == 403
    assert test_client.delete(u, headers=_h(world["manager"]["token"], world["org_id"])).status_code == 204
    assert _rows(test_client, world)["total"] == 0


# ── S8: CSV ────────────────────────────────────────────────────────────────

def _csv(test_client, world, actor="admin", **params):
    r = test_client.get(_url(world, f"/{world['list']['id']}/rows.csv"), params=params,
                        headers=_h(world[actor]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))


def test_csv_exports_all_rows_in_field_order_safely(test_client, world, create_report):
    names = [f"Co {i}" for i in range(130)] + ["=HYPERLINK(\"http://evil\")", "חברת אלפא בע\"מ"]
    for i in range(0, len(names), 50):
        submit(world, [_record(cp=n, value=-5 if n.startswith("=") else i) for n in names[i:i + 50]])
    rows = _csv(test_client, world, actor="viewer")
    assert rows[0] == ["counterparty", "annual_value", "currency", "renewal_date", "auto_renew"]
    assert len(rows) - 1 == len(names)  # more than one grid page (100) — all rows streamed
    cps = [r[0] for r in rows[1:]]
    assert "'=HYPERLINK(\"http://evil\")" in cps
    assert "חברת אלפא בע\"מ" in cps
    assert any(r[1] == "-5" for r in rows[1:])  # plain negative numbers stay numbers

    with_status = _csv(test_client, world, include="status,evidence,provenance")
    assert with_status[0][:3] == ["counterparty", "counterparty__status", "counterparty__quote"]
    assert "_row_id" in with_status[0]


def test_csv_report_filter_returns_only_that_runs_rows(test_client, world, create_report):
    second = create_report(title="Second", user_token=world["admin"]["token"], org_id=world["org_id"],
                           data_sources=[world["agent"]["id"]])
    world["second"] = second
    submit(world, [_record(cp="First Co")])
    submit(world, [_record(cp="Second Co")], report_key="second")
    rows = _csv(test_client, world, report_id=second["id"])
    assert [r[0] for r in rows[1:]] == ["Second Co"]


# ── S7: bow.<agent>.lists.<list> ───────────────────────────────────────────

def _query(world, request, actor="admin"):
    from app.models.organization import Organization
    from app.models.user import User
    from app.schemas.bow_source_schema import BowQuery
    from app.services.agent_lists.bow_lists import query_list

    async def _fn(db, maker):
        user = await db.get(User, world[actor]["user_id"])
        org = await db.get(Organization, world["org_id"])
        return await query_list(db, org, user, BowQuery.model_validate(request))
    return run_async(_fn)


def test_bow_list_query_returns_typed_rows_and_aggregates(world):
    submit(world, [_record(cp="A", value=100, currency="USD"), _record(cp="B", value=50, currency="USD"),
                   _record(cp="C", value=7.5, currency="EUR", renewal=None)])
    df = _query(world, {"dataset": "list", "list_id": world["list"]["id"]}, actor="viewer")
    assert len(df) == 3
    assert {"counterparty", "annual_value", "currency__status", "_row_id", "_edited_by_human"} <= set(df.columns)
    assert df["annual_value"].sum() == pytest.approx(157.5)

    agg = _query(world, {"dataset": "list", "list_id": world["list"]["id"], "group_by": ["currency"],
                         "metrics": [{"op": "sum", "field": "annual_value", "name": "total"}]})
    totals = dict(zip(agg["currency"], agg["total"]))
    assert totals == {"USD": 150, "EUR": 7.5}

    filtered = _query(world, {"dataset": "list", "list_id": world["list"]["id"], "query": "currency:eur",
                              "columns": ["counterparty", "renewal_date__status"]})
    assert filtered.to_dict("records") == [{"counterparty": "C", "renewal_date__status": "not_found"}]


def test_bow_list_text_filters_match_substrings_case_insensitively(world):
    submit(world, [_record(cp="Globex GmbH"), _record(cp="Acme Ltd", currency="EUR")])
    df = _query(world, {"dataset": "list", "list_id": world["list"]["id"], "query": "counterparty:globex"})
    assert list(df["counterparty"]) == ["Globex GmbH"]
    df = _query(world, {"dataset": "list", "list_id": world["list"]["id"], "query": "auto_renew:true currency:usd"})
    assert list(df["counterparty"]) == ["Globex GmbH"]


def test_quotes_of_the_users_own_message_verify(test_client, world):
    from app.models.completion import Completion

    # Direct write: the only API that stores a user message also starts an
    # agent run (needs an LLM). The message row itself is all this checks.
    async def _fn(db, maker):
        db.add(Completion(report_id=world["report"]["id"], role="user", message_type="table",
                          prompt={"content": "Acme renewed until 2029-01-31, update the list"}, completion={},
                          model="x", user_id=world["admin"]["user_id"]))
        await db.commit()
    run_async(_fn)
    out = submit(world, [_record(renewal_date=_env("2029-01-31", quote="Acme renewed until 2029-01-31",
                                                   kind="other", ref="user message"))])
    assert out["observation"]["unverified_quotes"] == []


def test_bow_list_query_requires_agent_view_and_survives_renames(test_client, world):
    submit(world, [_record()])
    with pytest.raises(PermissionError):
        _query(world, {"dataset": "list", "list_id": world["list"]["id"]}, actor="outsider")

    by_name = _query(world, {"dataset": "list", "list": world["list"]["table_name"]})
    assert len(by_name) == 1

    h = _h(world["admin"]["token"], world["org_id"])
    fields = world["list"]["fields"]
    test_client.put(_url(world, f"/{world['list']['id']}"),
                    json={"name": "Renamed", "fields": fields, "key_field": "counterparty"}, headers=h)
    test_client.put(f"/api/data_sources/{world['agent']['id']}", json={"name": "Totally New"}, headers=h)
    assert len(_query(world, {"dataset": "list", "list_id": world["list"]["id"]})) == 1
    with pytest.raises(ValueError):
        _query(world, {"dataset": "list", "list": world["list"]["table_name"]})


def test_edited_value_is_what_analysis_reads(test_client, world):
    submit(world, [_record(value=10)])
    row = _rows(test_client, world)["rows"][0]
    _patch(test_client, world, row, {"annual_value": 42})
    df = _query(world, {"dataset": "list", "list_id": world["list"]["id"]})
    assert df.loc[0, "annual_value"] == 42 and bool(df.loc[0, "_edited_by_human"]) is True


def test_schema_context_advertises_list_tables_in_chat_mode_without_run_history(world):
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    from app.models.data_source import DataSource
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    async def _fn(db, maker):
        user = await db.get(User, world["viewer"]["user_id"])
        org = await db.get(Organization, world["org_id"])
        report = await db.get(Report, world["report"]["id"])
        ds = await db.get(DataSource, world["agent"]["id"])
        b = SchemaContextBuilder(db, [ds], org, report, user=user, mode="chat")
        await b._append_list_tables(sections := [], None, None, None)
        return sections
    sections = run_async(_fn)
    assert len(sections) == 1 and sections[0].info.id == "builtin:bow"
    names = [t.name for t in sections[0].tables]
    assert names == [world["list"]["table_name"]]
    assert not any(n in ("bow.runs", "bow.tool_calls") for n in names)
    assert world["list"]["id"] in sections[0].tables[0].description
    # The viewer can read the list but not write it: the description says so,
    # so the agent explains instead of hunting for a save tool it doesn't have.
    assert "READ-ONLY" in sections[0].tables[0].description


def test_bow_client_refuses_run_history_outside_training(world):
    from app.data_sources.clients.bow_client import BowClient

    async def _fn(db, maker):
        loop = asyncio.get_running_loop()
        client = BowClient(maker, loop, world["org_id"], world["admin"]["user_id"], world["report"]["id"], db,
                           allow_history=False)
        from app.schemas.bow_source_schema import BowQuery
        with pytest.raises(PermissionError):
            await client._execute(BowQuery.model_validate({"dataset": "runs"}))
        df = await client._execute(BowQuery.model_validate({"dataset": "list", "list_id": world["list"]["id"]}))
        return len(df)
    assert run_async(_fn) == 0


# ── S5: evals ──────────────────────────────────────────────────────────────

def test_eval_rules_can_assert_on_submitted_list_records(world, seed_agent_executions):
    from app.schemas.test_expectations import ExpectationsSpec
    from app.services.test_evaluation_service import TestEvaluationService

    args = {"list_id": world["list"]["id"], "records": [_record(value=120000, currency="USD")]}
    seed_agent_executions(world["org_id"], world["report"]["id"], [{
        "user_id": world["admin"]["user_id"], "prompt": "extract", "status": "success",
        "tools": [{"name": "submit_list", "status": "success", "arguments": args}],
    }])

    def rule(field, matcher):
        return {"type": "field", "target": {"category": "tool:submit_list", "field": field}, "matcher": matcher}

    spec = ExpectationsSpec.model_validate({"rules": [
        rule("records.0.fields.annual_value.value", {"type": "number.cmp", "op": "eq", "value": 120000}),
        rule("records.0.fields.currency.value", {"type": "text.equals", "value": "USD"}),
        rule("count", {"type": "number.cmp", "op": "eq", "value": 1}),
        rule("records.0.fields.annual_value.value", {"type": "number.cmp", "op": "eq", "value": 999}),
        rule("records.3.fields.currency.value", {"type": "text.equals", "value": "USD"}),
        {"type": "tool.calls", "tool": "submit_list", "min_calls": 1},
    ]})

    async def _fn(db, maker):
        svc = TestEvaluationService()
        snap = await svc.build_final_snapshot(db, world["report"]["id"])
        return await svc.evaluate_final(db, spec, snap, world["report"]["id"], "extract")
    status, result = run_async(_fn)
    outcomes = ["skipped" if r.status == "skipped" else ("pass" if r.ok else "fail") for r in result.rule_results]
    assert outcomes == ["pass", "pass", "pass", "fail", "fail", "pass"], outcomes
    assert status == "fail"


def test_concurrent_submissions_of_the_same_new_key_yield_one_row(test_client, world):
    """Two runs racing to insert the same key: the unique (list_id, key_value)
    index rejects the loser, which retries as an update — never two rows."""
    from app.models.agent_list import AgentList
    from app.services.agent_lists.records import apply_submission_with_retry

    async def _fn(db, maker):
        async def one(value):
            async with maker() as s:
                lst = await s.get(AgentList, world["list"]["id"])
                return await apply_submission_with_retry(
                    s, lst, [_record(cp="Race Co", value=value)],
                    report_id=world["report"]["id"], tool_execution_id=None, user_id=None, sources=[])
        return await asyncio.gather(one(1), one(2), return_exceptions=True)
    results = run_async(_fn)
    assert not any(isinstance(r, Exception) for r in results), results
    rows = _rows(test_client, world)["rows"]
    assert len(rows) == 1
    assert sorted(r["inserted"] + r["updated"] + r["unchanged"] for r in results) == [1, 1]


# ── Who may write through the agent ────────────────────────────────────────

def test_agent_submissions_follow_the_edit_rule_unless_the_list_opts_in(test_client, world, create_report):
    """Saving rows through the agent is an edit: only agent managers get the
    submit tool — unless the list opts in to submissions from anyone who can
    use (view) the agent."""
    for actor in ("viewer", "manager"):
        world[f"{actor}_report"] = create_report(title=f"{actor} chat", user_token=world[actor]["token"],
                                                 org_id=world["org_id"], data_sources=[world["agent"]["id"]])

    assert _build(world, actor="viewer", report_key="viewer_report")[0] == []
    assert [d["name"] for d in _build(world, actor="manager", report_key="manager_report")[0]] == ["submit_contracts"]
    out = submit(world, [_record()], actor="viewer", report_key="viewer_report")
    assert out["output"]["success"] is False and _rows_count_via_db(world) == 0
    assert submit(world, [_record()], actor="manager", report_key="manager_report")["output"]["success"] is True

    h = _h(world["admin"]["token"], world["org_id"])
    body = {"name": "Contracts", "description": "Customer contracts", "fields": world["list"]["fields"],
            "key_field": "counterparty", "allow_viewer_submissions": True}
    updated = test_client.put(_url(world, f"/{world['list']['id']}"), json=body, headers=h).json()
    assert updated["allow_viewer_submissions"] is True and updated["version"] == 1
    assert [d["name"] for d in _build(world, actor="viewer", report_key="viewer_report")[0]] == ["submit_contracts"]
    assert submit(world, [_record(cp="Viewer Co")], actor="viewer", report_key="viewer_report")["output"]["success"] is True


def test_shared_artifact_chat_never_writes_to_lists(world):
    from sqlalchemy import update
    from app.models.report import Report

    # Direct write: artifact_chat reports are created by ArtifactChatService from
    # a published artifact; only the report_type matters to this contract.
    async def _fn(db, maker):
        await db.execute(update(Report).where(Report.id == world["report"]["id"]).values(report_type="artifact_chat"))
        await db.commit()
    run_async(_fn)
    assert _build(world)[0] == []
    out = submit(world, [_record()])
    assert out["output"]["success"] is False and _rows_count_via_db(world) == 0
