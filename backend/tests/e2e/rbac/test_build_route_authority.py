"""Authority on the /api/builds routes for per-agent managers.

A per-agent manager holds ``manage_instructions`` on some agents only. These
tests cover what such a manager can do to a build:

- Write ops (submit / reject / publish / rollback / content edits): they need
  authority over every instruction the build CHANGES. A global instruction
  (attached to no agent) reaches every agent, so changing one needs org-level
  ``manage_instructions``. This matches ``_require_instruction_authority``
  in ``app/routes/instruction.py``.
- Content edits must only pin a version that belongs to the named
  instruction. Otherwise a manager could bind another agent's version and
  read it back, or promote it.
- Reads (get / contents / diff): a build that touches an agent the caller
  can't access is hidden, the same as in ``GET /builds``.
"""
import uuid

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ds_a = sqlite_data_source(name=f"ba_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name=f"bb_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)

    def _scoped_manager(ds):
        u = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
        r = grant_resource(
            resource_type="data_source", resource_id=ds["id"],
            principal_type="user", principal_id=u["user_id"],
            permissions=["manage_instructions"], user_token=admin["token"], org_id=org_id,
        )
        assert r.status_code == 200, r.text
        return u

    return {
        "org_id": org_id, "admin": admin, "ds_a": ds_a, "ds_b": ds_b,
        "proposer": _scoped_manager(ds_a),
        "manager_a": _scoped_manager(ds_a),
        "manager_b": _scoped_manager(ds_b),
        "member": invite_user_to_org(org_id=org_id, admin_token=admin["token"]),
    }


def _create_instruction(test_client, user, org_id, text, ds_ids):
    r = test_client.post(
        "/api/instructions",
        json={"text": text, "status": "draft", "category": "general", "data_source_ids": ds_ids},
        headers=_hdr(user["token"], org_id),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _builds_containing(test_client, admin, org_id, instruction_id):
    r = test_client.get("/api/builds", params={"status": "all", "limit": 200},
                        headers=_hdr(admin["token"], org_id))
    assert r.status_code == 200, r.text
    hits = []
    for b in r.json()["items"]:
        c = test_client.get(f"/api/builds/{b['id']}/contents", headers=_hdr(admin["token"], org_id))
        assert c.status_code == 200, c.text
        if any(it["instruction_id"] == instruction_id for it in c.json()["items"]):
            hits.append(b)
    return hits


def _pending_build_for(test_client, admin, org_id, instruction_id):
    """The non-main build that stages ``instruction_id`` (a proposal awaiting review)."""
    for b in _builds_containing(test_client, admin, org_id, instruction_id):
        if not b.get("is_main") and b["status"] in ("draft", "pending_approval"):
            return b
    raise AssertionError(f"no pending build stages {instruction_id}")


def _main(test_client, admin, org_id):
    r = test_client.get("/api/builds/main", headers=_hdr(admin["token"], org_id))
    assert r.status_code == 200, r.text
    return r.json()


def _live_text(test_client, admin, org_id, instruction_id):
    r = test_client.get("/api/builds/main", headers=_hdr(admin["token"], org_id))
    if r.status_code == 404:
        return None  # nothing has ever gone live
    main = r.json()
    c = test_client.get(f"/api/builds/{main['id']}/contents", headers=_hdr(admin["token"], org_id))
    for it in c.json()["items"]:
        if it["instruction_id"] == instruction_id:
            return it["text"]
    return None


# ────────────────────────────────────────────────────────────────────
# Write ops on builds that change a GLOBAL instruction
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.parametrize("main_has_scoped_instruction", [False, True])
@pytest.mark.parametrize("action", ["publish", "reject", "submit"])
def test_per_agent_manager_cannot_act_on_build_changing_global(
    test_client, world, action, main_has_scoped_instruction
):
    """Changing a global instruction needs org-level authority, whatever else is in the snapshot.

    ``main_has_scoped_instruction`` covers the mixed case. Main then carries
    an instruction on the actor's own agent, so the build's DS set is not
    empty, but the change is still to a global rule.
    """
    org_id, admin = world["org_id"], world["admin"]
    if main_has_scoped_instruction:
        _create_instruction(test_client, admin, org_id, "ds_a rule", [world["ds_a"]["id"]])

    # A per-agent manager proposes an org-wide rule. It can't go live on their
    # own authority, so it waits in a pending build for an org-level reviewer.
    g = _create_instruction(test_client, world["proposer"], org_id, f"GLOBAL {uuid.uuid4().hex}", [])
    build = _pending_build_for(test_client, admin, org_id, g["id"])

    actor = world["manager_a"]  # manage_instructions on ds_a only
    body = {"reason": "nope"} if action == "reject" else None
    r = test_client.post(f"/api/builds/{build['id']}/{action}", json=body,
                         headers=_hdr(actor["token"], org_id))
    assert r.status_code == 403, f"{action}: {r.status_code} {r.text}"
    assert _live_text(test_client, admin, org_id, g["id"]) is None, "global went live"

    # An org-level manager still can.
    if action == "publish":
        ok = test_client.post(f"/api/builds/{build['id']}/publish", headers=_hdr(admin["token"], org_id))
        assert ok.status_code == 200, ok.text
        assert _live_text(test_client, admin, org_id, g["id"]) is not None


@pytest.mark.e2e
def test_per_agent_manager_cannot_roll_back_over_global_rules(test_client, world):
    """Rollback replaces the whole live set, globals included."""
    org_id, admin = world["org_id"], world["admin"]
    _create_instruction(test_client, admin, org_id, f"GLOBAL v1 {uuid.uuid4().hex}", [])
    old_main = _main(test_client, admin, org_id)
    _create_instruction(test_client, admin, org_id, f"GLOBAL v2 {uuid.uuid4().hex}", [])

    r = test_client.post(f"/api/builds/{old_main['id']}/rollback",
                         headers=_hdr(world["manager_a"]["token"], org_id))
    assert r.status_code == 403, r.text

    r_admin = test_client.post(f"/api/builds/{old_main['id']}/rollback",
                               headers=_hdr(admin["token"], org_id))
    assert r_admin.status_code == 200, r_admin.text


@pytest.mark.e2e
def test_per_agent_manager_keeps_authority_over_own_agent_builds(test_client, world):
    """No over-correction: a per-agent manager can still publish a build that only
    changes their own agent, even when main already holds global rules."""
    org_id, admin = world["org_id"], world["admin"]
    _create_instruction(test_client, admin, org_id, f"GLOBAL {uuid.uuid4().hex}", [])

    actor = world["manager_a"]
    created = test_client.post("/api/builds", json={"source": "user"}, headers=_hdr(actor["token"], org_id))
    # Creating a bare draft needs org-level manage_instructions, so stage the
    # change through the instruction API, which is how per-agent managers work.
    assert created.status_code == 403, created.text
    inst = _create_instruction(test_client, actor, org_id, f"own ds_a {uuid.uuid4().hex}", [world["ds_a"]["id"]])
    assert _live_text(test_client, admin, org_id, inst["id"]) is not None, "own-agent change should auto-publish"


# ────────────────────────────────────────────────────────────────────
# Content edits: the pinned version must belong to the instruction
# ────────────────────────────────────────────────────────────────────


def _version_id_of(test_client, admin, org_id, instruction_id):
    main = _main(test_client, admin, org_id)
    c = test_client.get(f"/api/builds/{main['id']}/contents", headers=_hdr(admin["token"], org_id))
    for it in c.json()["items"]:
        if it["instruction_id"] == instruction_id:
            return it["instruction_version_id"]
    raise AssertionError("not live")


def _draft_build(test_client, admin, org_id):
    r = test_client.post("/api/builds", json={"source": "user"}, headers=_hdr(admin["token"], org_id))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.e2e
def test_build_content_rejects_version_of_another_instruction(test_client, world):
    """Pinning instruction X to a version of instruction Y is refused for everyone."""
    org_id, admin = world["org_id"], world["admin"]
    secret = _create_instruction(test_client, admin, org_id, f"SECRET ds_b {uuid.uuid4().hex}", [world["ds_b"]["id"]])
    mine = _create_instruction(test_client, world["manager_a"], org_id, f"mine {uuid.uuid4().hex}", [world["ds_a"]["id"]])
    foreign_vid = _version_id_of(test_client, admin, org_id, secret["id"])
    build = _draft_build(test_client, admin, org_id)

    for who in ("manager_a", "admin"):
        user = world[who] if who != "admin" else admin
        r = test_client.put(
            f"/api/builds/{build['id']}/contents/{mine['id']}",
            json={"instruction_version_id": foreign_vid},
            headers=_hdr(user["token"], org_id),
        )
        assert r.status_code in (400, 403, 404), f"{who}: {r.status_code} {r.text}"

    c = test_client.get(f"/api/builds/{build['id']}/contents", headers=_hdr(admin["token"], org_id))
    row = next(it for it in c.json()["items"] if it["instruction_id"] == mine["id"])
    assert row["instruction_version_id"] != foreign_vid
    assert secret["text"] not in (row["text"] or "")


@pytest.mark.e2e
def test_build_content_edit_needs_authority_over_the_instruction(test_client, world):
    """Editing a build's pin for an instruction needs authority over THAT instruction."""
    org_id, admin = world["org_id"], world["admin"]
    other = _create_instruction(test_client, admin, org_id, f"ds_b rule {uuid.uuid4().hex}", [world["ds_b"]["id"]])
    glob = _create_instruction(test_client, admin, org_id, f"GLOBAL {uuid.uuid4().hex}", [])
    build = _draft_build(test_client, admin, org_id)

    for inst in (other, glob):
        vid = _version_id_of(test_client, admin, org_id, inst["id"])
        r = test_client.put(
            f"/api/builds/{build['id']}/contents/{inst['id']}",
            json={"instruction_version_id": vid},
            headers=_hdr(world["manager_a"]["token"], org_id),
        )
        assert r.status_code == 403, r.text
        r_del = test_client.delete(f"/api/builds/{build['id']}/contents/{inst['id']}",
                                   headers=_hdr(world["manager_a"]["token"], org_id))
        assert r_del.status_code == 403, r_del.text


# ────────────────────────────────────────────────────────────────────
# Reads: a build touching an agent the caller can't access is hidden
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.parametrize("viewer", ["manager_a", "member"])
def test_build_reads_never_expose_instructions_on_inaccessible_agents(test_client, world, viewer):
    """Every build is a whole-org snapshot, so reads filter rows instead of
    hiding builds: the caller still gets the build, minus the instructions on
    agents they can't access."""
    org_id, admin = world["org_id"], world["admin"]
    secret_text = f"SECRET ds_b {uuid.uuid4().hex}"
    secret = _create_instruction(test_client, admin, org_id, secret_text, [world["ds_b"]["id"]])
    before = _main(test_client, admin, org_id)
    _create_instruction(test_client, admin, org_id, f"ds_b v2 {uuid.uuid4().hex}", [world["ds_b"]["id"]])
    glob_text = f"GLOBAL {uuid.uuid4().hex}"
    _create_instruction(test_client, admin, org_id, glob_text, [])
    after = _main(test_client, admin, org_id)

    urls = [
        f"/api/builds/{before['id']}/contents",
        f"/api/builds/{after['id']}/contents",
        f"/api/builds/{after['id']}/diff?compare_to={before['id']}",
        f"/api/builds/{before['id']}/diff?compare_to={after['id']}",
        f"/api/builds/{after['id']}/diff/details?compare_to={before['id']}",
        f"/api/builds/{before['id']}/diff/details?compare_to={after['id']}",
    ]
    h = _hdr(world[viewer]["token"], org_id)
    for url in urls:
        r = test_client.get(url, headers=h)
        assert r.status_code == 200, f"{url}: {r.status_code}"
        assert secret_text not in r.text, url
        assert secret["id"] not in r.text, url

    # Global rules stay visible, and the counts match what is shown.
    details = test_client.get(urls[4], headers=h).json()
    assert [it["text"] for it in details["items"]] == [glob_text]
    assert details["added_count"] == 1 and details["modified_count"] == 0
    # /diff lists what compare_to adds relative to build_id.
    diff = test_client.get(urls[3], headers=h).json()
    assert diff["added_count"] == len(diff["added"]) == 1 and diff["modified_count"] == 0

    # Admin still reads everything.
    a = test_client.get(urls[0], headers=_hdr(admin["token"], org_id))
    assert secret_text in a.text


@pytest.mark.e2e
def test_build_reads_open_when_all_agents_accessible(test_client, world):
    """No over-correction: the same reads succeed when every agent the build
    touches is accessible to the caller (and global rules are visible to all)."""
    org_id, admin = world["org_id"], world["admin"]
    _create_instruction(test_client, admin, org_id, f"ds_a rule {uuid.uuid4().hex}", [world["ds_a"]["id"]])
    first = _main(test_client, admin, org_id)
    _create_instruction(test_client, admin, org_id, f"GLOBAL {uuid.uuid4().hex}", [])
    second = _main(test_client, admin, org_id)

    h = _hdr(world["manager_a"]["token"], org_id)
    for url in (
        f"/api/builds/{second['id']}",
        f"/api/builds/{second['id']}/contents",
        f"/api/builds/{second['id']}/diff?compare_to={first['id']}",
        f"/api/builds/{second['id']}/diff/details?compare_to={first['id']}",
    ):
        r = test_client.get(url, headers=h)
        assert r.status_code == 200, f"{url}: {r.status_code} {r.text}"
