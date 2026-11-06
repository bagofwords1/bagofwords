"""E2E coverage for per-agent instruction export (GET
/data_sources/{id}/instructions/export).

Lives under rbac/ so it can use the ``invite_user_to_org`` helper to exercise
the ``manage_instructions`` gate with a real non-admin member.
"""
import io
import zipfile

import pytest


def _headers(token: str, org_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _make_agent_with_instructions(create_data_source, create_instruction, token, org_id, tmp_path):
    agent = create_data_source(
        name="Export Agent",
        type="network_dir",
        config={"root_path": str(tmp_path)},
        credentials={"auth_type": "none"},
        user_token=token,
        org_id=org_id,
    )
    aid = agent["id"]
    specs = [
        ("Always format dates as ISO 8601.", "general"),
        ("Prefer CTEs over nested subqueries.", "code_gen"),
        ("Use blue for revenue series.", "visualization"),
        ("Dashboards default to the last 30 days.", "dashboard"),
        ("orders.status is an enum.", "data_modeling"),
    ]
    for text, cat in specs:
        create_instruction(
            text=text, category=cat, data_source_ids=[aid],
            status="published", user_token=token, org_id=org_id,
        )
    return aid, specs


@pytest.mark.e2e
def test_export_downloads_all_agent_instructions_as_markdown_zip(
    create_data_source, create_instruction, create_global_instruction,
    create_user, login_user, whoami, test_client, tmp_path,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    aid, specs = _make_agent_with_instructions(
        create_data_source, create_instruction, token, org_id, tmp_path
    )
    # A global (agent-less) instruction must NOT leak into a per-agent export.
    create_global_instruction(
        text="Org-wide rule", user_token=token, org_id=org_id, status="published"
    )

    resp = test_client.get(
        f"/api/data_sources/{aid}/instructions/export", headers=_headers(token, org_id)
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert '-agent-export.zip"' in resp.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = zf.namelist()

    # Instructions live under instructions/*.md — exactly the five agent
    # instructions, the global one excluded.
    md_names = [n for n in names if n.startswith("instructions/") and n.endswith(".md")]
    assert len(md_names) == len(specs), names

    dump = "\n\n".join(zf.read(n).decode("utf-8") for n in md_names)
    # Every category is represented in a YAML frontmatter block.
    for _text, cat in specs:
        assert f"category: {cat}" in dump, f"missing category {cat}"
    # Bodies made it through.
    assert "Prefer CTEs over nested subqueries." in dump
    # The global instruction's text is absent.
    assert "Org-wide rule" not in dump
    # Each instruction file has a frontmatter fence.
    for n in md_names:
        assert zf.read(n).decode("utf-8").startswith("---\n")

    # agent.yaml is included and is valid YAML naming this agent.
    assert "agent.yaml" in names, names
    import yaml as _yaml
    manifest = _yaml.safe_load(zf.read("agent.yaml").decode("utf-8"))
    assert manifest.get("name") == "Export Agent"


@pytest.mark.e2e
def test_export_handles_non_latin_agent_name(
    create_data_source, create_instruction,
    create_user, login_user, whoami, test_client, tmp_path,
):
    """A Hebrew/Arabic agent name must not 500 the download: response headers
    are latin-1, so the route sends an ASCII filename= fallback plus an RFC
    5987 UTF-8 filename*= carrying the real name."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    agent = create_data_source(
        name="סוכן מכירות",
        type="network_dir",
        config={"root_path": str(tmp_path)},
        credentials={"auth_type": "none"},
        user_token=token,
        org_id=org_id,
    )
    create_instruction(
        text="Hebrew agent rule", category="general",
        data_source_ids=[agent["id"]], status="published",
        user_token=token, org_id=org_id,
    )

    resp = test_client.get(
        f"/api/data_sources/{agent['id']}/instructions/export",
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    cd = resp.headers["content-disposition"]
    cd.encode("latin-1")  # header must be encodable — the original crash
    assert "filename*=UTF-8''" in cd
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert any(n.startswith("instructions/") for n in zf.namelist())


@pytest.mark.e2e
def test_export_includes_agent_saved_queries_as_yaml(
    create_data_source, create_instruction, create_global_entity,
    create_user, login_user, whoami, test_client, tmp_path,
):
    """Saved queries (Entities) scoped to the agent ride along in the bundle
    under queries/*.yaml — one readable file each, carrying the SQL and slug.
    A query attached to a *different* agent must not leak into this export."""
    import uuid
    import yaml as _yaml

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    aid, _specs = _make_agent_with_instructions(
        create_data_source, create_instruction, token, org_id, tmp_path
    )
    other = create_data_source(
        name="Other Agent", type="network_dir",
        config={"root_path": str(tmp_path)}, credentials={"auth_type": "none"},
        user_token=token, org_id=org_id,
    )

    # Two queries on our agent (one with a non-latin title to prove the YAML is
    # written readable, not \uXXXX-escaped), one on a different agent.
    mine = [
        ("Top customers", f"top-customers-{uuid.uuid4().hex[:6]}",
         "select * from customers order by spend desc"),
        ("הכנסות לפי חודש", f"revenue-by-month-{uuid.uuid4().hex[:6]}",
         "select month, sum(total) from invoices group by month"),
    ]
    for title, slug, code in mine:
        create_global_entity(
            title=title, slug=slug, code=code, status="published",
            data_source_ids=[aid], user_token=token, org_id=org_id,
        )
    create_global_entity(
        title="Not mine", slug=f"not-mine-{uuid.uuid4().hex[:6]}",
        code="select 1", status="published",
        data_source_ids=[other["id"]], user_token=token, org_id=org_id,
    )

    resp = test_client.get(
        f"/api/data_sources/{aid}/instructions/export", headers=_headers(token, org_id)
    )
    assert resp.status_code == 200, resp.text
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = zf.namelist()

    query_names = [n for n in names if n.startswith("queries/") and n.endswith(".yaml")]
    assert len(query_names) == len(mine), names

    docs = [_yaml.safe_load(zf.read(n).decode("utf-8")) for n in query_names]
    slugs = {d["slug"] for d in docs}
    assert {s for _t, s, _c in mine} == slugs
    # The other agent's query is absent.
    assert not any(d["title"] == "Not mine" for d in docs)
    # SQL bodies round-trip.
    codes = {d["code"] for d in docs}
    assert "select * from customers order by spend desc" in codes
    # Non-latin title is stored readable (allow_unicode), not escaped.
    dump = "\n".join(zf.read(n).decode("utf-8") for n in query_names)
    assert "הכנסות לפי חודש" in dump
    assert "\\u05" not in dump
    # The file is named after the readable TITLE, not the (often auto-generated,
    # e.g. step-<hash>) slug — queries promoted from a conversation have a
    # meaningful title but a cryptic slug.
    stems = {n.split("/")[-1][:-len(".yaml")] for n in query_names}
    assert "הכנסות-לפי-חודש" in stems, stems
    assert not any(s.startswith("revenue-by-month") for s in stems), stems


@pytest.mark.e2e
def test_export_includes_draft_and_archived_instructions_from_others(
    bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource,
    create_instruction, test_client,
):
    """The bundle must carry every instruction the agent's UI list shows — which
    includes draft/archived rows authored by *other* users: the /agents tree
    lists an agent with include_drafts + include_archived and its badge counts
    them the same way. An export that drops them reads as data loss (the "106 in
    the agent, 103 in the zip" report). The exporter's OWN drafts always ride
    along via include_own, so the gap only shows for others' / AI-authored rows,
    which is what this test exercises."""
    import uuid

    admin = bootstrap_admin()
    org_id = admin["org_id"]
    ds = sqlite_data_source(
        name=f"agent_{uuid.uuid4().hex[:6]}", user_token=admin["token"],
        org_id=org_id, is_public=True,
    )
    aid = ds["id"]

    # Admin's own published instruction — always exported.
    create_instruction(
        text="Published admin rule", category="general", data_source_ids=[aid],
        status="published", user_token=admin["token"], org_id=org_id,
    )

    # A second member who can manage the agent authors a draft and an archived
    # instruction. Those are "others" to the admin, so only include_drafts /
    # include_archived surface them in the admin's export.
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    grant_resource(
        resource_type="data_source", resource_id=aid, principal_type="user",
        principal_id=manager["user_id"], permissions=["manage"],
        user_token=admin["token"], org_id=org_id,
    )
    create_instruction(
        text="Draft member rule", category="general", data_source_ids=[aid],
        status="draft", user_token=manager["token"], org_id=org_id,
    )
    create_instruction(
        text="Archived member rule", category="general", data_source_ids=[aid],
        status="archived", user_token=manager["token"], org_id=org_id,
    )

    resp = test_client.get(
        f"/api/data_sources/{aid}/instructions/export",
        headers=_headers(admin["token"], org_id),
    )
    assert resp.status_code == 200, resp.text
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    dump = "\n\n".join(
        zf.read(n).decode("utf-8") for n in zf.namelist()
        if n.startswith("instructions/") and n.endswith(".md")
    )
    assert "Published admin rule" in dump
    assert "Draft member rule" in dump       # include_drafts
    assert "Archived member rule" in dump     # include_archived


@pytest.mark.e2e
def test_export_preserves_instruction_folder_layout(
    bootstrap_admin, sqlite_data_source, create_instruction, test_client,
):
    """Instructions nest under the same folders they sit in for the agent
    (InstructionDirectory tree), not a flat dump — so a round-tripped backup
    keeps the organization the customer built. An unfiled instruction stays at
    the archive root."""
    import uuid

    admin = bootstrap_admin()
    org_id = admin["org_id"]
    hdr = _headers(admin["token"], org_id)
    ds = sqlite_data_source(
        name=f"agent_{uuid.uuid4().hex[:6]}", user_token=admin["token"],
        org_id=org_id, is_public=True,
    )
    aid = ds["id"]

    foldered = create_instruction(
        text="Lives in a folder", category="general", data_source_ids=[aid],
        status="published", user_token=admin["token"], org_id=org_id,
    )
    create_instruction(
        text="Lives at the root", category="general", data_source_ids=[aid],
        status="published", user_token=admin["token"], org_id=org_id,
    )

    # Create a folder on the agent and move the first instruction into it.
    dir_resp = test_client.post(
        "/api/instructions/directories",
        json={"name": "Measures", "data_source_id": aid}, headers=hdr,
    )
    assert dir_resp.status_code == 200, dir_resp.text
    dir_id = dir_resp.json()["id"]
    place = test_client.put(
        f"/api/instructions/{foldered['id']}/directory",
        json={"directory_id": dir_id, "data_source_id": aid}, headers=hdr,
    )
    assert place.status_code == 200, place.text

    resp = test_client.get(
        f"/api/data_sources/{aid}/instructions/export", headers=hdr
    )
    assert resp.status_code == 200, resp.text
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = [n for n in zf.namelist()
             if n.startswith("instructions/") and n.endswith(".md")]

    nested = [n for n in names if n.startswith("instructions/measures/")]
    assert nested, names
    assert "Lives in a folder" in zf.read(nested[0]).decode("utf-8")

    # The unfiled instruction stays directly under instructions/ (one slash).
    root_level = [n for n in names if n.count("/") == 1]
    assert any("Lives at the root" in zf.read(n).decode("utf-8") for n in root_level)


@pytest.mark.e2e
def test_export_requires_manage_on_the_agent(
    create_data_source, create_instruction, invite_user_to_org,
    create_user, login_user, whoami, test_client, tmp_path,
    _rbac_relaxed_signup_flags,
):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    aid, _ = _make_agent_with_instructions(
        create_data_source, create_instruction, admin_token, org_id, tmp_path
    )
    url = f"/api/data_sources/{aid}/instructions/export"

    # Unauthenticated → 401.
    assert test_client.get(url).status_code == 401

    # A plain member (no `manage` on this private agent) → 403.
    member = invite_user_to_org(admin_token=admin_token, org_id=org_id, role="member")
    member_resp = test_client.get(url, headers=_headers(member["token"], org_id))
    assert member_resp.status_code == 403, member_resp.text

    # The admin (full_admin_access wildcard) → 200.
    admin_resp = test_client.get(url, headers=_headers(admin_token, org_id))
    assert admin_resp.status_code == 200, admin_resp.text

    # A nonexistent agent id → 404, not an empty zip (the admin wildcard passes
    # the permission decorator, so the service must reject the missing row).
    ghost = test_client.get(
        "/api/data_sources/00000000-0000-0000-0000-000000000000/instructions/export",
        headers=_headers(admin_token, org_id),
    )
    assert ghost.status_code == 404, ghost.text
