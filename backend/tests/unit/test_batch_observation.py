"""Batch observation semantics for the main planner loop.

Two contracts (both from the live Terra trace review on PR #837):

1. A parallel batch must never yield LESS data than serial calls — every
   substantive member embeds its full observation in the aggregate;
   bookkeeping members (notes/memory) stay as one-line acks.
2. A bookkeeping-only step (solo or batched) must not evict the previous
   substantive observation — the create_data preview / read_query rows the
   model is about to answer from stay in last_observation, with the ack
   attached.
"""
import pytest

from app.ai.agent_v2 import AgentV2, _BOOKKEEPING_TOOLS


def _outcome(tool, obs):
    return {"tool_name": tool, "observation": obs}


def test_bookkeeping_set_covers_note_and_memory_tools():
    assert _BOOKKEEPING_TOOLS == {"create_note", "edit_note", "update_user_memory"}


# --- aggregation: substantive members keep their full observation -----------

def test_batch_aggregate_embeds_full_substantive_observation():
    read_obs = {
        "summary": "5 rows",
        "query_id": "q1",
        "data_preview": {"rows": [[1, 2]], "columns": ["a", "b"]},
    }
    note_obs = {"summary": "Note updated", "note_id": "n1"}
    agg = AgentV2._aggregate_batch_observation(
        [_outcome("read_query", read_obs), _outcome("edit_note", note_obs)], []
    )
    entries = {e["tool_name"]: e for e in agg["parallel_actions"]}
    # Substantive member carries everything a solo call would have returned.
    assert entries["read_query"]["observation"]["data_preview"] == read_obs["data_preview"]
    # Bookkeeping member stays a one-line ack (summary + id, no embed).
    assert "observation" not in entries["edit_note"]
    assert entries["edit_note"]["note_id"] == "n1"


def test_batch_aggregate_hoists_images_out_of_embedded_observation():
    obs = {"summary": "shot", "images": [{"data": "x", "media_type": "image/png"}]}
    agg = AgentV2._aggregate_batch_observation(
        [_outcome("web_fetch", obs), _outcome("read_query", {"summary": "r"})], []
    )
    entries = {e["tool_name"]: e for e in agg["parallel_actions"]}
    assert "images" not in entries["web_fetch"].get("observation", {})
    assert agg["images"][0]["data"] == "x"


def test_single_action_batch_stays_verbatim():
    obs = {"summary": "5 rows", "data_preview": {"rows": []}}
    agg = AgentV2._aggregate_batch_observation([_outcome("read_query", obs)], [])
    assert agg is obs


# --- non-eviction: bookkeeping-only steps carry the previous observation ----

PREV = {"summary": "5 rows", "query_id": "q1", "data_preview": {"rows": [[1]]}}


def test_solo_note_step_carries_previous_observation():
    ack = {"summary": "Updated note n1", "note_id": "n1"}
    out = AgentV2._carry_substantive_observation(PREV, ack, [_outcome("edit_note", ack)])
    assert out["data_preview"] == PREV["data_preview"]
    assert "bookkeeping_ack" in out and "Updated note n1" in out["bookkeeping_ack"]


def test_bookkeeping_batch_carries_previous_observation():
    agg = AgentV2._aggregate_batch_observation(
        [
            _outcome("edit_note", {"summary": "ticked"}),
            _outcome("update_user_memory", {"summary": "remembered"}),
        ],
        [],
    )
    out = AgentV2._carry_substantive_observation(
        PREV, agg,
        [_outcome("edit_note", {"summary": "ticked"}), _outcome("update_user_memory", {"summary": "remembered"})],
    )
    assert out["query_id"] == "q1"
    assert "bookkeeping_ack" in out


def test_carry_does_not_nest_across_consecutive_note_steps():
    ack1 = {"summary": "tick 1"}
    once = AgentV2._carry_substantive_observation(PREV, ack1, [_outcome("edit_note", ack1)])
    ack2 = {"summary": "tick 2"}
    twice = AgentV2._carry_substantive_observation(once, ack2, [_outcome("edit_note", ack2)])
    assert twice["data_preview"] == PREV["data_preview"]
    assert "tick 2" in twice["bookkeeping_ack"] and "tick 1" not in twice["bookkeeping_ack"]


def test_substantive_member_passes_new_observation_through():
    new = {"summary": "read result", "parallel_actions": []}
    out = AgentV2._carry_substantive_observation(
        PREV, new, [_outcome("edit_note", {"summary": "t"}), _outcome("read_query", {"summary": "r"})]
    )
    assert out is new


def test_failed_bookkeeping_passes_through_so_the_error_surfaces():
    failed = {"summary": "anchor not found", "error": {"message": "anchor_not_found"}}
    out = AgentV2._carry_substantive_observation(PREV, failed, [_outcome("edit_note", failed)])
    assert out is failed


def test_no_previous_observation_passes_through():
    ack = {"summary": "Note created", "note_id": "n1"}
    out = AgentV2._carry_substantive_observation(None, ack, [_outcome("create_note", ack)])
    assert out is ack
