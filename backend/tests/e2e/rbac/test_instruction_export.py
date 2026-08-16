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
