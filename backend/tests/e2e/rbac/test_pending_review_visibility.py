"""Who is told that an instruction has PENDING CHANGES?

Reading a proposal and resolving one are two different tiers:

- SEE it — ``manage_instructions`` on ANY agent the instruction is attached to.
  Accepting the change alters that agent's behaviour, so its manager has a stake
  in reviewing it even when the rule is shared with agents they don't run.
- ACCEPT/REJECT it — ``manage_instructions`` on EVERY attached agent
  (``_require_instruction_authority``), covered by ``test_rbac_instructions`` and
  ``test_global_instruction_authority``; asserted here only where it has to pull
  apart from the seeing tier.

A scope-less (global) instruction reaches every agent, so it stays org-level on
both tiers.

The tier matters because a proposal carries more than its text: ``evidence`` is
lifted verbatim from the chat that produced it, and the row metadata names who
proposed it and when. Four surfaces disclose it and they must agree, or the
"N pending" badge points at a list the caller cannot open:
``/instructions/counts``, ``/instructions/pending-changes``,
``/instructions?pending_only=true`` (rows + their pending decoration), and
``/instructions/{id}/review-hunks``.

The proposal under test is an EDIT to an instruction the admin already published.
That matters: the row is live and visible to every member, so the only thing
these assertions can turn on is the pending gate. (A proposal that CREATES an
instruction is not in the main build at all, and the bulk list surfaces withhold
another user's not-yet-live row from non-admins for unrelated reasons — see
``_get_others_instructions_condition``.)

Proposals go through the AI capture/edit path as a member holding no grants —
the only route that yields a genuinely pending build, since an author with
publish authority has their write auto-approved and promoted. See
``test_instruction_pending_carryover`` for the same harness.
"""
import asyncio
import uuid
from types import SimpleNamespace

import pytest

LIVE_TEXT = "VIP customers are customers who have spent more than $100 USD in total purchases."
PROPOSED_ADDITION = "Exclude refunded orders from the total."


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource):
    """Two agents and four principals spanning the tiers.

    ``full`` holds manage_instructions on BOTH agents, ``partial`` on ds_a only —
    the pair that separates "may see" from "may resolve" for an instruction
    attached to both. Agents are private (as in every other RBAC probe), and the
    ``viewer`` is granted ``view`` on them, which implies no part of
    manage_instructions (see RESOURCE_PERM_IMPLIES) — so they can read the
    instruction rows while holding no review authority anywhere.
    """
    admin = bootstrap_admin("pending_vis_admin")
    org_id = admin["org_id"]
    ds_a = sqlite_data_source(
        name=f"pv_a_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id,
    )
    ds_b = sqlite_data_source(
        name=f"pv_b_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id,
    )

    def _member(*perms, on=()):
        u = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
        for ds in on:
            grant_resource(
                resource_type="data_source", resource_id=ds["id"],
                principal_type="user", principal_id=u["user_id"],
                permissions=list(perms), user_token=admin["token"], org_id=org_id,
            )
        return u

    return {
        "org_id": org_id, "admin": admin, "ds_a": ds_a, "ds_b": ds_b,
        # No grants: proposes, and cannot review.
        "author": _member(),
        "viewer": _member("view", on=(ds_a, ds_b)),
        "partial": _member("manage_instructions", on=(ds_a,)),
        "full": _member("manage_instructions", on=(ds_a, ds_b)),
    }


def _publish(test_client, world, ds_list, text=LIVE_TEXT):
    """Admin-authored, so it goes live immediately: the row lands in the main
    build, which is both what makes it visible to every member and what a later
    proposal forks from."""
    r = test_client.post(
        "/api/instructions",
        json={"text": text, "status": "published", "category": "general",
              "data_source_ids": [d["id"] for d in ds_list]},
        headers=_hdr(world["admin"]["token"], world["org_id"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _stage_edit(*, instruction_id, org_id, user_id, anchor, addition):
    """Stage an AI edit into a draft build, as the knowledge harness does."""
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool
    from app.dependencies import async_session_maker
    from app.services.build_service import BuildService

    async with async_session_maker() as db:
        build = await BuildService().get_or_create_draft_build(
            db, org_id=str(org_id), source="ai", user_id=str(user_id),
        )
        ctx = {
            "db": db, "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id), "mode": "training",
            "training_build_id": str(build.id),
        }
        async for evt in EditInstructionTool().run_stream(
            # Anchored append: repeat the anchor verbatim, then add.
            {"instruction_id": instruction_id, "old_text": anchor,
             "text": f"{anchor}\n{addition}",
             "evidence": "User confirmed the VIP threshold in chat."},
            ctx,
        ):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")


async def _stage_capture(*, user_id, org_id, ds_ids, text):
    """Stage a NEW instruction through the AI capture path. Returns its id."""
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": "knowledge",
            "report": SimpleNamespace(
                id=str(uuid.uuid4()),
                data_sources=[SimpleNamespace(id=d) for d in ds_ids],
            ),
        }
        end = None
        async for evt in CreateInstructionTool().run_stream(
            {"text": text, "category": "general", "confidence": 0.9,
             "evidence": "User confirmed this in chat."},
            ctx,
        ):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        assert end is not None and end.payload["output"]["success"], end
        return end.payload["output"]["instruction_id"]


def _propose_edit(world, iid, *, addition=PROPOSED_ADDITION):
    """The author holds no manage_instructions anywhere, so their edit has no
    publish authority and stays pending review."""
    asyncio.run(_stage_edit(
        instruction_id=iid, org_id=world["org_id"],
        user_id=world["author"]["user_id"], anchor=LIVE_TEXT, addition=addition,
    ))


# ── the four surfaces, read as one principal ────────────────────────────────

def _counts(test_client, world, token):
    r = test_client.get("/api/instructions/counts", headers=_hdr(token, world["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


def _pending_ids(test_client, world, token):
    r = test_client.get(
        "/api/instructions/pending-changes", headers=_hdr(token, world["org_id"])
    )
    assert r.status_code == 200, r.text
    return [str(i) for i in r.json()["instruction_ids"]]


def _list(test_client, world, token, **params):
    r = test_client.get(
        "/api/instructions",
        params={"include_drafts": "true", "include_archived": "true",
                "limit": 100, **params},
        headers=_hdr(token, world["org_id"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["items"] if isinstance(body, dict) else body


def _review_hunks(test_client, world, token, iid):
    r = test_client.get(
        f"/api/instructions/{iid}/review-hunks", headers=_hdr(token, world["org_id"])
    )
    assert r.status_code == 200, r.text
    return r.json()


def _proposed_text(suggestion):
    """What a suggestion's hunks propose the text should become, concatenated —
    so assertions don't depend on how the diff happens to split into hunks."""
    return " ".join(h.get("after") or "" for h in suggestion["hunks"])


def _pending_surfaces(test_client, world, token, iid):
    """Everything the /agents page can learn about pending changes, per caller."""
    counts = _counts(test_client, world, token)
    row = next(
        (x for x in _list(test_client, world, token) if str(x["id"]) == str(iid)), None
    )
    return {
        "pending_total": counts["pending_total"],
        "pending_by_agent": {
            k for k, v in (counts.get("pending_by_agent") or {}).items() if v
        },
        "pending_ids": _pending_ids(test_client, world, token),
        "pending_listed_ids": [
            str(x["id"]) for x in _list(test_client, world, token, pending_only="true")
        ],
        "row_present": row is not None,
        "row_build_status": (row or {}).get("current_build_status"),
        "row_source": (row or {}).get("pending_source"),
        "row_proposer": (row or {}).get("pending_created_by"),
        "suggestions": _review_hunks(test_client, world, token, iid)["suggestions"],
    }


# ── tests ───────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_a_member_who_may_not_review_is_told_nothing_about_a_pending_change(
    test_client, world
):
    """The instruction is live and readable by this member; the proposal is not.

    All four surfaces agree, the row carries no pending decoration (badge,
    proposer, timestamp), and the diff — with the evidence quoted from someone
    else's chat — is withheld.
    """
    iid = _publish(test_client, world, [world["ds_a"]])
    _propose_edit(world, iid)

    seen = _pending_surfaces(test_client, world, world["viewer"]["token"], iid)
    assert seen["row_present"], "precondition: the live instruction must be readable"
    assert seen["pending_total"] == 0, seen
    assert seen["pending_by_agent"] == set(), seen
    assert seen["pending_ids"] == [], seen
    assert seen["pending_listed_ids"] == [], seen
    assert seen["row_build_status"] is None, seen
    assert seen["row_source"] is None, seen
    assert seen["row_proposer"] is None, seen
    assert seen["suggestions"] == [], "the proposed diff and its evidence leaked"


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["admin", "full", "partial"])
def test_a_manager_of_an_attached_agent_sees_the_pending_change(test_client, world, who):
    """The control, across every tier that should see it: an org admin, a manager
    of every attached agent, and a manager of just one of the two."""
    iid = _publish(test_client, world, [world["ds_a"], world["ds_b"]])
    _propose_edit(world, iid)

    seen = _pending_surfaces(test_client, world, world[who]["token"], iid)
    assert seen["pending_total"] == 1, seen
    assert seen["pending_ids"] == [str(iid)], seen
    assert seen["pending_listed_ids"] == [str(iid)], seen
    assert seen["row_build_status"] is not None, seen
    assert len(seen["suggestions"]) == 1, seen
    sug = seen["suggestions"][0]
    assert sug["hunks"], sug
    # The proposal's text and the evidence quoted from the chat both reach a
    # caller on this tier — the payload the tier above is denied.
    assert PROPOSED_ADDITION in _proposed_text(sug), sug
    assert sug["evidence"], sug


@pytest.mark.e2e
def test_seeing_a_shared_proposal_does_not_confer_authority_to_resolve_it(
    test_client, world
):
    """The tiers pull apart on a rule shared across agents: ``partial`` manages
    one of the two, so they may read the proposal but the resolve endpoints still
    refuse them — while ``full`` succeeds."""
    iid = _publish(test_client, world, [world["ds_a"], world["ds_b"]])
    _propose_edit(world, iid)

    sug = _review_hunks(test_client, world, world["partial"]["token"], iid)["suggestions"]
    assert sug, "precondition: partial must be able to READ the proposal"
    build_id = sug[0]["build_id"]

    r = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        json={"build_id": build_id},
        headers=_hdr(world["partial"]["token"], world["org_id"]),
    )
    assert r.status_code == 403, f"partial manager resolved a shared rule: {r.status_code}"

    r_ok = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        json={"build_id": build_id},
        headers=_hdr(world["full"]["token"], world["org_id"]),
    )
    assert r_ok.status_code == 200, r_ok.text


@pytest.mark.e2e
def test_a_per_agent_manager_is_told_nothing_about_a_global_proposal(test_client, world):
    """A scope-less instruction governs every agent, so its proposals stay
    org-level — a per-agent grant is not a stake in it. Mirrors the create gate
    (``/instructions/global``) and the resolve gate."""
    iid = _publish(test_client, world, [])          # global: no attached agents
    _propose_edit(world, iid)

    for who in ("partial", "full", "viewer"):
        seen = _pending_surfaces(test_client, world, world[who]["token"], iid)
        assert seen["pending_ids"] == [], f"{who}: {seen}"
        assert seen["pending_total"] == 0, f"{who}: {seen}"
        assert seen["suggestions"] == [], f"a global proposal leaked to {who}"

    admin_seen = _pending_surfaces(test_client, world, world["admin"]["token"], iid)
    assert admin_seen["pending_ids"] == [str(iid)], admin_seen
    assert len(admin_seen["suggestions"]) == 1, admin_seen


@pytest.mark.e2e
def test_withholding_the_proposal_does_not_withhold_the_live_text(test_client, world):
    """review-hunks also carries the authoritative main text/version that the
    detail pane and the version history label "current" with — a member who may
    not review still needs those, with or without a proposal outstanding."""
    iid = _publish(test_client, world, [world["ds_a"]])
    _propose_edit(world, iid)

    body = _review_hunks(test_client, world, world["viewer"]["token"], iid)
    assert body["suggestions"] == []
    assert body["main_version_id"], body
    assert body["main_text"] == LIVE_TEXT, body
    assert PROPOSED_ADDITION not in (body["main_text"] or ""), body


@pytest.mark.e2e
def test_a_new_instruction_that_only_exists_as_a_proposal_stays_hidden(test_client, world):
    """The other proposal shape: a capture that CREATES an instruction. It is not
    in the main build, so it must not reach a member who cannot review it — via
    the pending surfaces or as a bare row."""
    _publish(test_client, world, [world["ds_a"]])  # give the org a main build
    iid = asyncio.run(_stage_capture(
        user_id=world["author"]["user_id"], org_id=world["org_id"],
        ds_ids=[world["ds_a"]["id"]],
        text="Proposed: settle FX at the order date.",
    ))

    seen = _pending_surfaces(test_client, world, world["viewer"]["token"], iid)
    assert seen["pending_ids"] == [], seen
    assert seen["pending_total"] == 0, seen
    assert seen["suggestions"] == [], seen

    admin_seen = _pending_surfaces(test_client, world, world["admin"]["token"], iid)
    assert admin_seen["pending_ids"] == [str(iid)], admin_seen
    assert len(admin_seen["suggestions"]) == 1, admin_seen
