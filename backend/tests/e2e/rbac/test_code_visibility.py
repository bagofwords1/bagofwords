"""End-to-end coverage for role-scoped generated-code visibility.

Two org permissions are under test:

  * ``view_code``       — see the SQL/Python the agent generated
  * ``run_custom_code`` — edit and execute caller-supplied code

The contract these tests defend is the one the feature exists for: an admin can
define a role that withholds code, and withholding it actually withholds it —
at the API, not merely in the UI. Before this feature the org-level
``enable_code_editing`` setting gated two Vue components and nothing else, so
the API happily executed caller-supplied SQL for anyone; that hole is the
reason the write-path tests below exist.
"""
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.widget import Widget

GENERATED_SQL = "SELECT region, SUM(revenue) FROM orders GROUP BY region"


async def _seed_step_for(user_id, org_id, code=GENERATED_SQL):
    """Seed a report + widget + query + step owned by ``user_id``.

    Direct DB writes (see tests/AGENTS.md rule 5): a Step with generated code is
    produced by the agent calling an LLM against a real data source, which an
    e2e test cannot do. Everything else here — users, orgs, memberships, roles —
    still goes through the real API.
    """
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = Report(
            title=f"CodeVis {suffix}", slug=f"codevis-{suffix}", status="draft",
            user_id=user_id, organization_id=org_id,
        )
        db.add(report)
        await db.flush()

        widget = Widget(title="W", slug=f"w-{suffix}", report_id=report.id)
        db.add(widget)
        await db.flush()

        query = Query(
            title="Q", report_id=report.id, widget_id=widget.id,
            organization_id=org_id, user_id=user_id,
        )
        db.add(query)
        await db.flush()

        step = Step(
            title="Step", slug=f"s-{suffix}", status="success",
            widget_id=widget.id, query_id=query.id, code=code,
            data={"rows": [{"region": "north", "revenue": 10}],
                  "columns": [{"field": "region"}, {"field": "revenue"}]},
            data_model={"type": "table", "columns": [{"field": "region"}]},
            view={"type": "table"},
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        await db.commit()
        return {"report_id": str(report.id), "query_id": str(query.id),
                "step_id": str(step.id)}


def _strip_code_role(test_client, admin, member, create_role, assign_role):
    """Leave the member holding ONLY a role without the code permissions.

    The existing `member` assignment has to go: RBAC unions across roles, so
    adding a restricted role next to `member` grants strictly more, never less.
    That is inherent to additive RBAC and is asserted on its own below.
    """
    org_id = admin["org_id"]
    role = create_role(
        name=f"viewer-{uuid.uuid4().hex[:6]}",
        permissions=["view_reports", "create_reports", "manage_files", "view_members"],
        user_token=admin["token"], org_id=org_id,
    )
    assert role.status_code == 200, role.text

    existing = test_client.get(
        f"/api/organizations/{org_id}/role-assignments",
        headers=_hdr(admin["token"], org_id),
        params={"principal_type": "user", "principal_id": member["user_id"]},
    )
    assert existing.status_code == 200, existing.text
    for assignment in existing.json():
        test_client.delete(
            f"/api/organizations/{org_id}/role-assignments/{assignment['id']}",
            headers=_hdr(admin["token"], org_id),
        )

    assigned = assign_role(
        role_id=role.json()["id"], principal_type="user",
        principal_id=member["user_id"], user_token=admin["token"], org_id=org_id,
    )
    assert assigned.status_code in (200, 201), assigned.text
    return role.json()["id"]


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _org(whoami, token, org_id):
    return next(o for o in whoami(token)["organizations"] if o["id"] == org_id)


def _perms(whoami, token, org_id):
    return set(_org(whoami, token, org_id)["permissions"])


# ── Defaults: nobody loses access by upgrading ───────────────────────────


@pytest.mark.e2e
def test_admin_holds_code_permissions_through_the_wildcard(
    test_client, bootstrap_admin, whoami
):
    """Admins need no explicit grant: full_admin_access already implies every
    org permission, so the seeded admin role stays a one-element list."""
    admin = bootstrap_admin()
    org = _org(whoami, admin["token"], admin["org_id"])
    assert "full_admin_access" in org["permissions"]


@pytest.mark.e2e
def test_member_holds_both_code_permissions_by_default(
    test_client, bootstrap_admin, invite_user_to_org, whoami
):
    """Default-on is the whole backward-compatibility story: an ordinary member
    must come out of the migration seeing code exactly as they did before."""
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])

    perms = _perms(whoami, member["token"], admin["org_id"])
    assert {"view_code", "run_custom_code"} <= perms


# ── The permission itself ────────────────────────────────────────────────


@pytest.mark.e2e
def test_run_custom_code_implies_view_code(
    test_client, bootstrap_admin, invite_user_to_org, whoami,
    create_role, assign_role, enterprise_license,
):
    """Editing code you may not read is not a coherent state, so the resolver
    closes the implication rather than leaving it to each call site."""
    admin = bootstrap_admin()
    role = create_role(
        name=f"coder-{uuid.uuid4().hex[:6]}",
        permissions=["run_custom_code"],
        user_token=admin["token"],
        org_id=admin["org_id"],
    )
    assert role.status_code == 200, role.text

    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    assign_role(
        role_id=role.json()["id"], principal_type="user",
        principal_id=member["user_id"], user_token=admin["token"],
        org_id=admin["org_id"],
    )

    assert "view_code" in _perms(whoami, member["token"], admin["org_id"])


@pytest.mark.e2e
def test_a_role_can_be_defined_without_code_permissions(
    test_client, bootstrap_admin, whoami, create_role, enterprise_license,
):
    """The point of the feature: an admin can author a restricted role at all."""
    admin = bootstrap_admin()
    resp = create_role(
        name=f"no-code-{uuid.uuid4().hex[:6]}",
        permissions=["view_reports"],
        user_token=admin["token"],
        org_id=admin["org_id"],
    )
    assert resp.status_code == 200, resp.text
    assert "view_code" not in resp.json()["permissions"]


# ── Registry contract ────────────────────────────────────────────────────


@pytest.mark.e2e
def test_code_permissions_are_offered_in_the_role_editor(
    test_client, bootstrap_admin
):
    """A permission absent from the registry response cannot be granted in the
    UI, which would make it withholdable in name only."""
    admin = bootstrap_admin()
    resp = test_client.get(
        "/api/permissions/registry", headers=_hdr(admin["token"], admin["org_id"])
    )
    assert resp.status_code == 200
    flat = {p for perms in resp.json()["categories"].values() for p in perms}
    assert {"view_code", "run_custom_code"} <= flat


@pytest.mark.e2e
def test_unknown_permission_strings_are_rejected(
    test_client, bootstrap_admin, create_role, enterprise_license,
):
    """Roles used to accept any string into a JSON column, so a typo produced a
    role that looked right and granted nothing."""
    admin = bootstrap_admin()
    resp = create_role(
        name=f"typo-{uuid.uuid4().hex[:6]}",
        permissions=["view_kode"],
        user_token=admin["token"],
        org_id=admin["org_id"],
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.e2e
def test_valid_permissions_are_still_accepted(
    test_client, bootstrap_admin, create_role, enterprise_license,
):
    """Guard against the validator being too strict — it must not reject the
    wildcard or any registered string."""
    admin = bootstrap_admin()
    resp = create_role(
        name=f"valid-{uuid.uuid4().hex[:6]}",
        permissions=["view_code", "run_custom_code", "manage_evals"],
        user_token=admin["token"],
        org_id=admin["org_id"],
    )
    assert resp.status_code == 200, resp.text


# ── Removing the org setting ─────────────────────────────────────────────


@pytest.mark.e2e
def test_enable_code_editing_setting_is_gone(
    test_client, bootstrap_admin
):
    """The setting was UI-only and is superseded by `run_custom_code`. It must
    disappear from the settings payload, not linger as a dead toggle an admin
    can flip with no effect."""
    admin = bootstrap_admin()
    resp = test_client.get(
        "/api/organization/settings", headers=_hdr(admin["token"], admin["org_id"])
    )
    assert resp.status_code == 200
    config = resp.json()["config"]
    assert "enable_code_editing" not in config
    assert "enable_code_editing" not in (config.get("ai_features") or {})


# ── The read path: what a restricted member actually receives ────────────


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_member_with_view_code_receives_the_generated_code(
    test_client, bootstrap_admin, invite_user_to_org
):
    """The permissive half of the contract. Asserted first and separately: if
    only the withheld case were covered, a serializer that returned None to
    *everyone* would pass the suite while breaking the product."""
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    seeded = await _seed_step_for(member["user_id"], admin["org_id"])

    resp = test_client.get(
        f"/api/queries/{seeded['query_id']}/default_step",
        headers=_hdr(member["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["step"]["code"] == GENERATED_SQL


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_member_without_view_code_receives_no_code(
    test_client, bootstrap_admin, invite_user_to_org, create_role, assign_role,
    enterprise_license,
):
    """The restrictive half: the code must be absent from the PAYLOAD, not
    merely hidden by the client. This is the assertion the whole feature turns
    on — the previous org setting passed the equivalent UI check and failed
    this one."""
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    seeded = await _seed_step_for(member["user_id"], admin["org_id"])
    _strip_code_role(test_client, admin, member, create_role, assign_role)

    resp = test_client.get(
        f"/api/queries/{seeded['query_id']}/default_step",
        headers=_hdr(member["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    step = resp.json()["step"]
    assert step["code"] is None
    # The rest of the step still works — withholding code must not cost the
    # user the answer itself.
    assert step["data"]["rows"], "restricting code must not withhold the data"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_withheld_code_is_absent_from_the_serialized_body(
    test_client, bootstrap_admin, invite_user_to_org, create_role, assign_role,
    enterprise_license,
):
    """Belt to the braces above: assert the SQL string appears nowhere in the
    raw response, so a second field echoing it somewhere else still fails."""
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    seeded = await _seed_step_for(member["user_id"], admin["org_id"])
    _strip_code_role(test_client, admin, member, create_role, assign_role)

    resp = test_client.get(
        f"/api/queries/{seeded['query_id']}/default_step",
        headers=_hdr(member["token"], admin["org_id"]),
    )
    assert "SUM(revenue)" not in resp.text


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_permissions_union_across_roles_so_member_role_must_be_removed(
    test_client, bootstrap_admin, invite_user_to_org, create_role, assign_role,
    whoami, enterprise_license,
):
    """Documents a sharp edge admins will hit: RBAC unions permissions, so
    ADDING a restricted role withholds nothing while `member` is still held.
    Asserting it here stops a future 'fix' from silently making roles
    subtractive."""
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])

    role = create_role(
        name=f"restricted-{uuid.uuid4().hex[:6]}", permissions=["view_reports"],
        user_token=admin["token"], org_id=admin["org_id"],
    )
    assign_role(
        role_id=role.json()["id"], principal_type="user",
        principal_id=member["user_id"], user_token=admin["token"],
        org_id=admin["org_id"],
    )

    # Still holds view_code — via the member role they also still have.
    assert "view_code" in _perms(whoami, member["token"], admin["org_id"])


# ── The write path: the hole the old org setting left open ───────────────


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_member_without_run_custom_code_cannot_execute_supplied_code(
    test_client, bootstrap_admin, invite_user_to_org, create_role, assign_role,
    enterprise_license,
):
    """Builder-mode run takes caller-supplied SQL. Under the old org setting
    this route accepted it from anyone; now it is a permission check."""
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    seeded = await _seed_step_for(member["user_id"], admin["org_id"])
    _strip_code_role(test_client, admin, member, create_role, assign_role)

    resp = test_client.post(
        f"/api/queries/{seeded['query_id']}/run",
        json={"code": "DROP TABLE orders", "mode": "builder"},
        headers=_hdr(member["token"], admin["org_id"]),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_member_without_run_custom_code_cannot_preview_supplied_code(
    test_client, bootstrap_admin, invite_user_to_org, create_role, assign_role,
    enterprise_license,
):
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    seeded = await _seed_step_for(member["user_id"], admin["org_id"])
    _strip_code_role(test_client, admin, member, create_role, assign_role)

    resp = test_client.post(
        f"/api/queries/{seeded['query_id']}/preview",
        json={"code": "SELECT 1", "mode": "builder"},
        headers=_hdr(member["token"], admin["org_id"]),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_viewer_mode_run_stays_open_to_restricted_members(
    test_client, bootstrap_admin, invite_user_to_org, create_role, assign_role,
    enterprise_license,
):
    """Viewer mode executes the query's ALREADY STORED step with parameter
    values — ordinary report usage, not custom code. Gating it would break
    dashboards for every restricted user, so it must not 403."""
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    seeded = await _seed_step_for(member["user_id"], admin["org_id"])
    _strip_code_role(test_client, admin, member, create_role, assign_role)

    resp = test_client.post(
        f"/api/queries/{seeded['query_id']}/run",
        json={"mode": "viewer", "params": {}},
        headers=_hdr(member["token"], admin["org_id"]),
    )
    assert resp.status_code != 403, resp.text


# ── The published-report path ────────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_signed_in_restricted_member_gets_no_code_from_the_public_route(
    test_client, bootstrap_admin, invite_user_to_org, create_role, assign_role,
    publish_report, enterprise_license,
):
    """The shared-link route is undecorated and takes an OPTIONAL user, so it
    never passes through requires_permission. Defaulting it to permissive made
    it a bypass: a signed-in member whose role withholds `view_code` could read
    the SQL simply by opening the published view of the same report.
    """
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    seeded = await _seed_step_for(member["user_id"], admin["org_id"])
    _strip_code_role(test_client, admin, member, create_role, assign_role)

    test_client.put(
        f"/api/reports/{seeded['report_id']}/visibility/artifact",
        json={"visibility": "internal"},
        headers=_hdr(member["token"], admin["org_id"]),
    )

    resp = test_client.get(
        f"/api/r/{seeded['report_id']}/queries/{seeded['query_id']}/step",
        headers=_hdr(member["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["code"] is None
    assert "SUM(revenue)" not in resp.text


# ── The monitoring console path ──────────────────────────────────────────


async def _seed_console_run_for(user_id, org_id, code=GENERATED_SQL):
    """Seed one agent run whose create_data call carries generated code.

    Direct DB writes (see tests/AGENTS.md rule 5): agent executions and their
    tool calls only come from a live agent run, which crosses the LLM boundary.
    """
    from datetime import datetime

    from app.models.agent_execution import AgentExecution
    from app.models.completion import Completion
    from app.models.completion_block import CompletionBlock
    from app.models.tool_execution import ToolExecution

    suffix = uuid.uuid4().hex[:8]
    now = datetime.utcnow()
    async with async_session_maker() as db:
        report = Report(
            title=f"ConsoleCode {suffix}", slug=f"console-code-{suffix}",
            status="draft", user_id=user_id, organization_id=org_id,
        )
        db.add(report)
        await db.flush()

        head = Completion(
            prompt={"content": "revenue by region"}, completion={"content": ""},
            role="user", message_type="user_message", report_id=report.id,
            user_id=user_id, created_at=now,
        )
        db.add(head)
        await db.flush()
        system = Completion(
            prompt={"content": ""}, completion={"content": "done"}, role="system",
            parent_id=head.id, report_id=report.id, created_at=now,
        )
        db.add(system)
        await db.flush()

        ae = AgentExecution(
            completion_id=system.id, organization_id=org_id, user_id=user_id,
            report_id=report.id, status="completed", created_at=now,
            started_at=now, completed_at=now,
        )
        db.add(ae)
        await db.flush()

        te = ToolExecution(
            agent_execution_id=ae.id, tool_name="create_data",
            arguments_json={"title": "Revenue"}, result_json={"code": code},
            status="success", success=True, started_at=now, completed_at=now,
            attempt_number=1, max_retries=0, created_at=now,
        )
        db.add(te)
        await db.flush()

        db.add(CompletionBlock(
            completion_id=system.id, agent_execution_id=ae.id, source_type="tool",
            tool_execution_id=te.id, block_index=0, title="Create data",
            status="completed",
        ))
        await db.commit()
        return {"report_id": str(report.id), "completion_id": str(system.id)}


def _console_trace_urls(seeded):
    return [
        f"/api/console/reports/{seeded['report_id']}/conversation",
        f"/api/console/agent_executions/by-completion/{seeded['completion_id']}",
    ]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_console_trace_shows_generated_code_to_a_code_viewer(
    test_client, bootstrap_admin
):
    """Console routes gate through `console_scope`, not `@requires_permission`.
    If that gate never publishes the code-visibility decision, the serializers
    fall back to deny and the monitoring trace loses its code for everyone —
    admins included."""
    admin = bootstrap_admin()
    seeded = await _seed_console_run_for(admin["user_id"], admin["org_id"])

    for url in _console_trace_urls(seeded):
        resp = test_client.get(url, headers=_hdr(admin["token"], admin["org_id"]))
        assert resp.status_code == 200, (url, resp.text)
        assert GENERATED_SQL in resp.text, url


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_console_trace_withholds_code_from_a_console_user_without_view_code(
    test_client, bootstrap_admin, invite_user_to_org, create_role, assign_role,
    enterprise_license,
):
    """Console access and code visibility are separate grants: opening the
    console must not become a way around a role that withholds code."""
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    seeded = await _seed_console_run_for(admin["user_id"], admin["org_id"])

    role_id = _strip_code_role(test_client, admin, member, create_role, assign_role)
    updated = test_client.put(
        f"/api/organizations/{admin['org_id']}/roles/{role_id}",
        json={"permissions": ["view_reports", "manage_settings"]},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert updated.status_code == 200, updated.text

    for url in _console_trace_urls(seeded):
        resp = test_client.get(url, headers=_hdr(member["token"], admin["org_id"]))
        assert resp.status_code == 200, (url, resp.text)
        assert "SUM(revenue)" not in resp.text, url
