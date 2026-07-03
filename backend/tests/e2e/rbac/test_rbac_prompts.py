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
"""
import pytest


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
