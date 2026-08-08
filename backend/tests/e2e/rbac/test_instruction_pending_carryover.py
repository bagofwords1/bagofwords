"""A pending build snapshots the whole org; only what it CHANGED is pending.

Every draft build carries a full copy of the main build's contents
(``BuildService._copy_build_contents``), so one proposal produces one changed
row plus one carry-over row per instruction already live in the org. The
pending signal — the /agents tree badges, the "N pending" count, the per-row
dots — must reflect only the changed rows, no matter how many instructions
ride along in the snapshot.

This lives in ``rbac/`` because producing a build that STAYS pending needs an
author without publish authority over what the build CHANGES: an org admin's
change is auto-approved and promoted, leaving nothing pending.

The proposal goes through the AI capture path (``CreateInstructionTool`` in
knowledge mode) as a member holding no grants — the same route the knowledge
harness uses in production, and the only one that yields a genuinely pending
build. Writing through ``POST /instructions`` cannot: that route requires
manage_instructions on the target agent, which is exactly the authority that
publishes the resulting build.
"""
import asyncio
import uuid
from types import SimpleNamespace

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def author_world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource):
    """Two agents; an author who may write instructions on the first one.

    The second agent exists so each pending build also carries instructions
    nobody proposed anything for — the carry-over rows these tests are about.
    """
    admin = bootstrap_admin("carryover_admin")
    org_id = admin["org_id"]
    ds_a = sqlite_data_source(name=f"ds_a_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name=f"ds_b_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)

    # No grants anywhere: their capture must land in review, never live.
    author = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    return {"org_id": org_id, "ds_a": ds_a, "ds_b": ds_b, "admin": admin, "author": author}


def _write(test_client, world, token, ds, text):
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": text,
            "status": "published",
            "category": "general",
            "data_source_ids": [ds["id"]],
        },
        headers=_hdr(token, world["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _seed_live(test_client, world, n_on_a, n_on_b=1):
    """Admin-authored instructions go live immediately, so a later proposal
    forks a build that carries every one of them."""
    live = {
        "a": [_write(test_client, world, world["admin"]["token"], world["ds_a"],
                     f"Rule a{i}: exclude refunded orders when computing revenue.")
              for i in range(n_on_a)],
        "b": [_write(test_client, world, world["admin"]["token"], world["ds_b"],
                     f"Rule b{i}: report amounts in the workspace's currency.")
              for i in range(n_on_b)],
    }
    return live


async def _stage_capture(*, user_id, org_id, ds_ids, text):
    """Stage an instruction through the AI capture path, as the knowledge
    harness does. Returns the new instruction id."""
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool

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
             "evidence": "User confirmed."},
            ctx,
        ):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        assert end is not None and end.payload["output"]["success"], end
        return end.payload["output"]["instruction_id"]


def _propose(test_client, world, text):
    """The author holds no manage_instructions anywhere, so their capture has
    no publish authority and stays pending review — the shape the /agents tree
    renders."""
    return asyncio.run(_stage_capture(
        user_id=world["author"]["user_id"], org_id=world["org_id"],
        ds_ids=[world["ds_a"]["id"]], text=text,
    ))


def _counts(test_client, world):
    resp = test_client.get(
        "/api/instructions/counts", headers=_hdr(world["admin"]["token"], world["org_id"])
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.e2e
@pytest.mark.parametrize("n_live", [2, 5])
def test_only_the_proposed_instruction_is_pending_not_the_carried_over_ones(
    test_client, author_world, n_live
):
    """The count tracks the proposal, not how many instructions the build snapshots."""
    world = author_world
    _seed_live(test_client, world, n_live)

    proposed = _propose(test_client, world, "Proposed: settle FX at the order date.")

    body = _counts(test_client, world)
    assert body["pending_total"] == 1, (
        f"{n_live + 1} instructions live, one proposal: pending_total="
        f"{body['pending_total']} (carry-over rows must not count)"
    )
    assert [str(i) for i in body["pending_instruction_ids"]] == [proposed]


@pytest.mark.e2e
def test_an_agent_whose_instructions_were_only_carried_over_shows_no_pending_dot(
    test_client, author_world
):
    """The per-agent dot is what the /agents tree renders — an agent nobody
    proposed anything for stays clean even though the pending build contains a
    full copy of its instructions."""
    world = author_world
    _seed_live(test_client, world, 3, n_on_b=2)

    _propose(test_client, world, "Proposed: drop internal test accounts.")

    body = _counts(test_client, world)
    assert body["pending_by_agent"].get(str(world["ds_a"]["id"])) is True
    assert not body["pending_by_agent"].get(str(world["ds_b"]["id"])), (
        "the other agent's instructions were only carried over — it has no pending change"
    )


@pytest.mark.e2e
def test_list_flags_only_the_proposed_instruction_as_pending(test_client, author_world):
    """Same invariant on the rows themselves: the list and the badge must not
    disagree about which instruction is pending."""
    world = author_world
    live = _seed_live(test_client, world, 4)

    proposed = _propose(test_client, world, "Proposed: count partial refunds as refunds.")

    listing = test_client.get(
        "/api/instructions",
        params={"limit": 100, "include_own": True, "include_drafts": True},
        headers=_hdr(world["admin"]["token"], world["org_id"]),
    )
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert {i["id"] for i in items} >= set(live["a"]) | set(live["b"]), \
        "the live instructions should all be listed"

    flagged = {
        i["id"] for i in items
        if i.get("current_build_id") and i.get("current_build_status") in ("draft", "pending_approval")
    }
    assert flagged == {proposed}, f"only the proposed row may read as pending, got {flagged}"


@pytest.mark.e2e
def test_a_second_proposal_adds_exactly_its_own_change(test_client, author_world):
    """Two proposals → two pending instructions, not two full snapshots.

    Guards the failure mode where the pending signal is derived from build
    membership: each build re-snapshots every instruction, so a membership rule
    makes the count climb with the org's size instead of with the proposals.
    """
    world = author_world
    _seed_live(test_client, world, 4)

    first = _propose(test_client, world, "Proposed: exclude no-show reservations.")
    second = _propose(test_client, world, "Proposed: cap the lookback at 24 months.")

    body = _counts(test_client, world)
    assert body["pending_total"] == 2, body
    assert {str(i) for i in body["pending_instruction_ids"]} == {first, second}


@pytest.mark.e2e
def test_total_counts_each_instruction_once_not_twice(test_client, author_world):
    """The "All instructions" badge must equal the number of instructions.

    A proposal that was never published is BOTH pending (folded into the live
    surfaces) and not-live (absent from the main build). Summing those two
    populations double-counted it, so the badge grew past the number of
    instructions that exist — a workspace with 139 of them saw 220.
    """
    world = author_world
    live = _seed_live(test_client, world, 4, n_on_b=2)          # 6 published
    proposals = [_propose(test_client, world, f"Proposed rule {i}: cap the lookback.")
                 for i in range(3)]                              # 3 never published

    body = _counts(test_client, world)
    physical = len(live["a"]) + len(live["b"]) + len(proposals)
    assert body["total"] == physical, (
        f"badge says {body['total']} for {physical} instructions "
        f"(not_live={body['not_live']}) — pending-and-not-live must not be counted twice"
    )


@pytest.mark.e2e
def test_a_reviewed_and_rejected_suggestion_stops_being_pending(test_client, author_world):
    """The tree's dots skip the per-hunk rebase for speed, but "I rejected this,
    stop showing it" still has to hold — rejected rows keep the exact check."""
    world = author_world
    _seed_live(test_client, world, 3)
    proposed = _propose(test_client, world, "Proposed: exclude no-show reservations.")

    body = _counts(test_client, world)
    assert proposed in [str(i) for i in body["pending_instruction_ids"]]

    hunks = test_client.get(f"/api/instructions/{proposed}/review-hunks",
                            headers=_hdr(world["admin"]["token"], world["org_id"]))
    assert hunks.status_code == 200, hunks.text
    review = hunks.json()
    suggestions = review.get("suggestions") or []
    assert suggestions, "a fresh proposal should have reviewable hunks"

    build_id = suggestions[0]["build_id"]
    for h in suggestions[0]["hunks"]:
        rej = test_client.post(
            f"/api/instructions/{proposed}/hunks/reject",
            json={
                "build_id": build_id,
                "hunk_key": h["key"],
                "against_main_build_id": review["main_build_id"],
                "against_main_version_id": review["main_version_id"],
            },
            headers=_hdr(world["admin"]["token"], world["org_id"]),
        )
        assert rej.status_code == 200, rej.text

    after = _counts(test_client, world)
    assert proposed not in [str(i) for i in after["pending_instruction_ids"]], (
        "every hunk was rejected — the instruction must stop reading as pending"
    )
