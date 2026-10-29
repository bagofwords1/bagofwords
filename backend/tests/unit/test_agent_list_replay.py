"""Round-2 contracts for submit_<list>: the streaming record counter, how a
past submission is replayed to the model, and what survives decay."""
import json

import pytest

from app.ai.agents.planner.planner_v3 import _RecordCounter
from app.ai.context.parts import Outcome, ToolCallPart, ToolResultPart
from app.ai.context.replay_args import compact_replayed_args
from app.ai.context.transcript import Transcript
from app.ai.agents.planner.prompt_builder import PromptBuilder
from app.ai.tools.implementations.submit_list import SubmitListTool

RECORDS = [
    {"row_id": None, "fields": {"title": {"value": 'a}{"[', "status": "found",
                                          "evidence": [{"kind": "file", "ref": "x.pdf", "page": 1, "quote": "q \\\" }"}],
                                          "note": None}}},
    {"row_id": "r2", "fields": {"title": {"value": "b", "status": "found", "evidence": [], "note": None}}},
    {"row_id": None, "fields": {}},
]


def _stream(chunk_size):
    s = json.dumps({"records": RECORDS})
    c = _RecordCounter()
    seen = [c.feed(s[i:i + chunk_size]) for i in range(0, len(s), chunk_size)]
    return seen


def test_counter_counts_finished_records_only():
    for size in (1, 3, 17, 10_000):
        seen = _stream(size)
        assert seen[-1] == 3
        assert seen == sorted(seen)  # monotone: never over-counts mid-record


def test_counter_ignores_braces_inside_strings_and_nested_objects():
    c = _RecordCounter()
    assert c.feed('{"records": [{"fields": {"a": {"value": "}}}]]", "evidence": [{"x": 1}]}}}') == 1
    assert c.feed(', {"fields": {}') == 1  # record still open
    assert c.feed('}]}') == 2


def test_successful_submission_args_are_compacted():
    args = {"list_id": "L1", "records": RECORDS}
    out = compact_replayed_args("submit_list", args, succeeded=True)
    assert out["list_id"] == "L1"
    # Every record survives with its row id, values and statuses; only long
    # quotes and notes are shortened. Nothing outside the schema is added.
    assert out["records"] == _expected_compact(RECORDS)
    assert set(out) == {"list_id", "records"}
    assert args["records"] is RECORDS  # stored args untouched
    # deterministic → prompt-cache stable
    assert out == compact_replayed_args("submit_list", args, succeeded=True)


def _expected_compact(records):
    return compact_replayed_args("submit_list", {"records": records}, succeeded=True)["records"]


LONG = "Clause 4.2: the supplier shall deliver all licensed software within thirty days " * 3


@pytest.mark.parametrize("records", [
    [{"row_id": None, "fields": {"title": {"value": f"new {i}", "status": "found",
                                           "evidence": [{"kind": "file", "ref": "a.pdf", "page": 2, "quote": LONG}],
                                           "note": LONG}}} for i in range(4)],     # all new rows
    RECORDS,                                                                       # mixed
    [{"row_id": "r9", "fields": {"title": None}}],                                 # update, field left unchanged
])
def test_a_saved_call_replays_as_the_save_it_was(records):
    # Live: a save of NEW rows replayed as {"records": []} next to "3 inserted".
    # The model copied it (records: [] -> a 30-minute loop) or decided its own
    # save was malformed and stopped; row-id stubs read as "no field data".
    # The replay must stay the call that was sent: same records, row ids,
    # values and statuses, schema keys only.
    out = compact_replayed_args("submit_list", {"list_id": "L1", "records": records}, succeeded=True)
    assert set(out) == {"list_id", "records"}
    assert len(out["records"]) == len(records)
    for sent, replayed in zip(records, out["records"]):
        assert replayed["row_id"] == sent["row_id"]
        assert set(replayed["fields"]) == set(sent["fields"])
        for name, env in sent["fields"].items():
            if env is None:
                assert replayed["fields"][name] is None
                continue
            r = replayed["fields"][name]
            assert set(r) == set(env)
            assert (r["value"], r["status"]) == (env["value"], env["status"])
            for e_sent, e_rep in zip(env.get("evidence") or [], r.get("evidence") or []):
                assert {k: v for k, v in e_rep.items() if k != "quote"} == {k: v for k, v in e_sent.items() if k != "quote"}
                assert len(e_rep["quote"] or "") <= 60
                assert (e_sent["quote"] or "").startswith((e_rep["quote"] or "").rstrip("…"))
    assert len(json.dumps(out)) <= len(json.dumps({"list_id": "L1", "records": records}))


def test_long_quotes_make_the_replay_smaller():
    records = [{"row_id": None, "fields": {"title": {"value": "x", "status": "found",
                "evidence": [{"kind": "file", "ref": "a.pdf", "page": 1, "quote": LONG}], "note": LONG}}}] * 20
    full = json.dumps({"records": records})
    assert len(json.dumps(compact_replayed_args("submit_list", {"records": records}, succeeded=True))) < len(full) / 2


def test_failed_submission_and_other_tools_replay_verbatim():
    args = {"list_id": "L1", "records": RECORDS}
    assert compact_replayed_args("submit_list", args, succeeded=False) is args
    other = {"records": [1, 2]}
    assert compact_replayed_args("create_data", other, succeeded=True) is other


def _transcript(outcome):
    t = Transcript()
    t.add_user_text("extract")
    t.add_assistant_step(calls=[ToolCallPart(id="c1", tool_name="submit_list",
                                             args={"list_id": "L1", "records": RECORDS})])
    t.add_tool_results([ToolResultPart(call_id="c1", tool_name="submit_list", outcome=outcome, content="{}")])
    return t


def _replayed_input(t):
    msgs = t.to_model_messages()
    return next(b for m in msgs if isinstance(m.content, list) for b in m.content
                if b.get("type") == "tool_use")["input"]


def test_transcript_replays_compact_args_after_success():
    assert _replayed_input(_transcript(Outcome.SUCCESS))["records"] == _expected_compact(RECORDS)


def test_transcript_keeps_full_args_after_failure():
    assert _replayed_input(_transcript(Outcome.FAILED))["records"] == RECORDS


def test_legacy_past_observations_compact_submit_args():
    obs = [{"tool_name": "submit_list", "tool_input": {"list_id": "L1", "records": RECORDS},
            "observation": {"success": True, "summary": "saved"}, "loop_index": 0},
           {"tool_name": "submit_list", "tool_input": {"list_id": "L1", "records": RECORDS},
            "observation": {"success": False, "summary": "rejected"}, "loop_index": 0}]
    out = PromptBuilder._compact_past_observations(obs)
    assert out[0]["tool_input"]["records"] == _expected_compact(RECORDS)
    assert out[1]["tool_input"]["records"] == RECORDS
    assert obs[0]["tool_input"]["records"] is RECORDS


def test_digest_names_the_outcome_not_the_payload():
    keys = SubmitListTool().metadata.digest_keys
    assert {"list_name", "inserted", "updated", "unchanged", "error"} <= set(keys)
    assert "records" not in keys


def _native_transcript(outcome):
    """A submit_<list> call as the agent loop buffers it: executed (and
    stored) as the submit_list gateway, issued by the model as submit_contracts."""
    t = Transcript()
    t.add_user_text("extract")
    t.add_assistant_step(calls=[ToolCallPart(
        id="c1", tool_name="submit_list", args={"list_id": "L1", "records": RECORDS},
        replay_name="submit_contracts", replay_args={"records": RECORDS})])
    t.add_tool_results([ToolResultPart(call_id="c1", tool_name="submit_list", outcome=outcome, content="{}")])
    return t


def _replayed_block(t):
    msgs = t.to_model_messages()
    return next(b for m in msgs if isinstance(m.content, list) for b in m.content
                if b.get("type") == "tool_use")


def test_native_list_call_replays_under_the_name_the_model_used():
    # BOW-103: replayed as submit_list — a tool not in the model's catalog —
    # the model did not recognise its own save and submitted again, forever.
    blk = _replayed_block(_native_transcript(Outcome.SUCCESS))
    assert blk["name"] == "submit_contracts"
    assert "list_id" not in blk["input"]
    assert blk["input"]["records"] == _expected_compact(RECORDS)
    blk = _replayed_block(_native_transcript(Outcome.FAILED))
    assert blk["name"] == "submit_contracts" and blk["input"] == {"records": RECORDS}


def test_buffered_native_call_records_issued_form():
    from types import SimpleNamespace
    from app.ai.agent_v2 import AgentV2

    agent = SimpleNamespace(transcript=Transcript(), _pending_transcript=[], registry=None)
    action = SimpleNamespace(id="c1", name="submit_contracts", signature=None, provider="anthropic",
                             arguments={"records": RECORDS, "_progress": {"records": 3}})
    AgentV2._buffer_transcript_part(agent, {
        "action": action, "tool_name": "submit_list",
        "tool_input": {"list_id": "L1", "records": RECORDS},
        "observation": {"success": True, "summary": "saved"},
    })
    call, _ = agent._pending_transcript[0]
    assert (call.tool_name, call.replay_name) == ("submit_list", "submit_contracts")
    assert call.replay_args == {"records": RECORDS}  # server-side _progress dropped


def test_rehydrated_gateway_calls_are_nativized():
    from app.ai.agent_v2 import nativize_replayed_calls

    t = _transcript(Outcome.SUCCESS)
    t.add_assistant_step(calls=[
        ToolCallPart(id="c2", tool_name="execute_mcp",
                     args={"connection_id": "K", "tool_name": "issue_create", "arguments": {"title": "x"}, "title": "Running"}),
        ToolCallPart(id="c3", tool_name="submit_list", args={"list_id": "GONE", "records": []}),
    ])
    n = nativize_replayed_calls(
        t.turns,
        {"submit_contracts": {"list_id": "L1", "list_name": "Contracts", "data_source_id": "D"}},
        {"mcp__k__issue_create": {"connection_id": "K", "tool_name": "issue_create"}},
    )
    assert n == 2
    calls = [p for turn in t.turns for p in turn.parts if isinstance(p, ToolCallPart)]
    assert [c.replay_name for c in calls] == ["submit_contracts", "mcp__k__issue_create", None]
    assert calls[1].replay_args == {"title": "x"}
    # idempotent
    assert nativize_replayed_calls(t.turns, {"submit_contracts": {"list_id": "L1"}}, {}) == 0


def test_a_call_to_a_tool_past_tools_max_replays_as_call_tool():
    """Its native name is not in the request's tools: replayed under it, the
    model would learn to call a tool it does not have."""
    from app.ai.agent_v2 import nativize_replayed_calls

    t = _transcript(Outcome.SUCCESS)
    t.add_assistant_step(calls=[
        ToolCallPart(id="c4", tool_name="execute_mcp",
                     args={"connection_id": "K", "tool_name": "find_artist", "arguments": {"q": "x"}}),
        ToolCallPart(id="c5", tool_name="execute_mcp",
                     args={"connection_id": "K", "tool_name": "get_albums", "arguments": {"artist_id": 9}}),
    ])
    routing = {
        "mcp__k__find_artist": {"connection_id": "K", "tool_name": "find_artist"},
        "mcp__k__get_albums": {"connection_id": "K", "tool_name": "get_albums"},
    }
    assert nativize_replayed_calls(t.turns, {}, routing, native_names={"mcp__k__find_artist"}) == 2
    native, deferred = [p for turn in t.turns for p in turn.parts if isinstance(p, ToolCallPart)][-2:]
    assert (native.replay_name, native.replay_args) == ("mcp__k__find_artist", {"q": "x"})
    assert (deferred.replay_name, deferred.replay_args) == (
        "call_tool", {"name": "mcp__k__get_albums", "arguments": {"artist_id": 9}})
