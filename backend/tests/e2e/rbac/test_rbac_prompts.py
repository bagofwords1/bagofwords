"""
RBAC end-to-end coverage for /api/prompts writes.

The prompt routes have no org-level permission string; the write policy
depends on the request body's (scope, data_source_ids) pair, so — like the
instruction routes' check_resource_permissions pattern — it is enforced
imperatively via ``prompt_service.authorize_write`` in the route body (and
re-run inside the service as a backstop for the AI training tools that call
it directly):

    private → any member; every referenced data source must be VISIBLE to
              the author (public or explicit membership/grant), or none
    agent   → `manage` on every referenced agent (the grant that gates
              editing the agent itself)
    global  → full_admin only

Update re-authorizes against the POST-merge (scope, data_sources) whenever
either changes, closing the historical bypass where an owner could promote a
private prompt to agent/global scope or swap in agents they don't manage.

Run-for coverage: per-target eligibility (public data sources count as
accessible — the run_prompt_for fix), and the /run-for/targets endpoint the
picker uses so it only offers targets that would actually run.
"""
import pytest


@pytest.fixture
def stub_completion(monkeypatch):
    """No-op CompletionService.create_completion recorder — run-for tests only
    assert the fan-out bookkeeping, never a real agent/LLM run."""
    calls = []

    async def _fake(self, db, report_id, completion_data, current_user, organization, *a, **kw):
        calls.append({"report_id": str(report_id), "user_id": str(current_user.id)})
        return {"stubbed": True}

    from app.services.completion_service import CompletionService
    monkeypatch.setattr(CompletionService, "create_completion", _fake)
    return calls


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def prompts_world(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    grant_resource,
):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    # Two private data sources + one public one.
    ds_a = sqlite_data_source(name="pr_ds_a", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name="pr_ds_b", user_token=admin["token"], org_id=org_id)
    ds_pub = sqlite_data_source(
        name="pr_ds_pub", user_token=admin["token"], org_id=org_id, is_public=True
    )

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    ds_a_viewer = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    ds_a_manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    for principal, perms in ((ds_a_viewer, ["view"]), (ds_a_manager, ["manage"])):
        resp = grant_resource(
            resource_type="data_source",
            resource_id=ds_a["id"],
            principal_type="user",
            principal_id=principal["user_id"],
            permissions=perms,
            user_token=admin["token"],
            org_id=org_id,
        )
        assert resp.status_code == 200, resp.text

    return {
        "org_id": org_id,
        "ds_a": ds_a,
        "ds_b": ds_b,
        "ds_pub": ds_pub,
        "principals": {
            "admin": admin,
            "member": member,
            "ds_a_viewer": ds_a_viewer,
            "ds_a_manager": ds_a_manager,
        },
    }


def _create_prompt(test_client, world, principal_name, *, scope, ds_ids, text="hello"):
    token = world["principals"][principal_name]["token"]
    return test_client.post(
        "/api/prompts",
        json={"text": text, "scope": scope, "data_source_ids": ds_ids},
        headers=_hdr(token, world["org_id"]),
    )


def _update_prompt(test_client, world, principal_name, prompt_id, payload):
    token = world["principals"][principal_name]["token"]
    return test_client.put(
        f"/api/prompts/{prompt_id}",
        json=payload,
        headers=_hdr(token, world["org_id"]),
    )


# ────────────────────────────────────────────────────────────────────
# Create
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_create_private_prompt_data_source_visibility(test_client, prompts_world):
    """Private prompts: anyone may create, but only with data sources they
    can SEE (public counts) — or none at all."""
    ds_a = prompts_world["ds_a"]["id"]
    ds_b = prompts_world["ds_b"]["id"]
    ds_pub = prompts_world["ds_pub"]["id"]

    failures = []
    for principal, ds_ids, want in [
        ("member", [], 200),                 # no DS → always fine
        ("member", [ds_a], 403),             # not visible to plain member
        ("member", [ds_pub], 200),           # public DS is visible to all
        ("ds_a_viewer", [ds_a], 200),        # explicit membership
        ("ds_a_viewer", [ds_a, ds_b], 403),  # ALL must be visible
        ("ds_a_manager", [ds_a], 200),       # manage grant implies membership
        ("admin", [ds_a], 200),              # full_admin sees everything
    ]:
        resp = _create_prompt(test_client, prompts_world, principal, scope="private", ds_ids=ds_ids)
        if resp.status_code != want:
            failures.append(
                f"{principal} ds={ds_ids}: want {want} got {resp.status_code} ({resp.text[:160]})"
            )
    assert not failures, "\n".join(failures)


@pytest.mark.e2e
def test_create_agent_prompt_requires_manage(test_client, prompts_world):
    """Agent-scoped prompts: only principals holding `manage` on EVERY
    referenced agent (view/membership is not enough)."""
    ds_a = prompts_world["ds_a"]["id"]
    ds_b = prompts_world["ds_b"]["id"]

    failures = []
    for principal, ds_ids, want in [
        ("member", [ds_a], 403),
        ("ds_a_viewer", [ds_a], 403),          # view ≠ manage
        ("ds_a_manager", [ds_a], 200),
        ("ds_a_manager", [ds_a, ds_b], 403),   # manage required on ALL
        ("admin", [ds_b], 200),
        ("ds_a_manager", [], 400),             # agent scope needs ≥1 agent
    ]:
        resp = _create_prompt(test_client, prompts_world, principal, scope="agent", ds_ids=ds_ids)
        if resp.status_code != want:
            failures.append(
                f"{principal} ds={ds_ids}: want {want} got {resp.status_code} ({resp.text[:160]})"
            )
    assert not failures, "\n".join(failures)


@pytest.mark.e2e
def test_create_global_prompt_admin_only(test_client, prompts_world):
    for principal, want in [("member", 403), ("ds_a_manager", 403), ("admin", 200)]:
        resp = _create_prompt(test_client, prompts_world, principal, scope="global", ds_ids=[])
        assert resp.status_code == want, f"{principal}: {resp.status_code} {resp.text[:160]}"


@pytest.mark.e2e
def test_create_unknown_scope_rejected(test_client, prompts_world):
    """An unrecognized scope must 400 — it would otherwise skip the write
    policy entirely while behaving like an agent prompt for visibility."""
    resp = _create_prompt(test_client, prompts_world, "member", scope="weird", ds_ids=[])
    assert resp.status_code == 400, f"{resp.status_code} {resp.text[:160]}"


# ────────────────────────────────────────────────────────────────────
# Update — post-merge re-authorization (bypass regressions)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_update_cannot_promote_private_to_agent_without_manage(test_client, prompts_world):
    """A viewer may own a private prompt pinned to ds_a, but promoting it to
    agent scope requires `manage` on ds_a — this was the historical bypass."""
    ds_a = prompts_world["ds_a"]["id"]

    created = _create_prompt(test_client, prompts_world, "ds_a_viewer", scope="private", ds_ids=[ds_a])
    assert created.status_code == 200, created.text
    pid = created.json()["id"]

    promote = _update_prompt(test_client, prompts_world, "ds_a_viewer", pid, {"scope": "agent"})
    assert promote.status_code == 403, f"{promote.status_code} {promote.text[:160]}"

    # Positive control: a manager CAN promote their own private prompt.
    created2 = _create_prompt(test_client, prompts_world, "ds_a_manager", scope="private", ds_ids=[ds_a])
    assert created2.status_code == 200, created2.text
    promote2 = _update_prompt(
        test_client, prompts_world, "ds_a_manager", created2.json()["id"], {"scope": "agent"}
    )
    assert promote2.status_code == 200, f"{promote2.status_code} {promote2.text[:160]}"
    assert promote2.json()["scope"] == "agent"


@pytest.mark.e2e
def test_update_cannot_swap_agent_data_sources_without_manage(test_client, prompts_world):
    """The owner of an agent prompt cannot repoint it at agents they don't
    manage; benign edits (title, unchanged DS set) remain owner-editable."""
    ds_a = prompts_world["ds_a"]["id"]
    ds_b = prompts_world["ds_b"]["id"]

    created = _create_prompt(test_client, prompts_world, "ds_a_manager", scope="agent", ds_ids=[ds_a])
    assert created.status_code == 200, created.text
    pid = created.json()["id"]

    swap = _update_prompt(
        test_client, prompts_world, "ds_a_manager", pid, {"data_source_ids": [ds_b]}
    )
    assert swap.status_code == 403, f"{swap.status_code} {swap.text[:160]}"

    grow = _update_prompt(
        test_client, prompts_world, "ds_a_manager", pid, {"data_source_ids": [ds_a, ds_b]}
    )
    assert grow.status_code == 403, f"{grow.status_code} {grow.text[:160]}"

    # No over-tightening: title-only edit and an unchanged DS set still work.
    title = _update_prompt(test_client, prompts_world, "ds_a_manager", pid, {"title": "renamed"})
    assert title.status_code == 200, title.text
    same_ds = _update_prompt(
        test_client, prompts_world, "ds_a_manager", pid, {"data_source_ids": [ds_a]}
    )
    assert same_ds.status_code == 200, same_ds.text


@pytest.mark.e2e
def test_update_private_prompt_data_source_visibility(test_client, prompts_world):
    """Attaching data sources to a private prompt on update follows the same
    visibility rule as create."""
    ds_a = prompts_world["ds_a"]["id"]
    ds_pub = prompts_world["ds_pub"]["id"]

    created = _create_prompt(test_client, prompts_world, "member", scope="private", ds_ids=[])
    assert created.status_code == 200, created.text
    pid = created.json()["id"]

    hidden = _update_prompt(test_client, prompts_world, "member", pid, {"data_source_ids": [ds_a]})
    assert hidden.status_code == 403, f"{hidden.status_code} {hidden.text[:160]}"

    public = _update_prompt(test_client, prompts_world, "member", pid, {"data_source_ids": [ds_pub]})
    assert public.status_code == 200, public.text
    assert public.json()["data_source_ids"] == [ds_pub]


# ────────────────────────────────────────────────────────────────────
# Run-for — eligibility (public data sources count) + targets endpoint
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_run_for_public_agent_runs_for_plain_member(test_client, prompts_world, stub_completion):
    """Regression: an agent prompt on a PUBLIC data source must run for a
    member with no explicit grant — public access counts as membership in the
    per-target eligibility gate (it used to be silently skipped)."""
    org_id = prompts_world["org_id"]
    admin = prompts_world["principals"]["admin"]
    member = prompts_world["principals"]["member"]
    ds_pub = prompts_world["ds_pub"]["id"]

    created = _create_prompt(test_client, prompts_world, "admin", scope="agent", ds_ids=[ds_pub])
    assert created.status_code == 200, created.text
    pid = created.json()["id"]

    resp = test_client.post(
        f"/api/prompts/{pid}/run-for",
        json={"principal_type": "users", "user_ids": [member["user_id"]]},
        headers=_hdr(admin["token"], org_id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ran"] == 1 and body["skipped"] == 0, body
    assert stub_completion and stub_completion[0]["user_id"] == member["user_id"]

    # And the member can now also SEE the prompt in their list (same fix).
    listing = test_client.get("/api/prompts", headers=_hdr(member["token"], org_id))
    assert pid in {p["id"] for p in listing.json()["prompts"]}


@pytest.mark.e2e
def test_run_for_private_agent_still_skips_nonmembers(test_client, prompts_world, stub_completion):
    """Control: a prompt on a PRIVATE data source still skips members without
    a grant — and runs for a member holding one."""
    org_id = prompts_world["org_id"]
    admin = prompts_world["principals"]["admin"]
    member = prompts_world["principals"]["member"]
    viewer = prompts_world["principals"]["ds_a_viewer"]
    ds_a = prompts_world["ds_a"]["id"]

    created = _create_prompt(test_client, prompts_world, "admin", scope="agent", ds_ids=[ds_a])
    assert created.status_code == 200, created.text
    pid = created.json()["id"]

    resp = test_client.post(
        f"/api/prompts/{pid}/run-for",
        json={"principal_type": "users", "user_ids": [member["user_id"], viewer["user_id"]]},
        headers=_hdr(admin["token"], org_id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ran"] == 1 and body["skipped"] == 1, body
    assert body["skipped_user_ids"] == [member["user_id"]]


@pytest.mark.e2e
def test_run_for_targets_endpoint(test_client, prompts_world):
    """GET /prompts/{id}/run-for/targets: only eligible members are offered;
    authz mirrors run-for (manage on the prompt's agents)."""
    org_id = prompts_world["org_id"]
    admin = prompts_world["principals"]["admin"]
    member = prompts_world["principals"]["member"]
    ds_a = prompts_world["ds_a"]["id"]
    ds_pub = prompts_world["ds_pub"]["id"]

    # Agent prompt on the private ds_a → viewer/manager/admin eligible, member not.
    created = _create_prompt(test_client, prompts_world, "admin", scope="agent", ds_ids=[ds_a])
    pid = created.json()["id"]
    resp = test_client.get(f"/api/prompts/{pid}/run-for/targets", headers=_hdr(admin["token"], org_id))
    assert resp.status_code == 200, resp.text
    offered = {u["id"] for u in resp.json()["users"]}
    assert prompts_world["principals"]["ds_a_viewer"]["user_id"] in offered
    assert prompts_world["principals"]["ds_a_manager"]["user_id"] in offered
    assert admin["user_id"] in offered
    assert member["user_id"] not in offered

    # Agent prompt on the public DS → everyone eligible.
    created2 = _create_prompt(test_client, prompts_world, "admin", scope="agent", ds_ids=[ds_pub])
    pid2 = created2.json()["id"]
    resp2 = test_client.get(f"/api/prompts/{pid2}/run-for/targets", headers=_hdr(admin["token"], org_id))
    offered2 = {u["id"] for u in resp2.json()["users"]}
    assert member["user_id"] in offered2

    # A plain member (no manage on the agents) cannot enumerate targets.
    denied = test_client.get(f"/api/prompts/{pid}/run-for/targets", headers=_hdr(member["token"], org_id))
    assert denied.status_code == 403, denied.text


@pytest.mark.e2e
def test_run_for_targets_group_counts(
    test_client, prompts_world, enterprise_license, create_group, add_user_to_group
):
    """Groups are annotated with eligible_count so the picker can hide/size them."""
    org_id = prompts_world["org_id"]
    admin = prompts_world["principals"]["admin"]
    member = prompts_world["principals"]["member"]
    viewer = prompts_world["principals"]["ds_a_viewer"]
    ds_a = prompts_world["ds_a"]["id"]

    g = create_group(name="rf-group", user_token=admin["token"], org_id=org_id)
    assert g.status_code == 200, g.text
    gid = g.json()["id"]
    for uid in (member["user_id"], viewer["user_id"]):
        added = add_user_to_group(group_id=gid, user_id=uid, user_token=admin["token"], org_id=org_id)
        assert added.status_code in (200, 201), added.text

    created = _create_prompt(test_client, prompts_world, "admin", scope="agent", ds_ids=[ds_a])
    pid = created.json()["id"]
    resp = test_client.get(f"/api/prompts/{pid}/run-for/targets", headers=_hdr(admin["token"], org_id))
    assert resp.status_code == 200, resp.text
    group = next(x for x in resp.json()["groups"] if x["id"] == gid)
    assert group["member_count"] == 2
    assert group["eligible_count"] == 1  # viewer yes, plain member no


# ────────────────────────────────────────────────────────────────────
# Read visibility — admin is scoped to explicit agent membership (/agents parity)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_admin_prompt_list_scoped_to_agent_membership(
    test_client, prompts_world, invite_user_to_org
):
    """A full admin must NOT see prompts for agents they are not a member of —
    the prompt list mirrors the default /agents list, which scopes admins to
    explicit membership (public counts). Regression for the leak where
    _is_visible short-circuited on full_admin and surfaced every agent prompt.
    The admin's manage/write bypass is unaffected (asserted below)."""
    org_id = prompts_world["org_id"]
    admin = prompts_world["principals"]["admin"]         # creator/member of ds_a
    ds_a = prompts_world["ds_a"]["id"]                    # private
    ds_pub = prompts_world["ds_pub"]["id"]               # public

    # A SECOND full admin, invited into the org — never added to ds_a.
    admin2 = invite_user_to_org(org_id=org_id, admin_token=admin["token"], role="admin")

    priv = _create_prompt(test_client, prompts_world, "admin", scope="agent", ds_ids=[ds_a])
    assert priv.status_code == 200, priv.text
    priv_pid = priv.json()["id"]

    glob = _create_prompt(test_client, prompts_world, "admin", scope="global", ds_ids=[])
    assert glob.status_code == 200, glob.text
    glob_pid = glob.json()["id"]

    pub = _create_prompt(test_client, prompts_world, "admin", scope="agent", ds_ids=[ds_pub])
    assert pub.status_code == 200, pub.text
    pub_pid = pub.json()["id"]

    listing = test_client.get("/api/prompts", headers=_hdr(admin2["token"], org_id))
    assert listing.status_code == 200, listing.text
    visible = {p["id"] for p in listing.json()["prompts"]}

    # The bug: an agent prompt on an agent admin2 doesn't belong to must be hidden…
    assert priv_pid not in visible, "admin saw a prompt for an agent they don't belong to"
    # …while global and public-agent prompts stay visible to every member.
    assert glob_pid in visible
    assert pub_pid in visible

    # GET-by-id is gated identically — 403, not a leak.
    denied = test_client.get(f"/api/prompts/{priv_pid}", headers=_hdr(admin2["token"], org_id))
    assert denied.status_code == 403, denied.text

    # Manage/write bypass preserved: the non-member admin can still CREATE an
    # agent prompt on ds_a. Only READ visibility was scoped.
    write = test_client.post(
        "/api/prompts",
        json={"text": "hi", "scope": "agent", "data_source_ids": [ds_a]},
        headers=_hdr(admin2["token"], org_id),
    )
    assert write.status_code == 200, write.text


@pytest.mark.e2e
def test_update_promote_to_global_requires_admin(test_client, prompts_world):
    created = _create_prompt(test_client, prompts_world, "member", scope="private", ds_ids=[])
    assert created.status_code == 200, created.text
    pid = created.json()["id"]

    promote = _update_prompt(test_client, prompts_world, "member", pid, {"scope": "global"})
    assert promote.status_code == 403, f"{promote.status_code} {promote.text[:160]}"

    admin_created = _create_prompt(test_client, prompts_world, "admin", scope="private", ds_ids=[])
    assert admin_created.status_code == 200, admin_created.text
    admin_promote = _update_prompt(
        test_client, prompts_world, "admin", admin_created.json()["id"], {"scope": "global"}
    )
    assert admin_promote.status_code == 200, admin_promote.text
    assert admin_promote.json()["scope"] == "global"
