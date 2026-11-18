"""E2E coverage for importing an agent from an export bundle
(POST /agents/import/preview, POST /agents/import).

The contract: a bundle downloaded from one environment becomes a NEW agent in
another, with its connections re-pointed by the caller, its active tables,
instructions (folders + table references), eval suites and saved queries
carried over — and nothing that already exists in the target is overwritten.

Lives under rbac/ for the multi-org ``bootstrap_admin`` and
``invite_user_to_org`` fixtures (a second org stands in for "a new
environment": none of the source org's connections, agents or users exist
there).
"""
import io
import json
import re
import uuid
import zipfile

import pytest
import yaml

from tests.e2e.rbac.conftest import CHINOOK_PATH


def _h(token: str, org_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _wait_indexed(test_client, connection_id, hdr):
    # Indexing runs in the background; observe its public state rather than
    # relying on timing (same pattern as test_sharepoint_2010).
    for _ in range(2000):
        r = test_client.get(f"/api/connections/{connection_id}/indexing", headers=hdr)
        assert r.status_code == 200, r.text
        state = r.json()
        if state and state.get("status") in ("completed", "failed", "cancelled"):
            assert state["status"] == "completed", state
            return
    pytest.fail("connection indexing did not finish")


def _tables(test_client, ds_id, hdr):
    r = test_client.get(f"/api/data_sources/{ds_id}/full_schema", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    return body.get("tables", body) if isinstance(body, dict) else body


def _post_bundle(test_client, path, data: bytes, hdr, options=None):
    return test_client.post(
        f"/api/agents/{path}",
        files={"file": ("agent-export.zip", data, "application/zip")},
        data={"options": json.dumps(options or {})},
        headers=hdr,
    )


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _sqlite_connection(test_client, create_connection, name, token, org_id):
    conn = create_connection(
        name=name, type="sqlite", config={"database": str(CHINOOK_PATH)},
        credentials={}, user_token=token, org_id=org_id,
    )
    _wait_indexed(test_client, conn["id"], _h(token, org_id))
    return conn


@pytest.fixture
def source_bundle(bootstrap_admin, sqlite_data_source, create_instruction,
                  create_global_entity, create_test_suite, test_client):
    """An agent with two chosen tables, a foldered + a table-scoped
    instruction, an eval suite and a saved query — exported through the real
    export route. Returns (zip bytes, source agent name, chosen table names)."""
    if not CHINOOK_PATH.exists():
        pytest.skip("chinook fixture missing")
    admin = bootstrap_admin("src")
    token, org_id = admin["token"], admin["org_id"]
    hdr = _h(token, org_id)
    name = f"Music {uuid.uuid4().hex[:6]}"
    ds = sqlite_data_source(name=name, user_token=token, org_id=org_id)
    _wait_indexed(test_client, ds["connections"][0]["id"], hdr)

    tables = _tables(test_client, ds["id"], hdr)
    names = sorted(t["name"] for t in tables)
    assert len(names) > 3, names
    chosen = [n for n in names if n.lower() in ("album", "artist")] or names[:2]
    r = test_client.put(
        f"/api/data_sources/{ds['id']}/update_tables_status",
        json={"activate": chosen, "deactivate": [n for n in names if n not in chosen]},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    album = next(t for t in _tables(test_client, ds["id"], hdr) if t["name"] == chosen[0])

    # A table-scoped instruction (carries `references` in its frontmatter).
    r = test_client.post(
        "/api/instructions",
        json={
            "text": "Album titles are unique per artist.", "status": "published",
            "category": "data_modeling", "data_source_ids": [ds["id"]],
            "references": [{"object_type": "datasource_table", "object_id": album["id"]}],
        },
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    foldered = create_instruction(
        text="Revenue is sum of invoice totals.", category="general",
        data_source_ids=[ds["id"]], status="published", user_token=token, org_id=org_id,
    )
    d = test_client.post(
        "/api/instructions/directories",
        json={"name": "Measures", "data_source_id": ds["id"]}, headers=hdr,
    )
    assert d.status_code == 200, d.text
    p = test_client.put(
        f"/api/instructions/{foldered['id']}/directory",
        json={"directory_id": d.json()["id"], "data_source_id": ds["id"]}, headers=hdr,
    )
    assert p.status_code == 200, p.text

    # A suite homed on the agent (what the per-agent evals list and the export
    # select), then filled with a case through the YAML import.
    create_test_suite(name="Music smoke", data_source_id=ds["id"],
                      user_token=token, org_id=org_id)
    suite_yaml = yaml.safe_dump({
        "name": "Music smoke", "data_source_slugs": [name],
        "cases": [{"name": "count albums", "prompt": {"content": "How many albums?"}}],
    })
    r = test_client.post("/api/tests/suites/import", content=suite_yaml,
                         headers={**hdr, "Content-Type": "application/x-yaml"})
    assert r.status_code == 200, r.text

    create_global_entity(
        title="Top artists", slug=f"top-artists-{uuid.uuid4().hex[:4]}",
        code="select * from Artist limit 10", status="published",
        data_source_ids=[ds["id"]], user_token=token, org_id=org_id,
    )

    r = test_client.get(f"/api/data_sources/{ds['id']}/instructions/export", headers=hdr)
    assert r.status_code == 200, r.text
    return r.content, name, chosen, ds["connections"][0]["name"]


@pytest.mark.e2e
def test_exported_agent_imports_into_another_environment(
    source_bundle, bootstrap_admin, create_connection, test_client,
):
    bundle, src_name, chosen, src_conn_name = source_bundle
    target = bootstrap_admin("dst")
    token, org_id = target["token"], target["org_id"]
    hdr = _h(token, org_id)
    # A differently-named connection to the same database: the new environment.
    conn = _sqlite_connection(test_client, create_connection, "Warehouse", token, org_id)
    assert conn["name"] != src_conn_name

    # Preview with no mapping: the only connection here is suggested, and every
    # chosen table is found on it.
    pv = _post_bundle(test_client, "import/preview", bundle, hdr)
    assert pv.status_code == 200, pv.text
    preview = pv.json()
    assert preview["agent_name"] == src_name and preview["name_taken"] is False
    [c] = preview["connections"]
    assert c["name"] == src_conn_name
    assert c["mapped_connection_id"] == conn["id"] and c["suggested"] is True
    assert c["tables_total"] == len(chosen) and c["tables_found"] == len(chosen)
    assert preview["counts"]["instructions"] == 2
    assert preview["counts"]["folders"] == 1
    assert preview["counts"]["evals"] == 1 and preview["counts"]["queries"] == 1

    r = _post_bundle(test_client, "import", bundle, hdr,
                     {"connection_map": {src_conn_name: conn["id"]}})
    assert r.status_code == 200, r.text
    result = r.json()
    ds_id = result["data_source_id"]
    assert result["name"] == src_name
    assert result["instructions_created"] == 2 and result["instructions_failed"] == 0
    assert result["references_resolved"] == 1 and result["references_unresolved"] == 0
    assert result["evals_created"] == 1 and result["queries_created"] == 1
    assert result["tables_missing"] == 0

    # Exactly the chosen tables are active on the new agent.
    active = sorted(t["name"] for t in _tables(test_client, ds_id, hdr) if t.get("is_active"))
    assert active == sorted(chosen)

    # Instructions land on the new agent as drafts (default) and keep folders.
    li = test_client.get(
        "/api/instructions",
        params={"data_source_ids": ds_id, "include_global": False, "include_drafts": True},
        headers=hdr,
    )
    assert li.status_code == 200, li.text
    items = li.json()["items"] if isinstance(li.json(), dict) else li.json()
    assert len(items) == 2
    assert {i["status"] for i in items} == {"draft"}
    # Untitled instructions stay untitled — the export names their files by
    # id, and that id must not leak in as a title.
    uuid_like = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    assert not any(uuid_like.match(i.get("title") or "") for i in items), items
    assert {i["text"] for i in items} == {
        "Album titles are unique per artist.", "Revenue is sum of invoice totals."}
    tree = test_client.get("/api/instructions/directories",
                           params={"data_source_id": ds_id}, headers=hdr).json()
    assert [d["name"] for d in tree["directories"]] == ["measures"]
    assert len(tree["placements"]) == 1

    # The eval suite now targets the imported agent.
    suites = test_client.get("/api/tests/suites", params={"data_source_id": ds_id}, headers=hdr)
    assert suites.status_code == 200, suites.text
    assert [s["name"] for s in suites.json()] == ["Music smoke"]


@pytest.mark.e2e
def test_import_never_overwrites_existing_agents_or_suites(
    source_bundle, bootstrap_admin, create_connection, test_client,
):
    bundle, src_name, _chosen, src_conn_name = source_bundle
    target = bootstrap_admin("dst")
    hdr = _h(target["token"], target["org_id"])
    conn = _sqlite_connection(test_client, create_connection, "Warehouse",
                              target["token"], target["org_id"])
    opts = {"connection_map": {src_conn_name: conn["id"]}}
    first = _post_bundle(test_client, "import", bundle, hdr, opts)
    assert first.status_code == 200, first.text

    # Same name again → typed 409, and the preview offers a free name.
    again = _post_bundle(test_client, "import", bundle, hdr, opts)
    assert again.status_code == 409, again.text
    assert again.json()["error_code"] == "agent_import.name_taken"
    pv = _post_bundle(test_client, "import/preview", bundle, hdr).json()
    assert pv["name_taken"] is True and pv["suggested_name"] != src_name

    # Importing under the suggested name creates a second agent; its suite gets
    # a distinct name instead of upserting into the first agent's suite.
    second = _post_bundle(test_client, "import", bundle, hdr,
                          {**opts, "name": pv["suggested_name"]})
    assert second.status_code == 200, second.text
    assert second.json()["data_source_id"] != first.json()["data_source_id"]
    names = [s["name"] for s in test_client.get(
        "/api/tests/suites", params={"limit": 100}, headers=hdr).json()
        if s["name"].startswith("Music smoke")]
    assert len(names) == 2 and len(set(names)) == 2, names
    # The first import's suite kept its name (and its home agent).
    first_suites = test_client.get(
        "/api/tests/suites",
        params={"data_source_id": first.json()["data_source_id"]}, headers=hdr).json()
    assert [s["name"] for s in first_suites] == ["Music smoke"]


@pytest.mark.e2e
def test_import_rejects_bad_bundles_and_unmapped_connections(
    source_bundle, bootstrap_admin, test_client,
):
    bundle, _name, _chosen, src_conn_name = source_bundle
    target = bootstrap_admin("dst")
    hdr = _h(target["token"], target["org_id"])

    not_zip = _post_bundle(test_client, "import/preview", b"definitely not a zip", hdr)
    assert not_zip.status_code == 400
    assert not_zip.json()["error_code"] == "agent_import.invalid_bundle"

    no_manifest = _post_bundle(test_client, "import/preview",
                               _zip({"instructions/a.md": "hello"}), hdr)
    assert no_manifest.status_code == 400
    assert no_manifest.json()["error_code"] == "agent_import.invalid_bundle"

    # Every connection skipped → nothing to build the agent on.
    skipped = _post_bundle(test_client, "import", bundle, hdr,
                           {"connection_map": {src_conn_name: None}})
    assert skipped.status_code == 400
    assert skipped.json()["error_code"] == "agent_import.no_connection"


@pytest.mark.e2e
def test_import_leaves_out_what_the_target_does_not_have(
    bootstrap_admin, create_connection, test_client,
):
    """A hand-written bundle (the format is the contract, not just our export):
    unknown members are dropped, missing tables are reported, a bundle nested
    under one folder is accepted, and imported instructions can be published."""
    if not CHINOOK_PATH.exists():
        pytest.skip("chinook fixture missing")
    target = bootstrap_admin("dst")
    token, org_id = target["token"], target["org_id"]
    hdr = _h(token, org_id)
    conn = _sqlite_connection(test_client, create_connection, "Lake", token, org_id)
    real = sorted(t["name"] for t in test_client.get(
        f"/api/connections/{conn['id']}/tables", headers=hdr).json())
    assert real, "connection has no tables"

    agent = f"Handmade {uuid.uuid4().hex[:6]}"
    manifest = {
        "name": agent,
        "connections": ["old-db"],
        "tables": {"include": [f"old-db.{real[0]}", "old-db.no_such_table"]},
        "members": [{"user": "nobody@elsewhere.example"}],
        "conversation_starters": ["What sold best?"],
    }
    bundle = _zip({
        "export/agent.yaml": yaml.safe_dump(manifest),
        "export/instructions/rule.md": "---\ntitle: Rule\nstatus: published\n---\n\nUse UTC.\n",
    })

    pv = _post_bundle(test_client, "import/preview", bundle, hdr,
                      {"connection_map": {"old-db": conn["id"]}}).json()
    [c] = pv["connections"]
    assert c["tables_total"] == 2 and c["tables_found"] == 1
    assert c["missing_tables"] == ["no_such_table"]
    assert pv["members_unresolved"] == ["nobody@elsewhere.example"]

    r = _post_bundle(test_client, "import", bundle, hdr, {
        "connection_map": {"old-db": conn["id"]}, "publish_instructions": True,
    })
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["tables_active"] == 1 and res["tables_missing"] == 1
    assert any("nobody@elsewhere.example" in w for w in res["warnings"])

    items = test_client.get(
        "/api/instructions",
        params={"data_source_ids": res["data_source_id"], "include_global": False},
        headers=hdr,
    ).json()
    items = items["items"] if isinstance(items, dict) else items
    assert [i["status"] for i in items] == ["published"]


@pytest.mark.e2e
def test_import_requires_agent_building_permission(
    source_bundle, bootstrap_admin, invite_user_to_org, create_connection, test_client,
):
    bundle, _name, _chosen, src_conn_name = source_bundle
    target = bootstrap_admin("dst")
    org_id = target["org_id"]
    conn = _sqlite_connection(test_client, create_connection, "Warehouse",
                              target["token"], org_id)
    member = invite_user_to_org(admin_token=target["token"], org_id=org_id, role="member")
    hdr = _h(member["token"], org_id)

    assert _post_bundle(test_client, "import/preview", bundle, hdr).status_code == 403
    r = _post_bundle(test_client, "import", bundle, hdr,
                     {"connection_map": {src_conn_name: conn["id"]}})
    assert r.status_code == 403, r.text
    # Nothing was created on the member's behalf.
    agents = test_client.get("/api/data_sources", headers=_h(target["token"], org_id)).json()
    assert agents == [] or all(a["name"] != _name for a in agents)
